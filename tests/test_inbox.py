"""Inbox filing uses the vault's files and the same reply convention as action-edits."""

import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from reginald.services.inbox import move_inbox_file


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    (root / "Inbox").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    for key, value in [("user.email", "test@example.org"), ("user.name", "Test")]:
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "initial", "--allow-empty"], check=True)
    return root


@pytest.mark.parametrize("destination", ["Topics/Payments/Briefs/review.md", "Templates/review.md",
                                         "Sources/Payments/review.md"])
def test_move_preserves_file_and_refuses_collision(vault, destination):
    source = vault / "Inbox/note.md"
    source.write_bytes(b"---\nuuid: keep-this\n---\nOriginal body\r\n")
    original = source.read_bytes()
    move_inbox_file(vault, "Inbox/note.md", destination)
    assert not source.exists()
    assert (vault / destination).read_bytes() == original
    source.write_text("another note")
    with pytest.raises(FileExistsError):
        move_inbox_file(vault, "Inbox/note.md", destination)
    assert source.read_text() == "another note"
    assert (vault / destination).read_bytes() == original


def test_protects_callout_only_note_until_all_threads_have_replies(vault):
    path = "Inbox/request.md"
    note = vault / path
    dest = "Templates/review.md"
    request = "---\nuuid: keep-me\n---\n> [!todo] Review this site\n> https://example.org"
    answered = request + "\n> > [!success] Reviewed it."
    followup = answered + "\n> > > [!question] What about the deadline?"
    note.write_text(followup)
    subprocess.run(["git", "-C", str(vault), "add", path], check=True)
    subprocess.run(["git", "-C", str(vault), "commit", "-qm", "Request"], check=True)
    for body in [followup, "# Draft\n" + followup]:
        note.write_text(body)
        with pytest.raises(ValueError, match="nested reply"):
            move_inbox_file(vault, path, dest)
        assert note.read_text() == body and not (vault / dest).exists()
    note.write_text(followup + "\n> > > > [!success] Confirmed.")
    subprocess.run(["git", "-C", str(vault), "commit", "-qam", "Reply"], check=True)
    move_inbox_file(vault, path, dest)
    assert (vault / dest).is_file()


def test_substantive_note_with_a_callout_can_be_filed(vault):
    (vault / "Inbox/memo.md").write_text("Source material\n\n> [!question] Check citation")
    move_inbox_file(vault, "Inbox/memo.md", "Sources/Law/memo.md")
    assert not (vault / "Inbox/memo.md").exists()


@pytest.mark.parametrize(("source", "destination"), [
    ("Elsewhere/file.md", "Templates/file.md"),
    ("Inbox/../Elsewhere/file.md", "Templates/file.md"),
    ("Inbox/file.md", "Topics/Tax/Materials/file.md"),
    ("Inbox/file.md", "Templates/../Setup/file.md"),
    ("Inbox/file.md", "Sources/.git/file.md"),
    ("Inbox/file.md", "/tmp/outside.md"),
    ("Inbox/file.md", "Templates/x\x00.md"),
    ("Inbox", "Templates/file.md"),
])
def test_rejects_out_of_scope_paths(vault, source, destination):
    (vault / "Inbox/file.md").write_text("source")
    with pytest.raises(ValueError):
        move_inbox_file(vault, source, destination)
    assert (vault / "Inbox/file.md").read_text() == "source"


def test_refuses_symlinks_at_either_end(vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("private")
    (vault / "Templates").symlink_to(outside)
    (vault / "Inbox/note.md").write_text("inside")
    with pytest.raises(OSError):
        move_inbox_file(vault, "Inbox/note.md", "Templates/escape.md")
    (vault / "Inbox/link.md").symlink_to(outside / "note.md")
    with pytest.raises(ValueError):
        move_inbox_file(vault, "Inbox/link.md", "Sources/Tax/link.md")
    assert not (outside / "escape.md").exists()
    assert (vault / "Inbox/note.md").exists()


def test_only_one_concurrent_move_gets_the_destination(vault):
    def move(name):
        try:
            move_inbox_file(vault, f"Inbox/{name}.pdf", "Sources/Tax/filing.pdf")
            return True
        except FileExistsError:
            return False

    for name in ("A", "B"):
        (vault / f"Inbox/{name}.pdf").write_bytes(name.encode())
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(move, ("A", "B"))) == [False, True]
    winner = (vault / "Sources/Tax/filing.pdf").read_bytes()
    [loser] = (vault / "Inbox").iterdir()
    assert {winner, loser.read_bytes()} == {b"A", b"B"}


def test_triage_run_files_through_stdio_and_keeps_request_queued(vault, tmp_path, monkeypatch):
    import asyncio
    import shutil
    from pathlib import Path

    mcp = pytest.importorskip("mcp")
    from mcp.client.stdio import stdio_client

    from reginald.core.context import AppContext
    from reginald.runners.base import RunnerResult

    shipped = Path(__file__).resolve().parents[1] / "config"
    cfg = tmp_path / "config"
    cfg.mkdir()
    for directory in ("tools.d", "tasks.d"):
        shutil.copytree(shipped / directory, cfg / directory)
    (cfg / "dev.yaml").write_text("git: {push: false}\nsite: {enabled: false}\n")
    monkeypatch.setenv("REGINALD_CONFIG_DIR", str(cfg))
    task_dir = vault / "Setup/Tasks"
    task_dir.mkdir(parents=True)
    (task_dir / "Triage inbox.md").write_text("File the source and leave pending requests in place.")
    ctx = AppContext(env="dev", vault_path=vault, db_path=tmp_path / "db")
    assert ctx.identify_changes.run().initialized
    (vault / "Inbox/request.md").write_text("> [!todo] @reginald Review this site")
    (vault / "Inbox/source.md").write_text("A primary source.")
    subprocess.run(["git", "-C", str(vault), "add", "."], check=True)
    subprocess.run(["git", "-C", str(vault), "commit", "-qm", "Inbox drop"], check=True)

    async def file_notes(tool):
        params = mcp.StdioServerParameters(command=tool.command, args=tool.args)
        async with stdio_client(params) as (reader, writer), mcp.ClientSession(reader, writer) as client:
            await client.initialize()
            assert [t.name for t in (await client.list_tools()).tools] == ["move_file"]
            held = await client.call_tool("move_file", {
                "source": "Inbox/request.md", "destination": "Templates/request.md",
            })
            assert held.isError and "nested reply" in held.content[0].text
            moved = await client.call_tool("move_file", {
                "source": "Inbox/source.md", "destination": "Sources/Payments/source.md",
            })
            assert not moved.isError, moved.content

    def run(_name, _task, _prompt, _vault_path, tools):
        asyncio.run(file_notes(tools["inbox"]))
        return RunnerResult(ok=True)

    monkeypatch.setattr(ctx.runs._runners["claude-agent"], "run", run)
    assert ctx.runs.execute("triage-inbox").status == "success"
    assert not (vault / "Inbox/source.md").exists()
    assert "A primary source." in (vault / "Sources/Payments/source.md").read_text()
    manifest, ids = ctx.identify_changes.manifest()
    assert len(ids) == 1 and "Inbox/request.md" in manifest
