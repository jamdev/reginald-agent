"""Tool execution spans shared by the Claude and Codex SDK adapters."""

from collections.abc import Iterable
from contextlib import ExitStack
from contextvars import copy_context

from ..core.observability import span


class ToolSpans:
    """Parallel calls are siblings. End each in the context that opened it."""

    def __init__(self, names: Iterable[str] = ()):
        # Capture the agent before streaming starts so calls are siblings of chat spans.
        self.parent = copy_context()
        self.names = set(names)
        self.active = {}
        self.names_by_id: dict[str, str] = {}

    def set_definitions(self, current) -> None:
        if current is not None and self.names:
            definitions = [{"type": "function", "name": name} for name in sorted(self.names)]
            current.set_attribute("gen_ai.tool.definitions", definitions)

    def start(self, call_id: str, name: str, *, turn: int) -> None:
        if call_id in self.names_by_id:
            return
        self.names_by_id[call_id] = name
        self.names.add(name)
        context = self.parent.copy()
        stack = ExitStack()
        attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                      "gen_ai.tool.call.id": call_id, "turn": turn}
        scope = span(f"execute_tool {name}", log_when_disabled=False, **attributes)
        current = context.run(stack.enter_context, scope)
        self.active[call_id] = (context, stack, current)

    def finish(self, call_id: str, status: str | None, *, result_urls: list[str] | None = None) -> None:
        active = self.active.pop(call_id, None)
        if active:
            context, stack, current = active
            try:
                if current is not None and result_urls:
                    current.set_attribute("tool.result_urls", result_urls)
                if current is not None and status:
                    current.set_attribute("tool.status", status)
                    if status in {"failed", "declined", "interrupted"}:
                        current.set_level("error")
            finally:
                context.run(stack.close)

    def close(self) -> None:
        for call_id in list(self.active):
            self.finish(call_id, "interrupted")
