# Task runners

Every [task](TASKS.md) must specify both `runner` and `model` in its effective configuration.

A runner is the harness Reginald uses to start the agent, pass it the rendered prompt and tools, and collect the result. Reginald calls the task's configured steps around that agent call. [Task steps](STEPS.md)

All runners use the environment’s `logfire.capture_chars` limit for conversation excerpts. [Read the agent conversation](LOGGING.md#read-the-agent-conversation)

Reginald validates these fields after merging the task definition and environment overlay. The seven shipped tasks explicitly select `claude-agent` and their existing Sonnet or Haiku model. To see the effective selections from the checkout, run `uv run reginald --env dev task list`. Each row shows the task, runner, model and tool grants. [Choosing and validating tools](TOOLS.md)

| Runner | How it runs the task | Work limit |
| --- | --- | --- |
| `claude-agent` | Claude Agent SDK with Claude Code tools in the vault | `max_turns` limits SDK turns (default 60) |
| `pydantic_agent` | Pydantic AI with the selected model’s native tools and MCP operations | `max_turns` limits model requests (default 60) |
| `codex_agent` | Local Codex app-server with a fresh thread rooted at the vault | One Codex turn with a required positive `timeout_seconds` |

Each example below is the body of a task definition in `config/tasks.d/`. The filename supplies its task key. Point `prompt_file` at a task note in the vault, or use the default prompt resolution. Reginald renders the prompt, runs the selected adapter, then commits file changes according to its Git settings. A final text answer alone creates no vault change or email. [Task and prompt configuration](USAGE.md#tasks-and-prompts)

## Checking runner credentials

Run `reginald doctor` in the environment whose tasks you want to check. It reads the merged task configuration. Each unused runner gets one `[WARN]` marked `Unused` and its credentials and SDK are skipped. A missing SDK or required credential for a configured task gives `[FAIL]` and exit code 1. Warnings alone give exit code 0.

| Runner selected by a task | What doctor checks locally |
| --- | --- |
| `claude-agent` | `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`. The `Claude auth` row names the credential that takes precedence. |
| `codex_agent` | `OPENAI_API_KEY` or a Codex login in the service user's Reginald profile. |
| `pydantic_agent` | One row per selected model provider, naming the tasks that use it. For example, `openrouter:anthropic/claude-sonnet-4.6` needs `OPENROUTER_API_KEY`. A direct `anthropic:` model needs `ANTHROPIC_API_KEY`. |

The checks report variable names and local login status without making a model request. Local Ollama endpoints can run without an API key. Providers that resolve credentials externally, such as AWS Bedrock, get a warning that their credentials need separate verification.

## Claude Agent SDK

```yaml
title: Triage inbox
runner: claude-agent
model: claude-haiku-4-5
prompt_file: Setup/Tasks/Triage inbox.md
tools: [Read, Write, Edit, Glob, Grep, mcp__inbox__move_file]
max_turns: 60
```

Select the Claude built-ins and MCP operations the task may use. The SDK runs in the vault directory. Reginald supplies the task's tool list, pre-approves the listed operations and denies calls that would require interactive approval. The runner uses `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`. When both have values, the API key takes precedence. With a Claude task configured, `doctor` reports which credential it found.

The journal shows per-turn tool summaries when `logging.agent_turns` is enabled. Logfire records tool definitions by name, individual tool executions and the number of main-agent turns that requested tools. Model spans identify calls made together. The agent span carries reported usage including cache tokens and the SDK's whole-run cost when available. [Claude logging](LOGGING.md)

## Pydantic AI

```yaml
title: Search regulatory sources
runner: pydantic_agent
model: openrouter:anthropic/claude-sonnet-4.6
tools: [mcp__brave-search__brave_web_search]
max_turns: 8
```

The model identifier selects the provider. This example needs `OPENROUTER_API_KEY` for the model and `BRAVE_API_KEY` for Brave. Pydantic AI calls the selected MCP operations over stdio or Streamable HTTP and returns a text answer. A task that edits notes needs an MCP operation that writes to the vault. The runner has no built-in vault editor or task output schema.

Pydantic AI emits model and tool spans through its Logfire instrumentation. Reginald also records request count, tool-calling turns, token totals and available estimated cost on the shared run span. [OpenRouter setup](USAGE.md#use-openrouter-with-the-pydantic-ai-runner) and [Pydantic AI tool traces](LOGGING.md#inspect-a-pydantic-ai-task-in-logfire)

## Codex agent

```yaml
title: Triage inbox
runner: codex_agent
model: gpt-5.4
prompt_file: Setup/Tasks/Triage inbox.md
tools: [CodexWorkspaceWrite]
timeout_seconds: 180
```

Select a model available to the account. The Codex Python SDK starts its pinned local runtime, opens an ephemeral thread, and submits one rendered prompt. Choose exactly one native tool mode in the task's `tools` list.

| Grant | Native tool access |
| --- | --- |
| `CodexReadOnly` | Codex's native tools, including sandboxed command execution, with read-only filesystem access. Use for inspection. |
| `CodexWorkspaceWrite` | Native tools, including sandboxed commands and file edits, with write access to the vault workspace. Use for tasks that must produce a Git diff. |

These are sandbox modes for the native tool set. The runner denies approval requests and disables shell network access. Add `web_search` to the task’s tools to enable live hosted search alongside the native sandbox tools. Native search is disabled when omitted. [Search grants by runner](TOOLS.md#search-grants-by-runner) For an external operation, add its MCP grant, such as `mcp__brave-search__brave_web_search`. An MCP server runs with its own permissions. Granting a write operation to an MCP server can change external state even in a native read-only session. The runner checks for ambient MCP servers before starting a thread and fails if it finds any. Use the Reginald tool registry for these connections.

A Codex turn may contain multiple model requests and tool calls. `timeout_seconds` covers SDK startup and execution. On expiry Reginald interrupts the worker, allows a short shutdown period, then terminates its process group before the run service rolls back the vault. When switching an existing Claude task to Codex in an environment overlay, set `max_turns: null` to clear the inherited limit, supply `timeout_seconds`, and replace the task’s `tools` list. [Codex SDK](https://developers.openai.com/codex/sdk/) and [app-server turn lifecycle](https://developers.openai.com/codex/app-server/)

### Authenticate on the host

The `agents` extra includes the locked Python SDK and its Codex executable. The locked Linux runtime has wheels for x86-64 and ARM64. For API billing, add `OPENAI_API_KEY` to `/etc/reginald/env` using `sudoedit /etc/reginald/env`. For local development, load it from the repository's `.env` using the [local credential instructions](INSTALL.md#local-development-secrets).

For ChatGPT-managed access, enable device-code login for the account or workspace and run the following on the installed host. Complete the one-time browser sign-in using the code it prints. The service user owns its login under `~/.local/share/reginald/codex`. When an API key is present, the runner uses it for the session and keeps the ChatGPT login available for later runs. [Codex authentication](https://developers.openai.com/codex/auth/)

```bash
cd /opt/reginald
sudo runuser -u reginald -- .venv/bin/python -m reginald.runners.codex_agent.auth
sudo reginald doctor
```

With a Codex task configured, look for green `Codex auth` and `module openai_codex` checks. These check local setup. A successful model response verifies account access to the selected model.

On Ubuntu, also [verify the Codex sandbox](INSTALL.md#verify-the-codex-sandbox) before the first unattended run. This exercises native command execution as the service user. The same guide covers the Bubblewrap loopback error on older installations.

### Inspect a run

In Logfire Live, expand `run.agent` and `invoke_agent` for the task. Command executions, file changes and MCP calls emit `execute_tool` spans. The journal logs tool names when `logging.agent_turns` is enabled. Codex reports cumulative usage within its single turn. Reginald records input, cached input, output, and cache creation when reported. Cached input is already included in input tokens. The turn count is the number of Codex conversation turns started, so several tool executions in that turn count as one tool-calling turn.

With API-key billing, Reginald estimates USD using the installed model price table when it has a price. ChatGPT plan usage and unknown API prices leave the USD field empty. Reginald's monthly USD limit only accounts for recorded costs.
