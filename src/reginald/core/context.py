"""AppContext — the composition root. Built once per process; every adapter
(CLI now, FastAPI later) gets its services from here and nowhere else."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from ..db.session import init_db, make_session_factory
from ..email.base import EmailProvider
from ..email.console import ConsoleProvider
from ..email.postmark import PostmarkProvider
from ..runners.registry import build_runners
from ..services.doctor import DoctorService
from ..services.escalations import EscalationService
from ..services.events import EventService
from ..services.identify_changes import IDENTIFY_CHANGES, IdentifyChangesService
from ..services.outbox import OutboxService
from ..services.publish import PublishService
from ..services.runs import RunService, Step, StepContext, StepOutcome
from ..services.sql import SqlService
from ..services.subscribers import SubscriberService
from ..services.topics import TopicService
from ..services.vault import VaultService
from . import observability
from .config import Config, load_config


def build_email_provider(config: Config) -> EmailProvider:
    if config.email.provider == "postmark":
        return PostmarkProvider(
            from_address=config.email.from_address,
            message_stream=config.email.message_stream,
            reply_to=config.email.reply_to,
        )
    return ConsoleProvider()


# Headroom over the manifest size for a work-sized uuid pool — one manifest
# item usually yields at most one new note, but a todo can spawn a couple.
UUID_POOL_MARGIN = 5


def build_steps(ctx: AppContext) -> dict[str, Step]:
    """Bind the pre and post step names in task YAML to the services they call."""

    def identify_changes_step(step_ctx: StepContext) -> StepOutcome:
        # Pull already happened in the run preamble; walk what arrived, then
        # build the manifest of pending items addressed to Reginald.
        result = ctx.identify_changes.run(pull=False)
        text, ids = ctx.identify_changes.manifest()
        step_ctx.shared["manifest_item_ids"] = ids
        # Size the uuid pool to the work: at most one new note per manifest
        # item, plus headroom. RunService reads this; the config default
        # applies to tasks with no such advice.
        step_ctx.shared["uuid_count"] = len(ids) + UUID_POOL_MARGIN
        return StepOutcome(
            detail=(
                f"{result.commits_scanned} commit(s) walked, "
                f"{len(result.events_recorded)} event(s), {result.items_new} new item(s), "
                f"{len(ids)} pending for reginald"
            ),
            manifest=text,
            skip_agent=not ids,
        )

    def escalations_sync_step(step_ctx: StepContext) -> StepOutcome:
        report = ctx.escalations.sync()
        return StepOutcome(
            detail=(
                f"escalations: {report.scanned} scanned, {len(report.repaired)} repair(s), "
                f"{report.adopted} adopted"
                + (f", {len(report.anomalies)} anomal(ies)" if report.anomalies else "")
            )
        )

    def normalize_frontmatter_step(step_ctx: StepContext) -> StepOutcome:
        result = ctx.vault.normalize_frontmatter()
        return StepOutcome(detail=f"frontmatter normalize: {result.fixed} note(s) fixed")

    def backfill_uuids_step(step_ctx: StepContext) -> StepOutcome:
        result = ctx.vault.backfill_uuids()
        return StepOutcome(detail=f"uuid backfill: {result.updated} note(s) updated")

    def set_publish_step(step_ctx: StepContext) -> StepOutcome:
        result = ctx.vault.set_publish_flags()
        return StepOutcome(
            detail=(
                f"publish flags: {result.set_true} published, "
                f"{result.set_false} hidden, {result.unchanged} unchanged"
            )
        )

    def callouts_verify_step(step_ctx: StepContext) -> StepOutcome:
        ids = step_ctx.shared.get("manifest_item_ids", [])
        actioned = ctx.identify_changes.verify_actioned(ids, run_id=step_ctx.run_id)
        return StepOutcome(detail=f"{actioned} of {len(ids)} manifest item(s) verified actioned")

    return {
        IDENTIFY_CHANGES: identify_changes_step,
        "escalations-sync": escalations_sync_step,
        "normalize-frontmatter": normalize_frontmatter_step,
        "backfill-uuids": backfill_uuids_step,
        "set-publish": set_publish_step,
        "callouts-verify": callouts_verify_step,
    }


class AppContext:
    def __init__(
        self,
        env: str | None = None,
        vault_path: Path | None = None,
        verbose: bool = False,
        *,
        db_path: Path | None = None,
        on_configured: Callable[[], None] | None = None,
    ):
        self.config = load_config(env=env, vault_path=vault_path, db_path=db_path)
        if verbose:
            # --verbose outranks both the YAML level and $REGINALD_LOG_LEVEL:
            # it is an explicit, per-invocation instruction.
            self.config.logging.level = "DEBUG"
            os.environ.pop(observability.LEVEL_ENV_VAR, None)
        observability.setup(self.config)
        if on_configured is not None:
            on_configured()
        init_db(self.config.db_path)
        sessions = make_session_factory(self.config.db_path)

        self.topics = TopicService(sessions, self.config)
        self.subscribers = SubscriberService(sessions, self.topics)
        self.publish = PublishService(self.config)
        self.events = EventService(sessions, self.topics)
        self.outbox = OutboxService(
            sessions, self.subscribers, build_email_provider(self.config), self.config
        )
        self.vault = VaultService(self.config)
        self.sql = SqlService(sessions, self.config)
        self.escalations = EscalationService(sessions, self.config)
        self.identify_changes = IdentifyChangesService(sessions, self.config, self.events, self.publish)
        self.runs = RunService(
            sessions, self.config, build_runners(self.config),
            self.topics, self.identify_changes, steps=build_steps(self),
        )
        self.doctor = DoctorService(
            self.config, escalations=self.escalations, identify_changes=self.identify_changes
        )
