"""Create the initial Reginald database schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    schema = sa.MetaData()

    sa.Table(
        "topics",
        schema,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False),
    )

    sa.Table(
        "runs",
        schema,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task", sa.String(), nullable=False),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id")),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("pre_commit", sa.String()),
        sa.Column("post_commit", sa.String()),
        sa.Column("num_turns", sa.Integer()),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    sa.Table(
        "content_events",
        schema,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("commit_hash", sa.String(), nullable=False),
        sa.Column("note_uuid", sa.String()),
        sa.Column("title", sa.String()),
        sa.Column("jurisdiction", sa.String()),
        sa.Column("effective_date", sa.String()),
        sa.Column("meta", sa.Text()),
        sa.Column("author_name", sa.String()),
        sa.Column("author_email", sa.String()),
        sa.Column("author_kind", sa.String()),
        sa.Column("summary", sa.String()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("commit_hash", "file_path", "kind", "topic_id"),
        sa.Index("ix_content_events_note_uuid", "note_uuid"),
    )

    sa.Table(
        "escalations",
        schema,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(), nullable=False, unique=True),
        sa.Column("number", sa.Integer(), unique=True),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("title", sa.String()),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("raised_date", sa.String()),
        sa.Column("status_changed_date", sa.String()),
        sa.Column("raised_by", sa.String()),
        sa.Column("priority", sa.String()),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id")),
        sa.Column("missing_since", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
    )

    sa.Table(
        "callout_items",
        schema,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("uuid", sa.String(), nullable=False, unique=True),
        sa.Column("content_hash", sa.String(), nullable=False, unique=True),
        sa.Column("note_uuid", sa.String()),
        sa.Column("file_path", sa.String(), nullable=False),
        sa.Column("escalation_id", sa.Integer(), sa.ForeignKey("escalations.id")),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("callout_items.id")),
        sa.Column("callout_type", sa.String(), nullable=False),
        sa.Column("title_line", sa.String()),
        sa.Column("body_excerpt", sa.Text()),
        sa.Column("author_name", sa.String()),
        sa.Column("author_email", sa.String()),
        sa.Column("author_kind", sa.String()),
        sa.Column("addressee", sa.String()),
        sa.Column("introduced_commit", sa.String()),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("actioned_run_id", sa.Integer(), sa.ForeignKey("runs.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actioned_at", sa.DateTime(timezone=True)),
        sa.Index("ix_callout_items_note_uuid", "note_uuid"),
    )

    sa.Table(
        "cursors",
        schema,
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("commit_hash", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    sa.Table(
        "subscribers",
        schema,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("name", sa.String()),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("digest_frequency", sa.String(), nullable=False),
        sa.Column("notify_new", sa.Boolean(), nullable=False),
        sa.Column("notify_updated", sa.Boolean(), nullable=False),
        sa.Column("notify_escalations", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    sa.Table(
        "subscriptions",
        schema,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscriber_id", sa.Integer(), sa.ForeignKey("subscribers.id"), nullable=False),
        sa.Column("topic_id", sa.Integer(), sa.ForeignKey("topics.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.UniqueConstraint("subscriber_id", "topic_id"),
    )

    sa.Table(
        "deliveries",
        schema,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscriber_id", sa.Integer(), sa.ForeignKey("subscribers.id"), nullable=False),
        sa.Column("event_ids", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("provider_message_id", sa.String()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
    )

    sa.Table(
        "delivered_events",
        schema,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscriber_id", sa.Integer(), sa.ForeignKey("subscribers.id"), nullable=False),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("content_events.id"), nullable=False),
        sa.Column("delivery_id", sa.Integer(), sa.ForeignKey("deliveries.id"), nullable=False),
        sa.UniqueConstraint("subscriber_id", "event_id"),
    )

    # The first application command may already have initialized the database.
    schema.create_all(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    raise NotImplementedError("The initial schema has no downgrade.")
