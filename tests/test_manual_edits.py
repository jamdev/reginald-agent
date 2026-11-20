"""Multi-author edits: scanning contributor commits preserves attribution; host-dirty trees are salvaged, never destroyed; callout dialogue
never raises content events.

This file replaces the old checkpoint/ingest tests: the "manual edits raise
no events" doctrine is rescinded — contributor content is first-class and
mails subscribers, subject to the substantive-change rule.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from reginald.core.config import Config, TaskConfig
from reginald.db.session import init_db, make_session_factory
from reginald.runners.base import RunnerResult
from reginald.services.events import EventService
from reginald.services.identify_changes import IdentifyChangesService
from reginald.services.publish import PublishService
from reginald.services.runs import SALVAGE_MESSAGE, RunService
from reginald.services.topics import TopicService

TOPIC = "Payments and E-money"
BRIEF_DIR = f"Topics/{TOPIC}/Briefs"


def git(vault: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(vault), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def write(vault: Path, rel: str, body: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def brief(title: str) -> str:
    return f"---\ntitle: {title}\ntype: brief\n---\n\n# {title}\n"


def contributor_commit(vault: Path, message: str = "add a brief") -> str:
    """Commit staged+unstaged work as a human contributor, not Reginald."""
    git(vault, "add", "-A")
    git(vault, "-c", "user.name=Chris", "-c", "user.email=chris@example.org",
        "commit", "-qm", message)
    return git(vault, "rev-parse", "HEAD")


class StubRunner:
    """Writes `creates` into the vault, then reports ok/failure."""

    def __init__(self, ok: bool = True, creates: dict[str, str] | None = None):
        self.ok = ok
        self.creates = creates or {}
        self.invoked = 0

    def run(self, task_name, task, prompt, vault_path, tools) -> RunnerResult:
        self.invoked += 1
        for rel, body in self.creates.items():
            write(vault_path, rel, body)
        return RunnerResult(ok=self.ok, num_turns=1, cost_usd=0.0,
                            error=None if self.ok else "stub failure")


@pytest.fixture
def vault(tmp_path) -> Path:
    v = tmp_path / "vault"
    (v / BRIEF_DIR).mkdir(parents=True)
    write(v, f"Topics/{TOPIC}/Index.md", f"---\ntitle: {TOPIC}\ntype: index\n---\n")
    write(v, f"{BRIEF_DIR}/existing.md", brief("Existing"))
    write(v, "Setup/Tasks/demo.md", "do the thing")
    git(v, "init", "-q", "-b", "main")
    # Repo-local identity: the test must not depend on a global git config.
    git(v, "config", "user.email", "test@example.org")
    git(v, "config", "user.name", "Test")
    git(v, "add", "-A")
    git(v, "commit", "-q", "-m", "initial")
    return v


@pytest.fixture
def config(vault, tmp_path) -> Config:
    return Config(
        env="dev",
        vault_path=vault,
        db_path=tmp_path / "test.db",
        git={"push": False},        # no remote in the test repo
        site={"enabled": False},    # no obsidian-headless
        tasks={"demo": TaskConfig(title="Demo", runner="pydantic_agent", model="test")},
    )


@pytest.fixture
def sessions(config) -> sessionmaker[Session]:
    init_db(config.db_path)
    return make_session_factory(config.db_path)


@pytest.fixture
def services(sessions, config):
    topics = TopicService(sessions, config)
    topics.sync()
    events = EventService(sessions, topics)
    changes = IdentifyChangesService(sessions, config, events, PublishService(config))
    # settle topics.sync's uuid writes, then prime the cursor: the
    # first-ever call initialises at HEAD without walking history
    if git(config.vault_path, "status", "--porcelain"):
        git(config.vault_path, "add", "-A")
        git(config.vault_path, "commit", "-q", "-m", "settle topic uuids")
    assert changes.run().initialized
    return topics, events, changes


def make_runs(sessions, config, services, runner) -> RunService:
    topics, _events, changes = services
    return RunService(sessions, config, {"pydantic_agent": runner}, topics, changes)


# -- salvage ----------------------------------------------------------------


def test_uncommitted_work_survives_a_failed_run(sessions, config, services, vault):
    """The case that used to lose data: reset --hard + clean -fd on failure."""
    write(vault, f"{BRIEF_DIR}/human-new.md", brief("Human New"))      # untracked
    write(vault, f"{BRIEF_DIR}/existing.md", brief("Edited By Human")) # modified

    runs = make_runs(sessions, config, services, StubRunner(ok=False))
    record = runs.execute("demo")

    assert record.status == "failed"
    # clean -fd would have deleted the new file; reset --hard reverted the edit
    assert (vault / BRIEF_DIR / "human-new.md").exists()
    assert "Edited By Human" in (vault / BRIEF_DIR / "existing.md").read_text()
    assert git(vault, "log", "-1", "--format=%s") == SALVAGE_MESSAGE


def test_salvaged_content_is_evented_not_hidden(sessions, config, services, vault):
    """Rescinded doctrine: content changes mail subscribers whoever made them.
    A salvage commit is scanned like any other commit."""
    _topics, events, _changes = services
    write(vault, f"{BRIEF_DIR}/host-edit.md", brief("Host Edit"))
    runner = StubRunner(ok=True, creates={f"{BRIEF_DIR}/agent.md": brief("Agent")})

    record = make_runs(sessions, config, services, runner).execute("demo")
    assert record.status == "success"

    paths = {e.file_path for e in events.list()}
    assert f"{BRIEF_DIR}/agent.md" in paths
    assert f"{BRIEF_DIR}/host-edit.md" in paths  # would have been silent before


def test_clean_vault_is_not_salvaged(sessions, config, services, vault):
    before = git(vault, "rev-list", "--count", "HEAD")
    make_runs(sessions, config, services, StubRunner(ok=True)).execute("demo")
    # agent changed nothing and the tree was clean: no salvage, no commit
    assert git(vault, "rev-list", "--count", "HEAD") == before


# -- identifying contributor changes -------------------------------------


def test_contributor_commit_is_evented_with_attribution(config, services, vault):
    _topics, events, changes = services
    write(vault, f"{BRIEF_DIR}/2026-08-10 - Chris finds a thing - UK.md", brief("Chris Finds"))
    contributor_commit(vault, "add a brief I spotted")

    result = changes.run()

    assert len(result.events_recorded) == 1
    [event] = events.list()
    assert event.author_name == "Chris"
    assert event.author_kind == "contributor"
    assert event.kind == "brief_created"


def test_reginald_commits_are_attributed_as_reginald(config, services, vault):
    _topics, events, changes = services
    write(vault, f"{BRIEF_DIR}/agent-brief.md", brief("Agent Brief"))
    git(vault, "add", "-A")
    git(vault, "-c", "user.name=Reginald", "-c", "user.email=reginald@localhost",
        "commit", "-qm", "run: demo 2026-08-10")
    changes.run()

    [event] = events.list()
    assert event.author_kind == "reginald"


def test_identify_changes_is_idempotent(config, services, vault):
    _topics, events, changes = services
    write(vault, f"{BRIEF_DIR}/once.md", brief("Once"))
    contributor_commit(vault)

    first = changes.run()
    second = changes.run()

    assert len(first.events_recorded) == 1
    assert second.commits_scanned == 0
    assert len(events.list()) == 1


def test_callout_only_edit_raises_no_event_but_queues_work(config, services, vault):
    """Dialogue is not content — but a [!todo] is work for Reginald."""
    _topics, events, changes = services
    path = vault / BRIEF_DIR / "existing.md"
    path.write_text(
        path.read_text() + "\n> [!todo] @reginald check this against the source\n> please\n"
    )
    contributor_commit(vault, "leave a note for reginald")

    result = changes.run()

    assert result.events_recorded == []      # substantive-change rule
    assert result.items_new == 1
    [item] = changes.items(state="pending")
    assert item.callout_type == "todo"
    assert item.addressee == "reginald"
    assert item.author_name == "Chris"
    assert item.author_kind == "contributor"


def test_a_nested_reply_actions_the_item(config, services, vault):
    _topics, _events, changes = services
    path = vault / BRIEF_DIR / "existing.md"
    path.write_text(path.read_text() + "\n> [!question] @reginald is this in force yet?\n")
    contributor_commit(vault, "question")
    changes.run()

    text = path.read_text()
    path.write_text(text + "> > [!success] 2026-08-11 — Yes, since 27 July\n")
    git(vault, "add", "-A")
    git(vault, "-c", "user.name=Reginald", "-c", "user.email=reginald@localhost",
        "commit", "-qm", "run: action-edits 2026-08-11")
    result = changes.run()

    assert result.items_actioned == 1
    assert changes.items(state="pending") == []
    [item] = changes.items(state="actioned")
    assert item.callout_type == "question"


def test_deleting_a_callout_marks_it_not_actioned(config, services, vault):
    _topics, _events, changes = services
    path = vault / BRIEF_DIR / "existing.md"
    original = path.read_text()
    path.write_text(original + "\n> [!todo] never mind\n")
    contributor_commit(vault, "note")
    changes.run()

    path.write_text(original)  # contributor thinks better of it
    contributor_commit(vault, "remove note")
    result = changes.run()

    assert result.items_not_actioned == 1
    assert changes.items(state="pending") == []


@pytest.mark.parametrize("queued", [False, True])
def test_scoped_run_only_events_its_topic_and_keeps_contributor_changes(
    sessions, config, services, vault, queued
):
    topics, events, changes = services
    write(vault, "Topics/Other/Index.md", "---\ntitle: Other\n---\n")
    write(vault, "Topics/Other/Briefs/manual.md", brief("Contributor"))
    topics.sync()
    contributor_commit(vault)
    runner = StubRunner(creates={
        f"{BRIEF_DIR}/in.md": brief("In scope"),
        "Topics/Other/Briefs/out.md": brief("Outside scope"),
    })
    runs = make_runs(sessions, config, services, runner)
    topic = next(t for t in topics.list() if t.name == TOPIC)
    if queued:
        requested = runs.request("demo", topic=topic.uuid)
        assert requested.topic == TOPIC
        [record] = runs.dispatch()
        assert record.id == requested.id
    else:
        record = runs.execute("demo", topic=topic.uuid)

    assert record.status == "success"
    assert record.topic == runs.show(record.id).topic == TOPIC
    by_title = {event.title: event for event in events.list()}
    assert set(by_title) == {"In scope", "Contributor"}
    assert by_title["In scope"].run_id == record.id
    assert by_title["Contributor"].run_id != record.id
    assert by_title["Contributor"].author_kind == "contributor"
    assert changes.run().events_recorded == []


def test_scoped_commit_is_identified_after_push_rebases_it(
    sessions, config, services, vault, tmp_path
):
    remote = tmp_path / "origin.git"
    git(vault, "clone", "--bare", str(vault), str(remote))
    git(vault, "remote", "add", "origin", str(remote))
    config.git.push = True
    peer = tmp_path / "peer"
    git(vault, "clone", str(remote), str(peer))

    class ConcurrentContributor(StubRunner):
        def run(self, task_name, task, prompt, vault_path, tools):
            write(peer, "Topics/Other/Briefs/peer.md", brief("Peer brief"))
            contributor_commit(peer)
            git(peer, "push", "origin", "main")
            return super().run(task_name, task, prompt, vault_path, tools)

    runner = ConcurrentContributor(creates={
        f"{BRIEF_DIR}/agent.md": brief("Agent brief"),
        "Topics/Other/Briefs/outside.md": brief("Outside scope"),
    })
    record = make_runs(sessions, config, services, runner).execute("demo", topic=TOPIC)

    assert record.post_commit == git(remote, "rev-parse", "main")
    assert git(vault, "log", "-1", "--format=%s", "HEAD^") == "add a brief"
    events = {e.title: e for e in services[1].list()}
    assert set(events) == {"Agent brief", "Peer brief"}
    assert events["Agent brief"].run_id == record.id
    assert events["Peer brief"].run_id != record.id


@pytest.mark.parametrize("stall", [False, True], ids=["completed", "timeout"])
def test_codex_edits_settled_by_run_service(stall, sessions, config, services, vault, monkeypatch):
    """Drive the production runner supervisor, using an offline child in place of the SDK."""
    import asyncio
    import sys

    from reginald.core.context import build_runners

    config.tasks["demo"] = TaskConfig(
        title="Demo", runner="codex_agent", model="gpt-5.4",
        tools=["CodexWorkspaceWrite"], timeout_seconds=1 if stall else 10,
    )
    head = git(vault, "rev-parse", "HEAD")
    worker = """
import json, pathlib, sys, time
job = json.load(sys.stdin)
assert job["write"]
vault = pathlib.Path(job["vault"])
(vault / "output.md").write_text("Codex edit")
print('{"kind":"turn"}', flush=True)
"""
    if stall:
        worker += """
try:
    time.sleep(60)
except KeyboardInterrupt:
    (vault / "final-partial-edit.md").write_text("still stopping")
"""
    else:
        worker += """print('{"kind":"done"}')"""
    spawn = asyncio.create_subprocess_exec

    async def start(*argv, **kwargs):
        assert argv[-1] == "reginald.runners.codex_agent.worker"
        return await spawn(sys.executable, "-I", "-c", worker, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)
    topics, _, changes = services
    runs = RunService(sessions, config, build_runners(config), topics, changes)
    record = runs.execute("demo")
    assert record.num_turns == 1
    if stall:
        assert record.status == "failed" and "timeout_seconds" in record.error
        assert git(vault, "rev-parse", "HEAD") == head
        assert not (vault / "output.md").exists()
        assert not (vault / "final-partial-edit.md").exists()
    else:
        assert record.status == "success"
        assert git(vault, "show", "HEAD:output.md") == "Codex edit"
    assert git(vault, "status", "--porcelain") == ""
