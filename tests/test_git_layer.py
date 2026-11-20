"""Multi-author git layer: commit walk, rename tracking, rebase sync.

Real repositories throughout — divergence, rename detection and rebase
conflicts are git behaviours a stub cannot reproduce.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reginald.core.git import EMPTY_TREE, RebaseConflictError, VaultGit


def sh(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit_as(root: Path, name: str, email: str, message: str) -> str:
    sh(root, "add", "-A")
    sh(root, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-qm", message)
    return sh(root, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    sh(seed, "init", "-q", "-b", "main")
    (seed / "Home.md").write_text("---\ntitle: Home\n---\nbody\n")
    commit_as(seed, "Founder", "founder@example.org", "seed")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    sh(seed, "remote", "add", "origin", str(bare))
    sh(seed, "push", "-q", "origin", "main")
    return bare


@pytest.fixture
def vault(tmp_path, remote) -> VaultGit:
    clone = tmp_path / "vault"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    return VaultGit(clone, author_name="Reginald", author_email="reginald@localhost")


def contributor_pushes(remote: Path, tmp_path: Path, filename: str, body: str,
                       name: str = "Chris", email: str = "chris@example.org") -> str:
    """Land a commit on the remote as a different git identity."""
    clone = tmp_path / f"contrib-{filename.replace('/', '_')}"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    path = clone / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    sha = commit_as(clone, name, email, f"edit {filename}")
    sh(clone, "push", "-q", "origin", "main")
    return sha


# -- commit walk -------------------------------------------------------------


def test_commits_between_reports_real_authors_oldest_first(vault):
    root = vault.vault_path
    base = vault.head()
    (root / "a.md").write_text("a\n")
    commit_as(root, "Alice", "alice@example.org", "alice adds a")
    (root / "b.md").write_text("b\n")
    commit_as(root, "Reginald", "reginald@localhost", "run: research-new-developments 2026-08-10")

    commits = vault.commits_between(base, vault.head())

    assert [c.author_name for c in commits] == ["Alice", "Reginald"]
    assert commits[0].author_email == "alice@example.org"
    assert commits[1].subject.startswith("run: research")


def test_root_commit_diffs_against_the_empty_tree(vault):
    root_sha = sh(vault.vault_path, "rev-list", "--max-parents=0", "HEAD")
    assert vault.parent_of(root_sha) == EMPTY_TREE
    entries = vault.diff_name_status(EMPTY_TREE, root_sha)
    assert [(e.status, e.path) for e in entries] == [("A", "Home.md")]


def test_renames_carry_their_old_path(vault):
    root = vault.vault_path
    pre = vault.head()
    sh(root, "mv", "Home.md", "Welcome.md")
    commit_as(root, "Alice", "alice@example.org", "rename")

    [entry] = vault.diff_name_status(pre, vault.head())
    assert entry.status == "R"
    assert entry.path == "Welcome.md"
    assert entry.old_path == "Home.md"


def test_first_added_date_survives_a_rename(vault):
    added = vault.first_added_date("Home.md")
    assert added is not None and added.startswith("20")


# -- rebase sync -------------------------------------------------------------


def test_sync_rebases_local_commit_onto_contributor_push(vault, remote, tmp_path):
    root = vault.vault_path
    (root / "reginald.md").write_text("reginald's work\n")
    local = commit_as(root, "Reginald", "reginald@localhost", "run: task 2026-08-10")
    upstream = contributor_pushes(remote, tmp_path, "chris.md", "chris's work\n")

    vault.sync_with_remote("origin", "main")

    # both survive; reginald's commit is rebased on top of the contributor's
    assert (root / "chris.md").exists() and (root / "reginald.md").exists()
    order = sh(root, "log", "--format=%s", "-3").splitlines()
    assert order[0] == "run: task 2026-08-10"
    assert vault.head() != local  # rebased => new sha
    assert sh(root, "rev-parse", "HEAD^") == upstream


def test_sync_conflict_aborts_loudly_and_restores_the_tree(vault, remote, tmp_path):
    root = vault.vault_path
    (root / "Home.md").write_text("---\ntitle: Home\n---\nreginald's version\n")
    local = commit_as(root, "Reginald", "reginald@localhost", "run: task 2026-08-10")
    contributor_pushes(remote, tmp_path, "Home.md", "---\ntitle: Home\n---\nchris's version\n")

    with pytest.raises(RebaseConflictError):
        vault.sync_with_remote("origin", "main")

    # aborted: no rebase in progress, local commit intact, tree clean
    assert not (Path(root) / ".git" / "rebase-merge").exists()
    assert vault.head() == local
    assert not vault.is_dirty()


def test_push_with_rebase_wins_the_contributor_race(vault, remote, tmp_path):
    root = vault.vault_path
    (root / "reginald.md").write_text("reginald's work\n")
    commit_as(root, "Reginald", "reginald@localhost", "run: task 2026-08-10")
    # contributor lands first — a plain push would be rejected (non-ff)
    contributor_pushes(remote, tmp_path, "chris.md", "chris's work\n")

    vault.push_with_rebase("origin", "main")

    remote_subjects = sh(remote, "log", "--format=%s", "main").splitlines()
    assert remote_subjects[0] == "run: task 2026-08-10"
    assert "edit chris.md" in remote_subjects


def test_sync_against_an_unborn_remote_branch_is_a_noop(tmp_path):
    bare = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True)
    (work / "a.md").write_text("a\n")
    commit_as(work, "Reginald", "reginald@localhost", "first")
    git = VaultGit(work)
    git.sync_with_remote("origin", "main")  # must not raise
    assert git.head()
