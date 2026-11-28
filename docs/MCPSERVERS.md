# MCP servers

Reginald uses the Model Context Protocol (MCP) for Brave Search, Tavily and filing Inbox notes. Only the shipped `triage-inbox` task grants the `inbox` server's `move_file` operation. It accepts two vault-relative paths, preserves the file's contents, and refuses an existing destination. [Inbox workflow](USAGE.md#filing-inbox-notes) · [Inbox server definition](../config/tools.d/inbox.yaml) · [Brave server definition](../config/tools.d/brave-search.yaml)

## How an agent chooses a tool

The research task shows how three pieces fit together.

1. **The task grants access.** Its YAML includes Claude's built-in `WebSearch` and the external operation `mcp__brave-search__brave_web_search`. Reginald passes the runner the connections selected by that task. [Research task](../config/tasks.d/research-new-developments.yaml) · [Run service](../src/reginald/services/runs.py)
2. **The server describes the tool.** Through MCP's `tools/list`, the runner can discover each operation's name, plain-English description and input schema. The model uses those details to choose an operation and supply its arguments. The runner calls the server and returns the result to the model. Native tools such as Claude's `Read` and `Grep` get their definitions from the runner's SDK. [MCP tool discovery and calls](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) · [Runner guide](RUNNERS.md)
3. **The task prompt gives the agent a reason to use it.** For a custom tool, put its purpose, inputs and expected result in the server's tool definition. Put team policy in the task's Markdown prompt. For example, a research prompt could say “When a cited source has moved, search for its new location and open the regulator's page before updating the citation.” The agent chooses whether to call a granted tool. Use a task step when Reginald must perform an operation on every applicable run. [Tasks and prompts](USAGE.md#tasks-and-prompts) · [Task steps](STEPS.md)

## Brave Search rate limits

Claude, Codex and Pydantic AI launch the same Brave MCP server. Its HTTP requests start at one per second until Brave reports the plan’s burst rate. The tool then spaces requests at that rate and checks the remaining allowance and reset time for each quota window. A monthly limit of zero means the plan has unlimited monthly requests. [Brave’s rate-limit guide](https://api-dashboard.search.brave.com/documentation/guides/rate-limiting)

After HTTP 429, the tool retries up to three times, waiting at least one, two and four seconds respectively and longer when an exhausted window requires it. A cooldown longer than 30 seconds returns a tool error with the remaining wait. Calls using the same API key and local OS account share the cooldown across runner processes and restarts. The state lives in `~/.local/state/reginald/brave-rate-limits.sqlite` and contains a hash of the key and pacing times. Other hosts using the same Brave subscription have their own local state.

The shipped connection pins the Brave server version and preloads the limiter through its Node.js arguments. Keep the shipped `args` when overriding the path to `npx`. The `{package_uri}` placeholder resolves to the installed Reginald package’s file URI. [Brave server definition](../config/tools.d/brave-search.yaml)

## Tavily

The bundled Tavily server uses stdio with Claude, Codex and Pydantic AI. It authenticates to `https://api.tavily.com` using the key in `TAVILY_API_KEY`. Tavily performs the searches and fetches the target URLs on its infrastructure. Registering the server makes its operations available for task grants. The process starts only for a task that selects it. [Server definition](../config/tools.d/tavily.conf) · [Endpoint grants and examples](TOOLS.md#tavily)

## Add another MCP server

All three runners support local stdio processes and Streamable HTTP endpoints. To add a compatible server, register its connection and grant its operations to the tasks that need them. The following example gives the development `source-verification` task access to Microsoft Learn search so it can locate a moved Microsoft documentation page.

### 1. Register the connection

Create `config/tools.d/microsoft-learn.yaml` with the following contents. Each `.yaml`, `.yml` or `.conf` file directly in that directory defines one server using YAML syntax. Its filename stem supplies the server name used by task grants. Microsoft publishes this endpoint and its operation names. [Microsoft Learn MCP reference](https://learn.microsoft.com/en-us/training/support/mcp-developer-reference)

```yaml
transport: http
url: https://learn.microsoft.com/api/mcp
```

For a local server, use `transport: stdio` and supply the executable in `command` and its arguments as a YAML list in `args`. The executable must be available to the account that runs Reginald. The shipped Brave connection uses `npx` and names `BRAVE_API_KEY` in its `env` list. Put secret values in the development process environment or the host's `/etc/reginald/env`. Reginald passes named variables to the child process when they are set. The HTTP configuration accepts an endpoint URL and has no field for authentication headers. [Brave stdio example](../config/tools.d/brave-search.yaml) · [Credential setup](INSTALL.md#secrets)

For a bundled Python server, `{python}` as the command selects the running Reginald interpreter. A whole argument of `{vault_path}` supplies the absolute vault root for this run, including CLI overrides. The Inbox server uses both so the task can supply only the two paths inside that vault. [Inbox server definition](../config/tools.d/inbox.yaml)

### 2. Grant the operation to a task

Find the operation's exact name in the server's published tool list. A grant has the form `mcp__<server>__<operation>`. For this example, merge the following mapping into `config/dev.yaml`, keeping the other development settings. The list includes the shipped source-verification tools because a list in an environment overlay replaces the whole inherited list. [Source-verification task](../config/tasks.d/source-verification.yaml) · [Configuration order](USAGE.md#configuration)

```yaml
tasks:
  source-verification:
    tools: [Read, Write, Edit, Glob, Grep, WebFetch, mcp__microsoft-learn__microsoft_docs_search]
```

For a grant shared across environments, add the operation to the task's existing `tools` list in `config/tasks.d/`. A wildcard such as `mcp__microsoft-learn__*` grants every operation that server exposes. Select individual operations for unattended tasks. An MCP server executes with its own permissions, including when a Codex task selects `CodexReadOnly`. [Runner permissions](RUNNERS.md#codex-agent)

### 3. Verify the grant and its use

After local development setup, run this from the repository root. The `source-verification` row should include `mcp__microsoft-learn__microsoft_docs_search` alongside its existing tools. [Local setup](INSTALL.md#local-development)

```bash
uv run reginald --env dev task list
```

Listing tasks checks the merged configuration and registered server names. A task dry run renders the prompt and skips the agent. At agent startup all three runners validate the selected operations against the connected servers. A missing operation fails the run. [Tool validation](TOOLS.md#mcp-grants-and-validation) To verify actual use, inspect the next development run that needs the operation for a tool call and its result. The journal records Claude tool names when per-turn logging is enabled. Debug logs include a summary of the result. [Inspect tool calls](LOGGING.md#find-individual-tool-calls-in-logfire)

## Proposed read-only vault server

**Status: design only.** None of these operations is implemented or available to grant. The proposal is one local server that reads the task's current vault checkout and its Git history. It could answer all three queries without a graph database or a vector index.

| Proposed operation | Inputs and results | When the agent would use it |
| --- | --- | --- |
| `find_references` | Accept a source URL or vault note path. Return each explicit citation or incoming note link with its vault path, line number and excerpt. | Trace a changed source to the briefs that cite it, then look up trackers linking to each brief. |
| `query_notes` | Filter frontmatter by primary or secondary topic, status or key-date range. Return matching note paths and field values. | Gather recorded deadlines for a digest, or locate briefs filed under another topic. |
| `note_history` | Accept a vault note path and a starting date. Return committed changes with author, date and diff. | Find the edit that introduced a disputed deadline before checking it against the official source. |

The first trial would give `find_references` to `update-trackers`. Its vault prompt could say “When a regulator changes a deadline, find the notes that cite its page, follow their backlinks to trackers, and check each linked entry.” Try it on a known deadline change and compare the returned paths with the trackers a reviewer expected. That tests whether the vault contains enough explicit links to make the lookup worthwhile.
