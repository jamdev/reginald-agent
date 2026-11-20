"""Service-owned Codex login, separate from an operator's interactive profile."""

import os
import subprocess
from pathlib import Path

from ...domain.models import DoctorCheck


def credential_home() -> Path:
    return Path.home() / ".local/share/reginald/codex"


def check_codex_auth() -> DoctorCheck:
    name = "Codex auth"
    if os.environ.get("OPENAI_API_KEY"):
        return DoctorCheck(name=name, ok=True, detail="OPENAI_API_KEY set")
    try:
        from codex_cli_bin import bundled_codex_path

        status = subprocess.run(
            [bundled_codex_path(), "login", "status"],
            env={**os.environ, "CODEX_HOME": str(credential_home())},
            capture_output=True, timeout=10, check=False,
        )
    except (ImportError, OSError, subprocess.TimeoutExpired) as exc:
        return DoctorCheck(name=name, ok=False, detail=f"Cannot check Codex login: {exc}")
    ok = status.returncode == 0
    detail = ("Codex login present" if ok
              else "Set OPENAI_API_KEY or sign in to Codex as the service user")
    return DoctorCheck(name=name, ok=ok, detail=detail)


if __name__ == "__main__":
    from codex_cli_bin import bundled_codex_path

    home = credential_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.execve(bundled_codex_path(), ["codex", "login", "--device-auth"],
              {**os.environ, "CODEX_HOME": str(home)})
