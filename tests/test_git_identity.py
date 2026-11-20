"""Commits must work with no git identity configured anywhere.

Regression guard for the first real prod run, which died with:

    GitError: git commit -m manual: pre-run checkpoint: Author identity unknown
    fatal: unable to auto-detect email address (got 'reginald@ubuntu.(none)')

The service user has no ~/.gitconfig. Identity is therefore passed to git per
invocation rather than read from the host, so it cannot break on a new host or
when the vault is re-cloned.

Note that git's own fallback is not portable and cannot be relied on: macOS
guesses a usable identity from the system, while the server derived
`reginald@ubuntu.(none)` and rejected it as invalid. Hence being explicit,
and hence asserting on the exact author and committer below rather than merely
that a commit succeeded — which would pass on a machine that happened to guess.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reginald.core.git import VaultGit


@pytest.fixture
def no_identity_env(monkeypatch, tmp_path):
    """Neutralise every source of git identity, as the reginald user has none."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent-home"))
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def bare_vault(tmp_path, no_identity_env) -> Path:
    """A git repo with no user.name/user.email set anywhere."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "seed.md").write_text("seed\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(vault)], check=True)
    return vault


def test_commit_all_succeeds_without_host_identity(bare_vault):
    sha = VaultGit(bare_vault).commit_all("manual: pre-run checkpoint")
    assert sha
    assert not VaultGit(bare_vault).is_dirty()


def test_configured_identity_is_used(bare_vault):
    VaultGit(bare_vault, author_name="Reginald", author_email="bot@example.org").commit_all("c")
    show = subprocess.run(
        ["git", "-C", str(bare_vault), "log", "-1", "--format=%an <%ae>|%cn <%ce>"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    author, committer = show.split("|")
    assert author == "Reginald <bot@example.org>"
    # Committer too: a mismatch here would still fail on hosts with no identity.
    assert committer == "Reginald <bot@example.org>"
