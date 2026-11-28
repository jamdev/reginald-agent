# Choosing tools for a task

A task's `tools` list determines what its agent may use unattended. The selected runner supplies its native tools. Each configured MCP server supplies its own operations. A grant makes a tool available to the model. Put instructions about **when** to use it in the task's Markdown prompt. [Task configuration](TASKS.md) · [Adding an MCP server](MCPSERVERS.md#add-another-mcp-server)

## Choosing a search provider

| Use case | Suggested grant | Why |
| --- | --- | --- |
| An agent needs a current source to answer a question or resolve a moved citation | Native search | Uses the selected provider’s search workflow and credentials. A task can search without a Brave API key. |
| A daily regulatory brief needs UK results from the past week | Brave | The same operation on all three runners accepts country and freshness filters. For this case use `country: GB` and `freshness: pw` in the tool call. Check the publication date on the source itself. |
| An operator needs to audit which URLs a search returned | Brave | With Logfire conversation capture enabled, the Brave tool span records the full result URLs, including query strings and fragments. These are the search hits. The task’s citations show which sources it used. |
| The first search misses a known consultation, or you want a second search provider’s results | Both | Give the prompt a trigger for the second search. For example, “Use native search first. If you cannot locate the original consultation, search Brave for its title and the regulator’s name.” |
| The task only edits or classifies existing vault notes | Neither | Leave the search grants out. Local file access and external search are separate capabilities. |

Brave requires `BRAVE_API_KEY`. Its tool shares rate-limit state across local runners using the same OS account and key. If Brave reports a long cooldown, the prompt can direct the model to use native search when that grant is present. Granting both gives the model a choice. Reginald does not automatically send the query to the second provider. [Brave search filters](https://api-dashboard.search.brave.com/app/documentation/web-search/query) · [Rate limits and retries](MCPSERVERS.md#brave-search-rate-limits) · [Find result URLs in Logfire](LOGGING.md#find-individual-tool-calls-in-logfire)

## Search grants by runner

These are complete `tools` lists for a search-only task. For a task that also edits notes, include its existing file grants or choose the appropriate Codex sandbox. An environment overlay replaces the entire list.

| Runner | Native search only | Brave only | Both |
| --- | --- | --- | --- |
| Claude | `[WebSearch]` | `[mcp__brave-search__brave_web_search]` | `[WebSearch, mcp__brave-search__brave_web_search]` |
| Codex | `[CodexReadOnly, web_search]` | `[CodexReadOnly, mcp__brave-search__brave_web_search]` | `[CodexReadOnly, web_search, mcp__brave-search__brave_web_search]` |
| Pydantic AI | `[web_search]` | `[mcp__brave-search__brave_web_search]` | `[web_search, mcp__brave-search__brave_web_search]` |

### Claude

Use the names advertised by Claude Code, such as `Read`, `Edit`, `WebSearch` and `WebFetch`. Search finds candidate sources. Grant `WebFetch` as well when the task should retrieve a particular page. Scoped permission rules such as `Bash(git status *)` keep their scope when passed to the SDK. Reginald checks the underlying tool name, `Bash` in this example, against the session’s startup inventory. [Claude Code tool reference](https://code.claude.com/docs/en/tools-reference)

### Codex

Choose exactly one sandbox grant. `CodexReadOnly` selects the runtime’s native tool set with read-only filesystem access. `CodexWorkspaceWrite` also permits writes in the vault workspace. The pinned Codex app-server supplies these tools as a set. Individual native commands and editors are managed by Codex. Add `web_search` to request **live** hosted search. Omitting it disables native search in Reginald. Either choice keeps shell network access disabled. MCP servers have their own access and must be granted separately. [Codex web search modes](https://developers.openai.com/codex/config-basic#web-search-mode) · [Runner permissions](RUNNERS.md#codex-agent)

For example, these fields switch the shipped research task to Codex in an environment overlay, retain its ability to edit the vault, and give it both search providers. Select a model available to the account. The null value clears the inherited Claude turn limit.

```yaml
tasks:
  research-new-developments:
    runner: codex_agent
    model: gpt-5.4
    max_turns: null
    timeout_seconds: 1800
    tools: [CodexWorkspaceWrite, web_search, mcp__brave-search__brave_web_search]
```

### Pydantic AI

Use the native tool’s SDK identifier, such as `web_search`. Reginald resolves the configured model and checks its profile for the native tool types supported by that model and provider adapter. A provider or model change can therefore make an existing grant invalid. If a profile lacks native search, a task can still grant Brave when its model supports ordinary tool calling. [Pydantic AI native tools](https://ai.pydantic.dev/native-tools/)

Use `native_tool_options` for arguments to a Pydantic AI native tool, including tools with required configuration. Key the options by the same name you granted under `tools`. The SDK validates the fields against the tool’s schema. For example, these fields select OpenAI Responses native search and request a high search context size.

```yaml
runner: pydantic_agent
model: openai-responses:gpt-5.4
tools: [web_search]
native_tool_options:
  web_search:
    search_context_size: high
max_turns: 8
```

## MCP grants and validation

Use `mcp__brave-search__brave_web_search` to grant one operation on the configured `brave-search` server. Its definition comes from `config/tools.d/brave-search.yaml`. A server-wide grant such as `mcp__brave-search__*` includes every operation the server advertises, including operations added in a later version. Prefer an exact operation when an unattended task only needs one. [Brave server definition](../config/tools.d/brave-search.yaml) · [Register a server](MCPSERVERS.md#add-another-mcp-server)

1. **When configuration loads**, Reginald checks the manifest structure, MCP grant syntax, registered server names and runner settings. From a configured development checkout, run `uv run reginald --env dev task list` to see the effective grants after overlays. Listing tasks and rendering a dry run leave MCP servers offline.
2. **When the agent starts**, Claude checks the native and MCP names advertised in its startup message. Codex checks each selected server’s operations on the active thread before starting the turn. Pydantic AI checks native grants against the resolved model profile and MCP grants against the connected servers before the model request. If a requested tool is missing, the run fails and names the grant.

If a task lists successfully but fails at startup, compare the name in the error with the selected runtime’s tool name or the server’s advertised operation. For Pydantic AI, check the provider prefix as well as the model. If validation passes but the task never searches, check the prompt and the run’s tool calls. [Inspect tool calls](LOGGING.md#find-individual-tool-calls-in-logfire)
