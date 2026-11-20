"""Prompt resolution and rendering.

Each task reads one Markdown note from the vault. Its default path is
`<prompts_dir>/<task-slug>.md`; the task can specify another with `prompt_file`.

Rendering replaces {{VARS}}: TODAY, PREV_RUN_DATE, DATE_PLUS_90,
DATE_MINUS_60, VAULT_ROOT, TOPIC, MANIFEST (pending callout items, supplied
by the identify-changes pre-step). It also appends a pool of freshly-minted UUIDv4s so
the agent never has to generate one itself (it has no shell — an unattended
run would otherwise burn turns flailing on denied `Bash uuidgen` calls before
finding a hack). Anything the agent doesn't use is caught by the deterministic
`backfill-uuids` post-step, so the pool size is an optimization, not a cliff.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import yaml

from .config import Config, TaskConfig

# Match only whole delimiter lines. A title or a code example may contain "---".
_NOTE_PROPERTIES = re.compile(r"\A---[ \t]*\n(.*?)^---[ \t]*(?:\n+|\Z)", re.MULTILINE | re.DOTALL)


class PromptNotFoundError(RuntimeError):
    pass


def resolve_prompt(config: Config, task_name: str, task: TaskConfig) -> str:
    relative = task.prompt_file or Path(config.prompts.prompts_dir) / f"{task_name}.md"
    path = config.vault_path / relative
    if not path.is_file():
        raise PromptNotFoundError(f"No prompt for task '{task_name}' at {path}")

    text = path.read_text(encoding="utf-8")
    header = _NOTE_PROPERTIES.match(text)
    if header:
        # Plain Markdown prompts can also start with a horizontal rule.
        try:
            properties = yaml.safe_load(header[1])
        except yaml.YAMLError:
            return text
        if isinstance(properties, dict):
            body = text[header.end():]
            if properties.get("type") == "task" and "requires" in properties:
                required = properties["requires"]
                if not isinstance(required, list) or not all(
                    isinstance(note, str) for note in required
                ):
                    raise ValueError(f"{path}: requires must be a list of vault note links")
                if required:
                    reading = "\n".join(f"- {note}" for note in required)
                    return f"Read these vault notes in order before starting:\n{reading}\n\n{body}"
            return body
    return text


def render_prompt(
    template: str,
    *,
    vault_root: Path,
    today: date | None = None,
    prev_run_date: str | None = None,
    topic: str | None = None,
    manifest: str | None = None,
    uuid_count: int = 25,
    new_uuids: list[str] | None = None,
) -> str:
    # Local civil date: "today" in a UK/EU regulatory brief means the operator's
    # calendar day, not a UTC instant. Injectable so tests can pin it.
    today = today or date.today()  # noqa: DTZ011
    variables = {
        "TODAY": today.isoformat(),
        "PREV_RUN_DATE": prev_run_date or (today - timedelta(days=1)).isoformat(),
        "DATE_PLUS_90": (today + timedelta(days=90)).isoformat(),
        "DATE_MINUS_60": (today - timedelta(days=60)).isoformat(),
        "VAULT_ROOT": str(Path(vault_root).resolve()),
        "TOPIC": topic or "",
        "MANIFEST": manifest or "(no pending items)",
    }
    rendered = template
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    if topic:
        rendered += (
            f"\n\nScope restriction for this run: operate ONLY on the topic "
            f"'{topic}' (folder Topics/{topic}/). Do not modify files under other topics."
        )
    # new_uuids injectable so tests are deterministic; count clamped to >= 1.
    pool = new_uuids if new_uuids is not None else [str(uuid4()) for _ in range(max(1, uuid_count))]
    rendered += _uuid_pool_block(pool)
    return rendered


def _uuid_pool_block(uuids: list[str]) -> str:
    listing = "\n".join(uuids)
    return (
        "\n\nFresh UUIDv4 values for this run. For any new note's `uuid` "
        "frontmatter field, use an **unused** value from this list — do NOT "
        "generate your own (you have no shell), and never reuse or edit an "
        "existing note's uuid. If you use every value below, simply leave the "
        "`uuid` field OFF any further new notes; it will be filled in "
        "automatically. Use each at most once:\n" + listing
    )
