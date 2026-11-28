"""Shared stdio MCP server for the Tavily API."""

import asyncio
import os
from pathlib import Path

import httpx
import yaml
from httpx_sse import aconnect_sse
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


def tool_definitions() -> dict[str, types.Tool]:
    with Path(__file__).with_name("tavily_inputs.yaml").open(encoding="utf-8") as file:
        definitions = yaml.safe_load(file)
    tools = {}
    for operation, schema in definitions.items():
        name = f"tavily_{operation}"
        tools[name] = types.Tool(
            name=name, description=schema.pop("description"),
            inputSchema={"type": "object", "additionalProperties": False, **schema},
        )
    return tools


def check_response(response: httpx.Response, api_key: str) -> None:
    if response.is_success:
        return
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("error")
    message = f"Tavily HTTP {response.status_code}: {detail or response.reason_phrase}"
    if retry_after := response.headers.get("Retry-After"):
        message += f" (Retry-After: {retry_after})"
    raise ValueError(message.replace(api_key, "[redacted]"))


async def request(client: httpx.AsyncClient, api_key: str, operation: str, arguments: dict) -> dict:
    """Called after MCP has validated the arguments against the selected tool."""
    headers = {"Authorization": f"Bearer {api_key}"}
    payload = dict(arguments)
    if operation == "research_status":
        request_id = payload.pop("request_id")
        response = await client.get(f"/research/{request_id}", params=payload, headers=headers)
    elif operation == "usage":
        if project := payload.get("project_id"):
            headers["X-Project-ID"] = project
        response = await client.get("/usage", headers=headers)
    elif operation == "research" and payload.get("stream"):
        async with aconnect_sse(client, "POST", "/research", headers=headers, json=payload) as stream:
            if not stream.response.is_success:
                await stream.response.aread()
                check_response(stream.response, api_key)
            events = []
            async for event in stream.aiter_sse():
                if event.event == "error":
                    raise ValueError(f"Tavily research stream failed: {event.data}".replace(api_key, "[redacted]"))
                if event.event == "done" or event.data == "[DONE]":
                    return {"events": events}
                data = event.json()
                if isinstance(data, dict) and data.get("object") == "error":
                    raise ValueError(f"Tavily research stream failed: {data.get('error')}".replace(api_key, "[redacted]"))
                events.append(data)
        raise ValueError("Tavily research stream ended before its completion event")
    else:
        if operation == "feedback" and not (payload.get("request_id") or payload.get("session_id")):
            raise ValueError("Tavily feedback requires a request_id or session_id")
        if operation == "search":
            if not payload.get("auto_parameters"):
                payload.setdefault("search_depth", "basic")
            payload.setdefault("include_raw_content", "markdown")
        elif operation in {"extract", "crawl"}:
            payload.setdefault("format", "markdown")
        response = await client.post(f"/{operation}", json=payload, headers=headers)
    check_response(response, api_key)
    return response.json()


def build_server(client: httpx.AsyncClient, api_key: str) -> Server:
    tools = tool_definitions()
    server = Server("tavily")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return list(tools.values())

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> dict:
        if name not in tools:
            raise ValueError(f"Unknown Tavily tool: {name}")
        try:
            return await request(client, api_key, name.removeprefix("tavily_"), arguments)
        except httpx.RequestError as exc:
            raise ValueError(f"Tavily {name}: {type(exc).__name__} contacting api.tavily.com") from exc

    return server


async def main() -> None:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("Set TAVILY_API_KEY for the Tavily MCP server")
    async with httpx.AsyncClient(base_url="https://api.tavily.com", timeout=httpx.Timeout(180, connect=10)) as client:
        server = build_server(client, api_key)
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
