import re
from datetime import date
from pathlib import Path

import pytest

from reginald.core.config import TaskConfig
from reginald.core.prompts import PromptNotFoundError, render_prompt, resolve_prompt

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def test_per_task_prompt_reads_only_note_body(config):
    config.prompts.prompts_dir = "Agent notes"
    path = config.vault_path / "Agent notes" / "research.md"
    path.parent.mkdir()
    body = "Check {{TODAY}}.\n\n---\n\nKeep this example:\n```text\nexample\n```\n"
    path.write_text('---\ntitle: "Research --- daily"\npublish: true\n---\n\n' + body)

    assert resolve_prompt(config, "research", TaskConfig(title="Research", runner="claude-agent", model="test")) == body


def test_missing_task_file_reports_path_even_with_an_old_prompt(config):
    agents = config.vault_path / "Setup/Agents"
    agents.mkdir(parents=True)
    (agents / "Orchestration prompts.md").write_text("## Research\n\n```\nStale prompt.\n```\n")

    with pytest.raises(PromptNotFoundError) as exc:
        resolve_prompt(config, "research", TaskConfig(title="Research", runner="claude-agent", model="test"))

    assert str(config.vault_path / "Setup/Tasks/research.md") in str(exc.value)


def test_explicit_prompt_file_is_the_only_source_for_task(config):
    default = config.vault_path / config.prompts.prompts_dir / "research.md"
    default.parent.mkdir(parents=True)
    default.write_text("Default prompt")
    task = TaskConfig(title="Research", runner="claude-agent", model="test", prompt_file="custom.md")
    with pytest.raises(PromptNotFoundError) as exc:
        resolve_prompt(config, "research", task)
    assert str(config.vault_path / "custom.md") in str(exc.value)

    (config.vault_path / "custom.md").write_text("Custom prompt {{TODAY}}")
    assert resolve_prompt(config, "research", task) == "Custom prompt {{TODAY}}"


def test_task_note_supplies_ordered_required_reading_and_body(config):
    path = config.vault_path / "Research.md"
    path.write_text(
        '---\ntype: task\nuuid: note-id\nrequires:\n'
        '  - "[[Setup/Persona/Persona File]]"\n  - "[[Escalations/Index]]"\n---\n'
        'Work from this manifest:\n{{MANIFEST}}'
    )
    prompt = resolve_prompt(config, "research", TaskConfig(title="Research", runner="claude-agent", model="test", prompt_file=path.name))
    persona = prompt.index("[[Setup/Persona/Persona File]]")
    protocol = prompt.index("[[Escalations/Index]]")
    assert persona < protocol < prompt.index("Work from this manifest:")
    assert "{{MANIFEST}}" in prompt and "note-id" not in prompt


def test_render_variables_and_topic_scope():
    rendered = render_prompt(
        "Run for {{TODAY}}, prev {{PREV_RUN_DATE}}, until {{DATE_PLUS_90}}.",
        vault_root="/vault",
        today=date(2026, 7, 2),
        prev_run_date="2026-07-01",
        topic="Payments and E-money",
    )
    assert "Run for 2026-07-02, prev 2026-07-01, until 2026-09-30." in rendered
    assert "ONLY on the topic 'Payments and E-money'" in rendered


def test_render_vault_root_from_a_relative_checkout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rendered = render_prompt("Read {{VAULT_ROOT}}/Setup", vault_root=Path("vault"))
    assert rendered.splitlines()[0] == f"Read {tmp_path.resolve()}/vault/Setup"


# -- fresh-UUID pool ---------------------------------------------------------


def _pool_uuids(rendered: str) -> list[str]:
    """The bare UUIDs listed at the end of a rendered prompt's pool block."""
    return [ln.strip() for ln in rendered.splitlines() if UUID_RE.match(ln.strip())]


def test_prompt_appends_a_pool_of_valid_uuidv4s():
    rendered = render_prompt("Do the work.", vault_root="/vault", uuid_count=25)
    pool = _pool_uuids(rendered)
    assert len(pool) == 25
    assert len(set(pool)) == 25              # all distinct
    assert "generate your own" in rendered   # tells the agent not to make its own
    assert "leave the `uuid` field OFF" in rendered  # graceful exhaustion, no flailing


def test_pool_size_is_configurable():
    assert len(_pool_uuids(render_prompt("x", vault_root="/v", uuid_count=3))) == 3
    assert len(_pool_uuids(render_prompt("x", vault_root="/v", uuid_count=50))) == 50


def test_pool_never_empty_even_at_zero():
    # A degenerate count still yields one uuid rather than an empty list.
    assert len(_pool_uuids(render_prompt("x", vault_root="/v", uuid_count=0))) == 1


def test_injected_uuids_are_used_verbatim():
    rendered = render_prompt(
        "x", vault_root="/v", new_uuids=["11111111-1111-4111-8111-111111111111"]
    )
    assert "11111111-1111-4111-8111-111111111111" in rendered
    assert len(_pool_uuids(rendered)) == 1
