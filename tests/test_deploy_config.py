"""Exercise the deploy checkout with a local origin and stop before system changes."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

UPDATE_SCRIPT = Path(__file__).resolve().parents[1] / "deploy/update.sh"
CONFIGS = ("config/base.yaml", "config/dev.yaml", "config/prod.yaml")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "test@example.org")
    git(origin, "config", "user.name", "Test")
    (origin / "config").mkdir()
    for path in CONFIGS:
        (origin / path).write_text("old setting\n")
        (origin / (path + "-example")).write_text("old setting\n")
    (origin / "code").write_text("old code\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "Release with tracked settings")
    git(origin, "tag", "v1")

    host = tmp_path / "host"
    git(tmp_path, "clone", "-q", str(origin), str(host))
    (origin / ".gitignore").write_text("".join(f"/{path}\n" for path in CONFIGS))
    git(origin, "rm", "--cached", "--", *CONFIGS)
    (origin / "config/base.yaml-example").write_text("new default\n")
    (origin / "code").write_text("new code\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "Release with host-local settings")
    git(origin, "tag", "v2")
    (origin / "config/prod.yaml-example").write_text("new production default\n")
    git(origin, "commit", "-qam", "Update the production template on main")

    # No root, /opt, network, package installs or systemd in this test. Exit 42
    # at the first dependency command proves Git finished the checkout.
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, body in [("id", "echo 0"), ("uv", "exit 42")]:
        stub = tools / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    source = UPDATE_SCRIPT.read_text()
    assert 'APP_DIR="/opt/reginald"' in source
    script = source.replace('APP_DIR="/opt/reginald"', f"APP_DIR={shlex.quote(str(host))}")
    env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"}

    def update(*ref):
        return subprocess.run(
            ["bash", "-c", script, "update.sh", *ref],
            cwd=host, env=env, capture_output=True, text=True, timeout=15, check=False,
        )

    return host, update


def test_deploy_preserves_settings_across_releases(deployment):
    host, update = deployment
    (host / CONFIGS[0]).write_text("operator override\n")
    (host / CONFIGS[1]).chmod(0o600)
    for ref, expected, changed in [
        ((), "v2", ["config/base.yaml-example"]),
        (("main",), "origin/main", ["config/prod.yaml-example"]),
        (("main",), "origin/main", []),
        (("v1",), "v1", ["config/base.yaml-example", "config/prod.yaml-example"]),
        (("v2",), "v2", ["config/base.yaml-example"]),
    ]:
        result = update(*ref)
        assert result.returncode == 42, result.stderr
        assert git(host, "rev-parse", "HEAD") == git(host, "rev-parse", expected)
        assert [(host / path).read_text() for path in CONFIGS] == [
            "operator override\n", "old setting\n", "old setting\n",
        ]
        assert (host / CONFIGS[1]).stat().st_mode & 0o777 == 0o600
        assert "WARNING" in result.stderr and "docs/DEPLOY.md" in result.stderr
        assert "docs/USAGE.md" in result.stderr
        changed_lines = [line.strip() for line in result.stderr.splitlines() if "Changed template: " in line]
        assert changed_lines == [f"Changed template: {path}" for path in changed]
        if expected != "v1":
            assert not git(host, "ls-files", "--", *CONFIGS)
            assert set(git(host, "check-ignore", "--", *CONFIGS).splitlines()) == set(CONFIGS)


def test_deploy_keeps_unrelated_checkout_conflicts_and_staged_settings(deployment):
    host, update = deployment
    (host / CONFIGS[0]).write_text("staged setting\n")
    git(host, "add", CONFIGS[0])
    (host / CONFIGS[0]).write_text("working setting\n")
    (host / "code").write_text("operator code edit\n")
    initial_index = git(host, "write-tree")
    result = update("v2")
    assert result.returncode not in (0, 42)
    assert "would be overwritten by checkout" in result.stderr
    assert git(host, "write-tree") == initial_index
    assert git(host, "rev-parse", "HEAD") == git(host, "rev-parse", "v1")
    assert (host / CONFIGS[0]).read_text() == "working setting\n"
    assert (host / "code").read_text() == "operator code edit\n"
