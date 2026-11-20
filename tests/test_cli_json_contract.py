"""The interface-extensibility invariant: CLI --json output parses back into
the exact domain models the future web API will serve."""

import json
import re
import subprocess
import sys

import pytest

typer_testing = pytest.importorskip("typer.testing")

# E402: these must follow the importorskip above — importing cli without
# typer.testing available would error instead of skipping the module.
from reginald import cli  # noqa: E402
from reginald.domain.models import RunRecord, SubscriberView, TopicView  # noqa: E402

runner = typer_testing.CliRunner()


def test_version_without_subcommand():
    """`reginald --version` must work on its own.

    Regression guard: with the check in the group callback body instead of an
    eager parameter callback, Click failed with "Missing command." before the
    body ever ran.
    """
    from reginald import __version__

    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == f"reginald {__version__}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / "Topics" / "Payments and E-money" / "Briefs").mkdir(parents=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "base.yaml").write_text(
        f"vault_path: {vault}\n"
        f"db_path: {tmp_path / 'cli.db'}\n"
        "tasks:\n"
        "  key-dates-digest:\n"
        "    title: Key dates digest\n"
        "    runner: claude-agent\n    model: test\n"
    )
    monkeypatch.setenv("REGINALD_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("REGINALD_ENV", "dev")
    return tmp_path


def invoke(*args):
    result = runner.invoke(cli.app, list(args))
    assert result.exit_code == 0, result.output
    return result.stdout


def test_db_override_selects_the_file_for_one_invocation(env):
    default_db = env / "dev.db"
    (env / "config" / "dev.yaml").write_text(f"db_path: {default_db}\n")
    selected_db = env / "scratch" / "team notes?copy.db"
    options = ("--db", str(selected_db))

    config = json.loads(invoke(*options, "config", "show"))
    assert config["db_path"] == str(selected_db)
    assert config["vault_path"] == str(env / "vault")
    invoke(*options, "task", "add-to-queue", "key-dates-digest")
    [run] = json.loads(invoke(*options, "--json", "task", "run-history"))
    assert run["status"] == "queued"
    assert selected_db.is_file() and not default_db.exists()

    assert json.loads(invoke("--json", "task", "run-history")) == []
    assert default_db.is_file()


def test_doctor_exit_code_and_unused_runner_warnings(env, monkeypatch):
    from reginald.domain.models import DoctorCheck
    from reginald.runners.claude_agent import ClaudeAgentRunner
    from reginald.runners.codex_agent import CodexAgentRunner

    def unused_probe(*args):
        pytest.fail("An unused runner was probed")

    monkeypatch.setattr(ClaudeAgentRunner, "doctor_checks", unused_probe)
    monkeypatch.setattr(CodexAgentRunner, "doctor_checks", unused_probe)
    monkeypatch.setattr("reginald.runners.pydantic_agent.runner.check_sdk",
                        lambda name: DoctorCheck(name=name, ok=True))
    monkeypatch.setattr("reginald.services.doctor.shutil.which", lambda name: "/bin/" + name)
    (env / "config" / "dev.yaml").write_text(
        "site: {enabled: false}\n"
        "tasks:\n  key-dates-digest: {runner: pydantic_agent, model: 'openrouter:test'}\n"
    )
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=env / "vault", check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "-q", "--allow-empty", "-m", "initial"], cwd=env / "vault", check=True)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    missing = runner.invoke(cli.app, ["doctor"])
    assert missing.exit_code == 1 and "[FAIL]" in missing.stdout
    assert "OPENROUTER_API_KEY" in missing.stdout
    assert missing.stdout.startswith("Reginald doctor")
    assert "dev" in missing.stdout.splitlines()[0]

    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    coloured = runner.invoke(cli.app, ["doctor"], color=True)
    for label, code in (("OK", 32), ("WARN", 33), ("FAIL", 31)):
        assert re.search(rf"\x1b\[{code}m\[{label}\]", coloured.stdout)

    monkeypatch.setenv("OPENROUTER_API_KEY", "example-secret")
    assert json.loads(invoke("--json", "doctor"))["ok"]
    monkeypatch.setenv("NO_COLOR", "1")
    human = invoke("doctor")
    assert human.count("[WARN]") == 2 and "[FAIL]" not in human
    assert "\x1b" not in human
    assert "example-secret" not in human
    report = json.loads(invoke("--json", "doctor"))
    assert report["ok"]
    assert {c["name"] for c in report["checks"] if c["warning"]} == {
        "runner claude-agent", "runner codex_agent",
    }


@pytest.mark.parametrize(("args", "has_logo"), [
    ([], True), (["--help"], True), (["task", "--help"], False), (["task", "run", "--help"], False),
])
def test_terminal_help_has_colours_and_panels(args, has_logo, monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    result = subprocess.run(
        [sys.executable, "-m", "reginald.cli", *args],
        capture_output=True, text=True, check=True,
    )
    assert re.search(r"\x1b\[(?:\d+;)*3[0-7]m", result.stdout), "Missing ANSI foreground colour"
    assert "╭─" in result.stdout and "╰─" in result.stdout, "Missing help panels"
    cap = "\x1b[38;2;156;170;203m.-----------------------------------"
    tassel = "\x1b[38;2;255;107;1m||"
    assert (cap in result.stdout) == has_logo
    assert (tassel in result.stdout) == has_logo


def test_no_arguments_show_only_global_options(monkeypatch):
    from typer import Context
    from typer.main import get_command

    monkeypatch.setattr(cli, "get_context", lambda: pytest.fail("Help must work before configuration"))
    options = invoke()
    assert "╭─ Options" in options
    assert "╭─ Commands" not in options and "task run-history" not in options
    root = get_command(cli.app)
    for param in root.get_params(Context(root)):
        if not param.hidden:
            assert all(flag in options for flag in [*param.opts, *param.secondary_opts])


def test_help_documents_every_visible_command_and_parameter(monkeypatch):
    from typer.main import get_command

    monkeypatch.setattr(cli, "get_context", lambda: pytest.fail("Help must work before configuration"))
    root = get_command(cli.app)
    overview = invoke("--help")
    assert "╭─ Commands" in overview
    assert "╭─ Options" not in overview and "--vault" not in overview
    cap_brim = "==================================="
    assert cap_brim in overview
    assert "task run-history" in overview
    assert root.commands["serve"].hidden and "serve" not in overview.split()
    for name, command in root.commands.items():
        if command.hidden:
            continue
        assert command.help, name
        group_help = invoke(name, "--help")
        assert cap_brim not in group_help, name
        for verb, leaf in getattr(command, "commands", {}).items():
            if leaf.hidden:
                continue
            assert f"{name} {verb}" in overview
            assert verb in group_help
            assert leaf.help, (name, verb)
            assert all(param.help for param in leaf.params), (name, verb)
            leaf_help = invoke(name, verb, "--help")
            assert "Usage:" in leaf_help
            assert cap_brim not in leaf_help, (name, verb)


@pytest.mark.parametrize("service,verb", [("vault", "git"), ("publish", "ob")])
def test_vault_wrappers_forward_native_help_and_flags(monkeypatch, service, verb):
    from types import SimpleNamespace

    calls = []

    def forward(args):
        calls.append(args)
        return SimpleNamespace(stdout="native output", stderr="native error", returncode=7)

    ctx = SimpleNamespace(**{service: SimpleNamespace(**{verb: forward})})
    monkeypatch.setattr(cli, "get_context", lambda: ctx)
    for given, expected in [
        (["--", "--help"], ["--help"]),
        (["status", "--help"], ["status", "--help"]),
        (["--no-pager", "--help"], ["--no-pager", "--help"]),
        (["--version"], ["--version"]),
        (["-c", "key=value", "log", "--", "one file.md"], ["-c", "key=value", "log", "--", "one file.md"]),
    ]:
        result = runner.invoke(cli.app, ["vault", verb, *given])
        assert result.exit_code == 7
        assert calls[-1] == expected
        assert "native output" in result.output and "native error" in result.output


def test_vault_identify_changes_previews_and_advances_cursor(env):
    vault = env / "vault"
    (env / "config" / "dev.yaml").write_text("git:\n  push: false\nsite:\n  enabled: false\n")

    def git(*args):
        subprocess.run(["git", *args], cwd=vault, capture_output=True, check=True)

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Contributor")
    git("config", "user.email", "contributor@example.org")
    git("commit", "--allow-empty", "-qm", "Initial vault")

    def scan(*args):
        return json.loads(invoke("--json", "vault", "identify-changes", *args))

    assert scan("--dry-run")["initialized"]
    assert scan()["initialized"]
    brief = vault / "Topics" / "Payments and E-money" / "Briefs" / "Sources.md"
    brief.write_text("# Sources\n\n> [!todo] Check the source\n")
    git("add", str(brief))
    git("commit", "-qm", "Request a source check")

    preview = scan("--dry-run")
    assert preview["dry_run"] and preview["items_new"] == 1
    assert json.loads(invoke("--json", "task", "run-history")) == []
    recorded = scan()
    assert recorded["commits_scanned"] == recorded["items_new"] == 1
    assert [event["kind"] for event in recorded["events_recorded"]] == ["brief_created"]
    [event] = json.loads(invoke("--json", "vault", "list-events", "--topic", "Payments and E-money"))
    assert event["file_path"] == brief.relative_to(vault).as_posix()
    assert scan()["commits_scanned"] == 0
    [run] = json.loads(invoke("--json", "task", "run-history"))
    assert run["task"] == "identify-changes"


def test_topics_json_roundtrip(env):
    invoke("topics", "sync")
    payload = json.loads(invoke("--json", "topics", "list"))
    views = [TopicView.model_validate(item) for item in payload]
    assert any(v.name == "Payments and E-money" for v in views)


def test_subscriber_json_roundtrip(env):
    invoke("topics", "sync")
    invoke("subscriber", "add", "x@example.org", "--all-topics")
    payload = json.loads(invoke("--json", "subscriber", "list"))
    views = [SubscriberView.model_validate(item) for item in payload]
    assert views[0].mode == "all"
    assert "Payments and E-money" in views[0].effective_topics


def test_runs_json_roundtrip(env):
    invoke("task", "add-to-queue", "key-dates-digest")
    payload = json.loads(invoke("--json", "task", "run-history"))
    views = [RunRecord.model_validate(item) for item in payload]
    assert views[0].status == "queued"
    assert views[0].task == "key-dates-digest"
    assert json.loads(invoke("--json", "task", "inspect-run", str(views[0].id))) == payload[0]


def test_standalone_actions_and_delivery_outcomes_reach_logfire(env, telemetry, monkeypatch):
    from reginald.core import observability
    from reginald.db.schema import ContentEvent, Run, Topic
    from reginald.db.session import make_session_factory
    from reginald.email.base import SendResult
    from sqlalchemy import select

    # Use the real entry point, SQLite and local exporter. Keep every send offline.
    _, exporter, _ = telemetry
    monkeypatch.setattr(observability, "setup", lambda config: None)
    monkeypatch.setattr("reginald.email.console.ConsoleProvider.send",
                        lambda *args: SendResult(ok=False, error="sensitive provider response"))

    invoke("topics", "sync")
    invoke("subscriber", "add", "private@example.org", "--all-topics")
    with make_session_factory(env / "cli.db").begin() as session:
        topic = session.scalar(select(Topic).where(Topic.name == "Payments and E-money"))
        run = Run(task="identify-changes", status="success")
        session.add(run)
        session.flush()
        session.add(ContentEvent(run_id=run.id, topic_id=topic.id, kind="brief_created", file_path="private.md",
                                 commit_hash="abc"))
    invoke("outbox", "drain", "--dry-run")
    dev = env / "config" / "dev.yaml"
    dev.write_text("email: {allowlist: [only@example.org]}")
    invoke("outbox", "drain")
    dev.write_text("email: {provider: console}")
    invoke("outbox", "retry")
    monkeypatch.setattr("reginald.email.console.ConsoleProvider.send",
                        lambda *args: SendResult(ok=True, provider_message_id="offline"))
    invoke("outbox", "retry")
    invoke("outbox", "drain")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=env / "vault", check=True)
    failed = runner.invoke(cli.app, ["vault", "git", "rev-parse", "HEAD"])
    assert failed.exit_code != 0
    invoke("vault", "git", "status", "--short")
    invoke("db", "backup")
    records = [s for s in exporter.get_finished_spans() if s.attributes.get("logfire.span_type") == "span"]
    commands = [s for s in records if s.name == "cli.command"]
    assert [s.attributes["command"] for s in commands] == [
        "topics sync", "subscriber add", "outbox drain", "outbox drain",
        "outbox retry", "outbox retry", "outbox drain", "vault git", "vault git", "db backup",
    ]
    assert commands[-3].attributes["exit_code"] == failed.exit_code
    assert commands[-3].status.status_code.name == "ERROR"
    assert commands[-2].attributes["exit_code"] == 0
    assert commands[-2].status.status_code.name != "ERROR"
    [backup] = [s for s in records if s.name == "db.backup"]
    assert backup.parent.span_id == commands[-1].context.span_id
    attempts = [s for s in records if s.name == "outbox.send"]
    assert [s.attributes["outcome"] for s in attempts] == ["dry_run", "blocked", "failed", "sent"]
    assert [s.parent.span_id for s in attempts] == [s.context.span_id for s in commands[2:6]]
    assert [s.attributes["attempts"] for s in attempts] == [0, 1, 2, 3]
    assert attempts[2].status.status_code.name == "ERROR"
    assert len({s.attributes["delivery_id"] for s in attempts}) == 1
    assert all("private@example" not in str(s.attributes) for s in records)
    assert all("sensitive provider" not in str(s.attributes) for s in records)
