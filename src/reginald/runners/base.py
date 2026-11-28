"""TaskRunner protocol — the seam between orchestration and cognition."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..core.config import TaskConfig, ToolConfig
from ..domain.models import DoctorCheck


def check_sdk(module: str) -> DoctorCheck:
    installed = importlib.util.find_spec(module) is not None
    return DoctorCheck(name=f"module {module}", ok=installed,
                       detail="installed" if installed else "Install the reginald[agents] extra")


def validate_tool_grants(source: str, requested: Iterable[str], advertised: Iterable[str]) -> None:
    names = set(advertised)
    missing = []
    for grant in requested:
        if grant.endswith("__*"):
            if any(name.startswith(grant[:-1]) for name in names):
                continue
        elif grant in names:
            continue
        missing.append(grant)
    if missing:
        raise ValueError(f"{source} does not advertise requested tools: {', '.join(sorted(missing))}")


@dataclass
class RunnerResult:
    ok: bool
    num_turns: int | None = None
    cost_usd: float | None = None
    output: str = ""
    error: str | None = None
    # OpenTelemetry usage suffixes. Cache reads and writes are subsets of input_tokens.
    token_usage: dict[str, int] = field(default_factory=dict)
    tool_calling_turns: int | None = None


@runtime_checkable
class TaskRunner(Protocol):
    def run(
        self,
        task_name: str,
        task: TaskConfig,
        prompt: str,
        vault_path: Path,
        tools: dict[str, ToolConfig],
    ) -> RunnerResult: ...
