"""Vault path -> published site URL.

Obsidian Publish serves a note at its vault-relative path with the `.md`
suffix dropped, so `Topics/Payments/Briefs/2026-08-10 - PSD3 - EU.md` becomes
`<base>/Topics/Payments/Briefs/2026-08-10%20-%20PSD3%20-%20EU`.
"""

from __future__ import annotations

from urllib.parse import quote


def note_url(base_url: str, file_path: str) -> str | None:
    """Public URL for a vault-relative note path, or None if unpublished.

    Returns None when no base_url is configured, so callers can fall back to
    an unlinked title rather than emitting a broken href.
    """
    if not base_url:
        return None
    return f"{base_url.rstrip('/')}/{quote(file_path.removesuffix('.md'))}"
