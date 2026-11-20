"""Drive the release script with a local Git remote and a recording GitHub CLI."""

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/release.sh"
REPO = "https://github.com/example/reginald"
TAG = "v1.0.2"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", repo, *args], text=True).strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    origin = tmp_path / "origin.git"
    repo = tmp_path / "checkout"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", origin)
    git(tmp_path, "clone", "-q", origin, repo)
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.org")
    (repo / "deploy").mkdir()
    shutil.copy2(SCRIPT, repo / "deploy/release.sh")
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.1"\n')
    (repo / "uv.lock").touch()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    git(repo, "push", "-q", "origin", "main")

    tools = tmp_path / "bin"
    tools.mkdir()
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    for name in ("make", "uv"):
        stub = tools / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    gh = tools / "gh"
    gh.write_text(f"""#!/usr/bin/env python3
import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ['repo', 'view']:
    assert args[2] == {str(origin)!r}
    if os.environ.get('GH_REPO_UNAVAILABLE'):
        sys.exit(1)
    print({REPO!r})
elif args[:2] == ['release', 'create']:
    # A resumed release can be an ancestor of main. It must already be on origin.
    ref = 'refs/tags/' + args[2]
    assert subprocess.check_output(['git', '-C', {str(origin)!r}, 'rev-parse', ref]) == subprocess.check_output(['git', 'rev-parse', ref])
    subprocess.run(['git', '-C', {str(origin)!r}, 'merge-base', '--is-ancestor', ref, 'main'], check=True)
    Path({str(tmp_path / "publication")!r}).write_text(json.dumps(args))
    sys.exit(int(os.environ.get('GH_CREATE_STATUS', '0')))
else:
    sys.exit(1)
""")
    gh.chmod(0o755)
    return repo


def release(repo, *flags, reply=""):
    return subprocess.run(
        ["bash", "deploy/release.sh", TAG, *flags], cwd=repo, input=reply,
        capture_output=True, check=False, text=True, timeout=15,
    )


def test_publishes_prerelease_with_empty_notes(checkout):
    result = release(checkout, "-y")
    assert result.returncode == 0, result.stderr
    args = json.loads((checkout.parent / "publication").read_text())
    assert args[:3] == ["release", "create", TAG]
    assert "--prerelease" in args and "--verify-tag" in args
    assert args[args.index("--repo") + 1] == REPO
    assert args[args.index("--title") + 1] == TAG
    assert args[args.index("--notes") + 1] == ""
    assert "--draft" not in args


@pytest.mark.parametrize(("flags", "reply"), [(["-n"], ""), ([], "n\n")])
def test_preview_and_declined_push_do_not_publish(checkout, flags, reply):
    before = git(checkout, "rev-parse", "origin/main")
    result = release(checkout, *flags, reply=reply)
    assert result.returncode == 0, result.stderr
    assert not (checkout.parent / "publication").exists()
    assert git(checkout.parent / "origin.git", "rev-parse", "main") == before
    assert git(checkout.parent / "origin.git", "tag", "--list") == ""
    if flags:
        assert git(checkout, "rev-parse", "HEAD") == before
        assert "pre-release" in result.stdout


def test_publication_failure_prints_a_retry_for_the_pushed_tag(checkout, monkeypatch):
    monkeypatch.setenv("GH_CREATE_STATUS", "1")
    result = release(checkout, "-y")
    assert result.returncode != 0
    retry = shlex.split(result.stderr.splitlines()[-1])
    assert retry[:4] == ["gh", "release", "create", TAG]
    attempted = json.loads((checkout.parent / "publication").read_text())
    assert retry[1:] == attempted
    monkeypatch.delenv("GH_CREATE_STATUS")
    subprocess.run(retry, cwd=checkout, check=True, timeout=15)
    assert git(checkout.parent / "origin.git", "tag", "--list") == TAG


def test_inaccessible_github_repo_stops_before_bump(checkout, monkeypatch):
    monkeypatch.setenv("GH_REPO_UNAVAILABLE", "1")
    result = release(checkout, "-y")
    assert result.returncode != 0
    assert "gh could not access origin" in result.stderr
    assert git(checkout, "status", "--porcelain") == ""
    assert git(checkout, "rev-parse", "HEAD") == git(checkout, "rev-parse", "origin/main")
    assert git(checkout, "tag", "--list") == ""


@pytest.fixture
def same_version(checkout):
    (checkout / "pyproject.toml").write_text('[project]\nversion = "1.0.2"\n')
    git(checkout, "commit", "-qam", "Set project version")
    return checkout


def test_force_tags_the_current_version_without_an_empty_commit(same_version):
    repo = same_version
    before = git(repo, "rev-parse", "HEAD")
    assert release(repo, "-y").returncode != 0
    preview = release(repo, "--force", "-n")
    assert preview.returncode == 0, preview.stderr
    assert git(repo, "tag", "--list") == ""
    assert not (repo.parent / "publication").exists()
    result = release(repo, "--force", "-y")
    assert result.returncode == 0, result.stderr
    assert git(repo, "rev-parse", "HEAD") == before
    assert git(repo.parent / "origin.git", "rev-parse", f"{TAG}^{{commit}}") == before
    assert "--prerelease" in json.loads((repo.parent / "publication").read_text())


@pytest.mark.parametrize("already_on_main", [False, True])
def test_force_reuses_the_original_local_tag(same_version, already_on_main):
    repo = same_version
    git(repo, "tag", "-a", TAG, "-m", "Original tag")
    tag_object = git(repo, "rev-parse", TAG)
    tagged_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "-q", "--allow-empty", "-m", "Follow-up on main")
    expected_main = tagged_commit
    if already_on_main:
        git(repo, "push", "origin", "main")
        expected_main = git(repo, "rev-parse", "HEAD")
    result = release(repo, "-f", "-y")
    assert result.returncode == 0, result.stderr
    origin = repo.parent / "origin.git"
    assert git(origin, "rev-parse", TAG) == tag_object
    assert git(origin, "rev-parse", "main") == expected_main
    assert "--prerelease" in json.loads((repo.parent / "publication").read_text())


def test_force_refuses_a_pushed_tag_even_without_a_local_copy(same_version):
    repo = same_version
    git(repo, "tag", TAG, "HEAD^")
    git(repo, "push", "origin", TAG)
    git(repo, "tag", "-d", TAG)
    result = release(repo, "--force", "-y")
    assert result.returncode != 0
    assert "already exists on origin" in result.stderr
    assert git(repo.parent / "origin.git", "rev-parse", TAG) == git(repo, "rev-parse", "HEAD^")
    assert not (repo.parent / "publication").exists()


def test_force_refuses_a_local_tag_with_the_wrong_version(same_version):
    repo = same_version
    git(repo, "tag", TAG, "HEAD^")
    result = release(repo, "--force", "-y")
    assert result.returncode != 0
    assert "version" in result.stderr
    assert git(repo.parent / "origin.git", "tag", "--list") == ""


@pytest.mark.parametrize(("tool", "args"), [("uv", "lock --check"), ("make", "test")])
def test_force_checks_the_tagged_code_before_pushing(same_version, tool, args):
    repo = same_version
    tagged_commit = git(repo, "rev-parse", "HEAD")
    git(repo, "tag", TAG)
    git(repo, "commit", "-q", "--allow-empty", "-m", "Later change")
    worktrees = git(repo, "worktree", "list", "--porcelain")
    stub = repo.parent / "bin" / tool
    stub.write_text(f'#!/bin/sh\nif [ "$*" = "{args}" ]; then git rev-parse HEAD >&2; exit 1; fi\n')
    result = release(repo, "--force", "-y")
    assert result.returncode != 0
    assert tagged_commit in result.stderr
    assert git(repo, "worktree", "list", "--porcelain") == worktrees
    assert git(repo.parent / "origin.git", "tag", "--list") == ""
    assert not (repo.parent / "publication").exists()
