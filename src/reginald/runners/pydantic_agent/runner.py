"""PydanticAgentRunner — model requests and MCP tool calls via Pydantic AI.

Model-agnostic (anthropic:*, ollama:*, openai:* — anything Pydantic AI
supports). MCP tools attach as toolsets. Lazy import, as with the agent runner.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ...core.config import TaskConfig, ToolConfig
from ...core.observability import agent_instrumentation, logger
from ...domain.models import DoctorCheck
from ..base import RunnerResult, check_sdk, validate_tool_grants
from .auth import check_pydantic_auth


class PydanticAgentRunner:
    @staticmethod
    def doctor_checks(tasks: dict[str, TaskConfig]) -> list[DoctorCheck]:
        return [check_sdk("pydantic_ai"), *check_pydantic_auth(tasks)]

    def run(
        self,
        task_name: str,
        task: TaskConfig,
        prompt: str,
        vault_path: Path,
        tools: dict[str, ToolConfig],
    ) -> RunnerResult:
        try:
            from pydantic_ai import Agent, capture_run_messages  # type: ignore
            from pydantic_ai.messages import ModelResponse, NativeToolCallPart, ToolCallPart
            from pydantic_ai.usage import RunUsage, UsageLimits  # type: ignore
        except ImportError:
            return RunnerResult(
                ok=False,
                error="pydantic-ai not installed (pip install 'reginald[agents]')",
            )

        usage = RunUsage()

        async def _run() -> str:
            instrumentation = agent_instrumentation()
            toolsets = []
            catalogs = {}
            mcp_operations = task.mcp_operations
            if mcp_operations:
                from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
                from pydantic_ai.mcp import MCPToolset

                trace_call = instrumentation.trace_mcp_call if instrumentation and instrumentation.limit else None
                for name, operations in mcp_operations.items():
                    cfg = tools[name]
                    if cfg.transport == "http":
                        transport = StreamableHttpTransport(str(cfg.url))
                    else:
                        transport = StdioTransport(
                            command=cfg.command, args=cfg.args, env=cfg.resolved_env(),
                        )
                    server = MCPToolset(transport, id=name, process_tool_call=trace_call)
                    catalogs[name] = server
                    if "*" not in operations:
                        server = server.filtered(lambda _ctx, tool, names=operations: tool.name in names)
                    toolsets.append(server.prefixed(name.replace("-", "_")))

            model = task.model
            capabilities = []
            native = [name for name in task.tools if not name.startswith("mcp__")]
            if native:
                from pydantic import TypeAdapter
                from pydantic_ai.capabilities import NativeTool
                from pydantic_ai.models import infer_model

                model = infer_model(task.model)
                supported = {tool.kind: tool for tool in model.profile["supported_native_tools"]}
                validate_tool_grants(task.model, native, supported)
                for name in native:
                    options = task.native_tool_options.get(name, {})
                    if "kind" in options:
                        raise ValueError(f"native_tool_options.{name}.kind is already set by the grant")
                    tool = TypeAdapter(supported[name]).validate_python(options, extra="forbid")
                    capabilities.append(NativeTool(tool))

            agent = Agent(model=model, name=task.title, toolsets=toolsets, capabilities=capabilities)
            agent.instrument = instrumentation
            async with agent:
                for name, server in catalogs.items():
                    prefix = f"mcp__{name}__"
                    validate_tool_grants(name, (prefix + op for op in mcp_operations[name]),
                                         (prefix + tool.name for tool in await server.list_tools()))
                result = await agent.run(
                    prompt,
                    usage=usage,
                    usage_limits=UsageLimits(request_limit=task.max_turns),
                )
            return str(result.output)

        logger.info("pydantic_agent run start task=%s model=%s", task_name, task.model)
        with capture_run_messages() as messages:
            try:
                result = RunnerResult(ok=True, output=asyncio.run(_run()))
            except Exception as exc:  # noqa: BLE001 — runner boundary
                result = RunnerResult(ok=False, error=f"{type(exc).__name__}: {exc}")
        responses = [message for message in messages if isinstance(message, ModelResponse)]
        if responses or result.ok:
            result.tool_calling_turns = sum(
                any(isinstance(part, (ToolCallPart, NativeToolCallPart)) for part in response.parts)
                for response in responses
            )
        if usage.requests or result.ok:
            result.num_turns = usage.requests
            result.cost_usd = float(usage.cost) if usage.cost is not None else None
            result.token_usage = {
                "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                "cache_read.input_tokens": usage.cache_read_tokens,
                "cache_creation.input_tokens": usage.cache_write_tokens,
            }
        return result
