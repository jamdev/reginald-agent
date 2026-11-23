"""Apply the conversation budget before Pydantic AI writes any message attributes."""

import json
from copy import copy

from pydantic_ai.models.instrumented import InstrumentationSettings

from ..conversation import excerpt


class ConversationInstrumentation(InstrumentationSettings):
    def __init__(self, config, limit: int):
        # Keep automatic copies (final_result, tool spans and validation data)
        # content-free. Only the message formatter below may include excerpts.
        super().__init__(tracer_provider=config.get_tracer_provider(),
                         meter_provider=config.get_meter_provider(),
                         include_content=False, include_binary_content=False,
                         include_model_request_parameters=False)
        self.limit = limit

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
                for key in ("content", "result", "arguments", "uri", "file_id"):
                    if key in part:
                        if self.limit:
                            part[key] = excerpt(part[key], self.limit)
                        else:
                            del part[key]
        return formatted
