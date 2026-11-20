"""Read base.yaml, discover tools.d and tasks.d, then apply the environment overlay.

Secrets (ANTHROPIC_API_KEY, POSTMARK_SERVER_TOKEN, BRAVE_API_KEY, ...) are NOT
in YAML — they come from the process environment (systemd EnvironmentFile= in
prod, shell env in dev).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import AnyHttpUrl, BaseModel, Field, field_validator, model_validator

ENV_VAR = "REGINALD_ENV"
CONFIG_DIR_VAR = "REGINALD_CONFIG_DIR"


class GitConfig(BaseModel):
    commit_per_run: bool = True
    push: bool = True
    remote: str = "origin"
    branch: str = "main"
    # Passed to git per-invocation. The service user has no ~/.gitconfig,
    # and relying on host config would break on every new host.
    author_name: str = "Reginald"
    author_email: str = "reginald@localhost"


class SiteConfig(BaseModel):
    enabled: bool = True
    # `reginald vault publish` uses this engine. `reginald vault ob` forwards
    # arguments directly to obsidian-headless.
    engine: Literal["ob"] = "ob"
    ob_binary: str = "ob"                   # obsidian-headless binary
    extra_args: list[str] = Field(default_factory=list)
    # Public address of the published vault, used to link notes in digests.
    # Empty means "not published": the digest falls back to unlinked titles.
    base_url: str = ""


class EmailConfig(BaseModel):
    provider: Literal["postmark", "console"] = "console"
    from_address: str = "reginald@example.org"
    message_stream: str = "broadcast"
    reply_to: str | None = None
    allowlist: list[str] = Field(default_factory=list)


class LogfireConfig(BaseModel):
    enabled: bool = False
    environment: str = "dev"
    # Non-default (e.g. EU-region or self-hosted) Logfire instance. Empty means
    # the SDK's default endpoint. Set per instance in YAML, never hardcoded in
    # code — the token in $LOGFIRE_TOKEN must belong to the same instance.
    base_url: str = ""


class LoggingConfig(BaseModel):
    """Console/journal logging. Distinct from logfire, which ships traces.

    $REGINALD_LOG_LEVEL overrides the level from the host's YAML settings.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    agent_turns: bool = True  # one line per agent tool call, so runs are visible


class PromptsConfig(BaseModel):
    prompts_dir: str = "Setup/Tasks"
    # How many freshly-minted UUIDv4s to inject into each rendered prompt so
    # the agent never generates one itself. The default applies to every task;
    # a work-sized task (action-edits) overrides it per run from its manifest.
    uuid_pool_size: int = 25


class BudgetConfig(BaseModel):
    monthly_limit_usd: float = 100.0


class ToolConfig(BaseModel):
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    url: AnyHttpUrl | None = None

    @model_validator(mode="after")
    def _require_endpoint(self) -> Self:
        if self.transport == "http" and self.url is None:
            raise ValueError("HTTP MCP tools require a url")
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("stdio MCP tools require a command")
        return self

    def resolved_env(self) -> dict[str, str]:
        """Materialise the named environment variables from the process env."""
        return {name: os.environ[name] for name in self.env if name in os.environ}

    def for_vault(self, vault_path: Path) -> Self:
        """Bind whole-argument placeholders for a local MCP process."""
        return self.model_copy(update={
            "command": sys.executable if self.command == "{python}" else self.command,
            "args": [str(vault_path.resolve()) if arg == "{vault_path}" else arg for arg in self.args],
        })


class TaskConfig(BaseModel):
    title: str
    runner: Literal["claude-agent", "pydantic_agent", "codex_agent"]
    model: str = Field(min_length=1, pattern=r"^\S+$")
    tools: list[str] = Field(default_factory=list)
    max_turns: int | None = Field(default=None, gt=0)
    timeout_seconds: int | None = Field(default=None, gt=0)
    prompt_file: str | None = None  # relative to vault; default resolution applies
    # Named deterministic steps from the step registry, run inside the lock:
    # `pre` after pull/before the prompt (may build {{MANIFEST}} or skip the
    # agent entirely); `post` after the agent, before the run commit.
    pre: list[str] = Field(default_factory=list)
    post: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_tools(cls, data: Any) -> Any:
        if isinstance(data, dict) and "allowed_tools" in data:
            raise ValueError(
                "Move allowed_tools entries into the task's tools list and replace bare "
                "MCP server names with mcp__<server>__<operation>. Remove allowed_tools."
            )
        return data

    @model_validator(mode="after")
    def _validate_runner_settings(self) -> Self:
        codex_modes = {"CodexReadOnly", "CodexWorkspaceWrite"}
        if self.runner == "codex_agent":
            if self.max_turns is not None or self.timeout_seconds is None:
                raise ValueError("Codex tasks require timeout_seconds and do not support max_turns")
            if len(codex_modes.intersection(self.tools)) != 1:
                raise ValueError("Select one native Codex tool mode: CodexReadOnly or CodexWorkspaceWrite")
        else:
            if self.timeout_seconds is not None:
                raise ValueError("timeout_seconds applies only to codex_agent")
            if self.max_turns is None:
                self.max_turns = 60
        for tool in self.tools:
            if tool in codex_modes:
                if self.runner != "codex_agent":
                    raise ValueError(f"{tool} requires codex_agent")
                continue
            if tool.startswith("mcp__"):
                server, _, operation = tool.removeprefix("mcp__").partition("__")
                if re.fullmatch(r"[\w-]+", server, re.ASCII) and re.fullmatch(
                    r"[\w.-]+|\*", operation, re.ASCII
                ):
                    continue
            elif re.fullmatch(r"[A-Z][A-Za-z0-9_]*(?:\(.*\))?", tool):
                if self.runner != "claude-agent":
                    raise ValueError(f"{tool!r} is a Claude built-in and requires claude-agent")
                continue
            raise ValueError(
                f"Invalid tool {tool!r} for {self.runner}. Use a supported native tool or mcp__<server>__<operation>."
            )
        return self

    @property
    def builtin_tools(self) -> list[str]:
        """Claude's tool menu takes bare names even when a permission rule is scoped."""
        return list(dict.fromkeys(
            tool.partition("(")[0] for tool in self.tools if not tool.startswith("mcp__")
        ))

    @property
    def mcp_operations(self) -> dict[str, set[str]]:
        """Group the task's selected MCP operations by registered server."""
        operations: dict[str, set[str]] = {}
        for tool in self.tools:
            if tool.startswith("mcp__"):
                _, server, operation = tool.split("__", 2)
                operations.setdefault(server, set()).add(operation)
        return operations


class IdentityConfig(BaseModel):
    # Reginald's callout @handle. Unaddressed [!todo]/[!question] items also
    # default to Reginald — it is the resident agent.
    handle: str = "reginald"


class Config(BaseModel):
    env: Literal["dev", "prod"] = "dev"
    vault_path: Path
    db_path: Path
    git: GitConfig = Field(default_factory=GitConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    site: SiteConfig = Field(default_factory=SiteConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    logfire: LogfireConfig = Field(default_factory=LogfireConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    tools: dict[str, ToolConfig] = Field(default_factory=dict)
    tasks: dict[str, TaskConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_mcp_servers(self) -> Self:
        for name, task in self.tasks.items():
            for server in task.mcp_operations:
                if server not in self.tools:
                    raise ValueError(f"Task {name!r} references unknown MCP server {server!r}")
        return self

    @property
    def lock_path(self) -> Path:
        """The single-writer run lock. Everything that writes the vault takes
        it, so it must resolve identically for every caller — hence here, and
        not rebuilt at each use."""
        return self.db_path.parent / "reginald.lock"

    @field_validator("vault_path", "db_path", mode="before")
    @classmethod
    def _expand(cls, v: Any) -> Any:
        if isinstance(v, str):
            return Path(v).expanduser()
        return v


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_mapping(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        data = yaml.safe_load(stream)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _load_definitions(directory: Path) -> dict[str, dict]:
    """Each YAML file defines one entry. Its filename supplies the registry key."""
    definitions: dict[str, dict] = {}
    if not directory.exists():
        return definitions
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in {".yaml", ".yml"} or not path.is_file():
            continue
        if path.stem in definitions:
            raise ValueError(f"Duplicate definition {path.stem!r} in {directory}. Use one YAML file per name.")
        definitions[path.stem] = _read_mapping(path)
    return definitions


def default_config_dir() -> Path:
    """Locate the config directory: $REGINALD_CONFIG_DIR, else <repo>/config."""
    if CONFIG_DIR_VAR in os.environ:
        return Path(os.environ[CONFIG_DIR_VAR]).expanduser()
    # installed alongside a checkout: src/reginald/core/config.py -> repo root
    repo_root = Path(__file__).resolve().parents[3]
    candidate = repo_root / "config"
    if candidate.is_dir():
        return candidate
    return Path("/etc/reginald/config")


def load_config(
    env: str | None = None,
    config_dir: Path | None = None,
    vault_path: Path | None = None,
    db_path: Path | None = None,
) -> Config:
    env = env or os.environ.get(ENV_VAR, "dev")
    config_dir = config_dir or default_config_dir()

    base_file = config_dir / "base.yaml"
    overlay_file = config_dir / f"{env}.yaml"

    data = _read_mapping(base_file) if base_file.exists() else {}
    for registry in ("tools", "tasks"):
        definitions = _load_definitions(config_dir / f"{registry}.d")
        if definitions:
            data = _deep_merge(data, {registry: definitions})
    # Apply the selected environment last so it can override individual fields
    # of an automatically discovered definition before the full config is validated.
    if overlay_file.exists():
        data = _deep_merge(data, _read_mapping(overlay_file))

    data["env"] = env
    if vault_path is not None:
        data["vault_path"] = str(vault_path)
    if db_path is not None:
        data["db_path"] = str(db_path)
    return Config.model_validate(data)
