# Reginald — usage guide

This guide covers operating Reginald. For setup, see [Installation](INSTALL.md). For releases and host updates, see [Deployment](DEPLOY.md).

- [Mental model](#mental-model)
- [The run pipeline](#the-run-pipeline)
- [Configuration](#configuration)
- [Tasks and prompts](#tasks-and-prompts)
- [Topics](#topics)
- [Content events](#content-events)
- [Identifying vault changes](#identifying-vault-changes)
- [Escalations and callouts](#escalations-and-callouts)
- [Subscribers](#subscribers)
- [Outbox and email](#outbox-and-email)
- [Contributing to the vault](#contributing-to-the-vault)
- [CLI reference](#cli-reference)
- [Scheduling](#scheduling)
- [Runbook](#runbook)
- [Known gaps](#known-gaps)

---

## Mental model

Reginald turns **a shared Obsidian vault under git** into **subscriber emails and a published site**. It is one contributor to that vault, alongside humans (and, later, other agents) who work in their own clones. Reginald runs Claude agents on a schedule and checks each new commit on `main`, whoever wrote it.

Concepts, in the order they appear:

| Concept | What it is |
|---|---|
| **Vault** | A shared Obsidian vault that is also a git repo. The single source of truth — reginald's database holds no content, only bookkeeping. |
| **Contributor** | Anyone who commits to the vault, identified by their git author. Reginald is one; you are another. |
| **Topic** | A folder under `Topics/`. The unit subscribers subscribe to. |
| **Task** | A named job (`research-new-developments`) defined in config, with a prompt stored in the vault. [Task guide](TASKS.md) |
| **Run** | One execution of a task. Recorded in the DB with cost, turn count, and the git commits either side. |
| **Step** | A named Python operation a task selects to run before or after its agent. [Task steps](STEPS.md) |
| **Change detection** | The `identify-changes` operation reads each new Git commit and records content events and callout work items. A saved cursor records how far it has read. |
| **Content event** | Something subscriber-facing a commit changed, derived from `git diff` and attributed to its author — not from anyone's claims. |
| **Callout item** | A `[!todo]`/`[!question]` left in a note — the work queue Reginald's `action-edits` task drains. |
| **Escalation** | A record under `Escalations/` needing human/agent attention, worked through callouts. |
| **Subscriber** | A team member's email address with a topic selection and new/updated preferences. |
| **Delivery** | One digest email to one subscriber, written to the DB *before* sending. |

**Change detection uses Git history.** Reginald compares each commit with its parent and maps changed paths onto events, attributing them to the commit's git author. Nobody — agent or human — can cause an email by *claiming* something; only by actually committing a file that matches a known path pattern, whose body substantively changed.

```
  contributors push          reginald task run <task>
  (own git identities)       (Agent/runner writes files)
          │                          │
          └──────────► main ◄────────┘
                        │
                        ▼
                  IDENTIFY CHANGES
                    ╱        ╲
        content_events        callout_items
             │                     │
             ▼                     ▼
      outbox ──► Postmark    action-edits run ──► nested replies
             └─► ob publish ──► Obsidian Publish
```

---

## The run pipeline

What `reginald task run <task>` does, in order. Source: `src/reginald/services/runs.py`.

**Before the lock**

1. **Validate the task** exists in the config registry.

**Under the run lock** (`<db_path parent>/reginald.lock`, so two tasks can never touch the vault at once)

2. **Check the budget.** Queued and immediate runs check the month's recorded spend after acquiring the lock. A run at or over `budget.monthly_limit_usd` stops before invoking the agent.
3. **Salvage a dirty tree.** If the host working tree is dirty, everything is committed as `reginald: salvage uncommitted changes` before anything else — so the failure path (step 8) can't destroy it. Host edits are an anti-pattern; see [Contributing to the vault](#contributing-to-the-vault).
4. **Sync with the remote (fetch + rebase).** Reginald rebases its own unpushed commits onto `origin/main`. A rebase *conflict* fails the run loudly and never auto-resolves; an offline failure is logged and non-fatal.
5. **Snapshot `HEAD`** as `pre_commit`, and open a run row with status `running`.
6. **Run the `pre` steps** (from the task's `pre:` config). For `action-edits` these are `identify-changes` (walk what arrived, build the `{{MANIFEST}}` of pending callout items) and `escalations-sync`. A dry run skips the pre-steps. If a pre-step reports **no work**, the remaining steps still run and the task uses zero model turns.
7. **Resolve and render the prompt** (see [Tasks and prompts](#tasks-and-prompts)), substituting `{{MANIFEST}}`. With `--dry-run`, stop here.
8. **Invoke the configured runner** (unless skipped). The task selects its runner, model, tools and work limit. [Runner configuration](RUNNERS.md)
9. **On failure: `git reset --hard` back to `pre_commit`.** A failed run leaves no partial edits in the vault. Status becomes `failed`.
10. **Run the `post` steps** before the commit so their repairs land alongside the agent's edits. [Registered steps and task configuration](STEPS.md#registered-steps)
11. **Commit and push** (`git.commit_per_run`, `git.push`). Push is rebase-and-retry-once against a contributor race; a final failure is a warning, and the next run carries the commits.
12. **Identify changes.** Reginald records attributed content events for everything the run (and any pull) brought in. A `Reginald-Run` trailer in each task commit identifies its run and starting commit, including after a rebase. Reginald applies the run's topic filter to that commit. Contributor commits follow their own topic routing. The scan then **publishes the site** when it finds new commits and publishing is enabled. A publish failure sets status `publish_failed`.
13. **Save the final run status**, then report it to Logfire when enabled. See [Task completion alerts](LOGGING.md#task-completion-alerts) for failed and missed runs.

### Run statuses

| Status | Meaning |
|---|---|
| `queued` | Requested via `task add-to-queue`, awaiting `task process-queue`. |
| `running` | In progress (or crashed mid-run — see [Runbook](#runbook)). |
| `success` | Completed; site published if the vault changed. |
| `publish_failed` | Agent worked and changes were committed, but `ob publish` failed. |
| `failed` | Runner failed; vault was reset to `pre_commit`. |

Note that `success` and `publish_failed` both count as "the task ran" for the purposes of `PREV_RUN_DATE`; `failed` does not.

---

## Configuration

For a new checkout, copy the three `config/*.yaml-example` templates to `config/base.yaml`, `config/dev.yaml` and `config/prod.yaml`. Git ignores these three files, so you can edit them on the host. During a deploy, `update.sh` lists any templates that changed and reminds you to check the documentation. Apply any new settings you need to your local copies. [Copy commands](INSTALL.md#copy-the-configuration-templates) · [Updating an existing host](DEPLOY.md#updating)

Reginald reads configuration in this order on each invocation.

1. `config/base.yaml` supplies shared defaults.
2. Every `.yaml` or `.yml` file directly inside `config/tools.d/` or `config/tasks.d/` registers one MCP server or one task. The filename without its extension supplies the name. Put the definition's fields at the top of the file. Use a unique filename stem within each directory. Existing inline definitions in `base.yaml` also load, and the discovered file takes precedence for any fields both define.
3. `config/dev.yaml` or `config/prod.yaml` overrides fields for the selected environment. Use the filename stem as the key under `tools` or `tasks`.

If you override the triage task’s tool list, include `mcp__inbox__move_file` after updating. [Filing Inbox notes](#filing-inbox-notes)

Mappings merge by key across all three layers. A later list replaces the entire earlier list, including when the override is `[]`. For example, this development override changes only the turn limit for inbox triage.

```yaml
# config/dev.yaml
tasks:
  triage-inbox:
    max_turns: 15
```

After merging the YAML, Reginald applies any `--vault` and `--db` paths supplied on the command line. From the project checkout, `uv run reginald --env dev config show` prints the effective configuration, including the discovered tools, tasks and overrides.

Environment selection, highest precedence first:

1. `--env dev|prod` on the command line
2. `$REGINALD_ENV`
3. `dev`

The config *directory* is `$REGINALD_CONFIG_DIR` if set, else `<repo>/config`, else `/etc/reginald/config`.

### Reference

| Key | Default | Meaning |
|---|---|---|
| `vault_path` | — | Vault root. `~` is expanded. |
| `db_path` | — | SQLite file. Its parent also holds the run lock. |
| `git.commit_per_run` | `true` | Commit the vault after each run. With `false`, no commit means no diff and therefore **no events and no emails**. |
| `git.push` | `true` | Push after committing, and pull before running. |
| `git.remote` / `git.branch` | `origin` / `main` | Remote and branch. |
| `git.author_name` / `git.author_email` | `Reginald` / `reginald@localhost` | Commit identity, passed to git per invocation. The service user has no `~/.gitconfig`, so this cannot be left to the host. Set a real address to attribute commits on GitHub. Change detection classifies a commit with a matching author email as `reginald` and other commits as `contributor`. |
| `identity.handle` | `reginald` | Reginald's callout `@handle`. Callouts addressed to it, or unaddressed, become its work; others are queued for the named contributor. |
| `site.enabled` | `true` | Whether to publish the site after a run. |
| `site.engine` | `ob` | Default publishing engine `reginald vault publish` uses. `ob` (obsidian-headless) today; more front-ends slot in here. |
| `site.ob_binary` | `ob` | obsidian-headless binary. |
| `site.extra_args` | `[]` | Extra args appended to `ob publish`. |
| `site.base_url` | `https://regdev.agenticgc.uk` | Public address of the published vault. Digest emails link each note to `<base_url>/<path without .md>`. Empty sends unlinked titles. |
| `email.provider` | `console` | `postmark` sends; `console` prints to stdout. |
| `email.from_address` | — | Sender. Must be a verified Postmark sender. |
| `email.message_stream` | `broadcast` | Postmark stream. |
| `email.reply_to` | `null` | Optional reply-to. |
| `email.allowlist` | `[]` | **Empty means no restriction.** Non-empty means only these addresses receive mail; everyone else is marked failed with a note. |
| `logfire.enabled` | `false` | Send traces, logs, task completions and usage metrics. Production uses `LOGFIRE_TOKEN` from the host environment. |
| `logfire.capture_chars` | `100` | Maximum characters per conversation field exported by any runner. Use a positive integer or YAML `false` to disable content capture and keep operational telemetry. The development template sets `1000`. [Inspect the conversation](LOGGING.md#read-the-agent-conversation) |
| `logfire.environment` | `dev` | Environment tag. |
| `logging.level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. Overridden by `$REGINALD_LOG_LEVEL`. |
| `logging.agent_turns` | `true` | One `INFO` line per agent tool call. Turn this off and a run logs only start and finish. |
| `prompts.prompts_dir` | `Setup/Tasks` | Per-task prompt directory, relative to the vault. |
| `budget.monthly_limit_usd` | `100.0` | Runs are refused once the month's spend reaches this. |
| `tools.<name>` | — | MCP server connection from a file in `config/tools.d/`. |
| `tasks.<name>` | — | Task definition from a file in `config/tasks.d/`. |
| `tasks.<name>.tools` | `[]` | Tools the task can call unattended. |

### Secrets

Never in YAML. Read from the process environment:

| Variable | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | Claude Agent SDK runs with API billing and Pydantic AI tasks using an `anthropic:` model. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Agent SDK runs using a Max/Pro plan. |
| `OPENAI_API_KEY` | Codex runs with API billing and Pydantic AI tasks using the OpenAI API directly. Codex also accepts a service-user login. |
| `OPENROUTER_API_KEY` | Pydantic AI tasks using an `openrouter:` model. |
| `POSTMARK_SERVER_TOKEN` | Sending email with `provider: postmark`. |
| `BRAVE_API_KEY` | Brave web search in `research-new-developments`, `update-trackers` and `action-edits`. Claude’s built-in `WebSearch` uses the Claude credential. |
| `LOGFIRE_TOKEN` | Shipping traces when `logfire.enabled`. Must match `logfire.base_url`'s instance. |

In production systemd loads these from `/etc/reginald/env` (mode `600`) via `EnvironmentFile=`. In development, export them in your shell.

### Agent authentication

Each runner has its own authentication. `reginald doctor` checks the runners selected by tasks in the current environment. [Checking runner credentials](RUNNERS.md#checking-runner-credentials)

For the Claude Agent SDK, the bundled Claude Code CLI selects its credential from the process environment.

- `ANTHROPIC_API_KEY` set → Console pay-as-you-go.
- `CLAUDE_CODE_OAUTH_TOKEN` set → your Max/Pro plan.
- **Both set → the API key wins** (the CLI's own precedence).

With a Claude task configured, the `Claude auth` row names the chosen credential and fails if both variables are empty or absent. The runner also logs the chosen variable name at the start of each run.

**Getting a subscription token.** Run `claude setup-token` once — it opens a browser for OAuth and prints a long-lived token. On a headless host, run it on a machine with a browser and put the token in `/etc/reginald/env`:

```
CLAUDE_CODE_OAUTH_TOKEN=<token from `claude setup-token`>
# ...and leave ANTHROPIC_API_KEY unset, or it takes precedence.
```

**Two things to know before pointing the unattended host at a subscription:**

- A subscription **token expires** and nothing refreshes it — when it lapses, scheduled runs start failing until you regenerate it. An API key does not expire, which makes it the safer choice for a box nobody watches.
- Subscription usage counts against your plan's limits (including weekly caps), and Claude Code subscriptions are intended for interactive developer use; running an unattended service against a personal plan is a grey area.

### The dev safety rails

`config/dev.yaml` exists to make it impossible to do real damage while testing:

```yaml
git:   { push: false }          # never pushes to the real vault remote
site:  { enabled: false }       # never republishes the live site
email: { provider: console, allowlist: [you@example.org] }
```

Point `vault_path` at a **scratch clone**, not your real vault. Runs commit locally even in dev — `push: false` stops them leaving the machine, but a run against your real vault still rewrites files.

---

## Tasks and prompts

Every task selects a runner and model and can name steps to run before and after the agent. The task guide follows a complete example. The runner reference covers tool grants, limits and authentication. [Task guide](TASKS.md) · [Runner reference](RUNNERS.md)

The file `config/tasks.d/research-new-developments.yaml` registers the `research-new-developments` task for both environments.

```yaml
title: Research new developments
prompt_file: Setup/Tasks/Research new developments.md
runner: claude-agent
model: claude-sonnet-5
tools: [Read, Write, Edit, Glob, Grep, Agent, WebFetch, WebSearch, mcp__brave-search__brave_web_search]
max_turns: 120
post: [normalize-frontmatter, backfill-uuids, set-publish]
```

List each tool the task may call once under `tools`. Claude can choose its built-in `WebSearch` or Brave's `brave_web_search` in this example. Reginald connects the Brave server and pre-approves that operation. Calls that would require a permission prompt are denied immediately. Research and tracker updates also list `Agent` so Claude can delegate parts of those tasks. [Claude Agent SDK permissions](https://code.claude.com/docs/en/agent-sdk/permissions)

Use `mcp__<server>__<operation>` for an external tool. Use `mcp__<server>__*` only when the task should have every operation on that server. `config/tools.d/brave-search.yaml` supplies the connection shared by tasks that select Brave.

```yaml
transport: stdio                      # stdio | http
command: npx
args: ["-y", "@brave/brave-search-mcp-server"]
env: [BRAVE_API_KEY]                  # names materialised from the process env
```

All three runners support local stdio servers and remote Streamable HTTP servers. For an HTTP server, set `transport: http` and supply its absolute HTTP or HTTPS endpoint in `url`. The stdio `env` list supplies environment variables to a local child process. [Add an MCP server and verify a task grant](MCPSERVERS.md#add-another-mcp-server)

**Upgrading custom task settings.** Move the Claude tool names from `allowed_tools` into the task's `tools` list and remove `allowed_tools`. Include any Claude built-ins the task used implicitly, such as `Agent`. Replace a bare server entry such as `brave-search` with the operations the task needs, such as `mcp__brave-search__brave_web_search`. From the project checkout, check the effective dev configuration with `uv run reginald --env dev task list`. Each task's row shows its tool list.

### Use OpenRouter with the Pydantic AI runner

Set `OPENROUTER_API_KEY` in the repository’s `.env` for local development. On the installed host, use `sudoedit /etc/reginald/env`. When a configured task uses OpenRouter, the `Pydantic auth (openrouter)` row in `reginald doctor` reports whether the key is present and lists the affected tasks. [Load local credentials](INSTALL.md#local-development-secrets) · [Pydantic AI OpenRouter setup](https://pydantic.dev/docs/ai/models/openrouter/)

For a task that needs web search, set these fields in its versioned task entry. The example uses the existing `brave-search` server, which also needs `BRAVE_API_KEY`.

```yaml
runner: pydantic_agent
model: openrouter:anthropic/claude-sonnet-4.6
tools: [mcp__brave-search__brave_web_search]
```

Choose an OpenRouter model that supports tool calling. Pydantic AI sends the selected operations’ tool definitions with the model request. When the model asks for a tool, Reginald calls the MCP server and returns its result to the model. Claude Code’s built-in `Read`, `Write` and `Edit` belong to the Claude runner. A Pydantic AI task that reads or edits notes needs an MCP server with the corresponding vault operations. The task’s final text is returned to the orchestrator, which commits file changes made during the run. [OpenRouter tool calling](https://openrouter.ai/docs/guides/features/tool-calling)

For model requests, tool executions, token counts and cost estimates, see [Pydantic AI tasks in Logfire](LOGGING.md#inspect-a-pydantic-ai-task-in-logfire).

### Prompt resolution

Each scheduled task reads its instructions directly from the vault note selected by `prompt_file`. For example, edit `Setup/Tasks/Research new developments.md` to change the research task. In a note with `type: task`, list the required vault notes as Obsidian links in the YAML `requires` list, in reading order. Reginald includes that reading list and the Markdown body in the prompt, fills in the runtime variables, and supplies the UUID pool.

For a custom task without `prompt_file`, the filename is the task key plus `.md` in `prompts.prompts_dir`. A missing note raises `PromptNotFoundError` with its expected path. When upgrading from separate prompt files, move their runtime variables and task-specific instructions into the corresponding task notes. Update the vault and Reginald together between scheduled runs.

### Template variables

Replaced literally as `{{NAME}}`:

| Variable | Value |
|---|---|
| `{{TODAY}}` | Today's date, ISO. Local civil date, not UTC. |
| `{{PREV_RUN_DATE}}` | Date of the last `success`/`publish_failed` run of *this* task. Falls back to `last_run_updater` in the vault's `Setup/Metadata/Metadata.md`, then to yesterday. |
| `{{DATE_PLUS_90}}` | Today + 90 days. |
| `{{DATE_MINUS_60}}` | Today − 60 days. |
| `{{VAULT_ROOT}}` | Absolute vault path. |
| `{{MANIFEST}}` | Pending callout work items supplied to Action edits. |
| `{{TOPIC}}` | The `--topic` value, or empty. |

When `--topic` is given, a scope restriction is also **appended** to the prompt instructing the agent to operate only within `Topics/<topic>/`. This is an instruction, not a sandbox — it is enforced by the model's compliance, whereas the event filter that follows it is enforced by code.

Preview any prompt without spending anything:

```bash
reginald task run research-new-developments --dry-run
```

---

## Topics

The **canonical set of topics** is the numbered list in `Setup/Taxonomy/Taxonomy
- Regulatory topics.md` — those names are the only valid primary or `secondary_topics` values. Each topic is realised as a folder under `Topics/`, created on demand: the first time a canonical topic is filed to (or cross-referenced by a brief), the orchestrator materialises `Topics/<name>/` with a skeleton `Index.md`. A topic reference that is **not** on the canonical list and has no folder is **skipped** (logged as a warning), so a typo or invented name (e.g. a `secondary_topics` entry missing a comma) never becomes a phantom topic. `reginald topics sync` reconciles the database with the folders.

> **`vault_path` must be the vault root, and the vault root must be the repo root.** Everything hangs off it: `Topics/`, `Setup/Tasks/`, `Setup/Logs/`, and `git -C <vault_path>`. If the repo wraps the vault in a subfolder, `topics sync` reports `synced 0 active topic(s)` because `<vault_path>/Topics` does not exist.
>
> Pointing `vault_path` at the subfolder looks like a fix and is a trap: git reports diff paths relative to the **repo** root, so they arrive as `Subfolder/Topics/…` and never match the event patterns in [Content events](#content-events). Runs would succeed, commit, publish — and silently never raise an event or send an email. Flatten the repo so the vault is at its root instead.

Sync behaviour:

- **New folder** → new topic row.
- **Folder matched by frontmatter `uuid`** → renames are tracked. If a topic's index note carries a `uuid`, renaming the folder updates the existing row rather than orphaning subscriptions.
- **Vanished folder** → the topic is **deactivated, never deleted**, because historical content events still reference it.

Run `topics sync` after adding or renaming any topic folder. Topics are otherwise created on demand from the canonical taxonomy list (above); a stale phantom row from a pre-canonical typo is deactivated by the next sync (no folder).

`__operational__` is a reserved internal topic, held inactive. Events filed against it (run reports under `Setup/Logs/`) are deliberately excluded from every digest, so operational noise never reaches subscribers.

---

## Content events

Reginald checks each commit for changes to the following paths.

| Path pattern | Event kind |
|---|---|
| `Topics/<topic>/Briefs/<name>.md` | `brief_created` (added) or `brief_updated` (modified) |
| `Topics/<topic>/Materials/<name>.md` **whose path contains `Key dates digest`** | `digest` |
| `Escalations/<name>.md` **when added** | `escalation` |
| `Escalations/Resolved/**`, `Escalations/Index.md` | **nothing** |
| `Setup/Logs/**/<name>.md` | **nothing** — run logs are bookkeeping, not content |

Everything else a contributor touches is invisible to subscribers.

### "Escalation" has one meaning

An escalation is **a note raised in `Escalations/`** — nothing else in the system produces one. In particular a brief's `flag:` frontmatter does **not**: it is a marker for humans reading the vault and raises no event of its own.

That used to be untrue, and it was the source of a long-running bug. `flag:` carries two unrelated things — a prose escalation reason from research/triage (`flag: "enforcement action naming a firm — 2026-06-25"`) and the source verification task's housekeeping vocabulary (`source-changed`, `source-unavailable`, `source-missing`, `verify-*`) meaning *our link* broke, not that the regulation moved. Any truthy `flag:` minted an `escalation` event, so a link-check run mailed subscribers ESCALATION for a dead URL — ten of them in the 2026-08-17 run alone, on notes whose body never changed.

Only the **raise** notifies. Resolving an escalation moves the file to `Escalations/Resolved/` and edits `status:`; both are bookkeeping and raise nothing, as does `escalations-sync` assigning `ESC-NNN`.

Escalations are delivered by the `topic:` in their frontmatter, like all other content. An escalation with **no `topic:`, or one outside the taxonomy**, has no audience of its own, so rather than being dropped it is filed against the `__unrouted__` sentinel topic and delivered to **every all-topics subscriber** (`mode: all`). Per-topic subscribers don't receive it — they haven't opted into everything. `__unrouted__` is routing machinery, not a topic: it is inactive, never appears in a subscriber's effective topics, and never renders as a tag, so an unrouted escalation simply shows no topic in the email.

### The substantive-change rule

A **modified** brief raises an `updated` event only if its **body, with callout blocks stripped, actually changed** between the commit and its parent. This is the linchpin of the multi-author design, and it means:

- **Callout dialogue never emails anyone.** A `[!todo]` or a nested reply is not content — the conversation happens in the note, but subscribers hear only about the underlying facts.
- **Frontmatter-only edits raise nothing** — uuid backfill, `last_verified` stamps, changelog housekeeping.
- **Pure renames raise nothing.** Reorganising folders doesn't spam "new brief" per moved file; the note's `uuid` keeps its identity stable.
- **A real correction by anyone — human or Reginald — raises `updated`** and is mailed, attributed to the commit's author.

There is no longer an exception for `flag:` — it is frontmatter, so it falls under "frontmatter-only edits raise nothing" like everything else.

### Attribution and the one-liner

Every event records its author (name, email, kind: `reginald` or `contributor`). Every `updated` event carries a **summary** — the one-liner subscribers read — chosen deterministically, no model:

1. the `note` of a `changelog` entry the commit added to that brief, else
2. the commit subject, else
3. `"updated"`.

New briefs use their `title`. Multiple undelivered updates to one note **coalesce** into a single digest line (latest summary + edit count).

### Digest layout

A digest has **three sections, not one per topic**:

| Section | Contents |
|---|---|
| **Escalations** | `escalation` events — notes raised in `Escalations/`, with `ESC-NNN` and priority |
| **New** | notes appearing for the first time (`brief_created`, a fresh `digest`) |
| **Changed** | edits to notes already reported (`brief_updated`) |

A note created *and* edited inside one digest window counts as New.

**Items carry their topics as tags rather than living under a topic heading.** This is what stops a cross-filed note from being repeated: a brief with `secondary_topics` has one event row per topic — that fan-out is what routes it to a subscriber of *either* topic — and those rows coalesce back into a single line tagged `[AI and Algorithmic Systems · Payments and E-money]`.

Tags describe the note, not the subscription: you see every topic a note is filed under, including ones you don't follow. Edit counts are counted by distinct commit, so a two-topic note edited once reads as one edit, not two.

### Per-kind delivery preferences

`notify_new`, `notify_updated` and `notify_escalations` (all on by default) gate which kinds a subscriber receives — the Changed section is governed by `notify_updated`. Escalations deliver by default because they are the team's business, but `notify_escalations off` lets a raised item reach only the subscribers who own it while everyone else still gets ordinary new/changed content.

### Other behaviours

- **Deletions never raise events.** A removed brief does not email anyone.
- **Malformed frontmatter does not drop the event** — metadata is treated as empty and the event still records.
- **Most `Materials/` files raise nothing** — only `Key dates digest` paths.
- Per-topic runs (`--topic`) filter events to that topic.
- Run logs under `Setup/Logs/` raise **nothing** (nesting included); the skip is per-path, so a log committed alongside a brief doesn't suppress it.

```bash
reginald vault list-events --limit 20
reginald vault list-events --topic "Payments and E-money" --since 2026-08-01
```

---

## Identifying vault changes

Reginald compares each new commit on `main` with its parent, attributes changes to the Git author and records content events and callout work items. The command, timer and task pipeline share the same saved position in Git history. [Change detection service](../src/reginald/services/identify_changes.py)

Reginald checks for changes at four points.

- **Before the `action-edits` agent runs.** Its `identify-changes` pre-step walks the synced vault and prepares the manifest of pending callouts.
- **After the commit stage of a task run.** Each non-dry run that reaches this point scans for changes, including the task's own commit.
- **On its own timer.** `reginald-identify-changes.timer` runs daily at 11:30, ahead of the weekday noon outbox drain.
- **By hand.** Run `reginald vault identify-changes` to record changes immediately. A dry run previews the commits already in the local checkout.

Properties you can rely on:

- **Deterministic and free.** No model is ever invoked.
- **Idempotent.** The events natural key and the callout content-hash ledger make re-walks no-ops, so overlapping triggers are safe.
- **Cursor-bounded.** The `identify-changes` row in `cursors` stores the last commit scanned. The first invocation without `--dry-run` sets it to `HEAD`, so processing begins with the next commit. A failed agent run resets the vault working tree but never the queue or cursor — detected work survives.
- **Publishes.** When a scan processes new commits and site publishing is enabled, Reginald republishes the vault. This lets a contributor's push reach the site between task runs.

```bash
reginald vault identify-changes --dry-run  # Preview local changes without advancing the cursor
reginald vault identify-changes            # Pull, record changes and publish if enabled
```

Contributor events appear under the synthetic task name `identify-changes` in `reginald task run-history`.

---

## Filing Inbox notes

Commit material for filing under `Inbox/` and push it to the vault. The `triage-inbox` task reads the file, chooses a destination and calls `mcp__inbox__move_file`. It can file a brief under `Topics/<topic>/Briefs/`, a source under `Sources/<topic>/` or a template under `Templates/`. An existing destination leaves both files in place. After a successful move, triage can edit the filed note and update the indexes.

To request work, commit an Inbox note containing just a callout.

```markdown
> [!todo] @reginald Review the FCA consultations page
> Check https://www.fca.org.uk/publications/consultation-papers for deadlines relevant to our topics and reply here.
```

The ordinary change scan queues this request for `action-edits`. Triage leaves a callout-only note in the Inbox while any todo or question in its thread awaits a nested reply. Frontmatter alone does not make it ready for filing. [Callout protocol](#the-callout-protocol)

In existing vaults, update `Setup/Tasks/Triage inbox.md` to skip pending request-only notes, use the move operation before editing the destination and update indexes only after success. [Task configuration](TASKS.md)

## Escalations and callouts

Escalations are records needing attention, and callouts are how humans and agents work them (and any note) together. The vault scan detects changes to both. The `action-edits` task works through pending callouts for Reginald.

### The callout protocol

Five Obsidian callout types. `[!todo]` and `[!question]` create work **for the other party**; the rest are reports:

| Callout | Creates work? |
|---|---|
| `[!todo]`, `[!question]` | yes — for whoever it's addressed to (`@handle`, else Reginald) |
| `[!success]`, `[!failure]`, `[!warning]` | no |

Replies **nest** one blockquote level under the callout they answer, so the thread structure *is* the machine state — an item counts as actioned only when a nested reply actually exists (verified post-run, never taken on trust). Editing a callout's text makes it a new item; deleting it (or its file) marks the item `not-actioned`. Callouts in `Archive/`, `Setup/Logs/`, `.obsidian/` and fenced code blocks are ignored.

### Escalation files

One file per escalation under `Escalations/`, resolved ones under `Escalations/Resolved/`. Frontmatter: `status` (one of `pending`, `actioned-by-agent`, `actioned-by-human`, `ignored`), `raised_date`, `status_changed_date`, `number`, `uuid`, `raised_by`. The invariant **`pending` ⟺ `Escalations/` root; anything else ⟺ `Resolved/`** is enforced by `escalations-sync` in both directions:

- change the `status`, and sync moves the file;
- move the file to `Resolved/` by hand, and sync sets `actioned-by-human` (moving *is* acting).

Sync also backfills uuids, stamps `status_changed_date` on transitions the author didn't date, assigns `number` (never in the filename — filenames are date-slug, so two clones can't collide), and adopts contributor-raised escalations into the DB.

```bash
reginald escalations list --status pending
reginald escalations show ESC-037
reginald escalations set-status ESC-037 actioned-by-human   # writes frontmatter, moves, mirrors
reginald escalations sync                                   # the repair matrix
reginald escalations items --state pending                  # the callout work queue
reginald escalations items --for reginald
```

### Migrating the legacy log

`reginald escalations migrate` splits the old `Setup/Escalations.md` into per-file records once, leaving a tombstone behind (old links keep working). It is idempotent — run on an already-migrated vault it just re-seeds the DB, which is how a fresh host DB catches up. Preview with `--dry-run`.

---

## Subscribers

Each subscriber has a **mode**:

- **`selected`** — receives exactly the topics listed.
- **`all`** — receives every topic, *including ones created later*, minus any explicit excludes.

```bash
# Everything, minus one topic
reginald subscriber add ops@example.org --all-topics --exclude "Crypto and MiCA"

# Two topics only
reginald subscriber add analyst@example.org \
  -t "Payments and E-money" -t "Consumer Duty"

# Inspect and adjust
reginald subscriber topics analyst@example.org
reginald subscriber topics analyst@example.org --include "Crypto and MiCA"
reginald subscriber topics analyst@example.org --mode all
reginald subscriber topics analyst@example.org --clear "Crypto and MiCA"
```

`--include`/`--exclude` set an explicit row for that topic; `--clear` removes the row, returning the topic to whatever the mode implies. The resulting **effective topics** are what digests actually use — always check them with `reginald subscriber topics <email>` after a change.

`reginald subscriber remove` **deactivates** rather than deletes, preserving delivery history. Use `subscriber list --all` to see inactive rows.

---

## Outbox and email

Sending uses the **transactional outbox pattern**: the `Delivery` row is written to the database *before* the provider is called, so a crash mid-send can never lose the record of what was owed.

`reginald outbox drain` does two things:

1. **Build.** For each active subscriber, gather content events that are in their effective topics and not already in `delivered_events`. If there are none, skip. Otherwise write one pending `Delivery` plus a `DeliveredEvent` row per event — which is what makes delivery **idempotent**: an event already marked delivered to a subscriber is never re-sent, no matter how often you drain.
2. **Send.** Attempt every `pending` or `failed` delivery with fewer than **5 attempts**, marking each `sent` or `failed`.

```bash
reginald outbox preview you@example.org   # render, send nothing
reginald outbox drain --dry-run           # build deliveries, send nothing
reginald outbox drain                     # real send
reginald outbox list --status failed
reginald outbox retry 42                  # one delivery, or bare to retry all failed
```

**`drain --dry-run` writes to the database, but does not consume events.** It runs the build step, so `Delivery` and `DeliveredEvent` rows are created — then returns before marking anything sent. The delivery stays `pending`, and the next real `drain` picks up *that same delivery* and sends it, with all its events intact. Dry-running repeatedly does not pile up duplicate deliveries either.

Only `preview` is genuinely read-only. Use it when you want to see a digest without touching anything.

Operators manage subscribers through the CLI. Reginald's preview shows the email body before delivery. Postmark appends its own unsubscribe link when sending through the configured Broadcast stream. [Postmark Broadcast footers](https://postmarkapp.com/support/article/1208-how-to-add-an-unsubscribe-link)

With Logfire enabled, each drain or retry has a command trace. Expand it to see the outcome of each delivery by ID. [Trace standalone commands and email](LOGGING.md#trace-standalone-commands-and-email)

Once a delivery reaches 5 attempts, `drain` ignores it permanently. `outbox retry` is the only way to revive it — worth checking after any provider outage.

The allowlist is a **send-time** rail, not a build-time one: if `email.allowlist` is non-empty and a subscriber is not on it, the delivery is marked failed with `recipient not in allowlist (dev safety rail)`. The digest was still built and its events still counted as delivered.

---

## Contributing to the vault

The vault is a **shared git repository**, and Reginald is one contributor among several. You — and anyone else on the knowledge-base team — work in your own clone under your own git identity, and push to `main`. Reginald notices what you pushed, attributes it to you, and mails the team.

### The flow

```bash
git clone <vault-remote> agentic-gc
cd agentic-gc
# ... edit in Obsidian, as yourself ...
git commit -am "Correct the CP26/21 deadline to 14 Aug"
git push
```

Once your commit reaches the host checkout, the next scan processes it. The daily timer can pick it up between task runs.

- a **substantive change** to a brief becomes an **updated** email to subscribers of that topic;
- a **new** brief becomes a **new** email;
- **callout dialogue, frontmatter tidy-ups and pure file moves mail nobody** — they are not content (see [Callouts](#callouts-working-with-reginald) below).

Two habits make you a good citizen of the vault:

- **Add a changelog entry when you change a brief.** The vault's notes carry a `changelog:` list in frontmatter; the `note:` of the entry you add *becomes the one-line summary* in the email. Say what you changed and the team reads it at a glance. Without one, the email falls back to your commit subject — so write good commit subjects too.
- **Commit as yourself.** Your git author name is what appears (name only, never your email) beside your updates in the digest. Set `user.name` / `user.email` in your clone.

### Callouts: working with Reginald

Leave Reginald (or a teammate) a task inside any note using an Obsidian [callout](https://help.obsidian.md/callouts). Five types carry the protocol:

| Callout | Means | Creates work? |
|---|---|---|
| `[!todo]` | do this | yes, for whoever it's addressed to |
| `[!question]` | answer this | yes |
| `[!success]` | done — here's what happened | no (a report) |
| `[!failure]` | tried, couldn't | no |
| `[!warning]` | caution / FYI | no |

Address a callout by opening its title with `@handle` (`> [!todo] @reginald verify this against the primary source`); unaddressed todos and questions default to Reginald. Reginald replies **nested underneath**, so the exchange reads as a thread:

```markdown
> [!todo] @reginald verify the CP26/21 deadline
> The Index.md row says 14 Aug — check the FCA page directly.
>
> > [!success] 2026-08-11 — Verified, no change needed
> > Fetched fca.org.uk/publications/consultation-papers/cp26-21 directly.
> > Deadline confirmed as 14 August 2026.
```

Reginald actions its queue on the `action-edits` schedule (01:00 daily) or when you run it by hand. Callouts anywhere under `Topics/` and `Escalations/` are picked up; `Archive/`, run logs and fenced code blocks are ignored. See the `Escalations/Index.md` note in the vault for the full protocol.

### Escalations

Escalations live in the `Escalations/` folder, one file per escalation, and are worked through callouts like anything else. A resolved escalation moves to `Escalations/Resolved/`. You never need to touch the number or move the file: change the `status:` frontmatter (or run `reginald escalations set-status`) and Reginald's sync moves it and renumbers. Full lifecycle in [Escalations and callouts](#escalations-and-callouts) above.

### Don't edit on the host

The Raspberry Pi's checkout is Reginald's working copy, not a place to edit. Work in your own clone and push. If the host tree is ever found dirty (a crashed run, a stray edit), Reginald salvages it into a `reginald: salvage uncommitted changes` commit and `doctor` flags it — that is a signal to investigate, not a supported workflow.

### `reginald vault git` — git on the host checkout, safely

`reginald vault git <any git command>` runs git against the vault **under the run lock**, so it queues behind any in-progress task instead of mutating the working tree under a live agent. This is why it's preferable to a raw `git -C /srv/vault/agentic-gc …` on the host: a bare `git reset --hard` mid-run would corrupt that run; routed through here it waits.

```bash
reginald vault git status
reginald vault git log --oneline -5
reginald vault git pull
reginald vault git push
reginald vault git reset --hard origin/main    # e.g. to drop a stale local run commit
```

Run `reginald vault git --help` for the wrapper or `reginald vault git -- --help` for native Git help. Arguments after the Git subcommand go to Git, including `reginald vault git log --help`. The wrapper forwards Git’s exit code and output. Commits use Reginald's identity by default; append `-c user.name=… -c user.email=…` to commit as yourself. `--json` wraps the result as `{returncode, stdout, stderr}`.

This is an operator convenience, **not** the contribution path — contributors work in their own clones and push. And it doesn't change the state model: reginald never treats an uncommitted tree as state (all state is commit-based), so using git on the vault between runs is safe regardless.

---

## CLI reference

Run `reginald` for global options or `reginald --help` to list every public command and its purpose. Both pages display Reginald’s logo. Use `reginald task --help` for the task group or `reginald task run --help` for that command’s arguments and options. [Command renames](CLI-COMMANDS.txt)

Global options come **before** the subcommand:

```bash
reginald --env prod --json task run-history
```

| Option | Effect |
|---|---|
| `--env dev\|prod` | Config overlay. Overrides `$REGINALD_ENV`. |
| `--vault <path>` | Override `vault_path` for this invocation. |
| `--db <file>` | Override `db_path` for this invocation. Vault writes share `reginald.lock` when their databases are in the same directory. |
| `--json` | Emit machine-readable output for commands that support it. |
| `-v`, `--verbose` | Enable debug logging. |
| `--version` | Print version and exit. |

Reginald creates the selected SQLite file if it is missing. To see the path SQLite opened, check the `main` row in the output of this command.

```bash
reginald --db ./scratch/reginald.db db run-sql "PRAGMA database_list"
```

Commands such as `task run-history` and `vault list-events` expose their domain records through `--json`.

### Tasks and runs

| Command | Purpose |
|---|---|
| `task run <task> [--topic T] [--dry-run]` | Execute now, under the run lock. Exits non-zero if the run failed. |
| `task list` | List registered tasks with runner, model and tools. |
| `task run-history [--task T] [--limit N]` | Recent runs with status and cost. Default limit 20. |
| `task inspect-run <id>` | Full JSON record for one run. |
| `task add-to-queue <task> [--topic T]` | Enqueue a run (status `queued`). |
| `task process-queue` | Execute all queued runs. |

`task run` executes immediately. `task add-to-queue` saves a request and `task process-queue` executes queued requests. Immediate and queued execution use the same vault lock.

### Subscribers

| Command | Purpose |
|---|---|
| `subscriber add <email> [--name N] [-t TOPIC ...] [--all-topics] [-x TOPIC ...] [--frequency F] [--notify new,updated,escalations]` | Add. Requires `--topics` or `--all-topics`. |
| `subscriber set-notify <email> [--new on\|off] [--updated on\|off] [--escalations on\|off]` | Flip per-kind notification preferences. |
| `subscriber list [--all]` | List active, or all with `--all`. |
| `subscriber topics <email> [--include T] [--exclude T] [--clear T] [--mode M]` | Show or adjust the topic selection. |
| `subscriber remove <email>` | Deactivate. |

### Outbox

| Command | Purpose |
|---|---|
| `outbox drain [--dry-run]` | Build pending digests and send them. |
| `outbox list [--status S]` | Deliveries, optionally filtered. |
| `outbox retry [id]` | Retry one delivery, or all failed. |
| `outbox preview <email>` | Render the next digest to stdout. |

### Topics

| Command | Purpose |
|---|---|
| `topics sync` | Reconcile topics with `Topics/` folders. |
| `topics list` | List topics with UUIDs. |

### Escalations

| Command | Purpose |
|---|---|
| `escalations list [--status S]` | Escalation records. |
| `escalations show <ESC-NNN\|uuid>` | One escalation's detail. |
| `escalations set-status <ESC-NNN\|uuid> <status>` | Set status; moves the file and mirrors the DB. |
| `escalations sync` | Run the repair matrix (status⟺folder, dates, uuids, numbers). |
| `escalations items [--state S] [--for HANDLE]` | The callout work queue. |
| `escalations migrate [--dry-run]` | One-time split of `Setup/Escalations.md`. |

### Vault

| Command | Purpose |
|---|---|
| `vault identify-changes [--dry-run]` | Scan new commits, record content events and callout work items, and republish if enabled. A dry run previews local commits. |
| `vault backfill-uuids [--dry-run]` | Add a frontmatter `uuid` to every note lacking one. |
| `vault normalize-frontmatter [--dry-run]` | Collapse duplicate top-level frontmatter keys, keeping the last value. |
| `vault set-publish [--dry-run]` | Set the `publish:` flag on every note (`true`, or `false` when matched by `.publish-ignore`). |
| `vault git <args...>` | **Admin tool.** Run any git command against the vault under the run lock (`vault git status`, `... git pull`, `... git push`). Passthrough: git's own flags, exit code, output. |
| `vault list-events [--topic T] [--since ISO] [--limit N]` | Content events. Default limit 50. |
| `vault publish [--dry-run]` | Republish the vault with the default engine (`site.engine`). |
| `vault ob <args...>` | Run obsidian-headless in the vault. Use `reginald vault ob --help` for wrapper help or `reginald vault ob -- --help` for native help. |

### Diagnostics and configuration

| Command | Purpose |
|---|---|
| `doctor` | Self-check. Exits non-zero on any failure. |
| `config show` | Print the resolved, merged config as JSON. |

### Database

| Command | Purpose |
|---|---|
| `db migrate` | Back up, then apply alembic migrations. |
| `db backup [--to PATH]` | Snapshot the SQLite file. |
| `db run-sql "<statement>" [--limit N] [--yes]` | **Admin tool.** Run one SQL statement against the database. See below. |

#### `db run-sql` — the admin escape hatch

For what a purpose-built command doesn't cover: inspecting a row, correcting data a past bug wrote, one-off surgery during an incident. Same spirit as `vault git`.

```bash
reginald db run-sql "select kind, count(*) from content_events group by kind"
reginald db run-sql "select * from escalations where number is null" --limit 50
reginald --json db run-sql "select id, kind, file_path from content_events limit 5"
```

A **read** (`SELECT`, `WITH`, `PRAGMA`, `EXPLAIN`) runs immediately and prints up to `--limit` rows, default 200, saying so when it truncates.

Anything else is a **write**, and gets two rails — both about making a mistake survivable, not preventing one:

- it **prompts for confirmation** (skip with `--yes`, e.g. in a script),
- it **backs the database up first** to `<db>.backup-<date>.db` and prints the path, and
- it runs **under the run lock**, so it cannot interleave with a live task — which also means it refuses while a run is in progress.

Classification skips comment lines, so a pasted `-- fix the rows\nUPDATE …` is correctly treated as a write.

**One statement per invocation.** The driver rejects a semicolon-separated script outright rather than applying half of it — so `"update …; drop table …"` fails with nothing applied.

`reginald doctor` opens with the selected environment, then checks the release, configuration, vault, Git, database and Node, plus Postmark and publishing when enabled. It checks credentials and SDKs for the runners that tasks select. In a terminal, green `[OK]`, yellow `[WARN]` and red `[FAIL]` labels identify each result. An unused runner gets one warning. With Logfire enabled, its row shows `[WARN]` if SDK setup fails or the project URL is unavailable when checked. Warnings alone return exit code 0. A failed required check returns 1. Set `NO_COLOR=1` for plain text, or use `reginald --json doctor` for the structured report. Advisory rows have `warning: true` and the top-level `ok` reflects the exit status. [Runner credential checks](RUNNERS.md#checking-runner-credentials)

The **release** line reports the installed version alongside the tag of the checkout it is running from — `0.6.0 (tag v0.6.0)` on a clean deploy. It never fails the run; it is there so a report can be attributed to a release. Read the variants as:

| Detail | Means |
|---|---|
| `0.6.0 (tag v0.6.0)` | Deployed release, checkout on its tag. |
| `0.6.0 (tag v0.6.0, working tree dirty)` | Same, with uncommitted changes on the host. |
| `0.6.0 (checkout v0.6.0-2-g1b87cc9 — ahead of its tag)` | Running unreleased commits, e.g. `update.sh main`. |
| `0.6.0 (checkout v0.5.0 — expected tag v0.6.0)` | **Drift.** HEAD moved without a re-sync, so the metadata and the code disagree — re-run `update.sh`. |
| `0.6.0 (no checkout — installed from a package)` | Installed as a wheel, no repo to describe. |

Run `vault backfill-uuids` once on a new vault. Agents write a `uuid:` into frontmatter for new notes; this guarantees coverage for everything pre-existing, so every content event carries a rename-stable identity. Note that a renamed brief is still reported as `brief_created` — event de-duplication is on `(commit_hash, file_path, kind)`, and the UUID is identity metadata for consumers rather than something that suppresses the event.

### Publishing: the `publish:` flag and `.publish-ignore`

**obsidian-headless only publishes notes whose frontmatter has `publish: true`** — a note without the flag is silently skipped, so a new or edited file never reaches the site. reginald closes that gap deterministically (no LLM):

- Every content task runs a `set-publish` post-step. It gives any note **lacking a `publish` flag** `publish: true`, so new and human-created notes publish.
- A note matched by **`.publish-ignore`** is forced to `publish: false`, overwriting any prior value — so *moving* a note into an excluded folder hides it on the next run.
- A note that already has a `publish` value (and isn't ignored) is **left alone**, so a human who deliberately set `publish: false` on a live note keeps it hidden.

`.publish-ignore` lives at the vault root and uses **`.gitignore` syntax** (comments, `!` negation, trailing-`/` dir-only, anchoring). The default excludes `Archive`. These changes are frontmatter-only, so they raise no content events and mail nobody. Run `reginald vault set-publish` by hand to seed a vault or after editing `.publish-ignore`; `--dry-run` reports without writing.

---

## Scheduling

Production runs each scheduled task through the systemd template `reginald-task@.service`. Vault change detection and the email outbox each have a dedicated timer. For example, once `reginald doctor` exits successfully, enable weekday research, the daily vault scan and the weekday outbox drain together.

```bash
sudo systemctl enable --now reginald-task@research-new-developments.timer
sudo systemctl enable --now reginald-identify-changes.timer
sudo systemctl enable --now reginald-outbox.timer
systemctl list-timers 'reginald-*'
```

All timers set `Persistent=true`, so a run missed during downtime executes on the next boot. [Timer schedules](TIMERS.md)

Each unit runs as the `reginald` user with `REGINALD_ENV=prod` and `EnvironmentFile=/etc/reginald/env`. Task runs allow up to 3 hours (`TimeoutStartSec=3h`) and are niced to 5.

Because every task takes the same lock, overlapping timers are safe — a second task waits rather than corrupting the vault.

---

## Runbook

**Running the CLI by hand in prod**

```bash
sudo reginald doctor
sudo reginald task run-history
sudo reginald -v task run research-new-developments
```

`/usr/local/bin/reginald` is a wrapper installed by `install.sh`. It exists because a manual command needs four things right, and each fails differently when forgotten:

| It does | Because otherwise |
|---|---|
| `cd /opt/reginald` | `runuser` keeps the caller's directory; from `/root` (mode `0700`) the `reginald` user cannot stat anything, and it surfaces as `PermissionError: 'pyproject.toml'` from inside logfire |
| sources `/etc/reginald/env` | API keys and tokens are absent |
| sets `REGINALD_ENV=prod` | the CLI defaults to `dev` — a different database and vault, so `task run-history` reports nothing and a successful run looks like it did nothing |
| `runuser -u reginald` | the vault, database and venv are owned by that user |

`REGINALD_ENV` from your shell still wins, so `REGINALD_ENV=dev sudo -E reginald config show` works for a one-off.

When anything reports empty, check what you are actually pointed at before investigating further:

```bash
sudo reginald config show | head -5
```

The wrapper is refreshed by `update.sh`, so fixes to it arrive with a deploy. Everything it does can still be done by hand if you need to:

```bash
cd /opt/reginald
set -a; . /etc/reginald/env; set +a
sudo runuser -u reginald -- env REGINALD_ENV=prod .venv/bin/reginald doctor
```

**Watching a run you started by hand**

```bash
sudo systemctl start --no-block reginald-task@research-new-developments.service
```

Without `--no-block`, `systemctl start` waits for the unit to finish — it is `Type=oneshot` with `TimeoutStartSec=3h`, so it appears to hang for as long as the agent runs. Ctrl-C detaches your shell only; the run continues.

**Reading the progress log**

For tool definitions, individual calls, tool-calling turns per run, cost and token usage, see [Reginald logging](LOGGING.md).

The Claude runner writes one `INFO` line per tool call by default. A live run looks like this in the journal.

```
[research-new-developments] turn 1 (3s): Glob Topics/**/Briefs/*.md
[research-new-developments] turn 2 (11s): WebSearch FCA consultation payments
[research-new-developments] turn 3 (48s): no tool calls
[research-new-developments] turn 4 (52s): Write Topics/Payments/Briefs/2026-08-08.md
[research-new-developments] finished in 71s: 4 turn(s), cost $0.1837
```

The elapsed seconds are what tell you whether it is working or wedged: a growing gap since the last line, or a run of consecutive `no tool calls`, is a stall. To confirm the process is alive at all:

```bash
systemctl is-active reginald-task@research-new-developments.service
ps -u reginald -o pid,etime,cmd | grep -i claude
```

### Turning up the logging

Three levers, highest precedence first.

**`--verbose` / `-v`** on any command, for an ad-hoc run. Adds the agent's prose, tool input and result excerpts, and SDK system message types:

```bash
sudo reginald -v task run research-new-developments
```

**`REGINALD_LOG_LEVEL`** in `/etc/reginald/env`, to change a host persistently:

```bash
sudo sh -c 'echo REGINALD_LOG_LEVEL=DEBUG >> /etc/reginald/env'
```

The next scheduled run reads the new value from `/etc/reginald/env`. Remove the override to use the level set in YAML again.

**`logging.level` / `logging.agent_turns`** in `config/base.yaml` set the default for this checkout. Use `config/dev.yaml` or `config/prod.yaml` to change it for one environment.

An unrecognised level falls back to `INFO` with a warning rather than failing the run — a typo in a log setting should not cost you a brief.

Note that `DEBUG` logs tool inputs and results, which include vault content. Long values are truncated to 300 characters, but do not leave it on a host whose journal you would not want that in.

`runuser` switches user but **keeps the current directory**, and `/root` is mode `0700`. Run from root's home and anything resolving a relative path fails with:

```
PermissionError: [Errno 13] Permission denied: 'pyproject.toml'
```

That traceback comes from logfire, not reginald: pydantic loads registered plugins the first time any model class is created, logfire is one, and its module-level config looks for `pyproject.toml` in the working directory. The `reginald` user cannot stat anything under `/root`, so it raises before the CLI starts. It is a working-directory problem, not a logfire or permissions bug in the install.

The systemd units set `WorkingDirectory=/opt/reginald`, so scheduled runs never hit this — only manual invocation does.

**Never touch the vault or database as root**

The vault is cloned by, and owned by, the `reginald` user; so is the database. Every scheduled run operates as that user. Run git against the vault as root and it refuses:

```
fatal: detected dubious ownership in repository at '/srv/vault/agentic-gc'
```

```bash
# wrong — and git's suggested `safe.directory` fix makes it worse
git -C /srv/vault/agentic-gc pull

# right
runuser -u reginald -- git -C /srv/vault/agentic-gc pull
```

Adding the `safe.directory` exception git recommends does silence the error, but it lets root write into a tree `reginald` must own. Every file root then creates or rewrites becomes root-owned, and the failure resurfaces as a permission error during an unattended 09:00 run — far from the change that caused it. Fix ownership rather than bypass the check:

```bash
find /srv/vault/agentic-gc ! -user reginald | head    # anything listed is a problem
chown -R reginald:reginald /srv/vault/agentic-gc
```

**The rule for both traps**

Manual commands are the only place these bite — the systemd units set both the working directory and the user correctly. When driving reginald by hand:

1. `cd /opt/reginald` first.
2. Prefix anything touching the vault, the database or the venv with `runuser -u reginald --`.

**Watch what is happening**

```bash
journalctl -u reginald-task@research-new-developments.service -f
journalctl -u 'reginald-*' --since today
systemctl list-timers 'reginald-*'
```

**A task produced no email**

Work down the pipeline in order — each step gates the next:

```bash
reginald task run-history --task research-new-developments --limit 5   # did it run, and succeed?
reginald vault list-events --limit 20                         # did it produce events?
reginald subscriber topics you@example.org              # are you on those topics?
reginald outbox list --status failed                    # did sending fail?
```

The common causes, in rough order of likelihood: the agent changed nothing; it wrote outside the event path patterns (a `Materials/` file not named `Key dates digest` is the easy trap); the subscriber's effective topics do not include the topic; `git.commit_per_run` is false so there is no diff; or the events were already marked delivered to that subscriber.

**Stopping a run**

```bash
sudo systemctl stop reginald-task@research-new-developments.service
ps -u reginald -o pid,etime,cmd | grep -i claude      # empty when it is gone
```

Use `systemctl stop`, not `kill <pid>`: systemd signals the whole cgroup, so the bundled Claude Code CLI and any `npx` MCP servers go too. Killing the Python process alone orphans the Node subprocess that is doing the work.

Afterwards, **check the vault**. `reset_hard` only runs when the runner returns a failure — a killed process never reaches it, so partial edits stay uncommitted, and the next run could salvage them. Any substantive content changes in that commit could reach subscribers as finished work.

```bash
sudo runuser -u reginald -- git -C /srv/vault/agentic-gc status --short
# to discard partial agent work (clean -fd deletes untracked files — read the
# status output first, any manual edits of yours are in there too):
sudo runuser -u reginald -- git -C /srv/vault/agentic-gc reset --hard HEAD
sudo runuser -u reginald -- git -C /srv/vault/agentic-gc clean -fd
```

**A run is stuck in `running`**

The process died without finalising, so `_finalise` never wrote a terminal status. It is cosmetic: the row stays as a historical record and the next run is unaffected.

**Do not delete `reginald.lock`.** The lock is an `fcntl.flock` on that file, which the kernel releases when the holding process dies — it cannot go stale. Deleting the file while a process holds it is actively harmful: the next run creates a new inode and takes a *different* lock, giving you two concurrent agent writers in the vault, which is the exact situation the lock prevents.

To see whether anything really is running:

```bash
systemctl list-units 'reginald-*' --all
ps -u reginald -o pid,etime,cmd
```

**Sending failed**

```bash
reginald outbox list --status failed
reginald outbox retry
```

Check the recorded `error`. `recipient not in allowlist` means `email.allowlist` is set — expected in dev, a misconfiguration in prod. After 5 attempts a delivery is only recoverable via `outbox retry`.

**Budget exhausted**

Runs refuse to start once the month's spend reaches `budget.monthly_limit_usd`. Raise it in config, or wait for the month to roll over. Check spend with `reginald task run-history`.

**Restore the database**

`db migrate` and `db backup` write `<db>.backup-<date>.db` beside the database. Stop the timers, copy the backup over `db_path`, and run `doctor`.

---

## Known gaps

Honest limitations in the current build:

- **`--frequency daily|weekly` is not implemented.** The value is stored and displayed, but the send path ignores it: every drain sends all undelivered events to every subscriber. Batching is determined solely by how often the timer runs `outbox drain`. Treat every subscriber as `per-run`.
- **`reginald serve` is a hidden stub.** The service layer and `--json` contract are built for it, but the web UI and API do not exist yet.
- **Rollbacks do not downgrade the database.** `update.sh <old-tag>` reverts code only; alembic migrations are not reversed. Crossing a schema change backwards needs the backup that `db migrate` took.
- **The `--topic` scope restriction is advisory.** It is appended to the prompt and relies on model compliance. The event filter that follows it *is* enforced in code, so a stray edit outside the topic will not email anyone — but it can still be committed to the vault.
