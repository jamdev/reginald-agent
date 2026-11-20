"""EmailProvider protocol + send result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class SendResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None


@runtime_checkable
class EmailProvider(Protocol):
    def send(self, to: str, subject: str, html_body: str, text_body: str) -> SendResult: ...
