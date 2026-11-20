"""Postmark email provider (transactional API, broadcast message stream)."""

from __future__ import annotations

import os

import httpx

from .base import SendResult

API_URL = "https://api.postmarkapp.com/email"
TOKEN_VAR = "POSTMARK_SERVER_TOKEN"


class PostmarkProvider:
    def __init__(self, from_address: str, message_stream: str = "broadcast", reply_to: str | None = None):
        self.from_address = from_address
        self.message_stream = message_stream
        self.reply_to = reply_to

    def send(self, to: str, subject: str, html_body: str, text_body: str) -> SendResult:
        token = os.environ.get(TOKEN_VAR)
        if not token:
            return SendResult(ok=False, error=f"{TOKEN_VAR} not set")
        payload = {
            "From": self.from_address,
            "To": to,
            "Subject": subject,
            "HtmlBody": html_body,
            "TextBody": text_body,
            "MessageStream": self.message_stream,
        }
        if self.reply_to:
            payload["ReplyTo"] = self.reply_to
        try:
            response = httpx.post(
                API_URL,
                json=payload,
                headers={"X-Postmark-Server-Token": token, "Accept": "application/json"},
                timeout=30,
            )
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"postmark request failed: {exc}")
        if response.status_code == 200:
            return SendResult(ok=True, provider_message_id=response.json().get("MessageID"))
        return SendResult(ok=False, error=f"postmark {response.status_code}: {response.text[:300]}")
