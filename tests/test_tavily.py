"""Exercise the shipped stdio connection and Tavily's HTTP request contract offline."""

import asyncio
import shutil
from pathlib import Path

import httpx
import pytest

from reginald.core.config import load_config

mcp = pytest.importorskip("mcp")

# Representative calls include the input sent by the agent and the HTTP payload.
CASES = [
    ("search", {"query": "regulator"},
     {"query": "regulator", "search_depth": "basic", "include_raw_content": "markdown"}),
    ("extract", {"urls": ["https://example.org"]}, {"urls": ["https://example.org"], "format": "markdown"}),
    ("map", {"url": "https://example.org", "limit": 2}, {"url": "https://example.org", "limit": 2}),
    ("crawl", {"url": "https://example.org"}, {"url": "https://example.org", "format": "markdown"}),
    ("research", {"input": "Find sources", "model": "mini"}, {"input": "Find sources", "model": "mini"}),
    ("research_status", {"request_id": "task-123", "include_usage": True}, None),
    ("feedback", {"request_id": "old-search", "agent_score": 1}, {"request_id": "old-search", "agent_score": 1}),
    ("usage", {"project_id": "project-1"}, None),
    ("logs", {"filter_by_api_key": True}, {"filter_by_api_key": True}),
]


def test_shipped_tavily_process(tmp_path, monkeypatch):
    from mcp.client.stdio import stdio_client

    shipped = Path(__file__).resolve().parents[1] / "config" / "tools.d"
    shutil.copytree(shipped, tmp_path / "tools.d")
    cfg = load_config(config_dir=tmp_path, vault_path=tmp_path, db_path=tmp_path / "db")
    tool = cfg.tools["tavily"].for_vault(tmp_path)
    monkeypatch.setenv("TAVILY_API_KEY", "offline-test")
    params = mcp.StdioServerParameters(command=tool.command, args=tool.args, env=tool.resolved_env())

    async def exercise():
        async with stdio_client(params) as streams, mcp.ClientSession(*streams) as client:
            await client.initialize()
            names = {t.name for t in (await client.list_tools()).tools}
            assert names == {"tavily_" + name for name, _, _ in CASES}
            result = await client.call_tool("tavily_search", {})
            assert result.isError and "query" in result.content[0].text
            result = await client.call_tool("tavily_research_status", {"request_id": "../usage"})
            assert result.isError

    asyncio.run(exercise())


def test_tavily_endpoint_contracts():
    import json

    from mcp.shared.memory import create_connected_server_and_client_session

    from reginald.services.tavily import build_server

    sent = []

    def handle(req):
        sent.append(req)
        return httpx.Response(200, json={"request_id": "demo", "results": [{"url": "https://example.org"}]})

    async def exercise():
        async with httpx.AsyncClient(base_url="https://api.tavily.com", transport=httpx.MockTransport(handle)) as http:
            async with create_connected_server_and_client_session(build_server(http, "offline")) as client:
                for name, arguments, body in CASES:
                    result = await client.call_tool("tavily_" + name, arguments)
                    assert not result.isError, result.content
                    assert result.structuredContent["results"][0]["url"] == "https://example.org"
                    [req] = sent
                    sent.clear()
                    assert req.url.host == "api.tavily.com"
                    assert req.headers["Authorization"] == "Bearer offline"
                    if name == "research_status":
                        assert req.method == "GET" and req.url.path == "/research/task-123"
                        assert req.url.params == httpx.QueryParams(include_usage="true")
                    elif name == "usage":
                        assert req.method == "GET" and req.url.path == "/usage"
                        assert req.headers["X-Project-ID"] == "project-1"
                    else:
                        assert req.method == "POST" and req.url.path == "/" + name
                        assert json.loads(req.content) == body

                result = await client.call_tool("tavily_search", {
                    "query": "regulator", "search_depth": "advanced", "include_raw_content": False,
                })
                assert not result.isError
                [req] = sent
                assert json.loads(req.content)["include_raw_content"] is False
                assert json.loads(req.content)["search_depth"] == "advanced"

    asyncio.run(exercise())


@pytest.mark.parametrize(("status", "headers", "body", "expected", "error"), [
    (429, {"Retry-After": "20"}, '{"detail":{"error":"Rate limited"}}', "Retry-After: 20", True),
    (401, {}, '{"detail":{"error":"Invalid offline-test-key"}}', "Invalid [redacted]", True),
    (200, {"Content-Type": "text/event-stream"},
     'data: {"choices":[{"delta":{"content":"Report"}}]}\n\nevent: done\n\n', "Report", False),
    (200, {"Content-Type": "text/event-stream"},
     'data: {"object":"error","error":"Research failed"}\n\n', "Research failed", True),
    (200, {"Content-Type": "text/event-stream"}, 'data: {"part":1}\n\n', "completion event", True),
])
def test_research_stream_and_errors(status, headers, body, expected, error):
    from mcp.shared.memory import create_connected_server_and_client_session

    from reginald.services.tavily import build_server

    requests = []

    def handle(req):
        requests.append(req)
        return httpx.Response(status, headers=headers, text=body)

    async def exercise():
        async with httpx.AsyncClient(base_url="https://api.tavily.com", transport=httpx.MockTransport(handle)) as http:
            async with create_connected_server_and_client_session(build_server(http, "offline-test-key")) as client:
                result = await client.call_tool("tavily_research", {"input": "Find sources", "stream": True})
                assert result.isError == error
                assert expected in result.content[0].text
                assert "offline-test-key" not in result.content[0].text
                assert len(requests) == 1

    asyncio.run(exercise())
