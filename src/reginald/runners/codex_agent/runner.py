"""Supervise one Codex SDK session and its tool processes under the vault lock."""

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

from ...core.config import TaskConfig, ToolConfig
from ...core.observability import agent_span, capture_chars, logger, set_run_usage
from ...domain.models import DoctorCheck
from ..base import RunnerResult, check_sdk
from ..conversation import Conversation
from ..tool_spans import ToolSpans
from .usage import update_usage


class CodexAgentRunner:
    @staticmethod
    def doctor_checks(_tasks: dict[str, TaskConfig]) -> list[DoctorCheck]:
        from .auth import check_codex_auth

        return [check_sdk("openai_codex"), check_codex_auth()]

    def __init__(self, log_turns: bool = True):
        self.log_turns = log_turns

    def run(self, task_name: str, task: TaskConfig, prompt: str, vault_path: Path,
            tools: dict[str, ToolConfig]) -> RunnerResult:
        result = RunnerResult(ok=False)
        with agent_span(agent_name=task.title, model=task.model, provider="openai") as agent:
            conversation = Conversation(prompt, capture_chars())
            # Native permission modes and wildcard grants are not callable names.
            calls = ToolSpans(
                name for name in task.tools if name.startswith("mcp__") and not name.endswith("__*")
            )
            try:
                asyncio.run(self._run(task_name, task, prompt, vault_path, tools, result, calls, conversation))
            except Exception as exc:  # noqa: BLE001 — runner boundary
                result.ok = False
                result.error = str(exc) or type(exc).__name__
                logger.error("[%s] Codex failed: %s", task_name, result.error)
            finally:
                calls.close()
                calls.set_definitions(agent)
            conversation.finish(agent, result.output)
            set_run_usage(agent, result)
        return result

    async def _run(self, task_name, task, prompt, vault_path, tools, result, calls, conversation):
        operations = task.mcp_operations
        job = {
            "vault": str(vault_path.resolve()), "prompt": prompt, "model": task.model,
            "capture_chars": conversation.limit,
            "write": "CodexWorkspaceWrite" in task.tools,
            "operations": {k: sorted(v) for k, v in operations.items()},
            "servers": {k: tools[k].model_dump(mode="json") for k in operations},
        }
        names = {"PATH", "HOME", "TMPDIR", "LANG", "OPENAI_API_KEY", "SSL_CERT_FILE",
                 "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
        for name in operations:
            names.update(tools[name].env)
        env = {k: os.environ[k] for k in names if k in os.environ}
        logger.info("codex_agent run start task=%s model=%s", task_name, task.model)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-m", "reginald.runners.codex_agent.worker",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            start_new_session=True, env=env, limit=16 * 1024 * 1024,
        )
        try:
            async with asyncio.timeout(task.timeout_seconds):
                process.stdin.write(json.dumps(job).encode())
                await process.stdin.drain()
                process.stdin.close()
                api_billing = False
                async for line in process.stdout:
                    event = json.loads(line)
                    match event["kind"]:
                        case "failed":
                            raise RuntimeError(event["error"])
                        case "auth":
                            api_billing = event["mode"] == "apiKey"
                        case "turn":
                            result.num_turns = 1
                            result.tool_calling_turns = 0
                        case "usage":
                            update_usage(result, event["total"], task.model, api_billing)
                        case "item/started":
                            result.tool_calling_turns = 1
                            calls.start(event["id"], event["name"], turn=1)
                            if self.log_turns:
                                logger.info("[%s] Codex tool: %s", task_name, event["name"])
                        case "item/completed":
                            calls.finish(event["id"], event["status"])
                            if conversation.limit:
                                part = {"type": "tool_call", "id": event["id"], "name": event["name"]}
                                if "arguments" in event:
                                    part["arguments"] = event["arguments"]
                                conversation.add("assistant", [part])
                                if "result" in event:
                                    conversation.add("user", [{"type": "tool_call_response", "id": event["id"],
                                                               "name": event["name"], "result": event["result"]}])
                        case "message":
                            conversation.add("assistant", [event["part"]])
                        case "output":
                            result.output = event["text"]
                            if conversation.limit:
                                conversation.add("assistant", [conversation.text(result.output)])
                        case "done":
                            result.ok = True
                status = await process.wait()
                if status or not result.ok:
                    raise RuntimeError(f"Codex worker exited with status {status} without a successful run")
        except TimeoutError as exc:
            raise RuntimeError(f"Codex exceeded timeout_seconds={task.timeout_seconds}") from exc
        finally:
            # Let the worker interrupt the turn, then reap the process group before rollback.
            try:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.send_signal(signal.SIGINT)
                    with suppress(TimeoutError, BrokenPipeError, ConnectionResetError):
                        await asyncio.wait_for(process.communicate(), 5)
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
