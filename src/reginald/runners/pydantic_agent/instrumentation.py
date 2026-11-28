"""Apply the conversation budget before Pydantic AI writes any message attributes."""

import json
from copy import copy

from opentelemetry.trace import get_current_span
from pydantic_ai.models.instrumented import InstrumentationSettings

from ..conversation import BRAVE_WEB_SEARCH, brave_result_urls, excerpt


class ConversationInstrumentation(InstrumentationSettings):
    def __init__(self, config, limit: int):
        # Keep automatic copies (final_result, tool spans and validation data)
        # content-free. Add Brave URLs explicitly when capture is enabled.
        super().__init__(tracer_provider=config.get_tracer_provider(),
                         meter_provider=config.get_meter_provider(),
                         include_content=False, include_binary_content=False,
                         include_model_request_parameters=False)
        self.limit = limit
        self.urls_by_call: dict[str, list[str]] = {}

    async def trace_mcp_call(self, ctx, call_tool, name, args):
        result = await call_tool(name, args)
        if name == BRAVE_WEB_SEARCH and (urls := brave_result_urls(result)):
            get_current_span().set_attribute("tool.result_urls", urls)
            self.urls_by_call[ctx.tool_call_id] = urls
        return result

    def system_instructions_attributes(self, instructions):
        if self.limit and instructions:
            parts = [{"type": "text", "content": instructions[:self.limit]}]
            return {"gen_ai.system_instructions": json.dumps(parts, ensure_ascii=False)}
        return {}

    def messages_to_otel_messages(self, messages):
        formatter = copy(self)
        formatter.include_content = bool(self.limit)
        formatted = InstrumentationSettings.messages_to_otel_messages(formatter, messages)
        for message in formatted:
            for part in message["parts"]:
                if part["type"] == "tool_call_response" and (urls := self.urls_by_call.get(part["id"])):
                    part["result_urls"] = urls
                for key in ("content", "result", "arguments", "uri", "file_id"):
                    if key in part:
                        if self.limit:
                            part[key] = excerpt(part[key], self.limit)
                        else:
                            del part[key]
        return formatted
