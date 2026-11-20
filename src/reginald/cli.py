"""reginald CLI — a thin Typer adapter over the service layer.

Rule: commands parse arguments, call one service method, format the result.
No SQL, no business logic here. --json output serialises the exact domain
models the future web API will return.
"""

from __future__ import annotations

import json as jsonlib
import os
from contextlib import ExitStack, contextmanager
from copy import copy
from datetime import datetime
from pathlib import Path

import typer
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from typer.core import TyperCommand, TyperGroup

try:
    from typer._click.globals import get_current_context
except ImportError:  # Typer versions using the standalone Click package
    from click import get_current_context

from . import __version__
from .cli_logo import print_logo
from .core.context import AppContext
from .core.locking import LockHeldError, run_lock
from .core.observability import span
from .services.escalations import EscalationError
from .services.identify_changes import IDENTIFY_CHANGES
from .services.sql import backup_database, is_read_only
from .services.vault import VaultSyncError


class CommandOverview(TyperGroup):
    """Show global options on a bare invocation and command paths on explicit help."""

    def parse_args(self, ctx, args):
        # Shell completion also parses an empty argument list.
        if not args and not ctx.resilient_parsing:
            ctx.meta["reginald.options_only"] = True
            args = ["--help"]
        return super().parse_args(ctx, args)

    def get_help_option(self, ctx):
        option = super().get_help_option(ctx)
        if option is not None:
            option.help = "List all public commands and exit."
        return option

    def format_help(self, ctx, formatter):
        print_logo()
        # Filter a copy so parsing and completion can still see the full CLI.
        overview = copy(self)
        if ctx.meta.get("reginald.options_only"):
            overview.commands = {}
            overview.epilog = None
        else:
            overview.params = []
            overview.add_help_option = False
            overview.commands = self._commands_by_path()
        TyperGroup.format_help(overview, ctx, formatter)

    def _commands_by_path(self):
        commands = {}
        for name, command in self.commands.items():
            if command.hidden:
                continue
            if isinstance(command, TyperGroup):
                for verb, leaf in command.commands.items():
                    entry = copy(leaf)
                    entry.name = f"{name} {verb}"
                    commands[entry.name] = entry
            else:
                commands[name] = command

        return dict(sorted(commands.items()))


class PassthroughCommand(TyperCommand):
    """Reserve bare --help for Reginald and pass native arguments through verbatim."""

    def parse_args(self, ctx, args):
        if args and args != ["--help"] and args[0] != "--":
            args = ["--", *args]
        return super().parse_args(ctx, args)


app = typer.Typer(
    cls=CommandOverview,
    help="Manage scheduled agents, vault changes and subscriber email.",
    epilog=(
        "Run reginald for global options. Add --help to a group or command for details, "
        "for example reginald task run --help."
    ),
)
task_app = typer.Typer(help="List tasks, run or queue work, and inspect previous runs.")
subscriber_app = typer.Typer(help="Manage subscribers, topic selections and notification preferences.")
outbox_app = typer.Typer(help="Inspect, preview and send subscriber email deliveries.")
topics_app = typer.Typer(help="List topics and reconcile the registry with vault folders.")
db_app = typer.Typer(help="Back up, migrate and query the Reginald database.")
vault_app = typer.Typer(help="Identify changes, inspect events, maintain notes and publish the vault.")
escalations_app = typer.Typer(help="Manage escalation records and inspect the callout work queue.")
config_app = typer.Typer(help="Inspect the resolved configuration for the selected environment.")
app.add_typer(task_app, name="task")
app.add_typer(subscriber_app, name="subscriber")
app.add_typer(outbox_app, name="outbox")
app.add_typer(topics_app, name="topics")
app.add_typer(db_app, name="db")
app.add_typer(vault_app, name="vault")
app.add_typer(escalations_app, name="escalations")
app.add_typer(config_app, name="config")

_state: dict = {"env": None, "vault": None, "db": None, "json": False, "verbose": False}


@contextmanager
def _command_trace(ctx: typer.Context):
    names = [ctx.info_name]
    parent = ctx.parent
    while parent is not None and parent.parent is not None:
        names.append(parent.info_name)
        parent = parent.parent
    command = " ".join(reversed(names))
    with ExitStack() as trace:
        action = trace.enter_context(span(
            "cli.command", command=command, log_when_disabled=False, dry_run=ctx.params.get("dry_run", False),
        ))
        if action is not None:
            action.message = f"reginald {command}"
        code = 0
        try:
            yield
        except typer.Exit as exc:
            code = exc.exit_code
            raise
        except BaseException:
            code = 1
            raise
        finally:
            if action is not None:
                action.set_attribute("exit_code", code)
                if code:
                    action.set_level("error")
            # A CLI exit can signal success. Exceptions can contain arguments
            # such as SQL or email addresses. Record the status and let Typer handle them.
            trace.close()


def get_context() -> AppContext:
    ctx = get_current_context()
    return AppContext(
        env=_state["env"],
        vault_path=_state["vault"],
        db_path=_state["db"],
        verbose=_state["verbose"],
        on_configured=lambda: ctx.with_resource(_command_trace(ctx)),
    )


def emit(payload, human: str | None = None) -> None:
    """JSON mode prints the domain model(s) verbatim — the API contract."""
    if _state["json"]:
        if isinstance(payload, BaseModel):
            typer.echo(payload.model_dump_json(indent=2))
        else:
            typer.echo(
                jsonlib.dumps(
                    [p.model_dump(mode="json") for p in payload], indent=2, default=str
                )
            )
    else:
        typer.echo(human if human is not None else payload)


def _version_callback(value: bool) -> None:
    """Print the version and exit.

    This has to be a parameter callback rather than a check in main(): the
    callback body of a Typer group never runs when no subcommand is given —
    Click fails with "Missing command." first — so `reginald --version` alone
    would never reach it. Parameter callbacks run during parsing, before that.
    """
    if value:
        typer.echo(f"reginald {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    env: str | None = typer.Option(None, "--env", help="dev | prod (default: $REGINALD_ENV or dev)"),
    vault: Path | None = typer.Option(None, "--vault", help="Override vault path"),
    db: Path | None = typer.Option(None, "--db", dir_okay=False, help="Override SQLite database path."),
    json_out: bool = typer.Option(False, "--json", help="Emit JSON for commands that support it."),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Debug logging (agent prose, tool inputs and results)"
    ),
    # The value is unused here: _version_callback prints and exits during parsing.
    version: bool = typer.Option(
        False,
        "--version",
        is_eager=True,
        callback=_version_callback,
        help="Show the version and exit",
    ),
):
    _state.update(env=env, vault=vault, db=db, json=json_out, verbose=verbose)


# -- tasks ---------------------------------------------------------------


@task_app.command("run")
def task_run(
    task: str = typer.Argument(..., help="Task name, e.g. research-new-developments"),
    topic: str | None = typer.Option(None, "--topic", help="Restrict to one topic (UUID or name)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Render the prompt and skip the agent and task steps."),
):
    """Execute a configured task immediately under the vault lock."""
    ctx = get_context()
    record = ctx.runs.execute(task, topic=topic, dry_run=dry_run)
    emit(record, f"run #{record.id} {record.task}: {record.status}"
         + (f" (cost ${record.cost_usd:.2f})" if record.cost_usd else ""))
    if record.status in ("failed",):
        raise typer.Exit(1)


@task_app.command("list")
def task_list():
    """List configured tasks with their runner, model and tools."""
    ctx = get_context()
    lines = [
        f"{name:24} runner={t.runner:12} model={t.model:28} tools={','.join(t.tools) or '-'}"
        for name, t in sorted(ctx.config.tasks.items())
    ]
    typer.echo("\n".join(lines))


@task_app.command("run-history")
def task_run_history(
    task: str | None = typer.Option(None, "--task", help="Filter runs by task name."),
    limit: int = typer.Option(20, "--limit", help="Maximum number of runs to show."),
):
    """List recent runs with status and cost, newest first."""
    ctx = get_context()
    records = ctx.runs.list(task=task, limit=limit)
    human = "\n".join(
        f"#{r.id:<5} {r.task:24} {r.status:15} {r.started_at or '-'}"
        f"  cost={f'${r.cost_usd:.2f}' if r.cost_usd else '-'}"
        for r in records
    ) or "(no runs)"
    emit(records, human)


@task_app.command("inspect-run")
def task_inspect_run(
    run_id: int = typer.Argument(..., help="Numeric ID shown by task run-history."),
):
    """Show the full stored record for a run ID."""
    ctx = get_context()
    record = ctx.runs.show(run_id)
    emit(record, record.model_dump_json(indent=2))


@task_app.command("process-queue")
def task_process_queue():
    """Execute queued task runs in order under the vault lock."""
    ctx = get_context()
    records = ctx.runs.dispatch()
    emit(records, f"dispatched {len(records)} queued run(s)")


@task_app.command("add-to-queue")
def task_add_to_queue(
    task: str = typer.Argument(..., help="Name of a task shown by task list."),
    topic: str | None = typer.Option(None, "--topic", help="Scope the run to a topic UUID or name."),
):
    """Queue a task for execution by task process-queue."""
    ctx = get_context()
    record = ctx.runs.request(task, topic=topic)
    emit(record, f"queued run #{record.id} ({record.task})")


# -- subscribers ----------------------------------------------------------


@subscriber_app.command("add")
def subscriber_add(
    email: str = typer.Argument(..., help="Subscriber email address."),
    name: str | None = typer.Option(None, "--name", help="Subscriber display name."),
    topics: list[str] | None = typer.Option(
        None, "--topics", "-t", help="Topic UUID or name (repeatable)"
    ),
    all_topics: bool = typer.Option(False, "--all-topics", help="Subscribe to every topic, incl. future ones"),
    exclude: list[str] | None = typer.Option(
        None, "--exclude", "-x", help="Topic to exclude (repeatable, with --all-topics)"
    ),
    frequency: str = typer.Option(
        "per-run", "--frequency", help="Store per-run, daily or weekly. Delivery currently uses per-run.",
    ),
    notify: str = typer.Option(
        "new,updated,escalations", "--notify",
        help="Which kinds to receive: any of 'new', 'updated', 'escalations' (default all)",
    ),
):
    """Subscribe an email address to selected topics or all topics."""
    from .domain.models import AddSubscriber

    if not all_topics and not topics:
        typer.echo("error: provide --topics or --all-topics", err=True)
        raise typer.Exit(2)
    kinds = {k.strip() for k in notify.split(",") if k.strip()}
    unknown = kinds - {"new", "updated", "escalations"}
    if unknown or not kinds:
        typer.echo(
            f"error: --notify takes 'new', 'updated' and/or 'escalations', not {sorted(unknown)}",
            err=True,
        )
        raise typer.Exit(2)
    ctx = get_context()
    view = ctx.subscribers.add(
        AddSubscriber(
            email=email,
            name=name,
            mode="all" if all_topics else "selected",
            topics=[t.strip() for t in (topics or []) if t.strip()],
            excludes=[t.strip() for t in (exclude or []) if t.strip()],
            digest_frequency=frequency,  # type: ignore[arg-type]
            notify_new="new" in kinds,
            notify_updated="updated" in kinds,
            notify_escalations="escalations" in kinds,
        )
    )
    emit(view, f"added {view.email} (mode={view.mode}, topics={len(view.effective_topics)}, "
               f"notify={'+'.join(sorted(kinds))})")


@subscriber_app.command("set-notify")
def subscriber_set_notify(
    email: str = typer.Argument(..., help="Subscriber email address."),
    new: str | None = typer.Option(None, "--new", help="Enable or disable these notifications (on or off)."),
    updated: str | None = typer.Option(None, "--updated", help="Enable or disable these notifications (on or off)."),
    escalations: str | None = typer.Option(None, "--escalations", help="Enable or disable these notifications (on or off)."),
):
    """Change which kinds of notification a subscriber receives."""
    def parse(value: str | None) -> bool | None:
        if value is None:
            return None
        if value not in ("on", "off"):
            typer.echo(f"error: expected on|off, got {value!r}", err=True)
            raise typer.Exit(2)
        return value == "on"

    ctx = get_context()
    view = ctx.subscribers.set_notify(
        email, new=parse(new), updated=parse(updated), escalations=parse(escalations)
    )
    emit(
        view,
        f"{view.email}: notify_new={view.notify_new} "
        f"notify_updated={view.notify_updated} notify_escalations={view.notify_escalations}",
    )


@subscriber_app.command("remove")
def subscriber_remove(email: str = typer.Argument(..., help="Subscriber email address.")):
    """Deactivate a subscriber and retain their delivery history."""
    ctx = get_context()
    view = ctx.subscribers.remove(email)
    emit(view, f"deactivated {view.email}")


@subscriber_app.command("list")
def subscriber_list(all: bool = typer.Option(False, "--all", help="Include inactive")):
    """List active subscribers and their notification settings."""
    ctx = get_context()
    views = ctx.subscribers.list(include_inactive=all)
    human = "\n".join(
        f"{v.email:40} mode={v.mode:8} freq={v.digest_frequency:8} "
        f"topics={len(v.effective_topics)}"
        + (f" excl={','.join(v.excluded_topics)}" if v.excluded_topics else "")
        + ("" if v.active else "  [inactive]")
        for v in views
    ) or "(no subscribers)"
    emit(views, human)


@subscriber_app.command("topics")
def subscriber_topics(
    email: str = typer.Argument(..., help="Subscriber email address."),
    include: str | None = typer.Option(None, "--include", help="Topic to include"),
    exclude: str | None = typer.Option(None, "--exclude", help="Topic to exclude"),
    clear: str | None = typer.Option(None, "--clear", help="Remove include/exclude row for topic"),
    mode: str | None = typer.Option(None, "--mode", help="selected | all"),
):
    """Show or change a subscriber’s effective topic selection."""
    ctx = get_context()
    view = ctx.subscribers.get(email)
    if mode:
        view = ctx.subscribers.set_mode(email, mode)
    if include:
        view = ctx.subscribers.set_topic(email, include, "include")
    if exclude:
        view = ctx.subscribers.set_topic(email, exclude, "exclude")
    if clear:
        view = ctx.subscribers.set_topic(email, clear, None)
    emit(view, f"{view.email}: mode={view.mode}, effective topics:\n  " + "\n  ".join(view.effective_topics))


# -- outbox ---------------------------------------------------------------


@outbox_app.command("drain")
def outbox_drain(
    dry_run: bool = typer.Option(False, "--dry-run", help="Build pending records and preview delivery without sending email."),
):
    """Build subscriber digests and attempt eligible pending or failed deliveries."""
    ctx = get_context()
    results = ctx.outbox.drain(dry_run=dry_run)
    human = "\n".join(
        f"#{d.id:<4} {d.subscriber_email:40} {d.status:8} events={d.event_count}"
        + (f"  error={d.error}" if d.error else "")
        for d in results
    ) or "(nothing to send)"
    emit(results, human)


@outbox_app.command("list")
def outbox_list(
    status: str | None = typer.Option(None, "--status", help="Filter by delivery status, such as pending or failed."),
):
    """List email deliveries with their status and attempt count."""
    ctx = get_context()
    results = ctx.outbox.list(status=status)
    human = "\n".join(
        f"#{d.id:<4} {d.subscriber_email:40} {d.status:8} events={d.event_count} attempts={d.attempts}"
        for d in results
    ) or "(outbox empty)"
    emit(results, human)


@outbox_app.command("retry")
def outbox_retry(
    delivery_id: int | None = typer.Argument(None, help="Delivery ID. Omit to retry all failed deliveries."),
):
    """Attempt one delivery again, or retry all failed deliveries."""
    ctx = get_context()
    results = ctx.outbox.retry(delivery_id)
    emit(results, f"retried {len(results)} deliver{'y' if len(results)==1 else 'ies'}")


@outbox_app.command("preview")
def outbox_preview(email: str = typer.Argument(..., help="Subscriber email address.")):
    """Render a subscriber's next digest to stdout without sending."""
    ctx = get_context()
    typer.echo(ctx.outbox.preview(email))


# -- topics / events ------------------------------------------------------


@topics_app.command("sync")
def topics_sync():
    """Sync the topic registry with vault folders and fill missing topic UUIDs."""
    ctx = get_context()
    views = ctx.topics.sync()
    emit(views, f"synced {sum(1 for v in views if v.active)} active topic(s)")


@topics_app.command("list")
def topics_list():
    """List registered topics with their UUIDs and active status."""
    ctx = get_context()
    views = ctx.topics.list()
    human = "\n".join(
        f"{v.uuid}  {v.name}" + ("" if v.active else "  [inactive]") for v in views
    ) or "(no topics — run 'reginald topics sync')"
    emit(views, human)


@vault_app.command("list-events")
def vault_list_events(
    topic: str | None = typer.Option(None, "--topic", help="Filter by exact topic name."),
    since: str | None = typer.Option(None, "--since", help="Show events recorded on or after this ISO date or timestamp."),
    limit: int = typer.Option(50, "--limit", help="Maximum number of events to show."),
):
    """List recorded vault content events, newest first."""
    ctx = get_context()
    since_dt = datetime.fromisoformat(since) if since else None
    views = ctx.events.list(topic=topic, since=since_dt, limit=limit)
    human = "\n".join(
        f"#{v.id:<5} {v.kind:15} {v.topic:45} {v.title or v.file_path}"
        for v in views
    ) or "(no events)"
    emit(views, human)


# -- escalations -----------------------------------------------------------


def _escalation_line(row) -> str:
    number = f"ESC-{row.number:03d}" if row.number else "ESC-???"
    flags = " [file missing]" if row.missing_since else ""
    return (f"{number:9} {row.status:18} raised {row.raised_date or '?'}  "
            f"changed {row.status_changed_date or '?'}  {row.title or row.file_path}{flags}")


@escalations_app.command("list")
def escalations_list(
    status: str | None = typer.Option(None, "--status", help="pending | actioned-by-agent | actioned-by-human | ignored"),
):
    """List escalation records, optionally filtered by status."""
    ctx = get_context()
    rows = ctx.escalations.list(status=status)
    typer.echo("\n".join(_escalation_line(r) for r in rows) or "(no escalations)")


@escalations_app.command("show")
def escalations_show(identifier: str = typer.Argument(..., help="ESC-NNN or uuid")):
    """Show an escalation’s status, file location and identifiers."""
    ctx = get_context()
    try:
        row = ctx.escalations.get(identifier)
    except EscalationError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(_escalation_line(row))
    typer.echo(f"file:  {row.file_path}")
    typer.echo(f"uuid:  {row.uuid}")
    typer.echo(f"by:    {row.raised_by or '?'}   priority: {row.priority or '-'}")


@escalations_app.command("set-status")
def escalations_set_status(
    identifier: str = typer.Argument(..., help="ESC-NNN or uuid"),
    status: str = typer.Argument(..., help="pending | actioned-by-agent | actioned-by-human | ignored"),
):
    """Change an escalation’s status, reconcile its file location and update the database."""
    ctx = get_context()
    try:
        with run_lock(ctx.config.lock_path):
            row = ctx.escalations.set_status(identifier, status)
            committed = _commit_stewardship(ctx, f"reginald: escalation ESC-{row.number:03d} -> {status}")
    except (EscalationError, LockHeldError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    emit_row = f"ESC-{row.number:03d} -> {row.status} ({row.file_path})"
    typer.echo(emit_row + (f"; committed {committed[:10]}" if committed else ""))


@escalations_app.command("sync")
def escalations_sync():
    """Reconcile escalation files with the database and commit any vault repairs."""
    ctx = get_context()
    try:
        with run_lock(ctx.config.lock_path):
            report = ctx.escalations.sync()
            committed = _commit_stewardship(ctx, "reginald: escalations sync") if report.changed else None
    except LockHeldError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    lines = [f"{report.scanned} scanned, {len(report.repaired)} repair(s), "
             f"{report.adopted} adopted, {report.missing} missing"]
    lines += [f"  {r}" for r in report.repaired]
    lines += [f"  ! {a}" for a in report.anomalies]
    if committed:
        lines.append(f"committed {committed[:10]}")
    typer.echo("\n".join(lines))


@escalations_app.command("items")
def escalations_items(
    state: str = typer.Option("pending", "--state", help="pending | actioned | not-actioned"),
    for_handle: str | None = typer.Option(None, "--for", help="Filter by @addressee"),
):
    """List callout work items from notes and escalations."""
    ctx = get_context()
    items = ctx.identify_changes.items(state=state or None, addressee=for_handle)
    lines = [
        f"#{i.id:<4} [{i.callout_type}] {i.state:12} "
        f"{('@' + i.addressee) if i.addressee else '(unaddressed)':<14} "
        f"from {i.author_name or '?'}: {i.title_line or (i.body_excerpt or '')[:60]}  ({i.file_path})"
        for i in items
    ]
    typer.echo("\n".join(lines) or f"(no {state} items)")


@escalations_app.command("migrate")
def escalations_migrate(
    dry_run: bool = typer.Option(False, "--dry-run", help="List the split without writing"),
):
    """Split the legacy escalation note into individual files.

    For an already migrated vault, sync the existing files into the database."""
    ctx = get_context()
    try:
        with run_lock(ctx.config.lock_path):
            report = ctx.escalations.migrate(dry_run=dry_run)
            committed = None
            if not dry_run and not report.already_migrated:
                committed = _commit_stewardship(ctx, "reginald: escalations migrated to Escalations/")
    except LockHeldError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    if report.already_migrated:
        typer.echo("already migrated — DB re-synced from the vault")
        return
    verb = "would create" if dry_run else "created"
    lines = [f"{verb} {report.entries} file(s) "
             f"({report.open_entries} open, {report.resolved_entries} resolved):"]
    lines += [f"  {f}" for f in report.files]
    if committed:
        lines.append(f"committed {committed[:10]}")
    typer.echo("\n".join(lines))


def _commit_stewardship(ctx: AppContext, message: str) -> str | None:
    """Commit structural repairs under Reginald's name (they are its work).
    A subsequent task run can push the commit."""
    from .core.git import VaultGit

    git = VaultGit(
        ctx.config.vault_path,
        author_name=ctx.config.git.author_name,
        author_email=ctx.config.git.author_email,
    )
    if not git.is_repo() or not git.is_dirty():
        return None
    return git.commit_all(message)


# -- site / db / diagnostics ----------------------------------------------


@vault_app.command("publish")
def vault_publish(
    dry_run: bool = typer.Option(False, "--dry-run", help="Ask the configured engine to preview what it would publish."),
):
    """Publish the vault using the configured site engine."""
    app_ctx = get_context()
    result = app_ctx.publish.publish(dry_run=dry_run)
    emit(result, f"publish {'ok' if result.ok else 'FAILED'}: {result.detail[:400]}")
    if not result.ok:
        raise typer.Exit(1)


@vault_app.command("ob", cls=PassthroughCommand)
def vault_ob(args: list[str] = typer.Argument(None, help="Arguments to pass to obsidian-headless.")):
    """Run the obsidian-headless CLI in the vault directory.

    For native help use reginald vault ob -- --help. To sign in, use reginald vault ob login.
    Output and exit status come from obsidian-headless.
    """
    app_ctx = get_context()
    result = app_ctx.publish.ob(args or [])
    if _state["json"]:
        emit(result)
    else:
        if result.stdout:
            typer.echo(result.stdout, nl=False)
        if result.stderr:
            typer.echo(result.stderr, nl=False, err=True)
    raise typer.Exit(result.returncode)


@db_app.command("migrate")
def db_migrate():
    """Apply alembic migrations (backs up the DB first)."""
    import subprocess
    import sys

    ctx = get_context()
    _backup(ctx)
    repo_root = Path(__file__).resolve().parents[2]
    # Prefer the alembic beside the running interpreter: under systemd and
    # update.sh, reginald is invoked by absolute venv path with no venv on PATH.
    alembic = Path(sys.executable).with_name("alembic")
    result = subprocess.run(
        [str(alembic) if alembic.exists() else "alembic",
         "-c", str(repo_root / "alembic.ini"), "upgrade", "head"],
        env={**__import__("os").environ, "REGINALD_DB_PATH": str(ctx.config.db_path)},
        check=False,  # the exit code is forwarded to the caller below
    )
    raise typer.Exit(result.returncode)


@db_app.command("backup")
def db_backup(
    to: Path | None = typer.Option(None, "--to", help="Destination file. Defaults to a dated backup beside the database."),
):
    """Create a consistent backup of the SQLite database."""
    ctx = get_context()
    typer.echo(f"backed up to {_backup(ctx, to)}")


def _backup(ctx: AppContext, to: Path | None = None) -> Path:
    return backup_database(ctx.config.db_path, to)


@db_app.command("run-sql")
def db_run_sql(
    statement: str = typer.Argument(..., help='SQL, e.g. "select * from escalations"'),
    limit: int = typer.Option(200, "--limit", help="Max rows returned by a read"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation on a write"),
):
    """Run one SQL statement against the database. **Admin tool.**

    Reads run immediately. A write (anything not SELECT/WITH/PRAGMA/EXPLAIN)
    asks first, backs the database up, and takes the run lock so it cannot
    interleave with a live task — so it will refuse while a run is in progress.

        reginald db run-sql "select kind, count(*) from content_events group by kind"
        reginald db run-sql "update content_events set kind='brief_created' where id=42"

    One statement per invocation: the driver rejects a semicolon-separated
    script rather than applying half of it.
    """
    ctx = get_context()
    if not is_read_only(statement) and not yes:
        typer.confirm(f"Write to {ctx.config.db_path}:\n  {statement}\nProceed?", abort=True)
    try:
        result = ctx.sql.run(statement, limit=limit)
    except (LockHeldError, ValueError) as exc:
        typer.echo(f"sql failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    except SQLAlchemyError as exc:
        typer.echo(f"sql failed: {exc.orig if hasattr(exc, 'orig') else exc}", err=True)
        raise typer.Exit(1) from exc

    if _state["json"]:
        emit(result)
        return
    if result.write:
        typer.echo(f"{result.rowcount} row(s) affected (backup: {result.backup_path})")
        return
    typer.echo(" | ".join(result.columns))
    for row in result.rows:
        typer.echo(" | ".join("" if v is None else str(v) for v in row))
    typer.echo(
        f"({result.rowcount} row(s)"
        + (f"; truncated at --limit {limit}" if result.truncated else "")
        + ")"
    )


@vault_app.command(IDENTIFY_CHANGES)
def vault_identify_changes(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview local commits without pulling or saving changes."
    ),
):
    """Record content events and callout work from new vault commits.

    The first scan starts at HEAD. Later scans process new commits and
    republish the site if enabled. The scan takes the run lock and uses no model.
    """
    ctx = get_context()
    with run_lock(ctx.config.lock_path, blocking=True):
        result = ctx.identify_changes.run(pull=not dry_run, dry_run=dry_run)
    if result.initialized:
        action = "Would initialise" if dry_run else "Initialised"
        emit(result, f"{action} the change cursor at {result.cursor_to[:10]} — no commits scanned")
        return
    verb = "would record" if result.dry_run else "recorded"
    emit(
        result,
        f"{result.commits_scanned} commit(s) walked; {verb} "
        f"{len(result.events_recorded)} event(s); items: {result.items_new} new, "
        f"{result.items_actioned} actioned, {result.items_not_actioned} not-actioned"
        + (f"; site published: {'ok' if result.published else 'FAILED'}"
           if result.published is not None else ""),
    )


@vault_app.command("backfill-uuids")
def vault_backfill_uuids(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report proposed changes without editing notes."),
):
    """Assign UUIDs to eligible vault notes with missing or invalid identifiers."""
    ctx = get_context()
    result = ctx.vault.backfill_uuids(dry_run=dry_run)
    emit(
        result,
        f"{'would update' if dry_run else 'updated'} {result.updated} of "
        f"{result.scanned} notes ({result.already_had} already had uuids)",
    )


@vault_app.command("normalize-frontmatter")
def vault_normalize_frontmatter(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report proposed changes without editing notes."),
):
    """Collapse duplicate top-level frontmatter keys, keeping the last value.

    Invalid YAML with a repeated key (e.g. an agent that emitted `uuid:` twice)
    is silently resolved by every parser to the last occurrence; this rewrites
    the file to match, dropping the stale earlier copy. Value-preserving and
    idempotent — a no-op on well-formed notes.
    """
    ctx = get_context()
    result = ctx.vault.normalize_frontmatter(dry_run=dry_run)
    verb = "would fix" if dry_run else "fixed"
    detail = ""
    if result.fixed_files:
        detail = ": " + ", ".join(
            f"{path} ({', '.join(keys)})" for path, keys in result.fixed_files.items()
        )
    emit(
        result,
        f"{verb} {result.fixed} of {result.scanned} note(s){detail}",
    )


@vault_app.command("set-publish")
def vault_set_publish(
    dry_run: bool = typer.Option(False, "--dry-run", help="Report proposed changes without editing notes."),
):
    """Apply .publish-ignore rules to the publish flag in note frontmatter.

    Set ignored notes to false and supply true where other notes lack the flag.
    """
    ctx = get_context()
    result = ctx.vault.set_publish_flags(dry_run=dry_run)
    verb = "would set" if dry_run else "set"
    emit(
        result,
        f"{verb} publish flags on {result.scanned} note(s): "
        f"{result.set_true} published, {result.set_false} hidden, "
        f"{result.unchanged} unchanged",
    )


@vault_app.command("git", cls=PassthroughCommand)
def vault_git(
    args: list[str] = typer.Argument(None, help="Git arguments, such as status or log --oneline."),
):
    """Run Git in the vault under the run lock using Reginald’s Git identity.

    For native help use reginald vault git -- --help. Flags after a Git subcommand
    go to Git, including reginald vault git log --help. Git supplies the exit status.
    """
    app_ctx = get_context()
    try:
        result = app_ctx.vault.git(args or [])
    except VaultSyncError as exc:
        typer.echo(f"vault git failed: {exc}", err=True)
        raise typer.Exit(1) from exc
    if _state["json"]:
        emit(result)
    else:
        if result.stdout:
            typer.echo(result.stdout, nl=False)
        if result.stderr:
            typer.echo(result.stderr, nl=False, err=True)
    raise typer.Exit(result.returncode)


@app.command()
def doctor():
    """Check the vault, database, credentials and tools. Exit 1 on failure."""
    ctx = get_context()
    colour = None  # Typer detects whether stdout is a terminal.
    if os.getenv("NO_COLOR"):
        colour = False
    elif force := os.getenv("FORCE_COLOR"):
        colour = force != "0"
    elif os.getenv("TERM") == "dumb":
        colour = False
    if not _state["json"]:
        typer.secho(f"Reginald doctor · {ctx.config.env}", fg=typer.colors.CYAN, bold=True, color=colour)
        typer.echo("Checking the vault, database, tools and runner credentials.")
        typer.echo("Unused runners appear as warnings.\n")

    report = ctx.doctor.check()
    if _state["json"]:
        emit(report)
    else:
        for check in report.checks:
            if not check.ok:
                mark, fg = "[FAIL]", typer.colors.RED
            elif check.warning:
                mark, fg = "[WARN]", typer.colors.YELLOW
            else:
                mark, fg = "[OK]", typer.colors.GREEN
            badge = typer.style(f"{mark:<6}", fg=fg)
            typer.echo(f"{badge} {check.name:24} {check.detail}", color=colour)
        if not report.ok:
            summary, fg = "Some checks failed.", typer.colors.RED
        elif any(check.warning for check in report.checks):
            summary, fg = "Passed with warnings.", typer.colors.YELLOW
        else:
            summary, fg = "All checks passed.", typer.colors.GREEN
        typer.secho(f"\n{summary}", fg=fg, color=colour)
    if not report.ok:
        raise typer.Exit(1)


@config_app.command("show")
def config_show():
    """Print the merged configuration for the selected environment as JSON."""
    ctx = get_context()
    typer.echo(ctx.config.model_dump_json(indent=2))


@app.command(hidden=True)
def serve():
    """Web UI + JSON API (future — see architecture doc § Interface extensibility)."""
    typer.echo("reginald serve is not implemented yet; the service layer is ready for it.")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
