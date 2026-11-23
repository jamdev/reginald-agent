"""Exercise the run path with local SDK responses and real, in-memory Logfire exporters."""

from __future__ import annotations

import json
import subprocess
from decimal import Decimal

import pytest

from reginald.core import observability
from reginald.core.config import TaskConfig
from reginald.core.context import build_runners
from reginald.services.events import EventService
from reginald.services.identify_changes import IdentifyChangesService
from reginald.services.publish import PublishService
from reginald.services.runs import RunService


@pytest.fixture
def runs(config, sessions, topics):
    vault = config.vault_path
    prompt = vault / "Setup/Tasks/demo.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Offline test")
    for args in (("init", "-q", "-b", "main"), ("config", "user.name", "Test"),
                 ("config", "user.email", "test@example.org"), ("add", "."),
                 ("commit", "-qm", "test vault")):
        subprocess.run(["git", *args], cwd=vault, check=True)
    config.tasks["demo"] = TaskConfig(title="Demo", runner="claude-agent", model="claude-sonnet-5")
    config.git.push = False
    config.site.enabled = False
    config.logging.agent_turns = False
    changes = IdentifyChangesService(sessions, config, EventService(sessions, topics), PublishService(config))
    return RunService(sessions, config, build_runners(config), topics, changes)


def completed_spans(exporter, name):
    return [s for s in exporter.get_finished_spans()
            if s.name == name and s.attributes.get("logfire.span_type") != "pending_span"]


def test_codex_exports_overlapping_tool_calls_as_siblings(telemetry):
    from reginald.runners.tool_spans import ToolSpans

    _, spans, _ = telemetry
    with observability.agent_span("Demo", "gpt-5.4", provider="openai"):
        calls = ToolSpans()
        calls.start("1", "commandExecution", turn=1)
        calls.start("2", "mcp__search__lookup", turn=1)
        calls.finish("1", "completed")
        calls.close()
    [agent] = completed_spans(spans, "invoke_agent Demo")
    assert agent.attributes["gen_ai.provider.name"] == "openai"
    for name, call_id, status in [("commandExecution", "1", "completed"), ("mcp__search__lookup", "2", "interrupted")]:
        [call] = completed_spans(spans, f"execute_tool {name}")
        assert call.parent.span_id == agent.context.span_id
        assert call.attributes["gen_ai.operation.name"] == "execute_tool"
        assert call.attributes["gen_ai.tool.call.id"] == call_id
        assert call.attributes["tool.status"] == status


def test_claude_exports_model_calls_and_one_usage_measurement_per_run(
    runs, telemetry, monkeypatch, config,
):
    monkeypatch.setattr(observability, "_capture_chars", False)
    sdk = pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk.types import StreamEvent

    _, spans, metrics = telemetry
    model = config.tasks["demo"].model
    usage = {"input_tokens": 5, "cache_read_input_tokens": 20, "cache_creation_input_tokens": 10}

    async def query(prompt, options):
        assert options.tools == []
        assert options.allowed_tools == []
        assert options.mcp_servers == {}
        yield sdk.SystemMessage("init", {"tools": ["Read", "Grep", "Write"]})
        for i in (1, 2):
            reply = {"id": f"msg-{i}", "model": model, "usage": {**usage, "output_tokens": 1}}
            yield StreamEvent("start", "s", {"type": "message_start", "message": reply})
            if i == 1:
                for tool in ("Read", "Grep"):
                    block = {"type": "tool_use", "id": tool, "name": tool, "input": {}}
                    yield StreamEvent("block", "s", {"type": "content_block_start", "content_block": block})
                    yield sdk.AssistantMessage(content=[sdk.ToolUseBlock(tool, tool, {"path": "private"})],
                                               model=model, message_id=reply["id"])
            yield StreamEvent("delta", "s", {"type": "message_delta", "usage": {"output_tokens": 7},
                                             "delta": {"stop_reason": "tool_use" if i == 1 else "end_turn"}})
            yield StreamEvent("stop", "s", {"type": "message_stop"})
            if i == 1:
                yield sdk.UserMessage(content=[sdk.ToolResultBlock("Grep", "private", is_error=True),
                                               sdk.ToolResultBlock("Read", "private")])
        yield sdk.ResultMessage(
            "success", 100, 80, False, 2, "s", total_cost_usd=0.0123,
            usage={"input_tokens": 10, "cache_read_input_tokens": 40,
                   "cache_creation_input_tokens": 20, "output_tokens": 14},
        )
        assert options.include_partial_messages

    monkeypatch.setattr(sdk, "query", query)
    runs.execute("demo", dry_run=True)
    record = runs.execute("demo")
    assert record.status == "success", record.error
    [agent] = completed_spans(spans, "invoke_agent Demo")
    calls = completed_spans(spans, f"chat {model}")
    assert len(calls) == 2
    for call in calls:
        assert call.parent.span_id == agent.context.span_id
        assert call.attributes["gen_ai.operation.name"] == "chat"
        assert call.attributes["gen_ai.provider.name"] == "anthropic"
        assert call.attributes["gen_ai.usage.input_tokens"] == 35
        assert call.attributes["gen_ai.usage.output_tokens"] == 7
        assert call.attributes["gen_ai.usage.cache_read.input_tokens"] == 20
        assert call.attributes["gen_ai.usage.cache_creation.input_tokens"] == 10
        assert call.attributes["operation.cost"] > 0
    assert agent.attributes["tool_calling_turns"] == 1
    assert {d["name"] for d in json.loads(agent.attributes["gen_ai.tool.definitions"])} == {"Read", "Grep", "Write"}
    tool_message, = json.loads(calls[0].attributes["gen_ai.output.messages"])
    assert [(p["type"], p["name"], p["id"]) for p in tool_message["parts"]] == [
        ("tool_call", "Read", "Read"), ("tool_call", "Grep", "Grep"),
    ]
    assert json.loads(calls[0].attributes["gen_ai.response.finish_reasons"]) == ["tool_call"]
    assert json.loads(calls[1].attributes["gen_ai.response.finish_reasons"]) == ["stop"]
    for name, status in [("Read", "completed"), ("Grep", "failed")]:
        tool, = completed_spans(spans, f"execute_tool {name}")
        assert tool.parent.span_id == agent.context.span_id
        assert tool.attributes["tool.status"] == status
        assert "private" not in str(tool.attributes) and "private" not in str(tool_message)
    assert agent.attributes["num_turns"] == record.num_turns == 2
    [summary] = completed_spans(spans, "run.agent")
    assert summary.attributes["gen_ai.usage.cost_usd"] == record.cost_usd == 0.0123
    assert summary.attributes["gen_ai.aggregated_usage.output_tokens"] == 14
    instruments = {m.name: m for r in metrics.get_metrics_data().resource_metrics
                   for scope in r.scope_metrics for m in scope.metrics}
    [turns] = instruments["reginald.agent.turns"].data.data_points
    [tool_turns] = instruments["reginald.agent.tool_calling_turns"].data.data_points
    assert (tool_turns.count, tool_turns.sum) == (1, 1)
    [cost] = instruments["reginald.agent.cost"].data.data_points
    assert (turns.count, turns.sum, cost.count, cost.sum) == (1, 2, 1, 0.0123)
    assert turns.attributes["task"] == "demo"
    tokens = instruments["reginald.agent.tokens"].data.data_points
    assert {p.attributes["token_type"]: (p.count, p.sum) for p in tokens} == {
        "input_tokens": (1, 70), "output_tokens": (1, 14),
        "cache_read.input_tokens": (1, 40), "cache_creation.input_tokens": (1, 20),
    }
    model_tokens = instruments["gen_ai.client.token.usage"].data.data_points
    assert {p.attributes["gen_ai.token.type"]: (p.count, p.sum) for p in model_tokens} == {
        "input": (2, 70), "output": (2, 14),
    }
    [model_cost] = instruments["operation.cost"].data.data_points
    assert model_cost.count == 2
    assert model_cost.sum == pytest.approx(sum(call.attributes["operation.cost"] for call in calls))


def test_claude_retains_reported_zeroes_on_sdk_failure(runs, telemetry, monkeypatch):
    sdk = pytest.importorskip("claude_agent_sdk")

    async def query(prompt, options):
        yield sdk.ResultMessage(
            "error_during_execution", 10, 5, True, 1, "s", total_cost_usd=0.0,
            usage={"input_tokens": 4, "output_tokens": 0, "cache_read_input_tokens": 0},
        )
        raise RuntimeError("transport closed")

    monkeypatch.setattr(sdk, "query", query)
    record = runs.execute("demo")
    assert record.status == "failed"
    assert (record.num_turns, record.cost_usd) == (1, 0.0)
    [summary] = completed_spans(telemetry[1], "run.agent")
    assert summary.attributes["gen_ai.aggregated_usage.output_tokens"] == 0
    assert summary.attributes["gen_ai.aggregated_usage.cache_read.input_tokens"] == 0
    assert "gen_ai.aggregated_usage.cache_creation.input_tokens" not in summary.attributes


def test_interrupted_model_stream_omits_placeholder_output_and_unknown_cost(runs, telemetry, monkeypatch):
    sdk = pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk.types import StreamEvent

    async def query(prompt, options):
        yield StreamEvent("start", "s", {"type": "message_start", "message": {
            "model": "model-without-a-price", "usage": {"input_tokens": 3, "output_tokens": 1},
        }})
        raise ConnectionError("stream interrupted")

    monkeypatch.setattr(sdk, "query", query)
    record = runs.execute("demo")
    assert record.status == "failed"
    assert record.num_turns is None and record.cost_usd is None
    [call] = completed_spans(telemetry[1], "chat model-without-a-price")
    assert call.attributes["gen_ai.usage.input_tokens"] == 3
    assert "gen_ai.usage.output_tokens" not in call.attributes
    assert "operation.cost" not in call.attributes


@pytest.mark.parametrize("raises", [True, False])
def test_claude_success_subtype_records_the_diagnosis(runs, telemetry, monkeypatch, raises):
    sdk = pytest.importorskip("claude_agent_sdk")

    async def query(prompt, options):
        yield sdk.AssistantMessage(content=[], model="<synthetic>", error="rate_limit")
        yield sdk.ResultMessage("success", 10, 5, True, 1, "s", result="Hit the hourly limit",
                                api_error_status=429, total_cost_usd=0.002)
        if raises:
            raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(sdk, "query", query)
    record = runs.execute("demo")
    assert record.status == "failed"
    assert "Hit the hourly limit" in record.error
    assert "rate_limit" in record.error
    assert "429" in record.error
    assert "success" not in record.error.lower()
    for name in ("invoke_agent Demo", "run.agent"):
        [span] = completed_spans(telemetry[1], name)
        assert span.attributes["error.message"] == record.error
        assert span.status.status_code.name == "ERROR"
        if raises and name == "invoke_agent Demo":
            [error] = [event for event in span.events if event.name == "exception"]
            assert error.attributes["exception.message"] == record.error
            assert "error result: success" not in error.attributes["exception.stacktrace"].splitlines()[-1]


@pytest.mark.parametrize("use_tools", [False, True])
def test_pydantic_agent_exports_the_reported_usage(runs, telemetry, monkeypatch, config, use_tools):
    sdk = pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    _, spans, _ = telemetry
    agent_class = sdk.Agent

    def read_note(name: str) -> str:
        return name

    def respond(messages, info):
        if use_tools and len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("read_note", {"name": n}) for n in ("a", "b")],
                                 finish_reason="tool_call", usage=RequestUsage(input_tokens=11, output_tokens=3, cost=Decimal("0")))
        return ModelResponse(parts=[TextPart("done")], usage=RequestUsage(
            input_tokens=30, output_tokens=4, cache_read_tokens=10, cache_write_tokens=5,
            cost=Decimal("0.007"),
        ))

    def offline_agent(model, **kwargs):
        return agent_class(FunctionModel(respond, model_name=model), tools=[read_note], **kwargs)

    monkeypatch.setattr(sdk, "Agent", offline_agent)
    config.tasks["demo"] = TaskConfig(title="Demo", runner="pydantic_agent", model="test")
    record = runs.execute("demo")
    assert record.status == "success", record.error
    assert (record.num_turns, record.cost_usd) == (2 if use_tools else 1, 0.007)
    [summary] = completed_spans(spans, "run.agent")
    assert summary.attributes["tool_calling_turns"] == int(use_tools)
    assert summary.attributes["runner"] == "pydantic_agent"
    assert summary.attributes["gen_ai.aggregated_usage.input_tokens"] == (41 if use_tools else 30)
    assert summary.attributes["gen_ai.aggregated_usage.cache_read.input_tokens"] == 10
    assert summary.attributes["gen_ai.aggregated_usage.cache_creation.input_tokens"] == 5
    assert len(completed_spans(spans, "invoke_agent Demo")) == 1
