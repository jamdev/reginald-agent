"""Logfire wiring: the GenAI agent span, and configure() plumbing.

logfire itself is faked — the suite is offline and must not reach the network
or require the observability extra.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

from reginald.core import observability
from reginald.core.observability import agent_span, setup
from reginald.services.doctor import DoctorService


# -- agent_span --------------------------------------------------------------


def test_agent_span_is_a_noop_yielding_none_when_logfire_is_off(monkeypatch):
    monkeypatch.setattr(observability, "_logfire", None)
    with agent_span(provider="anthropic", agent_name="Action edits", model="claude-sonnet-5") as sp:
        assert sp is None  # caller must guard for this


class _FakeSpan:
    def __init__(self):
        self.attrs: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_attribute(self, key, value):
        self.attrs[key] = value


def test_agent_span_sets_the_genai_attributes(monkeypatch):
    span = _FakeSpan()

    class FakeLogfire:
        def span(self, name, **kw):
            span.name = name
            return span

    monkeypatch.setattr(observability, "_logfire", FakeLogfire())
    with agent_span(provider="anthropic", agent_name="Research new developments", model="claude-opus-4-8") as sp:
        assert sp is span
    # The two the Agents page keys on, plus model/provider.
    assert span.attrs["gen_ai.operation.name"] == "invoke_agent"
    assert span.attrs["gen_ai.agent.name"] == "Research new developments"
    assert span.attrs["gen_ai.request.model"] == "claude-opus-4-8"
    assert span.name == "invoke_agent Research new developments"


# -- setup / configure -------------------------------------------------------


class _FakeHandler(logging.Handler):
    def emit(self, record):  # pragma: no cover - never fires in the test
        pass


class _FakeAdvanced:
    def __init__(self, base_url=None):
        self.base_url = base_url


class _FakeModule:
    def __init__(self):
        self.configure_kwargs = None
        self.DEFAULT_LOGFIRE_INSTANCE = SimpleNamespace(config=SimpleNamespace(_project_url=None))

    def configure(self, **kwargs):
        self.configure_kwargs = kwargs

    def AdvancedOptions(self, base_url=None):  # noqa: N802 - mirrors logfire API
        return _FakeAdvanced(base_url=base_url)

    def ConsoleOptions(self, show_project_link=True):  # noqa: N802 - mirrors logfire API
        return {"show_project_link": show_project_link}

    def LogfireLoggingHandler(self):  # noqa: N802 - mirrors logfire API
        return _FakeHandler()


def test_doctor_reports_logfire_setup_and_project_url_availability(monkeypatch, config_with_logfire):
    fake = _FakeModule()
    monkeypatch.setitem(sys.modules, "logfire", fake)
    monkeypatch.setattr(observability, "_logfire", None)
    monkeypatch.setattr(observability, "_logfire_handler", None)
    monkeypatch.setattr(observability.logger, "handlers", [])
    doctor = DoctorService(config_with_logfire)

    def missing_auth(**kwargs):
        raise RuntimeError("No user token was found matching the configured Logfire URL")

    setup(config_with_logfire)
    unresolved = next(c for c in doctor.check().checks if c.name == "logfire")
    assert unresolved.ok and unresolved.warning
    assert "project URL unavailable" in unresolved.detail

    project_url = f"{config_with_logfire.logfire.base_url}/example-org/example-project"
    fake.DEFAULT_LOGFIRE_INSTANCE.config._project_url = project_url
    configured = next(c for c in doctor.check().checks if c.name == "logfire")
    assert configured.ok and not configured.warning
    assert configured.detail == project_url

    monkeypatch.setattr(fake, "configure", missing_auth)
    setup(config_with_logfire)
    unavailable = next(c for c in doctor.check().checks if c.name == "logfire")
    assert unavailable.ok and unavailable.warning
    assert "LOGFIRE_TOKEN" in unavailable.detail


def test_setup_points_at_the_configured_base_url(monkeypatch, config_with_logfire):
    fake = _FakeModule()
    monkeypatch.setitem(sys.modules, "logfire", fake)
    monkeypatch.setattr(observability, "_logfire", None)
    try:
        setup(config_with_logfire)
        assert fake.configure_kwargs["service_name"] == "reginald"
        advanced = fake.configure_kwargs["advanced"]
        assert advanced.base_url == config_with_logfire.logfire.base_url
        assert fake.configure_kwargs["console"] == {"show_project_link": False}
        assert observability.capture_chars() == config_with_logfire.logfire.capture_chars
        assert observability._logfire is fake            # captured for span()/agent_span()
    finally:
        for h in list(logging.getLogger("reginald").handlers):
            if isinstance(h, _FakeHandler):
                logging.getLogger("reginald").removeHandler(h)


def test_setup_omits_advanced_when_no_base_url(monkeypatch, config):
    fake = _FakeModule()
    monkeypatch.setitem(sys.modules, "logfire", fake)
    monkeypatch.setattr(observability, "_logfire", None)
    config.logfire.enabled = True
    config.logfire.base_url = ""

    try:
        setup(config)
        assert "advanced" not in fake.configure_kwargs   # SDK default endpoint
    finally:
        for h in list(logging.getLogger("reginald").handlers):
            if isinstance(h, _FakeHandler):
                logging.getLogger("reginald").removeHandler(h)
