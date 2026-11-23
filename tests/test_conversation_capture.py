"""Exercise conversation capture at the SDK and exporter boundaries, without network access."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from reginald.core import observability
from reginald.core.config import LogfireConfig, TaskConfig
from reginald.runners.claude_agent import ClaudeAgentRunner
from reginald.runners.pydantic_agent import PydanticAgentRunner

TEXT = "λ" * 100 + "OFF_LIMITS"


@pytest.mark.parametrize("value", [0, -1, True, "100", 1.5, None])
def test_capture_limit_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        LogfireConfig(capture_chars=value)


@pytest.mark.parametrize("limit", [100, False])
def test_claude_conversation(telemetry, monkeypatch, tmp_path, limit):
    sdk = pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk.types import StreamEvent

    async def query(prompt, options):
        assert prompt == TEXT

        def event(kind, **fields):
            return StreamEvent("id", "session", {"type": kind, **fields})

        yield event("message_start", message={"id": "m", "model": "demo"})
        yield event("content_block_start", index=0, content_block={"type": "text", "text": ""})
        for chunk in (TEXT[:60], TEXT[60:]):
            yield event("content_block_delta", index=0, delta={"type": "text_delta", "text": chunk})
        yield sdk.AssistantMessage(model="demo", message_id="m", content=[
            sdk.TextBlock(TEXT), sdk.ThinkingBlock(TEXT, "SIGNATURE"), sdk.ToolUseBlock("id", "Read", {"key": TEXT}),
        ])
        yield event("message_stop")
        yield sdk.UserMessage(content=[sdk.ToolResultBlock("id", TEXT)])
        yield event("message_start", message={"id": "m2", "model": "demo"})
        yield event("content_block_start", index=0, content_block={"type": "thinking", "thinking": ""})
        for chunk in ("a" * 50, "b" * 50 + "OFF_LIMITS"):
            yield event("content_block_delta", index=0, delta={"type": "thinking_delta", "thinking": chunk})
        yield event("message_stop")
        yield sdk.ResultMessage("success", 1, 1, False, 1, "s", result=TEXT)

    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(observability, "_capture_chars", limit)
    result = ClaudeAgentRunner().run("demo", TaskConfig(title="Demo", model="demo", runner="claude-agent"), TEXT, tmp_path, {})
    assert result.ok and result.output == TEXT
    exported = telemetry[1].get_finished_spans()
    attrs = str([dict(s.attributes) for s in exported])
    assert "OFF_LIMITS" not in attrs and "SIGNATURE" not in attrs
    assert ("λ" * 100 in attrs) == bool(limit)
    if limit:
        agent = next(s for s in exported if s.name == "invoke_agent Demo" and "pydantic_ai.all_messages" in s.attributes)
        parts = [p for msg in json.loads(agent.attributes["pydantic_ai.all_messages"]) for p in msg["parts"]]
        assert any(p.get("content") == "a" * 50 + "b" * 50 for p in parts)
        assert {p["type"] for p in parts} >= {"text", "thinking", "tool_call", "tool_call_response"}
        chat = next(s for s in exported if s.name == "chat demo" and "gen_ai.output.messages" in s.attributes)
        assert json.loads(chat.attributes["gen_ai.output.messages"])[0]["parts"][0]["content"] == TEXT[:limit]
        second = next(s for s in exported if s.attributes.get("gen_ai.response.id") == "m2")
        assert json.loads(second.attributes["gen_ai.output.messages"])[0]["parts"] == [
            {"type": "thinking", "content": "a" * 50 + "b" * 50}
        ]
        assert json.loads(second.attributes["gen_ai.input.messages"])[-1]["parts"][0]["result"] == TEXT[:limit]


@pytest.mark.parametrize("limit", [6, False])
def test_pydantic_conversation(telemetry, monkeypatch, tmp_path, limit):
    sdk = pytest.importorskip("pydantic_ai")
    from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    def lookup(query: str):
        assert query == TEXT
        return TEXT

    def respond(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {"query": TEXT})])
        return ModelResponse(parts=[ThinkingPart("reasonOFF_LIMITS"), TextPart(TEXT)])

    real_agent = sdk.Agent
    monkeypatch.setattr(sdk, "Agent", lambda model, **kw: real_agent(FunctionModel(respond), tools=[lookup], **kw))
    monkeypatch.setattr(observability, "_capture_chars", limit)
    task = TaskConfig(title="Demo", model="test", runner="pydantic_agent")
    result = PydanticAgentRunner().run("demo", task, TEXT, tmp_path, {})
    assert result.ok and result.output == TEXT
    spans = telemetry[1].get_finished_spans()
    attrs = str([dict(s.attributes) for s in spans])
    assert "OFF_LIMITS" not in attrs and "λ" * 7 not in attrs
    assert ("λ" * 6 in attrs) == bool(limit)
    assert ("reason" in json.dumps([s.attributes.get("pydantic_ai.all_messages") for s in spans])) == bool(limit)


def test_codex_worker_sends_bounded_summary_and_tool_data(capsys):
    from reginald.runners.codex_agent.worker import relay

    def publish(**fields):
        payload = SimpleNamespace(item=SimpleNamespace(root=SimpleNamespace(**fields)))
        relay("item/completed", payload, 4)

    publish(type="reasoning", summary=[TEXT], content=["RAW_REASONING"])
    publish(type="mcpToolCall", id="1", server="vault", tool="read", arguments={"path": TEXT},
            status="completed", result=TEXT, error=None)
    output = capsys.readouterr().out
    assert TEXT[:4] in str([json.loads(line) for line in output.splitlines()])
    assert TEXT[:5] not in str([json.loads(line) for line in output.splitlines()])
    assert "RAW_REASONING" not in output and "OFF_LIMITS" not in output


@pytest.mark.parametrize("limit", [100, False])
def test_journal_mirror_limits_both_logfire_message_and_args(telemetry, monkeypatch, caplog, limit):
    import logging

    import logfire

    monkeypatch.setattr(observability, "_capture_chars", limit)
    instance, exporter, _ = telemetry
    handler = logfire.LogfireLoggingHandler(logfire_instance=instance)
    handler.addFilter(observability._mirror_conversation_record)
    logger = observability.logger
    logger.addHandler(handler)
    try:
        with caplog.at_level(logging.DEBUG, logger=logger.name):
            logger.debug("[%s] turn %d text: %s", "demo", 1, TEXT, extra={"conversation_excerpt": True})
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert TEXT in caplog.text
    record, = [s for s in exporter.get_finished_spans() if s.attributes.get("logfire.span_type") == "log"]
    assert "OFF_LIMITS" not in str(record.attributes)
    assert record.attributes["logfire.msg"] == "[demo] turn 1 text: " + TEXT[:limit]
