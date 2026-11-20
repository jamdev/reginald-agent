"""`doctor` must say which release it is reporting on.

The installed version and the checkout are separate facts — a host whose
checkout moved without a re-sync runs the new code while its metadata still
claims the old number — so the detail line has to carry both.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reginald.core.release import checkout_tag, release_detail


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A checkout with one commit tagged v1.2.3. Identity is set repo-locally
    so the test does not depend on the developer's global git config."""
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.org")
    git(root, "config", "user.name", "Test")
    (root / "file.txt").write_text("one\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    git(root, "tag", "v1.2.3")
    return root


def test_reports_the_tag_when_head_is_on_it(repo):
    assert checkout_tag(repo) == "v1.2.3"
    assert release_detail("1.2.3", repo) == "1.2.3 (tag v1.2.3)"


def test_a_dirty_tree_is_noted_but_is_not_drift(repo):
    (repo / "file.txt").write_text("edited\n")
    assert release_detail("1.2.3", repo) == "1.2.3 (tag v1.2.3, working tree dirty)"


def test_commits_past_the_tag_read_as_ahead(repo):
    (repo / "file.txt").write_text("two\n")
    git(repo, "commit", "-qam", "second")
    detail = release_detail("1.2.3", repo)
    assert "ahead of its tag" in detail
    assert detail.startswith("1.2.3 (checkout v1.2.3-1-g")


def test_a_checkout_on_the_wrong_tag_is_called_out(repo):
    """The deploy trap: HEAD moved, the metadata did not."""
    detail = release_detail("1.3.0", repo)
    assert detail == "1.3.0 (checkout v1.2.3 — expected tag v1.3.0)"


def test_an_untagged_checkout_falls_back_to_the_sha(repo):
    git(repo, "tag", "-d", "v1.2.3")
    detail = release_detail("1.2.3", repo)
    assert "expected tag v1.2.3" in detail
    assert git(repo, "rev-parse", "--short", "HEAD") in detail


def test_no_repo_at_all_still_reports_the_version(tmp_path):
    """Installed as a plain wheel there is no checkout to describe."""
    assert checkout_tag(tmp_path) is None
    assert release_detail("1.2.3", tmp_path) == "1.2.3 (no checkout — installed from a package)"
