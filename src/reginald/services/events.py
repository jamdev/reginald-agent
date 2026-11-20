"""EventService — the content_events feed: persisting it and reading it.

Events are produced by IdentifyChangesService's per-commit walk of the vault's git
history (any author — Reginald and contributors alike) and persisted here
with attribution. Re-persisting is a no-op: content_events is unique on
(commit_hash, file_path, kind, topic_id).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..core.observability import logger
from ..db.schema import ContentEvent, Topic
from ..domain.models import ContentEventView, PendingEvent
from ..publisher.diff import UNROUTED_TOPIC, RawEvent
from .topics import TopicService


def event_view(e: ContentEvent) -> ContentEventView:
    """Build the DTO with the topic name resolved (ORM holds a Topic object)."""
    return ContentEventView(
        id=e.id,
        run_id=e.run_id,
        topic=e.topic.name,
        kind=e.kind,  # type: ignore[arg-type]
        file_path=e.file_path,
        commit_hash=e.commit_hash,
        note_uuid=e.note_uuid,
        title=e.title,
        jurisdiction=e.jurisdiction,
        effective_date=e.effective_date,
        author_name=e.author_name,
        author_email=e.author_email,
        author_kind=e.author_kind,
        summary=e.summary,
        created_at=e.created_at,
    )


def _pending(raw: RawEvent) -> PendingEvent:
    return PendingEvent(
        topic=raw.topic,
        kind=raw.kind,  # type: ignore[arg-type]
        file_path=raw.file_path,
        title=raw.title,
    )


class EventService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        topics: TopicService,
    ):
        self._sessions = session_factory
        self._topics = topics

    # -- recording --------------------------------------------------------

    def persist(
        self,
        run_id: int,
        raws: list[RawEvent],
        author_name: str | None = None,
        author_email: str | None = None,
        author_kind: str | None = None,
    ) -> tuple[list[PendingEvent], int]:
        """Persist raw events with attribution. Returns (recorded, skipped)."""
        recorded: list[PendingEvent] = []
        skipped = 0
        with self._sessions.begin() as session:
            for raw in raws:
                if self._existing(session, raw):
                    skipped += 1
                    continue
                topic = self._topics.get_or_create(session, raw.topic)
                if topic is None and raw.kind == "escalation":
                    # A typo'd or invented `topic:` must not silently swallow a
                    # raise: park it in the unrouted bucket, which the outbox
                    # delivers to all-topics subscribers.
                    logger.warning(
                        "escalation %r names non-canonical topic %r; routing as unrouted",
                        raw.file_path, raw.topic,
                    )
                    topic = self._topics.get_or_create(session, UNROUTED_TOPIC)
                if topic is None:  # non-canonical topic reference (typo/invented)
                    skipped += 1
                    continue
                session.add(
                    ContentEvent(
                        run_id=run_id,
                        topic_id=topic.id,
                        kind=raw.kind,
                        file_path=raw.file_path,
                        commit_hash=raw.commit_hash,
                        note_uuid=raw.note_uuid,
                        title=raw.title,
                        jurisdiction=raw.jurisdiction,
                        effective_date=raw.effective_date,
                        author_name=author_name,
                        author_email=author_email,
                        author_kind=author_kind,
                        summary=raw.summary,
                        meta=raw.meta_json(),
                    )
                )
                recorded.append(_pending(raw))
        return recorded, skipped

    def partition_existing(self, raws: list[RawEvent]) -> tuple[list[RawEvent], int]:
        """Read-only split into (not yet recorded, count already recorded) —
        the dry-run path, which must not create topic rows."""
        fresh, skipped = [], 0
        with self._sessions() as session:
            for raw in raws:
                if self._existing(session, raw):
                    skipped += 1
                else:
                    fresh.append(raw)
        return fresh, skipped

    @staticmethod
    def _existing(session: Session, raw: RawEvent) -> ContentEvent | None:
        """Match on the natural key. Joins Topic by name so this works before
        the topic row exists (dry-run must not create one)."""
        return session.scalar(
            select(ContentEvent)
            .join(Topic, ContentEvent.topic_id == Topic.id)
            .where(
                ContentEvent.commit_hash == raw.commit_hash,
                ContentEvent.file_path == raw.file_path,
                ContentEvent.kind == raw.kind,
                Topic.name == raw.topic,
            )
        )

    # -- queries ----------------------------------------------------------

    def list(
        self,
        topic: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[ContentEventView]:
        with self._sessions() as session:
            stmt = (
                select(ContentEvent)
                .join(Topic, ContentEvent.topic_id == Topic.id)
                .order_by(ContentEvent.created_at.desc())
                .limit(limit)
            )
            if topic:
                stmt = stmt.where(Topic.name == topic)
            if since:
                stmt = stmt.where(ContentEvent.created_at >= since)
            return [event_view(e) for e in session.scalars(stmt).all()]
