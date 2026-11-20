"""Domain DTOs — the contract between services and every adapter (CLI, web).

SQLAlchemy rows never cross the service boundary; these models do. The CLI's
--json output serialises exactly these models, which is also the future JSON
API response shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, EmailStr, Field

RunStatus = Literal["queued", "running", "success", "publish_failed", "failed"]
EventKind = Literal["brief_created", "brief_updated", "escalation", "digest", "report", "other"]
SubscriberMode = Literal["selected", "all"]
DigestFrequency = Literal["per-run", "daily", "weekly"]
DeliveryStatus = Literal["pending", "sent", "failed"]


class _FromORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TopicView(_FromORM):
    id: int
    uuid: str
    name: str
    slug: str
    active: bool


class RunRecord(_FromORM):
    id: int
    task: str
    topic: str | None = Field(
        default=None, validation_alias=AliasChoices(AliasPath("topic", "name"), "topic")
    )
    status: RunStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pre_commit: str | None = None
    post_commit: str | None = None
    num_turns: int | None = None
    cost_usd: float | None = None
    error: str | None = None


class ContentEventView(_FromORM):
    id: int
    run_id: int
    topic: str
    kind: EventKind
    file_path: str
    commit_hash: str
    note_uuid: str | None = None
    title: str | None = None
    jurisdiction: str | None = None
    effective_date: str | None = None
    author_name: str | None = None
    author_email: str | None = None
    author_kind: str | None = None     # reginald | contributor
    summary: str | None = None         # digest one-liner
    created_at: datetime


class PendingEvent(BaseModel):
    """An event derived from a diff but not yet persisted (vault identify-changes --dry-run)."""

    topic: str
    kind: EventKind
    file_path: str
    title: str | None = None


class IdentifyChangesResult(BaseModel):
    """Outcome of scanning the vault for changes since the last recorded commit."""

    dry_run: bool = False
    initialized: bool = False          # first-ever run: cursor set to HEAD, nothing walked
    cursor_from: str | None = None
    cursor_to: str | None = None
    commits_scanned: int = 0
    events_recorded: list[PendingEvent] = Field(default_factory=list)
    events_skipped: int = 0            # already recorded — re-walks are no-ops
    items_new: int = 0                 # callout work items enqueued
    items_actioned: int = 0            # answered by a nested reply
    items_not_actioned: int = 0        # callout or file deleted before actioning
    published: bool | None = None      # None = publish not attempted


class AddSubscriber(BaseModel):
    email: EmailStr
    name: str | None = None
    mode: SubscriberMode = "selected"
    topics: list[str] = Field(default_factory=list)      # includes (mode=selected)
    excludes: list[str] = Field(default_factory=list)    # excludes (mode=all)
    digest_frequency: DigestFrequency = "per-run"
    # Team-member prefs: everyone hears about new content, updates and
    # escalations unless they opt out of a kind.
    notify_new: bool = True
    notify_updated: bool = True
    notify_escalations: bool = True


class SubscriberView(_FromORM):
    id: int
    email: str
    name: str | None = None
    active: bool
    mode: SubscriberMode
    digest_frequency: DigestFrequency
    notify_new: bool = True
    notify_updated: bool = True
    notify_escalations: bool = True
    effective_topics: list[str] = Field(default_factory=list)
    excluded_topics: list[str] = Field(default_factory=list)


class DeliveryView(_FromORM):
    id: int
    subscriber_email: str
    status: DeliveryStatus
    event_count: int
    attempts: int
    provider_message_id: str | None = None
    created_at: datetime
    sent_at: datetime | None = None
    error: str | None = None


class DigestItem(BaseModel):
    """One note, on one line. Coalesced two ways: across every topic it is
    filed under (the DB holds one event row per topic, which is what routes it
    to a subscriber of either) and across every edit in this digest window."""

    title: str
    file_path: str
    topics: list[str] = Field(default_factory=list)  # every topic the note is filed under
    jurisdiction: str | None = None
    effective_date: str | None = None
    author_name: str | None = None
    author_kind: str | None = None     # reginald | contributor
    summary: str | None = None
    edits: int = 1                     # distinct commits touching this note


class EscalationItem(BaseModel):
    """An Escalations/ note raised in this window. That folder is the only
    thing in the system that makes something an escalation."""

    title: str
    file_path: str
    topics: list[str] = Field(default_factory=list)
    number: int | None = None          # ESC-NNN, once escalations-sync has assigned it
    priority: str | None = None
    raised_by: str | None = None       # task name or git author


class Digest(BaseModel):
    """Three sections, not one per topic: a note filed under two topics is one
    line tagged with both, rather than repeated under each."""

    subject: str
    new: list[DigestItem] = Field(default_factory=list)
    changed: list[DigestItem] = Field(default_factory=list)
    escalations: list[EscalationItem] = Field(default_factory=list)


class SqlResult(BaseModel):
    """One admin SQL statement's outcome. Reads carry columns/rows; writes
    carry the affected row count and where the pre-write backup went."""

    statement: str
    write: bool = False
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    rowcount: int = 0
    truncated: bool = False            # more rows existed than --limit allowed
    backup_path: str | None = None


class PublishResult(BaseModel):
    ok: bool
    dry_run: bool = False
    detail: str = ""


class DoctorCheck(BaseModel):
    name: str
    ok: bool  # True when this check allows doctor to exit successfully.
    detail: str = ""
    warning: bool = False


class DoctorReport(BaseModel):
    ok: bool
    checks: list[DoctorCheck]
