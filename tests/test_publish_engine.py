"""PublishService: default-engine dispatch and the `ob` passthrough.

A fake `ob` executable (a shell script) stands in for obsidian-headless, so the
subprocess plumbing — argv, cwd, exit code, stdout/stderr — is exercised for
real rather than mocked.
"""

from __future__ import annotations

import os
import stat

from reginald.core.config import Config
from reginald.services.publish import PublishService


def _fake_ob(tmp_path, script: str) -> str:
    """Write an executable fake `ob` and return its path."""
    p = tmp_path / "ob"
    p.write_text("#!/usr/bin/env bash\n" + script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _config(tmp_path, **site) -> Config:
    vault = tmp_path / "vault"
    vault.mkdir()
    return Config(env="dev", vault_path=vault, db_path=tmp_path / "t.db", site=site)


# -- default-engine publish() ------------------------------------------------


def test_disabled_site_is_a_noop(tmp_path):
    svc = PublishService(_config(tmp_path, enabled=False))
    result = svc.publish()
    assert result.ok is True
    assert "disabled" in result.detail


def test_default_publish_invokes_ob_publish_with_the_vault_path(tmp_path):
    ob = _fake_ob(tmp_path, 'echo "args: $*"\n')
    svc = PublishService(_config(tmp_path, enabled=True, ob_binary=ob))
    result = svc.publish()
    assert result.ok is True
    assert "publish --path" in result.detail
    assert "--yes" in result.detail


def test_default_publish_reports_ob_failure(tmp_path):
    ob = _fake_ob(tmp_path, 'echo "boom" >&2\nexit 3\n')
    svc = PublishService(_config(tmp_path, enabled=True, ob_binary=ob))
    result = svc.publish()
    assert result.ok is False
    assert "boom" in result.detail


def test_dry_run_passes_the_flag_through(tmp_path):
    ob = _fake_ob(tmp_path, 'echo "args: $*"\n')
    svc = PublishService(_config(tmp_path, enabled=True, ob_binary=ob))
    result = svc.publish(dry_run=True)
    assert "--dry-run" in result.detail


def test_missing_ob_binary_is_reported(tmp_path):
    svc = PublishService(_config(tmp_path, enabled=True, ob_binary="definitely-not-a-real-binary"))
    result = svc.publish()
    assert result.ok is False
    assert "not found on PATH" in result.detail


def test_unknown_engine_is_reported(tmp_path):
    svc = PublishService(_config(tmp_path, enabled=True))
    svc._config.site.engine = "quartz"  # a future engine, not yet implemented
    result = svc.publish()
    assert result.ok is False
    assert "unknown publishing engine 'quartz'" in result.detail


# -- ob passthrough ----------------------------------------------------------


def test_ob_passthrough_returns_verbatim_output_and_exit(tmp_path):
    ob = _fake_ob(tmp_path, 'echo "out"\necho "err" >&2\nexit 0\n')
    svc = PublishService(_config(tmp_path, ob_binary=ob))
    result = svc.ob(["--version"])
    assert result.returncode == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_ob_passthrough_propagates_a_non_zero_exit(tmp_path):
    ob = _fake_ob(tmp_path, 'exit 5\n')
    svc = PublishService(_config(tmp_path, ob_binary=ob))
    assert svc.ob(["login"]).returncode == 5


def test_ob_passthrough_runs_in_the_vault_cwd(tmp_path):
    ob = _fake_ob(tmp_path, 'pwd\n')
    cfg = _config(tmp_path, ob_binary=ob)
    result = PublishService(cfg).ob(["whereami"])
    assert os.path.realpath(result.stdout.strip()) == os.path.realpath(str(cfg.vault_path))


def test_ob_passthrough_forwards_all_args(tmp_path):
    ob = _fake_ob(tmp_path, 'printf "%s\\n" "$@"\n')
    svc = PublishService(_config(tmp_path, ob_binary=ob))
    result = svc.ob(["publish-setup", "--site", "abc", "--path", "/x"])
    assert result.stdout.splitlines() == ["publish-setup", "--site", "abc", "--path", "/x"]


def test_ob_passthrough_missing_binary_returns_127(tmp_path):
    svc = PublishService(_config(tmp_path, ob_binary="no-such-ob-binary"))
    result = svc.ob(["login"])
    assert result.returncode == 127
    assert "not found" in result.stderr


def test_ob_passthrough_is_not_gated_on_site_enabled(tmp_path):
    # Explicit operator command runs even when auto-publish is off (dev overlay).
    ob = _fake_ob(tmp_path, 'echo ran\n')
    svc = PublishService(_config(tmp_path, enabled=False, ob_binary=ob))
    result = svc.ob(["--version"])
    assert result.returncode == 0
    assert result.stdout.strip() == "ran"
