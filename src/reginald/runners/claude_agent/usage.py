"""Translate Claude SDK usage into Logfire's run and model-call conventions."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from contextlib import ExitStack, closing
from typing import TYPE_CHECKING

from ...core.observability import logger, record_histogram, span
from ..tool_spans import ToolSpans

if TYPE_CHECKING:
    from claude_agent_sdk.types import Message, ResultMessage


def _token_counts(usage: object) -> dict[str, int]:
    counts = {}
    if not isinstance(usage, dict):
        return counts
    for source, key in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("cache_read_input_tokens", "cache_read.input_tokens"),
        ("cache_creation_input_tokens", "cache_creation.input_tokens"),
    ):
        value = usage.get(source)
        if type(value) is int and value >= 0:
            counts[key] = value
    # Anthropic reports uncached input separately. OTel includes cached tokens in input.
    if "input_tokens" in counts:
        counts["input_tokens"] += (
            counts.get("cache_read.input_tokens", 0) + counts.get("cache_creation.input_tokens", 0)
        )
    return counts


def result_usage(message: ResultMessage) -> tuple[float | None, dict[str, int]]:
    cost = message.total_cost_usd
    totals = _token_counts(message.usage)
    # The per-model breakdown includes subagents and can survive an error that
    # leaves the main loop's result.usage empty. These are alternative totals.
    models = getattr(message, "model_usage", None)
    if not isinstance(models, dict) or not models:
        return cost, totals
    if not all(isinstance(usage, dict) for usage in models.values()):
        return cost, totals
    by_model = [
        _token_counts({
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
            "cache_read_input_tokens": usage.get("cacheReadInputTokens"),
            "cache_creation_input_tokens": usage.get("cacheCreationInputTokens"),
        })
        for usage in models.values()
    ]
    for key in by_model[0]:
        if all(key in counts for counts in by_model):
            totals[key] = sum(counts[key] for counts in by_model)
    costs = [usage.get("costUSD") for usage in models.values()]
    if cost is None and all(isinstance(value, (int, float)) for value in costs):
        cost = sum(costs)
    return cost, totals


def _finish_call(call, labels: dict[str, str], usage: dict) -> None:
    counts = _token_counts(usage)
    for key, value in counts.items():
        call.set_attribute(f"gen_ai.usage.{key}", value)
    # Logfire consumers also recognise Pydantic AI's older cache attributes.
    for key, old_key in (("cache_read.input_tokens", "cache_read_tokens"),
                         ("cache_creation.input_tokens", "cache_write_tokens")):
        if key in counts:
            call.set_attribute(f"gen_ai.usage.details.{old_key}", counts[key])
    for kind in ("input", "output"):
        record_histogram(
            "gen_ai.client.token.usage", "{token}", counts.get(f"{kind}_tokens"),
            {**labels, "gen_ai.token.type": kind},
        )
    # The SDK prices the entire run. Price each completed call from its final
    # usage and the installed offline price table, including cache TTLs and tools.
    if "input_tokens" not in counts or "output_tokens" not in counts:
        return
    model = labels["gen_ai.response.model"]
    try:
        from genai_prices import extract_usage

        response = extract_usage({"model": model, "usage": usage}, provider_id="anthropic")
        if response.model is not None:
            cost = float(response.calc_price().total_price)
            call.set_attribute("operation.cost", cost)
            record_histogram("operation.cost", "{USD}", cost, labels)
    except Exception as exc:  # noqa: BLE001 — optional pricing must never fail a task
        logger.debug("could not price Claude model call for %s: %s", model, exc)


# Match the finish reasons Pydantic AI emits for the same Anthropic responses.
_FINISH_REASONS = {
    "tool_use": "tool_call", "end_turn": "stop", "stop_sequence": "stop",
    "max_tokens": "length", "model_context_window_exceeded": "length", "refusal": "content_filter",
}


def _finish_response(call, tool_names, finish, tools: ToolSpans) -> None:
    tools.set_definitions(call)
    if tool_names or finish:
        parts = [{"type": "tool_call", "id": call_id, "name": name} for call_id, name in tool_names.items()]
        call.set_attribute("gen_ai.output.messages", [{"role": "assistant", "parts": parts, **finish}])
    if finish:
        call.set_attribute("gen_ai.response.finish_reasons", [finish["finish_reason"]])


async def trace_model_calls(
    messages: AsyncIterable[Message], default_model: str, tools: ToolSpans,
) -> AsyncIterator[Message]:
    """Time each streamed response, forwarding ordinary messages to the runner.

    The CLI emits one start/stop pair per response, but can emit several assistant
    messages with the same ID. Only the final usage delta has the real output count.
    """
    from claude_agent_sdk.types import (
        AssistantMessage, ResultMessage, ServerToolUseBlock, StreamEvent, ToolUseBlock,
    )

    # A trailing SDK error describes the run. Its runner records the diagnosis,
    # including when the CLI exits without closing the last model stream.
    # The SDK can interleave subagent traffic with the main model stream.
    # Only the main agent's chat spans belong in its turn distribution.
    with closing(ExitStack()) as active:
        usage = None
        response_id = None
        tool_names: dict[str, str] = {}
        async for message in messages:
            if not isinstance(message, StreamEvent):
                if (usage is not None and isinstance(message, AssistantMessage)
                        and not message.parent_tool_use_id and message.message_id == response_id):
                    # Merged SDK messages and streamed content can describe the same call.
                    tool_names.update((block.id, block.name) for block in message.content
                                      if isinstance(block, (ToolUseBlock, ServerToolUseBlock)))
                if isinstance(message, ResultMessage):
                    active.close()
                    usage = None
                yield message
                continue
            event = message.event
            if message.parent_tool_use_id or not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "message_start":
                active.close()
                usage = None
                response = event.get("message")
                if not isinstance(response, dict):
                    continue
                model = response.get("model") or default_model
                labels = {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.system": "anthropic",
                    "gen_ai.provider.name": "anthropic",
                    "gen_ai.request.model": model,
                    "gen_ai.response.model": model,
                }
                call = active.enter_context(span(f"chat {model}", **labels))
                response_id = response.get("id")
                if response_id:
                    call.set_attribute("gen_ai.response.id", response_id)
                tool_names = {}
                finish = {}
                active.callback(_finish_response, call, tool_names, finish, tools)
                initial = response.get("usage")
                usage = initial.copy() if isinstance(initial, dict) else {}
                usage.pop("output_tokens", None)  # placeholder at message_start
                active.callback(_finish_call, call, labels, usage)
            elif kind == "content_block_start" and usage is not None:
                block = event.get("content_block")
                if isinstance(block, dict) and block.get("type") in ("tool_use", "server_tool_use"):
                    if block.get("id") and block.get("name"):
                        tool_names[block["id"]] = block["name"]
            elif kind == "message_delta" and usage is not None:
                reason = (event.get("delta") or {}).get("stop_reason")
                if reason:
                    finish["finish_reason"] = _FINISH_REASONS.get(reason, reason)
                delta = event.get("usage")
                if isinstance(delta, dict):
                    usage.update({k: v for k, v in delta.items() if v is not None})
            elif kind == "message_stop":
                active.close()
                usage = None
