"""RunService — the orchestrator.

execute(): lock -> salvage -> rebase-sync -> record run -> pre steps ->
render prompt -> runner (unless skipped) -> post steps -> commit/push ->
identify changes (events + publish) -> finalise run row.
A failed agent run resets the vault to the pre-run commit.

Task YAML selects pre and post steps by name. core.context.build_steps()
binds those names to service methods when the application starts.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import Config
from ..core.git import RUN_TRAILER, RebaseConflictError, VaultGit
from ..core.locking import run_lock
from ..core.observability import logger, record_run_completion, record_run_usage, span
from ..core.prompts import render_prompt, resolve_prompt
from ..db.schema import Run
from ..domain.models import RunRecord
from ..runners.base import RunnerResult, TaskRunner
from .identify_changes import IdentifyChangesService
from .topics import TopicService

# Message on the commit that sweeps up an unexpectedly dirty host tree at the
# start of a run. In a multi-author vault, contributors work on their own
# clones and arrive by pull — host dirt means Reginald's own leftovers
# (topics-sync uuid writes, a crashed run), so it is salvaged under
# Reginald's name and flagged by `doctor`, not treated as human content.
SALVAGE_MESSAGE = "reginald: salvage uncommitted changes"


class RunError(RuntimeError):
    pass


class BudgetExceededError(RunError):
    pass


@dataclass
class StepContext:
    """State shared by the pre and post steps of one run."""

    run_id: int
    # The identify-changes step saves manifest item IDs here for callouts-verify.
    shared: dict = field(default_factory=dict)


@dataclass
class StepOutcome:
    detail: str = ""
    manifest: str | None = None  # injected into the prompt as {{MANIFEST}}
    skip_agent: bool = False  # a pre-step can report that there is no work


Step = Callable[[StepContext], StepOutcome]


class RunService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        config: Config,
        runners: dict[str, TaskRunner],
        topics: TopicService,
        identify_changes: IdentifyChangesService,
        steps: dict[str, Step] | None = None,
    ):
        self._sessions = session_factory
        self._config = config
        self._runners = runners
        self._topics = topics
        self._identify_changes = identify_changes
        self._steps = steps or {}
        self._git = VaultGit(
            config.vault_path,
            author_name=config.git.author_name,
            author_email=config.git.author_email,
        )

    # -- commands ---------------------------------------------------------

    def request(self, task_name: str, topic: str | None = None) -> RunRecord:
        """Enqueue a run (web 'Run now' path; dispatcher executes it)."""
        self._require_task(task_name)
        topic = self._resolve_topic_name(topic)
        with self._sessions.begin() as session:
            run = Run(task=task_name, status="queued")
            if topic:
                resolved = self._topics.get_or_create(session, topic)
                if resolved is not None:
                    run.topic_id = resolved.id
            session.add(run)
            session.flush()
            return RunRecord.model_validate(run)

    def execute(self, task_name: str, topic: str | None = None, dry_run: bool = False) -> RunRecord:
        """Execute a task now (CLI path). Creates its own run row.
        `topic` accepts a topic UUID (preferred) or name."""
        task = self._require_task(task_name)
        topic = self._resolve_topic_name(topic)
        with run_lock(self._config.lock_path, blocking=True):
            self._check_budget()
            return self._execute_locked(task_name, task, topic, dry_run)

    def dispatch(self) -> list[RunRecord]:
        """Claim and execute queued runs (the web-triggered path)."""
        results = []
        with run_lock(self._config.lock_path, blocking=True):
            with self._sessions() as session:
                queued = session.scalars(
                    select(Run).where(Run.status == "queued").order_by(Run.created_at)
                ).all()
                jobs = [(r.id, r.task, r.topic.name if r.topic else None) for r in queued]
            for run_id, task_name, topic in jobs:
                self._check_budget()
                task = self._require_task(task_name)
                results.append(
                    self._execute_locked(task_name, task, topic, dry_run=False, run_id=run_id)
                )
        return results

    # -- core flow --------------------------------------------------------

    def _execute_locked(
        self, task_name, task, topic: str | None, dry_run: bool, run_id: int | None = None
    ) -> RunRecord:
        config = self._config

        # 1. sync vault + snapshot
        if not self._git.is_repo():
            raise RunError(f"vault at {config.vault_path} is not a git repository")

        # Salvage any dirty tree as its own commit, before the run starts.
        # Two things depend on this happening here rather than later:
        #   - the failure path resets --hard to pre_commit, which would
        #     otherwise discard uncommitted work (and `clean -fd` would
        #     delete new files outright)
        #   - the rebase-sync below refuses to run on a dirty tree
        # Salvage commits can raise events under the substantive-change
        # rule, like every other commit.
        if self._git.is_dirty():
            salvage = self._git.commit_all(SALVAGE_MESSAGE)
            if salvage:
                logger.warning("salvaged uncommitted vault changes as %s", salvage[:10])

        if config.git.push:
            try:
                # fetch + rebase: ff-only deadlocks the moment a contributor
                # pushes while Reginald holds an unpushed commit.
                self._git.sync_with_remote(config.git.remote, config.git.branch)
            except RebaseConflictError as exc:
                # A conflict is never auto-resolved: fail loudly, vault intact.
                raise RunError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 — offline is not fatal
                logger.warning("git sync failed (continuing offline): %s", exc)
        pre_commit = self._git.head()

        # 2. open run row
        with self._sessions.begin() as session:
            if run_id is not None:
                run = session.get(Run, run_id)
            else:
                run = Run(task=task_name)
                session.add(run)
                if topic:
                    resolved = self._topics.get_or_create(session, topic)
                    if resolved is not None:
                        run.topic_id = resolved.id
            run.status = "running"
            run.started_at = datetime.now(UTC)
            run.pre_commit = pre_commit
            session.flush()
            run_id = run.id

        # 3. pre steps (skipped on dry-run: steps write to the DB)
        step_ctx = StepContext(run_id=run_id)
        manifest: str | None = None
        skip_agent = False
        if not dry_run:
            outcomes = self._run_steps(task.pre, step_ctx)
            manifests = [o.manifest for o in outcomes if o.manifest]
            manifest = "\n\n".join(manifests) if manifests else None
            skip_agent = any(o.skip_agent for o in outcomes)

        # 4. prompt. A pre step may advise a work-sized uuid pool (action-edits
        # sizes it to its manifest); otherwise use the config default.
        prev_run = self._previous_run_date(task_name)
        template = resolve_prompt(config, task_name, task)
        uuid_count = step_ctx.shared.get("uuid_count", config.prompts.uuid_pool_size)
        prompt = render_prompt(
            template,
            vault_root=config.vault_path,
            prev_run_date=prev_run,
            topic=topic,
            manifest=manifest,
            uuid_count=uuid_count,
        )

        if dry_run:
            logger.info("dry-run: rendered prompt for %s:\n%s", task_name, prompt[:2000],
                        extra={"conversation_excerpt": True})
            return self._finalise(run_id, "success", dry_run=True, error="dry-run: no agent invoked")

        # 5. agent — unless the pre steps found no work (skip-empty: the
        # scheduled run on a quiet day must not spend money)
        if skip_agent:
            logger.info("skipping agent for %s: pre steps report no work", task_name)
            result = RunnerResult(ok=True, num_turns=0, cost_usd=0.0)
        else:
            runner = self._runners[task.runner]
            tool_cfgs = {
                name: config.tools[name].for_vault(config.vault_path) for name in task.mcp_operations
            }
            labels = {"task": task_name, "runner": task.runner, "model": task.model}
            with span("run.agent", run_id=run_id, **labels) as agent_run:
                result = runner.run(task_name, task, prompt, config.vault_path, tool_cfgs)
                record_run_usage(agent_run, result, labels)

        if not result.ok:
            logger.error("run failed, resetting vault to %s", pre_commit)
            try:
                self._git.reset_hard(pre_commit)
            except Exception as exc:  # noqa: BLE001
                logger.error("vault reset failed: %s", exc)
            return self._finalise(
                run_id, "failed", num_turns=result.num_turns,
                cost_usd=result.cost_usd, error=result.error or "runner reported failure",
            )

        # 6. post steps (verification, structural repair) — before the commit,
        # so their edits land in the run commit
        self._run_steps(task.post, step_ctx)

        # 7. commit + push
        post_commit = pre_commit
        if config.git.commit_per_run:
            # Local civil date: the commit log should read as the day the run happened.
            subject = f"run: {task_name} {date.today().isoformat()}"  # noqa: DTZ011
            # Change detection can identify the producing run before finalisation and after a rebase.
            committed = self._git.commit_all(f"{subject}\n\n{RUN_TRAILER}: {run_id} {pre_commit}")
            post_commit = committed or pre_commit
            if committed and config.git.push:
                try:
                    self._git.push_with_rebase(config.git.remote, config.git.branch)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("git push failed (will push on next run): %s", exc)
                post_commit = self._git.head()

        # 8. Identify changes: events (attributed, incl. anything pulled at the start)
        # + site publish. The cursor makes this idempotent; on failure the
        # commit survives and the next scan catches up.
        status = "success"
        try:
            changes = self._identify_changes.run(pull=False)
            if changes.published is False:
                status = "publish_failed"
        except Exception as exc:  # noqa: BLE001 — vault work is committed; notification failed
            logger.error("post-run identify-changes failed (the next scan will catch up): %s", exc)
            return self._finalise(
                run_id, "failed", post_commit=post_commit,
                num_turns=result.num_turns, cost_usd=result.cost_usd,
                error=f"post-run identify-changes failed: {exc}",
            )

        return self._finalise(
            run_id, status, post_commit=post_commit,
            num_turns=result.num_turns, cost_usd=result.cost_usd,
        )

    def _run_steps(self, names: list[str], ctx: StepContext) -> list[StepOutcome]:
        outcomes: list[StepOutcome] = []
        for name in names:
            step = self._steps.get(name)
            if step is None:
                raise RunError(f"task references unknown step '{name}'")
            with span("run.step", step=name):
                outcome = step(ctx)
            if outcome.detail:
                logger.info("step %s: %s", name, outcome.detail)
            outcomes.append(outcome)
        return outcomes

    # -- queries ----------------------------------------------------------

    def list(self, task: str | None = None, limit: int = 20) -> list[RunRecord]:
        with self._sessions() as session:
            stmt = select(Run).order_by(Run.created_at.desc()).limit(limit)
            if task:
                stmt = stmt.where(Run.task == task)
            return [RunRecord.model_validate(r) for r in session.scalars(stmt).all()]

    def show(self, run_id: int) -> RunRecord:
        with self._sessions() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise RunError(f"no such run: {run_id}")
            return RunRecord.model_validate(run)

    def month_cost(self) -> float:
        now = datetime.now(UTC)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with self._sessions() as session:
            total = session.scalar(
                select(func.coalesce(func.sum(Run.cost_usd), 0.0)).where(
                    Run.created_at >= month_start
                )
            )
            return float(total or 0.0)

    # -- helpers ----------------------------------------------------------

    def _resolve_topic_name(self, identifier: str | None) -> str | None:
        """Topic UUID or name -> current name (used for folder-path scoping)."""
        if identifier is None:
            return None
        name = self._topics.resolve_name(identifier)
        if name:
            return name
        if (self._config.vault_path / "Topics" / identifier).is_dir():
            return identifier  # folder exists but not yet synced
        raise RunError(f"unknown topic: {identifier!r} (run 'reginald topics sync')")

    def _require_task(self, task_name: str):
        task = self._config.tasks.get(task_name)
        if task is None:
            known = ", ".join(sorted(self._config.tasks))
            raise RunError(f"unknown task '{task_name}' (known: {known})")
        return task

    def _check_budget(self) -> None:
        limit = self._config.budget.monthly_limit_usd
        spent = self.month_cost()
        if spent >= limit:
            raise BudgetExceededError(
                f"monthly budget exhausted: ${spent:.2f} of ${limit:.2f} — "
                # Local month name for a human-facing message; not a stored timestamp.
                f"raise budget.monthly_limit_usd or wait for {calendar.month_name[datetime.now().month % 12 + 1]}"  # noqa: DTZ005
            )

    def _previous_run_date(self, task_name: str) -> str | None:
        with self._sessions() as session:
            last = session.scalar(
                select(Run)
                .where(Run.task == task_name, Run.status.in_(["success", "publish_failed"]))
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            if last and last.started_at:
                return last.started_at.date().isoformat()
        # fall back to the vault's Metadata.md
        metadata = self._config.vault_path / "Setup" / "Metadata" / "Metadata.md"
        if metadata.exists():
            from ..publisher.frontmatter import parse_frontmatter

            fm = parse_frontmatter(metadata.read_text())
            value = fm.get("last_run_updater")
            if value:
                return str(value)
        return None

    def _finalise(self, run_id: int, status: str, *, dry_run: bool = False, **fields) -> RunRecord:
        with self._sessions.begin() as session:
            run = session.get(Run, run_id)
            run.status = status
            run.finished_at = datetime.now(UTC)
            for key, value in fields.items():
                if value is not None:
                    setattr(run, key, value)
            record = RunRecord.model_validate(run)
        if not dry_run:
            record_run_completion(record)
        return record
