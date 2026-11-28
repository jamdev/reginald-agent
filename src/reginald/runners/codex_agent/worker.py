"""Own one local Codex app-server. Emit bounded tool records to the run process."""

import json
import os
import sys

from pydantic import RootModel

from ..base import validate_tool_grants
from ..conversation import BRAVE_WEB_SEARCH, brave_result_urls, excerpt
from .auth import credential_home


def emit(kind, **fields):
    print(json.dumps({"kind": kind, **fields}), flush=True)


def mcp_servers(operations, registry):
    servers = {}
    for name, selected in operations.items():
        cfg = registry[name]
        if cfg["transport"] == "http":
            server = {"url": cfg["url"]}
        else:
            server = {"command": cfg["command"], "args": cfg["args"], "env_vars": cfg["env"]}
        server.update(required=True, default_tools_approval_mode="approve")
        if "*" not in selected:
            server["enabled_tools"] = sorted(selected)
        servers[name] = server
    return servers


def validate_mcp_operations(client, thread, operations):
    from openai_codex.generated.v2_all import ListMcpServerStatusResponse

    for name, selected in operations.items():
        params = {"threadId": thread, "serverName": name}
        advertised = set()
        while True:
            page = client.request("mcpServerStatus/list", params, response_model=ListMcpServerStatusResponse)
            for server in page.data:
                if server.name == name:
                    if server.tools_error:
                        raise ValueError(f"Cannot discover MCP tools on {name}: {server.tools_error}")
                    advertised.update(tool.name for tool in server.tools.values())
            if not page.next_cursor:
                break
            params["cursor"] = page.next_cursor
        prefix = f"mcp__{name}__"
        validate_tool_grants(name, (prefix + op for op in selected), (prefix + op for op in advertised))


# SDK fields exposed by each tool item. Take the excerpt in the worker so
# command output and MCP payloads do not inflate the supervisor's event stream.
_TOOL_FIELDS = {
    "commandExecution": ("command", "aggregated_output"),
    "fileChange": ("changes", None),
    "mcpToolCall": ("arguments", "result"),
    "dynamicToolCall": ("arguments", "content_items"),
    "webSearch": ("query", "results"),
    "imageView": ("path", None),
}


def relay(method, payload, limit: int = 0):
    if method == "turn/started":
        emit("turn")
    elif method == "thread/tokenUsage/updated":
        emit("usage", total=payload.token_usage.total.model_dump())
    elif method in ("item/started", "item/completed"):
        item = payload.item.root
        if item.type == "agentMessage":
            if method == "item/completed" and (item.phase is None or item.phase.value == "final_answer"):
                emit("output", text=item.text)
            elif method == "item/completed" and limit:
                emit("message", part={"type": "text", "content": item.text[:limit]})
        elif item.type == "reasoning" and method == "item/completed" and limit and item.summary:
            emit("message", part={"type": "thinking", "content": "\n".join(item.summary)[:limit]})
        elif item.type in _TOOL_FIELDS:
            name = f"mcp__{item.server}__{item.tool}" if item.type == "mcpToolCall" else item.type
            status = getattr(item, "status", None)
            fields = {}
            args_key, result_key = _TOOL_FIELDS[item.type]
            if limit and (arguments := getattr(item, args_key, None)) is not None:
                fields["arguments"] = excerpt(arguments, limit)
            if limit and method == "item/completed":
                result = getattr(item, result_key) if result_key else None
                if item.type == "mcpToolCall" and item.tool == BRAVE_WEB_SEARCH and result is not None:
                    fields["result_urls"] = brave_result_urls(result.content)
                if result is None:
                    error = getattr(item, "error", None)
                    result = getattr(error, "message", error)
                elif item.type == "mcpToolCall" and getattr(status, "value", status) == "failed":
                    # MCP errors often arrive as successful transport replies with a text block.
                    result = next((b["text"] for b in result.content
                                   if isinstance(b, dict) and b.get("type") == "text" and b.get("text")), result)
                if result is not None:
                    fields["result"] = excerpt(result, limit)
            emit(method, id=item.id, name=name, status=getattr(status, "value", status), **fields)
    elif method == "turn/completed":
        turn = payload.turn
        if turn.status.value != "completed":
            raise RuntimeError(turn.error.message if turn.error else f"Codex turn {turn.status.value}")
        emit("done")


def execute(job):
    try:
        from openai_codex import CodexConfig
        from openai_codex.client import CodexClient
    except ImportError as exc:
        raise RuntimeError("Install the reginald[agents] extra for the Codex SDK and runtime") from exc
    vault = job["vault"]
    home = credential_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = os.environ.pop("OPENAI_API_KEY", None)
    overrides = [
        'model_provider="openai"', 'approval_policy="never"', f'web_search={json.dumps(job["web_search"])}',
        'shell_environment_policy.inherit="none"',
        f'shell_environment_policy.set.PATH={json.dumps(os.environ.get("PATH", os.defpath))}',
        'features.plugins=false', 'features.hooks=false',
        'features.apps=false', 'features.multi_agent=false', 'features.multi_agent_v2=false',
        'analytics.enabled=false', 'agents.max_threads=1', 'allow_login_shell=false',
        f'projects.{json.dumps(vault)}.trust_level="untrusted"',
    ]
    if key:
        overrides.append('cli_auth_credentials_store="ephemeral"')
    config = CodexConfig(cwd=str(home), env={"CODEX_HOME": str(home)}, config_overrides=tuple(overrides))
    # The SDK's default callback accepts command and file approval requests.
    # Deny any unexpected approval even if the runtime asks despite policy=never.
    with CodexClient(config, approval_handler=lambda method, params: {"decision": "decline"}) as client:
        client.initialize()
        # Reject ambient servers before starting a thread. Codex merges maps across config layers.
        effective = client.request("config/read", {"cwd": vault}, response_model=RootModel[dict])
        if effective.root["config"].get("mcp_servers"):
            raise ValueError("Remove ambient MCP servers from the Reginald Codex profile or system config")
        if key:
            client.account_login_start({"type": "apiKey", "apiKey": key})
        account = client.account_read().account
        if account is None:
            raise ValueError("Codex requires OPENAI_API_KEY or a service-user Codex login")
        emit("auth", mode=account.root.type)
        writable = job["write"]
        sandbox = "workspace-write" if writable else "read-only"
        started = client.thread_start({
            "cwd": vault, "model": job["model"], "modelProvider": "openai",
            "allowProviderModelFallback": False, "ephemeral": True, "approvalPolicy": "never",
            "sandbox": sandbox,
            "config": {"mcp_servers": mcp_servers(job["operations"], job["servers"])},
        })
        if started.model != job["model"] or started.model_provider != "openai":
            raise ValueError(f"Unexpected Codex model selection: {started.model_provider}/{started.model}")
        policy = {"type": "readOnly", "networkAccess": False}
        if writable:
            policy = {"type": "workspaceWrite", "writableRoots": [vault], "networkAccess": False,
                      "excludeSlashTmp": True, "excludeTmpdirEnvVar": True}
        thread = started.thread.id
        validate_mcp_operations(client, thread, job["operations"])
        turn = client.turn_start(thread, job["prompt"], {"sandboxPolicy": policy}).turn.id
        try:
            while True:
                event = client.next_turn_notification(turn)
                relay(event.method, event.payload, job["capture_chars"])
                if event.method == "turn/completed":
                    break
        except KeyboardInterrupt:
            client.turn_interrupt(thread, turn)
            while client.next_turn_notification(turn).method != "turn/completed":
                pass
            raise


if __name__ == "__main__":
    try:
        execute(json.load(sys.stdin))
    except KeyboardInterrupt:
        sys.exit(130)  # The supervisor records the timeout on the run.
    except Exception as exc:  # noqa: BLE001 — process boundary
        emit("failed", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
