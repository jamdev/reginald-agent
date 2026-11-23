"""Bounded OpenTelemetry message parts for SDKs that expose their own event stream."""

from pydantic_core import PydanticSerializationError, to_json


def excerpt(value, limit: int) -> str:
    """Count Unicode characters, including JSON punctuation for structured tool data."""
    if not limit:
        return ""
    if not isinstance(value, str):
        try:
            value = to_json(value, fallback=str).decode()
        except (ValueError, PydanticSerializationError):
            # An opaque or binary tool result must not fail the agent run.
            return ""
    return value[:limit]


class Conversation:
    def __init__(self, prompt: str, limit: int):
        self.limit = limit
        self.messages: list[dict] = []
        self.tool_names: dict[str, str] = {}
        if limit:
            self.add("user", [self.text(prompt)])

    def text(self, value: str) -> dict:
        return {"type": "text", "content": excerpt(value, self.limit)}

    def add(self, role: str, parts: list[dict], *, message: dict | None = None) -> dict | None:
        if not self.limit or not parts:
            return message
        for part in parts:
            if part["type"] == "tool_call":
                self.tool_names[part["id"]] = part["name"]
            elif part["type"] == "tool_call_response" and part["id"] in self.tool_names:
                part["name"] = self.tool_names[part["id"]]
        if message is None:
            message = {"role": role}
            self.messages.append(message)
        message["parts"] = parts
        return message

    def finish(self, agent, output: str) -> None:
        if not self.limit or agent is None:
            return
        agent.set_attribute("gen_ai.input.messages", self.messages[:1])
        if output:
            part = self.text(output)
            final = {"role": "assistant", "parts": [part]}
            agent.set_attribute("gen_ai.output.messages", [final])
            agent.set_attribute("final_result", part["content"])
            last = self.messages[-1]
            if last["role"] != "assistant" or part not in last["parts"]:
                self.messages.append(final)
        agent.set_attribute("pydantic_ai.all_messages", self.messages)
