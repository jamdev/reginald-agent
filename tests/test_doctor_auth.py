"""Doctor checks credentials against the runner and model selected by each task."""

from __future__ import annotations

import pytest

from reginald.core.config import TaskConfig
from reginald.services.doctor import DoctorService


def _auth_check(config):
    config.tasks["demo"] = TaskConfig(title="Demo", runner="claude-agent", model="test")
    report = DoctorService(config).check()
    return next(c for c in report.checks if c.name == "Claude auth")


def test_green_with_the_api_key(config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    check = _auth_check(config)
    assert check.ok is True
    assert check.detail == "ANTHROPIC_API_KEY set"


def test_green_with_the_oauth_token(config, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-y")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    check = _auth_check(config)
    assert check.ok is True
    assert check.detail == "CLAUDE_CODE_OAUTH_TOKEN set"


def test_both_present_reports_the_api_key_as_taking_precedence(config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-y")
    check = _auth_check(config)
    assert check.ok is True
    assert "takes precedence" in check.detail
    assert "ANTHROPIC_API_KEY" in check.detail


def test_red_only_when_neither_is_present(config, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    check = _auth_check(config)
    assert check.ok is False
    assert "neither" in check.detail


def test_pydantic_checks_each_configured_provider_once(config, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "example-anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "example-codex-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    for name, model in [
        ("research", "openrouter:anthropic/claude-sonnet"),
        ("triage", "openrouter:openai/gpt-5"),
        ("brief", "anthropic:claude-sonnet"),
    ]:
        config.tasks[name] = TaskConfig(title=name, runner="pydantic_agent", model=model)

    report = DoctorService(config).check()
    [router] = [c for c in report.checks if c.name == "Pydantic auth (openrouter)"]
    assert not router.ok and "OPENROUTER_API_KEY" in router.detail
    assert "research" in router.detail and "triage" in router.detail
    assert next(c for c in report.checks if c.name == "Pydantic auth (anthropic)").ok
    assert "example-anthropic-secret" not in report.model_dump_json()
    assert "example-codex-secret" not in report.model_dump_json()


@pytest.mark.parametrize(("model", "provider", "warning"), [
    ("test", "test", False),
    ("ollama:llama3", "ollama", False),
    ("bedrock:anthropic.claude", "bedrock", True),
])
def test_pydantic_models_without_a_required_api_key(config, monkeypatch, model, provider, warning):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    config.tasks["demo"] = TaskConfig(title="Demo", runner="pydantic_agent", model=model)
    checks = DoctorService(config).check().checks
    auth = next(c for c in checks if c.name == f"Pydantic auth ({provider})")
    assert auth.ok and auth.warning == warning
