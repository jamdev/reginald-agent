"""Exercise both SDK adapters without connecting to a model or an MCP server."""

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from reginald.core.config import Config, TaskConfig, ToolConfig, load_config
from reginald.runners.claude_agent import ClaudeAgentRunner
from reginald.runners.pydantic_agent import PydanticAgentRunner


def test_http_requires_an_absolute_http_url():
    for url in (None, "", "local/path", "ftp://example.org/tools"):
        with pytest.raises(ValidationError):
            ToolConfig(transport="http", url=url)


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_shipped_tasks_grant_expected_tools(env, tmp_path):
    shipped = Path(__file__).resolve().parents[1] / "config"
    for name in ("base", env):
        shutil.copyfile(shipped / f"{name}.yaml-example", tmp_path / f"{name}.yaml")
    for directory in ("tasks.d", "tools.d"):
        shutil.copytree(shipped / directory, tmp_path / directory)
    config = load_config(env, tmp_path)
    search_tasks = {"research-new-developments", "update-trackers", "action-edits"}
    for name, task in config.tasks.items():
        mcp_tools = [tool for tool in task.tools if tool.startswith("mcp__")]
        if name == "triage-inbox":
            assert mcp_tools == ["mcp__inbox__move_file"]
        else:
            assert mcp_tools == (["mcp__brave-search__brave_web_search"] if name in search_tasks else [])
        assert ("WebSearch" in task.tools) == (name in search_tasks)
        assert ("Agent" in task.tools) == (name in {"research-new-developments", "update-trackers"})


@pytest.mark.parametrize(("settings", "error"), [
    ({"allowed_tools": ["Read"]}, "allowed_tools"),
    ({"tools": ["brave-search"]}, "mcp__"),
    ({"tools": ["mcp__brave-search__"]}, "mcp__"),
    ({"tools": ["mcp__typo__search"]}, "typo"),
    ({"runner": "pydantic_agent", "tools": ["Read"]}, "Read"),
])
def test_invalid_task_tools_fail_at_config_load(tmp_path, settings, error):
    with pytest.raises(ValidationError, match=error):
        Config(vault_path=tmp_path, db_path=tmp_path / "db",
               tools={"brave-search": ToolConfig(command="tool")},
               tasks={"demo": {"title": "Demo", "runner": "claude-agent", "model": "test", **settings}})


def test_claude_configures_both_transports(monkeypatch, tmp_path):
    sdk = pytest.importorskip("claude_agent_sdk")
    captured = []

    async def query(prompt, options):
        captured.append(options)
        yield sdk.AssistantMessage(content=[], model="test")

    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setenv("TOOL_KEY", "test-key")
    tools = {
        "remote": ToolConfig(transport="http", url="https://example.org/mcp"),
        "local": ToolConfig(command="tool", args=["serve"], env=["TOOL_KEY"]),
    }
    task = TaskConfig(title="Demo", runner="claude-agent", model="test", tools=[
        "Read", "Bash(git status *)", "mcp__remote__lookup", "mcp__local__list_records",
    ])
    result = ClaudeAgentRunner().run("demo", task, "", tmp_path, tools)
    assert result.ok
    [options] = captured
    assert options.tools == ["Read", "Bash"]
    assert options.allowed_tools == task.tools
    assert options.permission_mode == "dontAsk"
    assert options.strict_mcp_config
    assert options.mcp_servers == {
        "remote": {"type": "http", "url": "https://example.org/mcp"},
        "local": {"type": "stdio", "command": "tool", "args": ["serve"], "env": {"TOOL_KEY": "test-key"}},
    }
    # The task limit also applies on a host with an existing, broader MCP approval.
    [matcher] = options.hooks["PreToolUse"]
    [check] = matcher.hooks
    assert asyncio.run(check({"tool_name": "mcp__remote__lookup"}, None, {})) == {}
    denied = asyncio.run(check({"tool_name": "mcp__remote__delete_record"}, None, {}))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pydantic_agent_configures_both_transports(monkeypatch):
    sdk = pytest.importorskip("pydantic_ai")
    mcp = pytest.importorskip("pydantic_ai.mcp")
    from mcp.types import Tool

    transports = []
    exposed_tools = []

    async def list_tools(server):
        transports.append(server.client.transport)
        return [Tool(name=name, inputSchema={"type": "object"})
                for name in ("lookup", "list_records", "delete_record")]

    class OfflineAgent:
        def __init__(self, model, name, toolsets):
            self.toolsets = toolsets

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def run(self, prompt, usage, usage_limits):
            for toolset in self.toolsets:
                exposed_tools.extend(await toolset.get_tools(SimpleNamespace(max_retries=1)))
            usage.requests += 1
            return SimpleNamespace(output="done", usage=usage)

    monkeypatch.setattr(sdk, "Agent", OfflineAgent)
    monkeypatch.setattr(mcp.MCPToolset, "list_tools", list_tools)
    monkeypatch.setenv("TOOL_KEY", "test-key")
    tools = {
        "remote-tool": ToolConfig(transport="http", url="https://example.org/mcp"),
        "local-tool": ToolConfig(command="tool", args=["serve"], env=["TOOL_KEY"]),
    }
    task = TaskConfig(title="Demo", runner="pydantic_agent", model="test",
                      tools=["mcp__remote-tool__lookup", "mcp__local-tool__list_records"])
    result = PydanticAgentRunner().run("demo", task, "", Path("."), tools)
    assert result.ok, result.error
    assert exposed_tools == ["remote_tool_lookup", "local_tool_list_records"]
    remote, local = transports
    assert remote.url == "https://example.org/mcp"
    assert local.command == "tool"
    assert list(local.args) == ["serve"]
    assert local.env["TOOL_KEY"] == "test-key"
