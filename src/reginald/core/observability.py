"""Optional Logfire/OTel integration. Everything degrades to a no-op when
logfire is not installed or not enabled — observability must never block work."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from functools import lru_cache

from ..domain.models import DoctorCheck, RunRecord
from ..runners.base import RunnerResult
from .config import Config

logger = logging.getLogger("reginald")

LEVEL_ENV_VAR = "REGINALD_LOG_LEVEL"

_logfire = None
_logfire_handler: logging.Handler | None = None


def logfire_project_url() -> str | None:
    """The SDK can populate the project URL asynchronously after setup."""
    if _logfire is None:
        return None
    config = getattr(_logfire.DEFAULT_LOGFIRE_INSTANCE, "config", None)
    return getattr(config, "_project_url", None)


def check_logfire(environment: str) -> DoctorCheck:
    if _logfire is None:
        return DoctorCheck(
            name="logfire", ok=True, warning=True,
            detail="Tracing unavailable. Check the startup warning and LOGFIRE_TOKEN for the configured region.",
        )
    project_url = logfire_project_url()
    return DoctorCheck(
        name="logfire", ok=True, warning=not project_url,
        detail=project_url or f"configured ({environment}); project URL unavailable",
    )


def resolve_level(config: Config) -> int:
    """Use $REGINALD_LOG_LEVEL or the configured level. Unknown values fall back to INFO."""
    name = os.environ.get(LEVEL_ENV_VAR) or config.logging.level
    level = logging.getLevelName(str(name).strip().upper())
    if not isinstance(level, int):
        logging.getLogger("reginald").warning(
            "unknown log level %r (from %s or config); using INFO", name, LEVEL_ENV_VAR
        )
        return logging.INFO
    return level


def setup(config: Config) -> None:
    global _logfire, _logfire_handler  # noqa: PLW0603 — process-wide SDK and its log handler
    level = resolve_level(config)
    # force=True: something imported earlier (logfire's plugin, for one) may have
    # already configured the root logger, which would make basicConfig a no-op.
    logging.basicConfig(
        level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s", force=True
    )
    logger.setLevel(level)
    _logfire = None
    if _logfire_handler is not None:
        logger.removeHandler(_logfire_handler)
        _logfire_handler.close()
        _logfire_handler = None
    if not config.logfire.enabled:
        return
    try:
        import logfire  # type: ignore

        configure_kwargs: dict = {
            "environment": config.logfire.environment,
            "service_name": "reginald",
            # Don't print "Logfire project URL: …" on every invocation; the URL
            # is surfaced by `reginald doctor` instead. This suppresses only the
            # banner — telemetry still flows to Logfire.
            "console": logfire.ConsoleOptions(show_project_link=False),
        }
        # Point at a non-default instance (EU region / self-hosted) when set.
        # The token in $LOGFIRE_TOKEN must belong to this same instance.
        if config.logfire.base_url:
            configure_kwargs["advanced"] = logfire.AdvancedOptions(
                base_url=config.logfire.base_url
            )
        logfire.configure(**configure_kwargs)

        # Mirror the full reginald log (the same lines the CLI/journal shows) to
        # Logfire. Records emitted while an agent span is current nest under it,
        # so the per-turn agent log shows as that agent's log on the Agents page.
        handler = logfire.LogfireLoggingHandler()
        handler.setLevel(level)
        logger.addHandler(handler)
        _logfire_handler = handler

        # The `pydantic_agent` runner uses Pydantic AI; instrumenting it makes its
        # agent runs emit GenAI spans automatically. Claude and Codex use agent_span().
        try:
            logfire.instrument_pydantic_ai()
        except Exception as exc:  # noqa: BLE001 — pydantic-ai may be absent
            logger.debug("pydantic-ai instrumentation skipped: %s", exc)

        _logfire = logfire
    except ImportError:  # pragma: no cover
        logger.warning("logfire enabled in config but not installed; continuing without it")
    except Exception as exc:  # noqa: BLE001 — e.g. no LOGFIRE_TOKEN set
        logger.warning("logfire enabled but not configured (%s); continuing without it", exc)


@contextmanager
def span(name: str, *, log_when_disabled: bool = True, **attributes):
    if _logfire is not None:
        with _logfire.span(name, **attributes) as current:
            yield current
    else:
        if log_when_disabled:
            logger.info("%s %s", name, attributes or "")
        yield None


@contextmanager
def agent_span(agent_name: str, model: str, *, provider: str, **extra):
    """A GenAI `invoke_agent` span so the run shows on Logfire's Agents page.

    Follows the OpenTelemetry GenAI agent semantic conventions: span name
    `invoke_agent <agent>`, `gen_ai.operation.name = "invoke_agent"`, and
    `gen_ai.agent.name` (which becomes the agent's name on the page). Yields
    the span so the caller can attach usage (turns, cost) at the end, or None
    when logfire is off — the caller must guard for that.

    Claude and Codex use this span. Pydantic AI emits its own through
    instrument_pydantic_ai().
    """
    if _logfire is None:
        yield None
        return
    # Pre-format the name (no {} placeholders) so logfire doesn't try to
    # introspect f-string arguments.
    with _logfire.span(f"invoke_agent {agent_name}") as sp:
        sp.set_attribute("gen_ai.operation.name", "invoke_agent")
        sp.set_attribute("gen_ai.agent.name", agent_name)
        sp.set_attribute("gen_ai.request.model", model)
        sp.set_attribute("gen_ai.system", provider)
        sp.set_attribute("gen_ai.provider.name", provider)
        for key, value in extra.items():
            sp.set_attribute(key, value)
        yield sp


def record_run_completion(run: RunRecord) -> None:
    """Emit the committed task outcome independently of the journal log level."""
    if _logfire is None:
        return
    try:
        _logfire.info("run.completed", task=run.task, run_id=run.id, status=run.status)
    except Exception as exc:  # noqa: BLE001 — telemetry must not fail a completed run
        logger.warning("could not report completion of run %s to Logfire: %s", run.id, exc)


def set_run_usage(current, result: RunnerResult) -> None:
    """Put run totals on a span, keeping model-call attributes exclusive to calls."""
    if current is None:
        return
    if result.num_turns is not None:
        current.set_attribute("num_turns", result.num_turns)
        current.set_attribute("gen_ai.usage.num_turns", result.num_turns)
    if result.tool_calling_turns is not None:
        current.set_attribute("tool_calling_turns", result.tool_calling_turns)
    if result.cost_usd is not None:
        current.set_attribute("gen_ai.usage.cost_usd", result.cost_usd)
    for key, value in result.token_usage.items():
        current.set_attribute(f"gen_ai.aggregated_usage.{key}", value)
    current.set_attribute("error", not result.ok)
    if not result.ok:
        if result.error:
            current.set_attribute("error.message", result.error)
        current.set_level("error")


@lru_cache(maxsize=16)
def _histogram(logfire, name: str, unit: str):
    return logfire.metric_histogram(name, unit=unit)


def record_histogram(name: str, unit: str, value: float | None, labels: dict[str, str]) -> None:
    if _logfire is None or value is None:
        return
    try:
        _histogram(_logfire, name, unit).record(value, labels)
    except Exception as exc:  # noqa: BLE001 — a telemetry failure must not fail a paid run
        logger.warning("could not record Logfire metric %s: %s", name, exc)


def record_run_usage(current, result: RunnerResult, labels: dict[str, str]) -> None:
    """One measurement per agent invocation and reported quantity, including zero."""
    if current is None:
        return
    set_run_usage(current, result)
    record_histogram("reginald.agent.turns", "{turn}", result.num_turns, labels)
    record_histogram("reginald.agent.tool_calling_turns", "{turn}", result.tool_calling_turns, labels)
    record_histogram("reginald.agent.cost", "{USD}", result.cost_usd, labels)
    for key, count in result.token_usage.items():
        record_histogram("reginald.agent.tokens", "{token}", count, {**labels, "token_type": key})
