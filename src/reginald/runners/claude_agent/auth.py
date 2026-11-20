"""Credential reporting for the Claude Agent SDK's bundled CLI."""

from __future__ import annotations

from collections.abc import Mapping

from ...domain.models import DoctorCheck

API_KEY_VAR = "ANTHROPIC_API_KEY"
OAUTH_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def effective_credential(environ: Mapping[str, str]) -> str | None:
    """The credential the CLI will authenticate with, or None if neither is
    set. API key wins when both are present, matching the CLI's precedence."""
    if environ.get(API_KEY_VAR):
        return API_KEY_VAR
    if environ.get(OAUTH_TOKEN_VAR):
        return OAUTH_TOKEN_VAR
    return None


def check_claude_auth(environ: Mapping[str, str]) -> DoctorCheck:
    credential = effective_credential(environ)
    if credential is None:
        detail = f"neither {API_KEY_VAR} nor {OAUTH_TOKEN_VAR} set"
    elif credential == API_KEY_VAR and environ.get(OAUTH_TOKEN_VAR):
        detail = f"{API_KEY_VAR} (takes precedence over {OAUTH_TOKEN_VAR})"
    else:
        detail = f"{credential} set"
    return DoctorCheck(name="Claude auth", ok=credential is not None, detail=detail)
