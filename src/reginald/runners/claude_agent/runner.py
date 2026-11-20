"""ClaudeAgentRunner — heavy agentic vault work via the Claude Agent SDK.

The SDK import is lazy so the rest of reginald (tests, subscriber admin,
outbox) works on machines without the agents extra installed.

Progress is logged per turn. Without it a run that is working and a run that is
wedged look identical from outside for up to TimeoutStartSec (3h), which is no
use on a box nobody is watching.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path

from ...core.config import TaskConfig, ToolConfig
from ...core.observability import agent_span, logger, set_run_usage
from ...domain.models import DoctorCheck
from ..base import RunnerResult, check_sdk
from ..tool_spans import ToolSpans
from .auth import API_KEY_VAR, OAUTH_TOKEN_VAR, check_claude_auth, effective_credential
from .usage import result_usage, trace_model_calls

# Long tool inputs and agent prose are truncated in the journal; the full
# exchange belongs in logfire, not in syslog.
_MAX_LOG_CHARS = 300

# Checked in order — the first present key is what the call is "about".
_TARGET_KEYS = ("file_path", "path", "notebook_path", "pattern", "command", "query", "url")

# The SDK relays the CLI's result `subtype` when the CLI flags is_error but
# supplies no structured `errors` — and for a usage/credit or auth failure the
# CLI leaves subtype at its default "success". So this exact string means "the
# CLI failed and told us nothing useful", not that anything succeeded.
_DETAIL_LESS_ERROR_SUFFIX = "error result: success"


def _truncate(text: str, limit: int = _MAX_LOG_CHARS) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _error_detail(text: str | None) -> str:
    text = (text or "").strip()
    if text.casefold() == "success" or text.casefold().endswith(_DETAIL_LESS_ERROR_SUFFIX):
        return ""
    return text


def humanize_agent_error(
    exc: Exception | None,
    stderr_tail: list[str],
    api_error_status: object | None = None,
    *,
    result_detail: str = "",
    assistant_error: str | None = None,
    subtype: str | None = None,
) -> str:
    """Recover the CLI's diagnosis even if the SDK raises its fallback exception."""
    message = _error_detail(result_detail)
    if not message and exc is not None and _error_detail(str(exc)):
        message = f"{type(exc).__name__}: {exc}"
    if not message:
        message = _error_detail(subtype)
    tail = " ".join(line.strip() for line in stderr_tail if line.strip())
    if not message:
        message = "Claude Code exited with an error"
        if not (assistant_error or api_error_status or tail):
            message += " but reported no reason"
    if assistant_error:
        message += f" [{assistant_error}]"
    if api_error_status:
        message += f" [API error status: {api_error_status}]"
    if tail:
        message += f" — stderr: {_truncate(tail, 500)}"
    return message


def _tool_target(tool_input: object) -> str:
    """A short summary of what a tool call is acting on, for the INFO line."""
    if not isinstance(tool_input, dict):
        return ""
    for key in _TARGET_KEYS:
        value = tool_input.get(key)
        if value:
            return _truncate(value, 120)
    return ""


class ClaudeAgentRunner:
    @staticmethod
    def doctor_checks(_tasks: dict[str, TaskConfig]) -> list[DoctorCheck]:
        return [check_sdk("claude_agent_sdk"), check_claude_auth(os.environ)]

    def __init__(self, log_turns: bool = True):
        self._log_turns = log_turns

    def run(
        self,
        task_name: str,
        task: TaskConfig,
        prompt: str,
        vault_path: Path,
        tools: dict[str, ToolConfig],
    ) -> RunnerResult:
        try:
            from claude_agent_sdk import (  # type: ignore
                AssistantMessage,
                ClaudeAgentOptions,
                HookMatcher,
                ResultMessage,
                ServerToolResultBlock,
                ServerToolUseBlock,
                SystemMessage,
                TextBlock,
                ToolResultBlock,
                ToolUseBlock,
                UserMessage,
                query,
            )
        except ImportError:
            return RunnerResult(
                ok=False,
                error="claude-agent-sdk not installed (pip install 'reginald[agents]')",
            )

        # Auth is environment-driven — the SDK hands os.environ to the CLI,
        # which picks its own credential (API key over token). Just report
        # which one will be used, or warn if neither is set.
        cred = effective_credential(os.environ)
        if cred is None:
            logger.warning(
                "no agent credential set (%s or %s) — the CLI run will likely fail auth",
                API_KEY_VAR, OAUTH_TOKEN_VAR,
            )
        else:
            logger.info("agent auth: using %s", cred)

        mcp_operations = task.mcp_operations
        mcp_servers = {}
        for name in mcp_operations:
            cfg = tools[name]
            if cfg.transport == "http":
                mcp_servers[name] = {"type": "http", "url": str(cfg.url)}
            else:
                mcp_servers[name] = {
                    "type": "stdio", "command": cfg.command,
                    "args": cfg.args, "env": cfg.resolved_env(),
                }

        async def check_mcp_tool(event, _tool_use_id, _context):
            # A saved Claude permission on the host can also pre-approve a call.
            # Keep the task's operation list in force even when that rule is broader.
            name = event["tool_name"]
            _, server, operation = name.split("__", 2)
            selected = mcp_operations.get(server, ())
            if operation in selected or "*" in selected:
                return {}
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Task {task_name!r} does not enable {name}.",
                }
            }

        # Collect the tail of the CLI's stderr: on failure it holds the real
        # reason the structured error result omits. Bounded so a chatty run
        # can't grow it without limit.
        stderr_tail: deque[str] = deque(maxlen=20)

        options = ClaudeAgentOptions(
            cwd=str(vault_path),
            model=task.model,
            tools=task.builtin_tools,
            allowed_tools=task.tools,
            permission_mode="dontAsk",
            max_turns=task.max_turns,
            mcp_servers=mcp_servers,
            strict_mcp_config=True,
            hooks={"PreToolUse": [HookMatcher(matcher="^mcp__", hooks=[check_mcp_tool])]},
            stderr=stderr_tail.append,
        )

        result = RunnerResult(ok=True)
        api_error_status = None
        assistant_error = None
        assistant_detail = ""
        subtype = None
        started = time.monotonic()

        async def _run() -> None:
            nonlocal api_error_status, assistant_error, assistant_detail, subtype
            turns = 0
            turn_numbers: dict[str, int] = {}
            tool_turns: set[int] = set()
            messages = query(prompt=prompt, options=options)
            if options.include_partial_messages:
                messages = trace_model_calls(messages, task.model, calls)
            async for message in messages:
                elapsed = time.monotonic() - started
                if isinstance(message, AssistantMessage):
                    if not message.parent_tool_use_id:
                        assistant_error = getattr(message, "error", None)
                        assistant_detail = ""
                        if assistant_error:
                            assistant_detail = _truncate(" ".join(
                                block.text for block in message.content if isinstance(block, TextBlock)
                            ), 500)
                    # Parallel tool calls can arrive as separate blocks of the same response.
                    message_id = getattr(message, "message_id", None)
                    turn = turn_numbers.get(message_id)
                    if turn is None:
                        turns += 1
                        turn = turns
                        if message_id:
                            turn_numbers[message_id] = turn
                            if not (message.parent_tool_use_id or assistant_error):
                                result.num_turns = (result.num_turns or 0) + 1
                    main_response = not (message.parent_tool_use_id or message.error)
                    used_a_tool = False
                    for block in message.content:
                        if isinstance(block, (ToolUseBlock, ServerToolUseBlock)):
                            used_a_tool = True
                            if main_response:
                                tool_turns.add(turn)
                            if agent is not None:
                                calls.start(block.id, block.name, turn=turn)
                            if self._log_turns:
                                logger.info(
                                    "[%s] turn %d (%.0fs): %s %s", task_name, turn, elapsed,
                                    block.name, _tool_target(block.input),
                                )
                            logger.debug(
                                "[%s] turn %d input: %s", task_name, turn,
                                _truncate(json.dumps(block.input, default=str)),
                            )
                        elif isinstance(block, ServerToolResultBlock):
                            failed = "error_code" in block.content
                            calls.finish(block.tool_use_id, "failed" if failed else "completed")
                        elif isinstance(block, TextBlock):
                            logger.debug(
                                "[%s] turn %d text: %s", task_name, turn, _truncate(block.text)
                            )
                    if main_response:
                        result.tool_calling_turns = len(tool_turns)
                    if self._log_turns and not used_a_tool:
                        logger.info("[%s] turn %d (%.0fs): no tool calls", task_name, turn, elapsed)

                elif isinstance(message, UserMessage):
                    content = message.content if isinstance(message.content, list) else []
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            calls.finish(block.tool_use_id, "failed" if block.is_error else "completed")
                            logger.debug(
                                "[%s] tool result %s: %s", task_name,
                                "ERROR" if block.is_error else "ok", _truncate(str(block.content)),
                            )

                elif isinstance(message, SystemMessage):
                    if message.subtype == "init" and isinstance(names := message.data.get("tools"), list):
                        calls.names.update(name for name in names if isinstance(name, str))
                    logger.debug("[%s] system/%s", task_name, message.subtype)

                elif isinstance(message, ResultMessage):
                    # The single-shot SDK can raise *after* yielding this result.
                    result.num_turns = message.num_turns
                    if not message.is_error:
                        result.tool_calling_turns = len(tool_turns)
                    result.cost_usd, result.token_usage = result_usage(message)
                    result.output = message.result or ""
                    result.ok = not message.is_error
                    api_error_status = getattr(message, "api_error_status", None)
                    subtype = message.subtype
                    result.error = None
                    if message.is_error:
                        errors = [_error_detail(s) for s in getattr(message, "errors", None) or []]
                        result.error = " | ".join(filter(None, errors)) or _error_detail(result.output)

            logger.info(
                "[%s] finished in %.0fs: %s turn(s), cost %s%s",
                task_name, time.monotonic() - started,
                result.num_turns if result.num_turns is not None else turns,
                f"${result.cost_usd:.4f}" if result.cost_usd is not None else "unknown",
                "" if result.ok else " [ERROR]",
            )

        logger.info("claude-agent run start task=%s model=%s", task_name, task.model)
        with agent_span(provider="anthropic", agent_name=task.title, model=task.model) as agent:
            calls = ToolSpans(name for name in task.tools if not name.endswith("__*"))
            options.include_partial_messages = agent is not None
            failure = None
            try:
                asyncio.run(_run())
            except Exception as exc:  # noqa: BLE001 — runner boundary
                result.ok = False
                failure = exc
            finally:
                calls.close()
                calls.set_definitions(agent)
            if not result.ok:
                result.error = humanize_agent_error(
                    failure, list(stderr_tail), api_error_status,
                    result_detail=result.error or assistant_detail,
                    assistant_error=assistant_error, subtype=subtype,
                )
                logger.error("[%s] claude-agent run errored: %s", task_name, result.error)
                if agent is not None and failure is not None:
                    diagnostic = failure
                    if result.error != f"{type(failure).__name__}: {failure}":
                        # Keep the SDK stack when replacing the fallback with the CLI's diagnosis.
                        diagnostic = RuntimeError(result.error).with_traceback(failure.__traceback__)
                    agent.record_exception(diagnostic)
            set_run_usage(agent, result)
        return result
