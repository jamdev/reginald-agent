"""Log level resolution and agent turn logging.

The runner used to log only at start and completion, so a working run and a
wedged one were indistinguishable from the journal for up to three hours.
"""

from __future__ import annotations

import logging

import pytest

from reginald.core.config import Config
from reginald.core.observability import LEVEL_ENV_VAR, resolve_level
from reginald.runners.claude_agent.runner import _tool_target, _truncate


def make_config(level: str = "INFO") -> Config:
    return Config(
        env="dev",
        vault_path="/tmp/vault",
        db_path="/tmp/test.db",
        logging={"level": level},
    )


# -- level resolution ------------------------------------------------------


def test_level_comes_from_config_by_default(monkeypatch):
    monkeypatch.delenv(LEVEL_ENV_VAR, raising=False)
    assert resolve_level(make_config("WARNING")) == logging.WARNING


def test_env_var_overrides_config(monkeypatch):
    """/etc/reginald/env is the only host-local override that survives a deploy."""
    monkeypatch.setenv(LEVEL_ENV_VAR, "DEBUG")
    assert resolve_level(make_config("INFO")) == logging.DEBUG


def test_env_var_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(LEVEL_ENV_VAR, " debug ")
    assert resolve_level(make_config("INFO")) == logging.DEBUG


def test_unknown_level_falls_back_to_info(monkeypatch):
    """A typo in a log setting must not take down an unattended run."""
    monkeypatch.setenv(LEVEL_ENV_VAR, "CHATTY")
    assert resolve_level(make_config("INFO")) == logging.INFO


def test_config_rejects_an_invalid_level():
    with pytest.raises(ValueError):
        make_config("CHATTY")


# -- log line helpers ------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_input", "expected"),
    [
        ({"file_path": "Topics/Payments/Briefs/x.md"}, "Topics/Payments/Briefs/x.md"),
        ({"command": "git status"}, "git status"),
        ({"query": "FCA consultation"}, "FCA consultation"),
        ({"url": "https://example.org"}, "https://example.org"),
        ({"unrecognised": "value"}, ""),
        ("not a dict", ""),
        ({}, ""),
    ],
)
def test_tool_target_summarises_the_call(tool_input, expected):
    assert _tool_target(tool_input) == expected


def test_tool_target_truncates_long_values():
    target = _tool_target({"command": "x" * 500})
    assert len(target) == 120
    assert target.endswith("...")


def test_truncate_collapses_whitespace():
    assert _truncate("a\n\n  b\tc") == "a b c"


def test_truncate_respects_the_limit():
    assert _truncate("y" * 100, limit=10) == "yyyyyyy..."
