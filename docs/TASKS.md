# Tasks

A **task** is a named job Reginald can do in the vault, such as answering a contributor's questions or checking a cited source. Its YAML file selects how the job runs. Its Markdown prompt in the vault tells the agent what to do. Each execution of the task, whether started from the command line or by a timer, is a **run**.

Each task brings together a runner and any steps it needs.

- The **runner** is the code that gives the agent its rendered prompt, model and permitted tools, then returns the result to Reginald. A task selects one runner. [Runner guide](RUNNERS.md) · [Task tools](TOOLS.md)
- A **step** is a named Python operation Reginald calls automatically at a point chosen by the task. The optional `pre` and `post` lists put steps before or after the agent. [Step guide](STEPS.md)

## How the action-edits task runs

The `action-edits` task answers questions and to-dos that contributors leave in vault notes. Its definition connects the prompt, runner and steps. [Shipped task definition](../config/tasks.d/action-edits.yaml)

```yaml
title: Action edits
prompt_file: Setup/Tasks/Action edits.md
runner: claude-agent
model: claude-sonnet-5
tools: [Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, mcp__brave-search__brave_web_search]
max_turns: 80
pre: [identify-changes, escalations-sync]
post: [callouts-verify, escalations-sync, normalize-frontmatter, backfill-uuids, set-publish]
```

1. Reginald runs `identify-changes` to collect pending callouts and `escalations-sync` to reconcile escalation notes. It inserts the list of callouts into the prompt at `{{MANIFEST}}`.
2. When there is work, the `claude-agent` runner starts Claude with that prompt. Claude can choose from the listed tools as it reads and answers the notes. An empty list of callouts lets Reginald skip the agent entirely.
3. After the agent succeeds or is skipped, Reginald runs the `post` steps in order. For example, `callouts-verify` rereads the notes to check which callouts received a reply. The remaining steps reconcile escalations and tidy note metadata before the run's commit. [Registered steps](STEPS.md#registered-steps)

## Finding and changing a task

Reginald loads each `.yaml` or `.yml` file directly inside `config/tasks.d/`. The filename supplies the task name, so `action-edits.yaml` registers `action-edits`. To change its settings for one environment, override fields under `tasks.action-edits` in `config/dev.yaml` or `config/prod.yaml`. An override of `tools`, `pre` or `post` replaces that whole list. [Task definitions](../config/tasks.d/) · [Configuration order](USAGE.md#configuration)

To change what the agent is asked to do, edit the vault note named by `prompt_file`. To change when the task runs, edit its systemd timer. [Prompt lookup](USAGE.md#prompt-resolution) · [Task schedules](TIMERS.md)

After setting up the development environment, run this from the repository root to see one row per task with its effective runner, model and tools. [Local development setup](INSTALL.md#local-development-mac)

```bash
uv run reginald --env dev task list
```
