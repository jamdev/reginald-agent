"""Codex routing, permissions, usage and failure handling without a model or network."""

import asyncio
import json
import sys

import pytest
from pydantic import ValidationError

from reginald.core.config import TaskConfig
from reginald.core.context import build_runners
from reginald.runners.codex_agent import CodexAgentRunner
from reginald.services.doctor import DoctorService


def task(**changes):
    return TaskConfig.model_validate({
        "title": "Demo", "model": "gpt-5.4", "runner": "codex_agent",
        "timeout_seconds": 10, "tools": ["CodexReadOnly"], **changes,
    })


@pytest.mark.parametrize("settings", [
    {"tools": []}, {"tools": ["CodexReadOnly", "CodexWorkspaceWrite"]},
    {"tools": ["CodexReadOnly", "Bash"]}, {"timeout_seconds": 0}, {"max_turns": 1},
    {"timeout_seconds": None}, {"model": " "},
])
def test_invalid_codex_task(settings):
    with pytest.raises(ValidationError):
        task(**settings)


def test_codex_only_doctor_checks_its_own_credentials(config, monkeypatch):
    pytest.importorskip("openai_codex")
    config.tasks["demo"] = task()
    monkeypatch.setenv("OPENAI_API_KEY", "offline-key")
    checks = {c.name: c for c in DoctorService(config).check().checks}
    assert checks["Codex auth"].ok
    assert checks["module openai_codex"].ok
    assert checks["runner claude-agent"].warning and checks["runner pydantic_agent"].warning
    assert "Claude auth" not in checks
    assert "module claude_agent_sdk" not in checks
    assert isinstance(build_runners(config)["codex_agent"], CodexAgentRunner)


def test_mcp_grants_and_transports():
    pytest.importorskip("openai_codex")
    from reginald.runners.codex_agent.worker import mcp_servers

    servers = mcp_servers(
        {"search": ["lookup"], "local": ["*"]},
        {"search": {"transport": "http", "url": "https://example.org/mcp"},
         "local": {"transport": "stdio", "command": "test", "args": [], "env": ["TEST_KEY"]},
         "unused": {"transport": "stdio"}},
    )
    assert set(servers) == {"search", "local"}
    assert servers["search"]["enabled_tools"] == ["lookup"]
    assert servers["search"]["url"] == "https://example.org/mcp"
    assert servers["local"]["command"] == "test"
    assert servers["local"]["env_vars"] == ["TEST_KEY"]
    assert "enabled_tools" not in servers["local"]


def mock_worker(monkeypatch, code):
    spawn = asyncio.create_subprocess_exec

    async def start(*argv, **kwargs):
        assert argv[-1] == "reginald.runners.codex_agent.worker"
        return await spawn(sys.executable, "-I", "-c", code, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)


@pytest.mark.parametrize("mode", ["apiKey", "chatgpt"])
def test_completed_run_reports_usage_and_tools(tmp_path, monkeypatch, caplog, mode):
    counts = {"input_tokens": 30, "output_tokens": 7, "cached_input_tokens": 5}
    events = [{"kind": "auth", "mode": mode}, {"kind": "turn"},
              {"kind": "item/started", "id": "1", "name": "commandExecution"},
              {"kind": "item/completed", "id": "1", "name": "commandExecution", "status": "completed"},
              {"kind": "usage", "total": counts}, {"kind": "usage", "total": counts},
              {"kind": "output", "text": "done"}, {"kind": "done"}]
    wire = "\n".join(json.dumps(event) for event in events)
    mock_worker(monkeypatch, f"import sys; sys.stdin.read(); print({wire!r})")
    with caplog.at_level("INFO", logger="reginald"):
        result = CodexAgentRunner().run("demo", task(), "hello", tmp_path, {})
    assert result.ok and result.output == "done"
    assert result.tool_calling_turns == 1
    assert result.num_turns == 1
    assert result.token_usage == {"input_tokens": 30, "output_tokens": 7, "cache_read.input_tokens": 5}
    if mode == "chatgpt":
        assert result.cost_usd is None
    else:
        pytest.importorskip("genai_prices")
        assert result.cost_usd > 0
    assert "Codex tool: commandExecution" in caplog.text


def test_codex_worker_failure_preserves_usage(tmp_path, monkeypatch):
    mock_worker(monkeypatch, """
import sys
sys.stdin.read()
print('{"kind":"usage","total":{"input_tokens":14,"output_tokens":3,"cached_input_tokens":0}}')
print('{"kind":"failed","error":"account rate limit"}')
sys.exit(1)
""")
    result = CodexAgentRunner().run("demo", task(), "hello", tmp_path, {})
    assert not result.ok and result.error == "account rate limit"
    assert result.token_usage["input_tokens"] == 14


def test_abrupt_exit_cannot_report_success(tmp_path, monkeypatch):
    mock_worker(monkeypatch, "import sys; sys.stdin.read()")
    result = CodexAgentRunner().run("demo", task(), "hello", tmp_path, {})
    assert not result.ok and "without a successful run" in result.error


@pytest.mark.parametrize("limit", [4, False])
def test_conversation_export_keeps_the_full_task_output(tmp_path, monkeypatch, telemetry, limit):
    from reginald.core import observability

    monkeypatch.setattr(observability, "_capture_chars", limit)
    mock_worker(monkeypatch, """
import json, sys
from types import SimpleNamespace
from reginald.runners.codex_agent.worker import relay
job = json.load(sys.stdin)
assert job["prompt"] == "task-secret"
for fields in (
    dict(type="reasoning", summary=["plan-secret"], content=["private reasoning"]),
    dict(type="agentMessage", phase=SimpleNamespace(value="commentary"), text="look-secret"),
    dict(type="commandExecution", id="id", command="echo secret", aggregated_output="file-secret", status="completed"),
    dict(type="agentMessage", phase=SimpleNamespace(value="final_answer"), text="done-secret"),
):
    payload = SimpleNamespace(item=SimpleNamespace(root=SimpleNamespace(**fields)))
    if fields["type"] == "commandExecution":
        relay("item/started", payload, job["capture_chars"])
    relay("item/completed", payload, job["capture_chars"])
print('{"kind":"done"}')
""")
    result = CodexAgentRunner().run("demo", task(), "task-secret", tmp_path, {})
    assert result.ok and result.output == "done-secret"
    agent, = [s for s in telemetry[1].get_finished_spans() if s.name.startswith("invoke_agent")
              and s.attributes.get("logfire.span_type") == "span"]
    attrs = dict(agent.attributes)
    assert "private reasoning" not in str(attrs) and "-secret" not in str(attrs)
    if limit:
        messages = json.loads(attrs["pydantic_ai.all_messages"])
        assert [p.get("content", p.get("arguments", p.get("result")))
                for message in messages for p in message["parts"]] == ["task", "plan", "look", "echo", "file", "done"]
    else:
        assert "pydantic_ai.all_messages" not in attrs and "final_result" not in attrs


@pytest.mark.parametrize("limit", [100, False])
def test_brave_urls_on_codex_tool_span_and_conversation(tmp_path, monkeypatch, telemetry, limit):
    from reginald.core import observability

    monkeypatch.setattr(observability, "_capture_chars", limit)
    urls = ["https://example.org/" + "long-path/" * 20 + "?page=2#dates", "https://example.org/b"]
    mock_worker(monkeypatch, f"""
import json, sys
from types import SimpleNamespace
from reginald.runners.codex_agent.worker import relay
from pydantic import BaseModel
class Result(BaseModel):
    content: list[dict]
content = [dict(type="text", text="No web results found")]
for url in {urls!r}:
    content.append(dict(type="text", text=json.dumps(dict(title="x" * 200 + "PRIVATE", url=url))))
item = SimpleNamespace(type="mcpToolCall", server="brave-search", tool="brave_web_search", id="search",
                       arguments={{"query": "dates"}}, result=Result(content=content), status="completed")
payload = SimpleNamespace(item=SimpleNamespace(root=item))
job = json.load(sys.stdin)
for method in ("item/started", "item/completed"):
    relay(method, payload, job["capture_chars"])
print('{{"kind":"done"}}')
""")
    result = CodexAgentRunner().run("demo", task(), "", tmp_path, {})
    assert result.ok, result.error
    spans = {s.name: s.attributes for s in telemetry[1].get_finished_spans()
             if s.attributes.get("logfire.span_type") == "span"}
    tool = spans["execute_tool mcp__brave-search__brave_web_search"]
    agent = spans["invoke_agent Demo"]
    assert "PRIVATE" not in str(spans)
    if limit:
        assert json.loads(tool["tool.result_urls"]) == urls
        parts = [p for m in json.loads(agent["pydantic_ai.all_messages"]) for p in m["parts"]]
        response, = [p for p in parts if p["type"] == "tool_call_response"]
        assert response["result_urls"] == urls
        assert len(response["result"]) == limit
    else:
        assert "tool.result_urls" not in tool
        assert "pydantic_ai.all_messages" not in agent


@pytest.mark.parametrize("search", [False, True])
def test_native_search_is_opt_in(tmp_path, monkeypatch, search):
    mode = "live" if search else "disabled"
    mock_worker(monkeypatch, f"""
import json, sys
job = json.load(sys.stdin)
assert job["web_search"] == {mode!r}
assert not job["write"]
print('{{"kind":"done"}}')
""")
    grants = ["CodexReadOnly", "web_search"] if search else ["CodexReadOnly"]
    result = CodexAgentRunner().run("demo", task(tools=grants), "", tmp_path, {})
    assert result.ok, result.error


def test_codex_checks_mcp_operations_on_active_thread():
    from types import SimpleNamespace

    pytest.importorskip("openai_codex")
    from reginald.runners.codex_agent.worker import validate_mcp_operations

    class Client:
        def request(self, method, params, response_model):
            assert method == "mcpServerStatus/list"
            assert params == {"serverName": "local", "threadId": "t"}
            server = SimpleNamespace(name="local", tools_error=None,
                                     tools={"find": SimpleNamespace(name="find")})
            return SimpleNamespace(data=[server], next_cursor=None)

    validate_mcp_operations(Client(), "t", {"local": ["find", "*"]})
    with pytest.raises(ValueError, match="mcp__local__typo"):
        validate_mcp_operations(Client(), "t", {"local": ["typo", "*"]})
