"""`vault git` — an arbitrary git command against the vault, under the run lock.

Real repositories with a real (bare, local) remote: the interesting behaviour
is what git itself does (status, log, pull, push, non-zero exits), which a stub
would not reproduce.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from reginald.core.config import Config
from reginald.core.locking import run_lock
from reginald.services.vault import VaultService, VaultSyncError


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    # Repo-local identity: the tests must not depend on global git config.
    git(root, "config", "user.email", "test@example.org")
    git(root, "config", "user.name", "Test")


@pytest.fixture
def remote(tmp_path) -> Path:
    """A bare repo standing in for origin, with one commit on main."""
    seed = tmp_path / "seed"
    init(seed)
    (seed / "Home.md").write_text("---\ntitle: Home\n---\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "seed")

    bare = tmp_path / "origin.git"
    # -b main: without it the bare repo's HEAD follows init.defaultBranch,
    # and a clone of a repo whose HEAD names a branch that was never pushed
    # checks out nothing.
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    return bare


@pytest.fixture
def vault(tmp_path, remote) -> Path:
    clone = tmp_path / "vault"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    git(clone, "config", "user.email", "test@example.org")
    git(clone, "config", "user.name", "Test")
    return clone


@pytest.fixture
def service(tmp_path, vault) -> VaultService:
    return VaultService(
        Config(env="prod", vault_path=vault, db_path=tmp_path / "test.db")
    )


# -- passthrough behaviour ---------------------------------------------------


def test_status_returns_git_output_and_zero_exit(service):
    result = service.git(["status", "--porcelain"])
    assert result.returncode == 0
    assert result.stdout == ""  # clean clone


def test_a_read_command_reports_the_log(service, vault):
    (vault / "New.md").write_text("x\n")
    git(vault, "add", "-A")
    git(vault, "commit", "-qm", "local work")

    result = service.git(["log", "--oneline", "-1"])
    assert result.returncode == 0
    assert "local work" in result.stdout


def test_non_zero_exit_is_reported_verbatim_not_raised(service):
    # A bad ref: git exits non-zero; passthrough returns the code + stderr.
    result = service.git(["rev-parse", "--verify", "no-such-ref"])
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr  # git's own message


def test_pull_fast_forwards_from_the_remote(service, vault, remote, tmp_path):
    # land a commit on the remote from elsewhere
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    (other / "Upstream.md").write_text("from elsewhere\n")
    git(other, "add", "-A")
    git(other, "-c", "user.name=O", "-c", "user.email=o@e.org", "commit", "-qm", "upstream")
    git(other, "push", "-q", "origin", "main")

    result = service.git(["pull", "--ff-only", "origin", "main"])
    assert result.returncode == 0
    assert (vault / "Upstream.md").exists()


def test_push_sends_a_local_commit(service, vault, remote):
    (vault / "New.md").write_text("x\n")
    git(vault, "add", "-A")
    git(vault, "commit", "-qm", "local work")

    result = service.git(["push", "origin", "main"])
    assert result.returncode == 0
    assert git(remote, "rev-parse", "main") == git(vault, "rev-parse", "HEAD")


def test_commit_uses_reginalds_identity_by_default(service, vault):
    (vault / "New.md").write_text("x\n")
    service.git(["add", "-A"])
    result = service.git(["commit", "-qm", "via passthrough"])
    assert result.returncode == 0
    assert git(vault, "log", "-1", "--format=%an") == "Reginald"


def test_caller_can_commit_under_their_own_identity(service, vault):
    (vault / "New.md").write_text("x\n")
    service.git(["add", "-A"])
    service.git(["-c", "user.name=Chris", "-c", "user.email=chris@example.org",
                 "commit", "-qm", "as chris"])
    assert git(vault, "log", "-1", "--format=%an") == "Chris"


def test_a_non_repo_vault_is_reported(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    service = VaultService(Config(env="dev", vault_path=plain, db_path=tmp_path / "t.db"))
    with pytest.raises(VaultSyncError, match="not a git repository"):
        service.git(["status"])


# -- the run lock ------------------------------------------------------------


def test_git_will_not_interleave_with_a_run(service):
    """The whole point of routing git through reginald: it queues behind a run
    rather than mutating the working tree under a live agent."""
    started = Event()

    def status():
        started.set()
        return service.git(["status"])

    with ThreadPoolExecutor(max_workers=1) as pool:
        with run_lock(service._config.lock_path):
            future = pool.submit(status)
            assert started.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
        assert future.result(timeout=2).returncode == 0
