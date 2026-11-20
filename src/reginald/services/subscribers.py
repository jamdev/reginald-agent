"""SubscriberService — subscriber and subscription management.

Effective topic set is resolved at read/send time, never stored:
  mode='selected' -> include rows
  mode='all'      -> all active topics minus exclude rows
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..db.schema import Subscriber, Subscription, Topic
from ..domain.models import AddSubscriber, SubscriberView
from .topics import OPERATIONAL_TOPIC, TopicService


class SubscriberError(RuntimeError):
    pass


class SubscriberService:
    def __init__(self, session_factory: sessionmaker[Session], topics: TopicService):
        self._sessions = session_factory
        self._topics = topics

    # -- commands ---------------------------------------------------------

    def add(self, cmd: AddSubscriber) -> SubscriberView:
        with self._sessions.begin() as session:
            if session.scalar(select(Subscriber).where(Subscriber.email == cmd.email)):
                raise SubscriberError(f"subscriber already exists: {cmd.email}")
            sub = Subscriber(
                email=cmd.email,
                name=cmd.name,
                mode=cmd.mode,
                digest_frequency=cmd.digest_frequency,
                notify_new=cmd.notify_new,
                notify_updated=cmd.notify_updated,
                notify_escalations=cmd.notify_escalations,
            )
            session.add(sub)
            session.flush()
            names = cmd.topics if cmd.mode == "selected" else cmd.excludes
            kind = "include" if cmd.mode == "selected" else "exclude"
            for name in names:
                topic = self._require_topic(session, name)
                session.add(Subscription(subscriber_id=sub.id, topic_id=topic.id, kind=kind))
            return self._view(session, sub)

    def set_notify(
        self,
        email: str,
        new: bool | None = None,
        updated: bool | None = None,
        escalations: bool | None = None,
    ) -> SubscriberView:
        """Flip per-kind notification prefs; None leaves a pref untouched."""
        with self._sessions.begin() as session:
            sub = self._require_subscriber(session, email)
            if new is not None:
                sub.notify_new = new
            if updated is not None:
                sub.notify_updated = updated
            if escalations is not None:
                sub.notify_escalations = escalations
            return self._view(session, sub)

    def remove(self, email: str) -> SubscriberView:
        """Deactivate (soft delete)."""
        with self._sessions.begin() as session:
            sub = self._require_subscriber(session, email)
            sub.active = False
            return self._view(session, sub)

    def set_topic(self, email: str, topic_name: str, kind: str | None) -> SubscriberView:
        """kind='include'|'exclude' upserts a row; kind=None removes any row."""
        with self._sessions.begin() as session:
            sub = self._require_subscriber(session, email)
            topic = self._require_topic(session, topic_name)
            row = session.scalar(
                select(Subscription).where(
                    Subscription.subscriber_id == sub.id, Subscription.topic_id == topic.id
                )
            )
            if kind is None:
                if row:
                    session.delete(row)
            elif row:
                row.kind = kind
            else:
                session.add(Subscription(subscriber_id=sub.id, topic_id=topic.id, kind=kind))
            return self._view(session, sub)

    def set_mode(self, email: str, mode: str) -> SubscriberView:
        with self._sessions.begin() as session:
            sub = self._require_subscriber(session, email)
            sub.mode = mode
            return self._view(session, sub)

    # -- queries ----------------------------------------------------------

    def list(self, include_inactive: bool = False) -> list[SubscriberView]:
        with self._sessions() as session:
            stmt = select(Subscriber).order_by(Subscriber.email)
            if not include_inactive:
                stmt = stmt.where(Subscriber.active.is_(True))
            return [self._view(session, s) for s in session.scalars(stmt).all()]

    def get(self, email: str) -> SubscriberView:
        with self._sessions() as session:
            return self._view(session, self._require_subscriber(session, email))

    def effective_topics(self, session: Session, sub: Subscriber) -> list[str]:
        rows = session.scalars(
            select(Subscription).where(Subscription.subscriber_id == sub.id)
        ).all()
        includes = {r.topic.name for r in rows if r.kind == "include"}
        excludes = {r.topic.name for r in rows if r.kind == "exclude"}
        if sub.mode == "all":
            active = {
                t.name
                for t in session.scalars(select(Topic).where(Topic.active.is_(True))).all()
                if t.name != OPERATIONAL_TOPIC
            }
            return sorted(active - excludes)
        return sorted(includes - excludes)

    # -- helpers ----------------------------------------------------------

    def _view(self, session: Session, sub: Subscriber) -> SubscriberView:
        rows = session.scalars(
            select(Subscription).where(Subscription.subscriber_id == sub.id)
        ).all()
        view = SubscriberView.model_validate(sub)
        view.effective_topics = self.effective_topics(session, sub)
        view.excluded_topics = sorted(r.topic.name for r in rows if r.kind == "exclude")
        return view

    @staticmethod
    def _require_subscriber(session: Session, email: str) -> Subscriber:
        sub = session.scalar(select(Subscriber).where(Subscriber.email == email))
        if sub is None:
            raise SubscriberError(f"no such subscriber: {email}")
        return sub

    def _require_topic(self, session: Session, identifier: str) -> Topic:
        """Accepts a topic UUID (preferred) or exact name."""
        topic = self._topics.resolve(session, identifier)
        if topic is None:
            raise SubscriberError(
                f"no such topic: {identifier!r} (run 'reginald topics sync' or check spelling)"
            )
        return topic
