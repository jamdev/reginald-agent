"""Console email provider — the dev backend. Prints digests to stdout."""

from __future__ import annotations

import uuid

from .base import SendResult


class ConsoleProvider:
    def send(self, to: str, subject: str, html_body: str, text_body: str) -> SendResult:
        print(f"\n=== EMAIL (console provider) ===\nTo: {to}\nSubject: {subject}\n\n{text_body}\n=== END EMAIL ===\n")
        return SendResult(ok=True, provider_message_id=f"console-{uuid.uuid4().hex[:12]}")
