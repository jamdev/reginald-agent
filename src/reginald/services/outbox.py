"""OutboxService — digest building and the transactional outbox.

Flow: undelivered events × effective topic sets -> one digest per subscriber
-> Delivery row inserted BEFORE sending (outbox pattern) -> provider send ->
mark sent/failed. DeliveredEvent rows make per-subscriber delivery idempotent.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime

from jinja2 import Environment, PackageLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import Config
from ..core.observability import logger, span
from ..db.schema import ContentEvent, DeliveredEvent, Delivery, Escalation, Subscriber
from ..domain.models import (
    DeliveryView,
    Digest,
    DigestItem,
    EscalationItem,
)
from ..email.base import EmailProvider
from ..publisher.diff import UNROUTED_TOPIC
from ..publisher.links import note_url
from .subscribers import SubscriberService
from .topics import OPERATIONAL_TOPIC, SENTINEL_TOPICS

MAX_ATTEMPTS = 5


def _wants(sub: Subscriber, event: ContentEvent) -> bool:
    """Per-kind preference filter. Escalations deliver by default (they are the
    team's business), but `notify_escalations` can be turned off so a raised
    item reaches only the subscribers who own it while others still get ordinary
    new/changed content."""
    if event.kind == "escalation":
        return sub.notify_escalations
    if event.kind == "brief_updated":
        return sub.notify_updated
    return sub.notify_new


def _routes_to(sub: Subscriber, event: ContentEvent, topics: set[str]) -> bool:
    """Topic routing. `__unrouted__` holds escalations raised without a usable
    `topic:` — they have no audience of their own, so the all-topics
    subscribers act as the catch-all rather than the raise going unseen."""
    if event.topic.name == UNROUTED_TOPIC:
        return sub.mode == "all"
    return event.topic.name in topics and event.topic.name != OPERATIONAL_TOPIC


def _topic_tags(group: list[ContentEvent]) -> list[str]:
    """The topics shown against an item. Sentinel buckets are routing
    machinery, not topics, so they never render — an unrouted escalation shows
    no tag at all, which is the honest answer."""
    return sorted({e.topic.name for e in group} - set(SENTINEL_TOPICS))


def _subject(new: int, changed: int, escalations: int) -> str:
    parts = []
    if new:
        parts.append(f"{new} new")
    if changed:
        parts.append(f"{changed} changed")
    if escalations:
        parts.append(f"{escalations} escalation{'s' if escalations != 1 else ''}")
    return "Reginald regulatory update — " + (", ".join(parts) or "nothing new")


class OutboxService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        subscribers: SubscriberService,
        provider: EmailProvider,
        config: Config,
    ):
        self._sessions = session_factory
        self._subscribers = subscribers
        self._provider = provider
        self._config = config
        self._jinja = Environment(
            loader=PackageLoader("reginald", "templates"),
            autoescape=select_autoescape(["html"]),
        )

    # -- digest building --------------------------------------------------

    def build_pending(self) -> None:
        """Create pending Delivery rows for all undelivered event/subscriber
        pairs."""
        with self._sessions.begin() as session:
            subscribers = session.scalars(
                select(Subscriber).where(Subscriber.active.is_(True))
            ).all()
            for sub in subscribers:
                topics = set(self._subscribers.effective_topics(session, sub))
                if not topics:
                    continue
                delivered = {
                    de.event_id
                    for de in session.scalars(
                        select(DeliveredEvent).where(DeliveredEvent.subscriber_id == sub.id)
                    ).all()
                }
                events = [
                    e
                    for e in session.scalars(
                        select(ContentEvent).order_by(ContentEvent.created_at)
                    ).all()
                    if e.id not in delivered and _routes_to(sub, e, topics) and _wants(sub, e)
                ]
                if not events:
                    continue
                delivery = Delivery(
                    subscriber_id=sub.id,
                    event_ids=json.dumps([e.id for e in events]),
                    status="pending",
                )
                session.add(delivery)
                session.flush()
                for e in events:
                    session.add(
                        DeliveredEvent(
                            subscriber_id=sub.id, event_id=e.id, delivery_id=delivery.id
                        )
                    )

    def _digest_for(
        self, session: Session, events: list[ContentEvent]
    ) -> Digest:
        """Three sections — New, Changed, Escalations — not one per topic.

        A note filed under a primary and a secondary topic has one event row
        per topic (that fan-out is what delivers it to a subscriber of either),
        so the same note used to appear once under each topic heading. Here the
        rows coalesce back into one line carrying every topic as a tag.
        """
        new: list[DigestItem] = []
        changed: list[DigestItem] = []
        for group in self._by_note(e for e in events if e.kind != "escalation"):
            # Created-then-edited inside one window is still new.
            is_new = any(e.kind != "brief_updated" for e in group)
            (new if is_new else changed).append(self._item(group))

        escalations = [
            self._escalation(session, group)
            for group in self._by_note(e for e in events if e.kind == "escalation")
        ]

        new.sort(key=lambda i: (i.effective_date or "", i.title), reverse=True)
        changed.sort(key=lambda i: i.title)
        escalations.sort(key=lambda i: (i.number or 0, i.title))

        return Digest(
            subject=_subject(len(new), len(changed), len(escalations)),
            new=new,
            changed=changed,
            escalations=escalations,
        )

    @staticmethod
    def _by_note(events: Iterable[ContentEvent]) -> list[list[ContentEvent]]:
        """Group events by note identity — the frontmatter uuid, which is
        rename-stable, falling back to the path for notes without one."""
        groups: dict[str, list[ContentEvent]] = {}
        for e in events:
            groups.setdefault(e.note_uuid or e.file_path, []).append(e)
        return list(groups.values())

    @staticmethod
    def _item(group: list[ContentEvent]) -> DigestItem:
        latest = max(group, key=lambda e: (e.created_at, e.id))
        return DigestItem(
            title=latest.title or latest.file_path,
            file_path=latest.file_path,
            topics=_topic_tags(group),
            jurisdiction=latest.jurisdiction,
            effective_date=latest.effective_date,
            author_name=latest.author_name,
            author_kind=latest.author_kind,
            summary=latest.summary,
            # Distinct commits, not rows: a two-topic note edited once has two
            # event rows and must still read as one edit.
            edits=len({e.commit_hash for e in group}),
        )

    @staticmethod
    def _escalation(session: Session, group: list[ContentEvent]) -> EscalationItem:
        """ESC-NNN and priority come from the escalations table, not the event:
        the number is assigned by escalations-sync *after* change detection records the
        raise, so the event row never carries it."""
        latest = max(group, key=lambda e: (e.created_at, e.id))
        row = session.scalar(
            select(Escalation).where(Escalation.file_path == latest.file_path)
        )
        if row is None and latest.note_uuid:
            row = session.scalar(
                select(Escalation).where(Escalation.uuid == latest.note_uuid)
            )
        return EscalationItem(
            title=latest.title or latest.file_path,
            file_path=latest.file_path,
            topics=_topic_tags(group),
            number=row.number if row else None,
            priority=row.priority if row else None,
            raised_by=(row.raised_by if row else None) or latest.summary,
        )

    def render(self, digest: Digest) -> tuple[str, str]:
        """Return (html, text) bodies."""
        ctx = {
            "digest": digest,
            # Templates call this per event; None when the site has no
            # base_url, which both templates render as an unlinked title.
            "note_url": lambda path: note_url(self._config.site.base_url, path),
        }
        html = self._jinja.get_template("digest.html.j2").render(**ctx)
        text = self._jinja.get_template("digest.txt.j2").render(**ctx)
        return html, text

    # -- sending ----------------------------------------------------------

    def drain(self, dry_run: bool = False) -> list[DeliveryView]:
        """Build new deliveries then attempt every pending one."""
        self.build_pending()
        results: list[DeliveryView] = []
        with self._sessions.begin() as session:
            pending = session.scalars(
                select(Delivery).where(
                    Delivery.status.in_(["pending", "failed"]),
                    Delivery.attempts < MAX_ATTEMPTS,
                )
            ).all()
            for delivery in pending:
                results.append(self._attempt(session, delivery, dry_run=dry_run))
        return results

    def retry(self, delivery_id: int | None = None) -> list[DeliveryView]:
        with self._sessions.begin() as session:
            stmt = select(Delivery).where(Delivery.status == "failed")
            if delivery_id is not None:
                stmt = select(Delivery).where(Delivery.id == delivery_id)
            return [self._attempt(session, d, dry_run=False) for d in session.scalars(stmt).all()]

    def _attempt(self, session: Session, delivery: Delivery, dry_run: bool) -> DeliveryView:
        sub = delivery.subscriber
        allowlist = self._config.email.allowlist
        event_ids = json.loads(delivery.event_ids)
        with span("outbox.send", subscriber_id=sub.id, delivery_id=delivery.id,
                  provider=self._config.email.provider, event_count=len(event_ids), dry_run=dry_run) as attempt:
            events = list(session.scalars(select(ContentEvent).where(ContentEvent.id.in_(event_ids))))
            digest = self._digest_for(session, events)
            if dry_run:
                outcome = "dry_run"
                logger.info("dry-run: would send %s to %s", digest.subject, sub.email)
            elif allowlist and sub.email not in allowlist:
                outcome = "blocked"
                delivery.attempts += 1
                delivery.status = "failed"
                delivery.error = "recipient not in allowlist (dev safety rail)"
            else:
                html, text = self.render(digest)
                result = self._provider.send(sub.email, digest.subject, html, text)
                delivery.attempts += 1
                if result.ok:
                    outcome = delivery.status = "sent"
                    delivery.provider_message_id = result.provider_message_id
                    delivery.sent_at = datetime.now(UTC)
                    delivery.error = None
                else:
                    outcome = delivery.status = "failed"
                    delivery.error = result.error

            if attempt is not None:
                attempt.message = f"Email delivery #{delivery.id}: {outcome}"
                attempt.set_attribute("outcome", outcome)
                attempt.set_attribute("attempts", delivery.attempts)
                if outcome in {"failed", "blocked"}:
                    attempt.set_level("error")
        return self._delivery_view(delivery, sub.email)

    # -- queries ----------------------------------------------------------

    def list(self, status: str | None = None) -> list[DeliveryView]:
        with self._sessions() as session:
            stmt = select(Delivery).order_by(Delivery.created_at.desc())
            if status:
                stmt = stmt.where(Delivery.status == status)
            return [
                self._delivery_view(d, d.subscriber.email) for d in session.scalars(stmt).all()
            ]

    def preview(self, email: str) -> str:
        """Render what the subscriber WOULD receive right now (no send, no rows)."""
        with self._sessions() as session:
            sub = session.scalar(select(Subscriber).where(Subscriber.email == email))
            if sub is None:
                raise ValueError(f"no such subscriber: {email}")
            topics = set(self._subscribers.effective_topics(session, sub))
            delivered = {
                de.event_id
                for de in session.scalars(
                    select(DeliveredEvent).where(DeliveredEvent.subscriber_id == sub.id)
                ).all()
            }
            events = [
                e
                for e in session.scalars(
                    select(ContentEvent).order_by(ContentEvent.created_at)
                ).all()
                if e.id not in delivered and e.topic.name in topics and _wants(sub, e)
            ]
            if not events:
                return "(no undelivered events for this subscriber)"
            digest = self._digest_for(session, events)
            _, text = self.render(digest)
            return text

    @staticmethod
    def _delivery_view(delivery: Delivery, email: str) -> DeliveryView:
        return DeliveryView(
            id=delivery.id,
            subscriber_email=email,
            status=delivery.status,  # type: ignore[arg-type]
            event_count=len(json.loads(delivery.event_ids)),
            attempts=delivery.attempts,
            provider_message_id=delivery.provider_message_id,
            created_at=delivery.created_at,
            sent_at=delivery.sent_at,
            error=delivery.error,
        )
