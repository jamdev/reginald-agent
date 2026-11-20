"""The pre/post step mechanism and the action-edits flow: manifest
injection, skip-empty (no model on a quiet day), post-run verification."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock, call

import pytest

from reginald.core import observability
from reginald.core.config import Config, TaskConfig, ToolConfig
from reginald.core.context import UUID_POOL_MARGIN, AppContext, build_steps
from reginald.db.session import init_db, make_session_factory
from reginald.domain.models import IdentifyChangesResult
from reginald.publisher.frontmatter import parse_frontmatter
from reginald.runners.base import RunnerResult
from reginald.services.events import EventService
from reginald.services.identify_changes import IdentifyChangesService
from reginald.services.publish import PublishService
from reginald.services.runs import RunError, RunService
from reginald.services.topics import TopicService
from reginald.services.vault import VaultService

TOPIC = "Payments and E-money"
BRIEF = f"Topics/{TOPIC}/Briefs/existing.md"
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def git(vault: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(vault), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def write(vault: Path, rel: str, body: str) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


class RecordingRunner:
    def __init__(self):
        self.prompts: list[str] = []

    def run(self, task_name, task, prompt, vault_path, tools) -> RunnerResult:
        self.prompts.append(prompt)
        # the agent replies to the first manifest item, nested
        brief = vault_path / BRIEF
        text = brief.read_text()
        brief.write_text(text + "> > [!success] 2026-08-11 — done as asked\n")
        return RunnerResult(ok=True, num_turns=3, cost_usd=0.01)


@pytest.fixture
def world(tmp_path):
    vault = tmp_path / "vault"
    write(vault, f"Topics/{TOPIC}/Index.md", f"---\ntitle: {TOPIC}\ntype: index\n---\n")
    write(vault, BRIEF, "---\ntitle: Existing\ntype: brief\nuuid: 11111111-1111-4111-8111-111111111111\n---\nbody\n")
    write(vault, "Setup/Tasks/action-edits.md", "Work these items:\n{{MANIFEST}}\n")
    git(vault, "init", "-q", "-b", "main")
    git(vault, "config", "user.email", "test@example.org")
    git(vault, "config", "user.name", "Test")
    git(vault, "add", "-A")
    git(vault, "commit", "-q", "-m", "initial")

    config = Config(
        env="dev", vault_path=vault, db_path=tmp_path / "test.db",
        git={"push": False}, site={"enabled": False},
        tasks={"action-edits": TaskConfig(
            title="Action edits", runner="pydantic_agent", model="test",
            pre=["identify-changes"], post=["callouts-verify"],
        )},
    )
    init_db(config.db_path)
    sessions = make_session_factory(config.db_path)
    topics = TopicService(sessions, config)
    topics.sync()
    events = EventService(sessions, topics)
    changes = IdentifyChangesService(sessions, config, events, PublishService(config))
    if git(vault, "status", "--porcelain"):
        git(vault, "add", "-A")
        git(vault, "commit", "-q", "-m", "settle")
    changes.run()  # prime cursor

    steps = build_steps(Mock(spec=AppContext, identify_changes=changes, vault=VaultService(config)))
    return vault, config, sessions, topics, changes, steps


def leave_todo(vault: Path) -> None:
    brief = vault / BRIEF
    brief.write_text(brief.read_text() + "\n> [!todo] @reginald check the source\n> body\n")
    git(vault, "add", "-A")
    git(vault, "-c", "user.name=Chris", "-c", "user.email=chris@example.org",
        "commit", "-qm", "note for reginald")


def test_manifest_reaches_the_prompt_and_verify_marks_actioned(world):
    vault, config, sessions, topics, changes, steps = world
    leave_todo(vault)
    runner = RecordingRunner()
    runs = RunService(sessions, config, {"pydantic_agent": runner}, topics, changes, steps=steps)

    record = runs.execute("action-edits")

    assert record.status == "success"
    assert len(runner.prompts) == 1
    assert "check the source" in runner.prompts[0]       # manifest injected
    assert "{{MANIFEST}}" not in runner.prompts[0]
    [item] = changes.items(state="actioned")
    assert item.actioned_run_id == record.id             # verified, not trusted


def test_skip_empty_never_invokes_the_model(world):
    """The 01:00 run on a quiet day must not spend money."""
    vault, config, sessions, topics, changes, steps = world
    runner = RecordingRunner()
    runs = RunService(sessions, config, {"pydantic_agent": runner}, topics, changes, steps=steps)

    record = runs.execute("action-edits")

    assert record.status == "success"
    assert runner.prompts == []          # model never invoked
    assert record.cost_usd == 0.0


def test_run_passes_only_selected_mcp_connections(world):
    _, config, sessions, topics, changes, steps = world
    task = config.tasks["action-edits"]
    task.pre = []
    task.tools = ["mcp__search__find", "mcp__search__lookup"]
    config.tools = {"search": ToolConfig(command="search"), "unused": ToolConfig(command="unused")}
    runner = Mock()
    runner.run.return_value = RunnerResult(ok=True)
    runs = RunService(sessions, config, {"pydantic_agent": runner}, topics, changes, steps=steps)

    assert runs.execute("action-edits").status == "success"
    assert runner.run.call_args.args[-1] == {"search": config.tools["search"]}


def test_moving_run_reports_does_not_queue_quoted_callouts(world):
    vault, _, _, _, changes, _ = world
    old = "Setup/Tasks/Logs/archive/sweep.md"
    new = "Setup/Logs/archive/sweep.md"
    write(vault, old, "> [!todo] @reginald Quoted in a report\n")
    git(vault, "add", old)
    git(vault, "commit", "-qm", "old report")
    (vault / new).parent.mkdir(parents=True)
    git(vault, "mv", old, new)
    git(vault, "commit", "-qm", "move report")
    result = changes.run(pull=False)
    assert result.commits_scanned == 2
    assert result.events_recorded == []
    assert result.items_new == 0


class UuidlessRunner:
    """Records its prompt, then files a NEW brief with no uuid — as a weaker
    model might, having exhausted or ignored the pool."""

    def __init__(self):
        self.prompts: list[str] = []

    def run(self, task_name, task, prompt, vault_path, tools) -> RunnerResult:
        self.prompts.append(prompt)
        (vault_path / f"Topics/{TOPIC}/Briefs/new.md").write_text(
            "---\ntitle: New\ntype: brief\n---\nbody\n"
        )
        return RunnerResult(ok=True, num_turns=1, cost_usd=0.01)


def test_pool_is_sized_to_the_manifest_and_backfill_fills_gaps(world):
    vault, config, sessions, topics, changes, steps = world
    config.tasks["action-edits"].post = ["backfill-uuids"]  # the safety net
    leave_todo(vault)  # exactly one manifest item
    runner = UuidlessRunner()
    runs = RunService(sessions, config, {"pydantic_agent": runner}, topics, changes, steps=steps)

    record = runs.execute("action-edits")

    assert record.status == "success"
    pool = [ln.strip() for ln in runner.prompts[0].splitlines() if UUID_RE.match(ln.strip())]
    assert len(pool) == 1 + UUID_POOL_MARGIN          # sized to the work, not the default 25

    fm = parse_frontmatter((vault / f"Topics/{TOPIC}/Briefs/new.md").read_text())
    assert fm.get("uuid")                             # backfill filled what the agent omitted


def test_set_publish_step_flags_the_agents_new_note(world):
    """A brief the agent files with no publish flag must come out `publish:
    true` after the post steps — otherwise obsidian-headless never publishes
    it. (The vault fixture has a .publish-ignore excluding Archive.)"""
    vault, config, sessions, topics, changes, steps = world
    (vault / ".publish-ignore").write_text("Archive\n")
    config.tasks["action-edits"].post = ["set-publish"]
    leave_todo(vault)
    runner = UuidlessRunner()
    runs = RunService(sessions, config, {"pydantic_agent": runner}, topics, changes, steps=steps)

    runs.execute("action-edits")

    fm = parse_frontmatter((vault / f"Topics/{TOPIC}/Briefs/new.md").read_text())
    assert fm.get("publish") is True


def test_default_pool_size_when_no_step_advises(world):
    """A task with no work-sizing pre-step gets the configured default."""
    vault, config, sessions, topics, changes, steps = world
    config.prompts.uuid_pool_size = 25
    config.tasks["action-edits"].pre = []             # no manifest advice this run
    config.tasks["action-edits"].post = []
    runner = RecordingRunner()
    runs = RunService(sessions, config, {"pydantic_agent": runner}, topics, changes, steps=steps)

    runs.execute("action-edits")

    pool = [ln.strip() for ln in runner.prompts[0].splitlines() if UUID_RE.match(ln.strip())]
    assert len(pool) == 25


def test_unknown_step_name_fails_loudly(world):
    vault, config, sessions, topics, changes, steps = world
    config.tasks["action-edits"].pre = ["no-such-step"]
    runs = RunService(sessions, config, {"pydantic_agent": RecordingRunner()}, topics, changes, steps=steps)
    with pytest.raises(RunError, match="unknown step"):
        runs.execute("action-edits")

@pytest.mark.parametrize("outcome", ["success", "failed", "publish_failed", "scan_error"])
def test_run_completion_reports_committed_outcome(world, monkeypatch, outcome):
    _, config, sessions, topics, changes, steps = world
    config.tasks["action-edits"].pre = []
    runner = Mock()
    runner.run.return_value = RunnerResult(ok=outcome != "failed")
    if outcome == "publish_failed":
        monkeypatch.setattr(changes, "run", Mock(return_value=IdentifyChangesResult(published=False)))
    elif outcome == "scan_error":
        monkeypatch.setattr(changes, "run", Mock(side_effect=RuntimeError("scan failed")))
    runs = RunService(sessions, config, {"pydantic_agent": runner}, topics, changes, steps=steps)
    logfire = MagicMock()
    monkeypatch.setattr(observability, "_logfire", logfire)
    observed = []

    def on_completion(name, *, task, run_id, status):
        # A separate DB session must already see the terminal status.
        observed.append((name, task, run_id, status, runs.show(run_id).status))

    logfire.info.side_effect = on_completion
    record = runs.execute("action-edits")

    status = "failed" if outcome == "scan_error" else outcome
    assert observed == [("run.completed", "action-edits", record.id, status, status)]


def test_quiet_queued_run_reports_completion_and_dry_run_is_silent(world, monkeypatch):
    _, config, sessions, topics, changes, steps = world
    runs = RunService(sessions, config, {}, topics, changes, steps=steps)
    logfire = MagicMock()
    monkeypatch.setattr(observability, "_logfire", logfire)

    runs.execute("action-edits", dry_run=True)
    queued = runs.request("action-edits")
    logfire.info.side_effect = RuntimeError("telemetry unavailable")
    [record] = runs.dispatch()

    assert record.id == queued.id
    assert record.status == "success"
    assert logfire.info.call_args_list == [
        call("run.completed", task="action-edits", run_id=queued.id, status="success")
    ]
