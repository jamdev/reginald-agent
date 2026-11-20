"""SQLAlchemy ORM schema. Operational state only — the vault owns content."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, default=lambda: str(uuid4())
    )
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task: Mapped[str] = mapped_column(String, nullable=False)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"), nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pre_commit: Mapped[str | None] = mapped_column(String)
    post_commit: Mapped[str | None] = mapped_column(String)
    num_turns: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    topic: Mapped[Topic | None] = relationship()


class ContentEvent(Base):
    __tablename__ = "content_events"
    __table_args__ = (UniqueConstraint("commit_hash", "file_path", "kind", "topic_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), nullable=False)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    commit_hash: Mapped[str] = mapped_column(String, nullable=False)
    note_uuid: Mapped[str | None] = mapped_column(String, index=True)  # frontmatter uuid
    title: Mapped[str | None] = mapped_column(String)
    jurisdiction: Mapped[str | None] = mapped_column(String)
    effective_date: Mapped[str | None] = mapped_column(String)
    meta: Mapped[str | None] = mapped_column(Text)  # JSON: key_dates, flags, secondary_topics
    # Attribution: the git author of the commit that produced this event.
    author_name: Mapped[str | None] = mapped_column(String)
    author_email: Mapped[str | None] = mapped_column(String)
    author_kind: Mapped[str | None] = mapped_column(String)  # reginald | contributor
    summary: Mapped[str | None] = mapped_column(String)  # digest one-liner (changelog note)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    topic: Mapped[Topic] = relationship()


class Escalation(Base):
    """DB mirror of an Escalations/*.md file. The vault file is the truth;
    this row is bookkeeping (queries, doctor, number allocation)."""

    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    number: Mapped[int | None] = mapped_column(Integer, unique=True)  # ESC-NNN, sync-assigned
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    raised_date: Mapped[str | None] = mapped_column(String)  # ISO date
    status_changed_date: Mapped[str | None] = mapped_column(String)  # ISO date
    raised_by: Mapped[str | None] = mapped_column(String)  # task name or git author
    priority: Mapped[str | None] = mapped_column(String)
    topic_id: Mapped[int | None] = mapped_column(ForeignKey("topics.id"))
    missing_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    topic: Mapped[Topic | None] = relationship()


class CalloutItem(Base):
    """One detected [!todo]/[!question] — the durable work queue.

    content_hash is the idempotency key (note identity + type + normalised
    text), so re-scanning any commit range is a no-op.
    """

    __tablename__ = "callout_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uuid: Mapped[str] = mapped_column(
        String, unique=True, nullable=False, default=lambda: str(uuid4())
    )
    content_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    note_uuid: Mapped[str | None] = mapped_column(String, index=True)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    escalation_id: Mapped[int | None] = mapped_column(ForeignKey("escalations.id"))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("callout_items.id"))
    callout_type: Mapped[str] = mapped_column(String, nullable=False)  # todo | question
    title_line: Mapped[str | None] = mapped_column(String)
    body_excerpt: Mapped[str | None] = mapped_column(Text)
    author_name: Mapped[str | None] = mapped_column(String)
    author_email: Mapped[str | None] = mapped_column(String)
    author_kind: Mapped[str | None] = mapped_column(String)  # reginald | contributor
    addressee: Mapped[str | None] = mapped_column(String)  # @handle, lowercased
    introduced_commit: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    actioned_run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    escalation: Mapped[Escalation | None] = relationship()


class Cursor(Base):
    """Named progress marker over the vault's git history."""

    __tablename__ = "cursors"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    commit_hash: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Subscriber(Base):
    __tablename__ = "subscribers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="selected")
    digest_frequency: Mapped[str] = mapped_column(String, nullable=False, default="per-run")
    # Team-member notification prefs: both on by default — subscribers are
    # collaborators on the knowledge base, not an external mailing list.
    notify_new: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_updated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Escalations are always delivered by default, but can be turned off so a
    # flagged item reaches only the subscribers who own it while everyone else
    # still gets ordinary new/updated content.
    notify_escalations: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="subscriber", cascade="all, delete-orphan"
    )


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("subscriber_id", "topic_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"), nullable=False)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="include")

    subscriber: Mapped[Subscriber] = relationship(back_populates="subscriptions")
    topic: Mapped[Topic] = relationship()


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"), nullable=False)
    event_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    provider_message_id: Mapped[str | None] = mapped_column(String)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    subscriber: Mapped[Subscriber] = relationship()


class DeliveredEvent(Base):
    """Idempotency ledger: an event is delivered to a subscriber at most once."""

    __tablename__ = "delivered_events"
    __table_args__ = (UniqueConstraint("subscriber_id", "event_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscriber_id: Mapped[int] = mapped_column(ForeignKey("subscribers.id"), nullable=False)
    event_id: Mapped[int] = mapped_column(ForeignKey("content_events.id"), nullable=False)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id"), nullable=False)
