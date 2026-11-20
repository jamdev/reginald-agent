"""Load task and tool definitions through the same entry point as the CLI."""

import pytest
import yaml

from reginald.core.config import load_config


@pytest.fixture
def config_dir(tmp_path):
    (tmp_path / "base.yaml").write_text("vault_path: ./vault\ndb_path: ./test.db\n")
    (tmp_path / "tools.d").mkdir()
    (tmp_path / "tasks.d").mkdir()
    return tmp_path


def test_discovers_definitions_by_filename(config_dir):
    (config_dir / "tools.d/catalog.yml").write_text("command: catalog\n")
    (config_dir / "tasks.d/lookup.yaml").write_text(
        "title: Look up a record\nrunner: claude-agent\nmodel: test\ntools: [Read, mcp__catalog__lookup]\n"
    )
    (config_dir / "tasks.d/archive.yml").write_text("title: Archive\nrunner: claude-agent\nmodel: test\n")
    (config_dir / "tasks.d/notes.md").write_text("Operator notes")

    cfg = load_config("dev", config_dir)

    assert set(cfg.tasks) == {"lookup", "archive"}
    assert cfg.tasks["lookup"].tools == ["Read", "mcp__catalog__lookup"]
    assert cfg.tools["catalog"].command == "catalog"


@pytest.mark.parametrize(("env", "turns", "args"), [
    ("dev", 3, []),
    ("prod", 8, ["--prod"]),
])
def test_merges_base_then_definitions_then_selected_environment(config_dir, env, turns, args):
    with (config_dir / "base.yaml").open("a") as base:
        base.write(
            "tools:\n  catalog: {command: old, env: [CATALOG_KEY]}\n"
            "tasks:\n  lookup: {title: Old title, runner: claude-agent, model: test, pre: [identify-changes]}\n  inline: {title: Inline, runner: claude-agent, model: test}\n"
        )
    (config_dir / "tools.d/catalog.yaml").write_text("command: catalog\nargs: [--common]\n")
    (config_dir / "tasks.d/lookup.yml").write_text("title: Lookup\nmax_turns: 20\n")
    (config_dir / "dev.yaml").write_text(
        "tools:\n  catalog: {args: []}\ntasks:\n  lookup: {max_turns: 3}\n"
    )
    (config_dir / "prod.yaml").write_text(
        "tools:\n  catalog: {args: [--prod]}\ntasks:\n  lookup: {max_turns: 8}\n"
    )

    cfg = load_config(env, config_dir)

    assert cfg.tasks["inline"].title == "Inline"
    assert cfg.tasks["lookup"].title == "Lookup"
    assert cfg.tasks["lookup"].pre == ["identify-changes"]
    assert cfg.tasks["lookup"].max_turns == turns
    assert cfg.tools["catalog"].command == "catalog"
    assert cfg.tools["catalog"].env == ["CATALOG_KEY"]
    assert cfg.tools["catalog"].args == args


def test_duplicate_definition_name_is_reported(config_dir):
    for filename in ("catalog.yaml", "catalog.yml"):
        (config_dir / "tools.d" / filename).write_text("command: catalog\n")
    with pytest.raises(ValueError, match=r"(?i)duplicate.*catalog"):
        load_config("dev", config_dir)


@pytest.mark.parametrize(("content", "error"), [
    ("- one\n- two\n", ValueError),
    ("title: [\n", yaml.YAMLError),
])
def test_invalid_definition_names_its_file(config_dir, content, error):
    (config_dir / "tasks.d/broken.yaml").write_text(content)
    with pytest.raises(error, match=r"broken\.yaml"):
        load_config("dev", config_dir)


@pytest.mark.parametrize("field", ["runner", "model"])
def test_task_requires_explicit_routing(config_dir, field):
    from pydantic import ValidationError

    task = {"title": "Demo", "runner": "claude-agent", "model": "claude-sonnet-5"}
    del task[field]
    (config_dir / "tasks.d/demo.yaml").write_text(yaml.safe_dump(task))
    with pytest.raises(ValidationError, match=field):
        load_config("dev", config_dir)
