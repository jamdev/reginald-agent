# Repository layout

This is the Reginald application repository. The Obsidian vault lives in a separate Git repository. A task's YAML definition lives here in `config/tasks.d/`, while its prompt note lives in the vault under `Setup/Tasks/`. [Tasks and prompts](USAGE.md#tasks-and-prompts)

```text
src/reginald/
  cli.py               Typer commands and the --json interface
  core/                Configuration loading, AppContext, prompts, Git, locking and logging
    context.py         Connects services and registers the steps a task can run
  services/            Task execution, vault upkeep, change detection, email and publishing
    runs.py            Runs a task's steps and agent under the vault lock
    identify_changes.py  Scans Git commits for content events and callout work
    outbox.py          Builds and delivers subscriber digests
  runners/             TaskRunner protocol and Claude, Codex and Pydantic AI adapters
  publisher/           Interprets vault diffs, frontmatter and callouts
  email/               EmailProvider protocol and the Postmark and console providers
  templates/           HTML and plain-text email digest templates
  domain/              Pydantic models shared by services and the CLI
  db/                  SQLAlchemy schema and SQLite sessions
  web/                 Reserved for a future web interface

config/
  base.yaml-example    Template for shared settings
  dev.yaml-example     Template for development overrides
  prod.yaml-example    Template for production overrides
  tasks.d/             One YAML file per task
  tools.d/             One YAML mapping per MCP server (.conf, .yaml or .yml)

deploy/                Host installation, deploy keys, updates, CLI wrapper and releases
  systemd/             Service units and their timers
migrations/            Alembic environment and database schema revisions
tests/                 Offline pytest suite
docs/                  Installation, operation and contributor guides
pyproject.toml         Package metadata, dependencies and CLI entry point
uv.lock                Locked dependency versions used by deployments
Makefile               Local setup, test and lint commands
```

Each checkout has its own Git-ignored `config/base.yaml`, `config/dev.yaml` and `config/prod.yaml`, copied from the templates. Reginald reads the shared settings first, then the task and tool definitions, then the selected environment's overrides. Each task or tool takes its name from its filename. [Configuration guide](USAGE.md#configuration)
