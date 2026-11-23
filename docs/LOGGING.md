# Reginald logging

With Logfire enabled, Reginald traces tasks, email delivery and standalone commands. The Claude and Codex runners also write tool summaries to the journal at `INFO` by default. For the written account of what a task checked, changed or flagged, open `Setup/Logs/Index.md` in the vault.

## Read the agent conversation

In **Agents**, select a task, open **Runs**, choose a run and open its conversation. In **Live**, expand `run.agent` and select `invoke_agent`. Its `pydantic_ai.all_messages` attribute contains the conversation observed by the runner. Claude and Pydantic AI also attach message excerpts to individual `chat` spans. Codex exposes a conversation turn through the SDK, so its messages appear on the agent span.

With Logfire enabled, all three runners capture the task prompt, assistant text, tool arguments, tool results and any reasoning text or summary the SDK makes visible. Claude's conversation covers the main agent. The production default is the first **100 characters per content field**, including punctuation in serialized tool data. The development template uses **1,000**. The limit applies to each complete field across all its streaming chunks. Tool names, IDs, outcomes, timing and usage are recorded separately.

To change production capture, run `sudoedit /opt/reginald/config/prod.yaml` and set `capture_chars: 100` to the desired positive integer in the existing `logfire` section. Set `capture_chars: false` there for metadata-only traces. For development, use the same key in `config/dev.yaml`. The next run picks up the setting. Existing traces keep the content originally recorded. The same limit applies to conversation excerpts mirrored from the journal, including at `DEBUG`. The journal has its own verbosity and excerpt limits. [Configuration reference](USAGE.md#configuration)

## Find individual tool calls in Logfire

1. On the production host, run `sudo reginald doctor` and open the project URL on the `logfire` row.
2. In **Agents**, select the task, open **Runs**, choose a run recorded after the update, and open **Tools**. Claude reports the tool names advertised by the SDK, including tools it never called. **Trace** shows an `execute_tool` span for each observed call. Claude and Codex record the tool name, call ID, turn number and outcome. For Claude, the duration runs from receipt of the tool request to receipt of its result.
3. To find individual calls across tasks, open **Live**, select **prod** and paste this condition into the SQL filter. Expand a match to see the agent invocation and its parent `run.agent` span, which carries the Reginald run ID.

   ```sql
   service_name = 'reginald' AND attributes->>'gen_ai.operation.name' = 'execute_tool'
   ```

Tool execution spans are available with `logging.agent_turns` switched off. On a Claude `chat` span, `gen_ai.output.messages` identifies the tools requested together in that response. For example, a parallel `Read` and `Grep` produce two tool spans and count as one tool-calling turn. [Logfire Agents view](https://pydantic.dev/docs/logfire/observe/agents/) · [Agent run distributions](https://pydantic.dev/docs/logfire/observe/llms/#agent-run-distributions)

## Trace standalone commands and email

In **Live**, select **prod** and use this filter to see operator commands and timer invocations, including vault scans and outbox drains that found no work.

```sql
service_name = 'reginald' AND span_name = 'cli.command'
```

Each span identifies the command, its duration, whether it was a dry run and its exit code. Expand the trace to see any database backup, vault sync, commit, push, reset, change scan or site publish it invoked. Help and version output work before Reginald loads its configuration. To verify tracing on the host with a read-only command, run `sudo reginald outbox list` and look for `reginald outbox list` in **Live**.

To inspect email, expand an `outbox drain` or `outbox retry` trace, or filter directly for its deliveries.

```sql
service_name = 'reginald' AND span_name = 'outbox.send'
```

Each delivery span includes the delivery and subscriber IDs, provider, event count, attempt count and outcome. A retry keeps the same delivery ID. The outcome is `sent` when the provider accepts the message, `failed` when it returns a failure, `blocked` when the allowlist prevents sending, and `dry_run` for a preview. Failed and blocked deliveries appear at error level even when the command itself exits successfully. An empty drain has a command span with no delivery children. For the provider's error detail, run `sudo reginald outbox list --status failed`.

## Inspect a Pydantic AI task in Logfire

With Logfire enabled, open **Live** and expand the task’s `run.agent` span. All runners attach available aggregate token counts, turns and available cost to this span and feed the same `reginald.agent.*` metrics. Filter the metrics by `runner = pydantic_agent` for Pydantic AI tasks, including OpenRouter. For these tasks each model request counts as one turn. A tool call followed by a second model request counts as two turns. The USD figure is Pydantic AI’s estimate when pricing is available. OpenRouter’s billing records give the actual charge.

Inside `invoke_agent`, Pydantic AI emits a `chat` span for each model request and an `execute_tool` span for each tool execution.

The Pydantic AI runner logs its start to the journal. Its per-call detail comes from these spans. The `logging.agent_turns` switch controls Claude’s per-turn summaries and Codex’s journal entries for individual tool calls.

## Find cost, tokens and turns

After deploying this version, choose a time range that includes a new agent run. In **Agents**, select the task by its title. **Metrics** shows trends and **Runs** lets you open one run. In **Live**, expand `run.agent` for the SDK totals and a `chat` span for an individual model response. **LLMs** groups model calls by provider and model. [Logfire Agents](https://pydantic.dev/docs/logfire/observe/agents/) and [Logfire LLMs](https://pydantic.dev/docs/logfire/observe/llms/)

In **Metrics Explorer**, open the **reginald** namespace. Each agent invocation contributes one observation per available value. Filter or break down by `task`, `runner` or `model`. [Metrics Explorer guide](https://pydantic.dev/docs/logfire/observe/metrics-explorer/)

| Metric | How to read it |
| --- | --- |
| `reginald.agent.cost` | Select **sum** for estimated USD spent over the time window, or **avg** for cost per run. |
| `reginald.agent.turns` | Select **avg** for turns per run. Claude reports its turn count and the Pydantic AI runner counts model requests. Codex counts conversation turns. Several tool calls can share a turn. |
| `reginald.agent.tool_calling_turns` | Select **avg** or **p90** for tool-calling turns per agent invocation. Claude counts main-agent responses and Pydantic AI counts model responses that request at least one tool. Codex reports one conversation turn per job, so its value is zero or one. |
| `reginald.agent.tokens` | Break down by `token_type` and select **sum**. `input_tokens` includes `cache_read.input_tokens` and `cache_creation.input_tokens`. Add only `input_tokens` and `output_tokens` to get the total. |

For one row per invocation, run this in **SQL Workbench**. The run ID matches the host's run history. A blank cell means the SDK supplied no figure for that field. A reported zero appears as zero.

```sql
SELECT start_timestamp,
       attributes->>'run_id' AS run_id,
       attributes->>'task' AS task,
       attributes->>'num_turns' AS turns,
       attributes->>'tool_calling_turns' AS tool_calling_turns,
       attributes->>'gen_ai.usage.cost_usd' AS cost_usd,
       attributes->>'gen_ai.aggregated_usage.input_tokens' AS input_tokens,
       attributes->>'gen_ai.aggregated_usage.output_tokens' AS output_tokens,
       attributes->>'gen_ai.aggregated_usage.cache_read.input_tokens' AS cache_read_tokens,
       attributes->>'gen_ai.aggregated_usage.cache_creation.input_tokens' AS cache_write_tokens
FROM records
WHERE service_name = 'reginald' AND span_name = 'run.agent' AND kind = 'span'
ORDER BY start_timestamp DESC
LIMIT 50
```

Claude's run cost comes from the SDK. Its per-model usage totals include subagents when the CLI supplies that breakdown. Individual Claude model spans cover streamed responses from the main agent. Their costs use the installed model prices and can differ from the whole-run estimate. A run that fails can still consume tokens and incur cost. If Claude stops before sending a final result, the turn count reflects the distinct main-agent responses Reginald observed. Individual model spans retain the usage received before the interruption. Reginald also saves reported run costs in its run history and includes them in the monthly budget calculation. Usage reporting works with `logging.agent_turns` off. Dry runs and tasks skipped for lack of work produce no agent measurements. [Claude SDK cost tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking)

## Diagnose a Claude run that reports “success” as an error

The Claude CLI can flag a result as failed while leaving its subtype set to `success`. When its list of errors is empty, the Python SDK can use that subtype as the exception text. The run failed. That text alone gives no cause.

Open the failed `invoke_agent` or `run.agent` span in **Live** and read `error.message`. Reginald includes the CLI's diagnostic, assistant error category, HTTP status and stderr when available. For example, `rate_limit` with HTTP 429 identifies a rate limit. On the host, the following command prints the latest run records with their `error` fields.

```bash
sudo reginald --json task run-history --limit 10
```

For a trace from before this change, check the same run in the journal or run history. If Claude supplied no further detail there, the cause of that failure is unknown.

## Read the host journal

On the production host, read the last 24 hours of scheduled research runs with this command. Look for the same task name, turn number and tool name as in Live.

```bash
sudo journalctl -u reginald-task@research-new-developments.service --since '24 hours ago' --no-pager
```

To follow the next lines as they arrive, run the following command. Press Ctrl-C to stop watching.

```bash
sudo journalctl -fu reginald-task@research-new-developments.service
```

## Choose the level of detail

| Level | What the Claude runner records |
| --- | --- |
| `INFO` (default) | Tool name and a target such as a path, pattern, query or URL, capped at 120 characters. Several calls can share a turn number. A turn without a call emits `no tool calls`. The completion line reports elapsed time, turn count and cost when available. |
| `DEBUG` | Adds tool inputs, results marked `ok` or `ERROR`, agent text, and SDK system message types. Each input, result and text excerpt is capped at 300 characters and can contain vault content. |
| `WARNING` or `ERROR` | Shows records at or above the selected severity. Tool-call summaries use `INFO`. |

Reginald resolves the level at startup, in this order:

1. **One invocation.** The global `-v` or `--verbose` flag selects `DEBUG`. Place it before the subcommand. Use `sudo reginald -v task run research-new-developments` when you intend to start a real production research run.
2. **Host override.** Run `sudoedit /etc/reginald/env` and set `REGINALD_LOG_LEVEL=DEBUG` on its own line. The next scheduled run reads the file automatically. Remove the assignment to restore the configured level.
3. **Local YAML settings.** Set `logging.level` in `config/base.yaml` for both environments or override it in `config/dev.yaml` or `config/prod.yaml`. The separate `logging.agent_turns` setting controls the per-turn `INFO` lines and defaults to `true`. [Base configuration template](../config/base.yaml-example)

## When records are missing

- **The Tools tab is empty despite journal tool calls.** Select a run recorded after deploying structured tool tracing. Older Claude runs can have tool names only in the mirrored journal messages. In **Live**, filter on `service_name = 'reginald'`, include **Info**, and expand the agent span to see those lines.
- **The journal has tool calls, but Live has no Reginald records.** Run `sudo reginald doctor`. A `[WARN]` with `Tracing unavailable` means SDK setup failed. Read the startup warning and check that `/etc/reginald/env` supplies a project write token for the configured region. A `[WARN]` with `project URL unavailable` means the SDK initialised and could not yet report a project URL. The SDK can read it from saved credentials or obtain it by validating a token with Logfire in the background. To confirm delivery, look for a recent Reginald record in Live with the expected project, environment and time range. [Configure Logfire](INSTALL.md#configure-logfire)
- **Tool names are visible, but inputs and results are missing.** Check `logfire.capture_chars` in the environment used for that run. Open the conversation on the agent span of a run recorded with a positive limit. Older Claude runs may have excerpts only in mirrored debug logs.

## Task completion alerts

With Logfire enabled, Reginald emits one `run.completed` log after committing a task's final status to SQLite. Its `task`, `run_id` and `status` attributes match the run history. Scheduled, manual and dispatched tasks all count, including a successful `action-edits` with no work. Dry runs emit no completion. A stopped host, a missed timer or a process that exits before finalisation leaves a gap. The standalone `reginald vault identify-changes` command emits an `identify-changes.walk` span when it scans new commits. Its timer and the outbox run independently of task completion reporting.

After deploying, open the Logfire project shown by `sudo reginald doctor`. In **Live**, select **prod**, include **Info**, and filter on `service_name = 'reginald' AND span_name = 'run.completed'`. After the next task finishes, compare its attributes with the corresponding row on the host:

```bash
sudo reginald task run-history
```

In **Alerts → New alert → Custom query**, create the following alerts and attach your notification channel to each. [Logfire alerts](https://pydantic.dev/docs/logfire/observe/alerts/)

**Failed tasks.** Check every five minutes with a five-minute lookback and notify when the query has any results. Each row identifies a failed run.

```sql
SELECT start_timestamp, attributes->>'task' AS task,
       attributes->>'run_id' AS run_id, attributes->>'status' AS status
FROM records
WHERE service_name = 'reginald' AND deployment_environment = 'prod'
  AND span_name = 'run.completed'
  AND attributes->>'status' IN ('failed', 'publish_failed')
```

**Missed task.** Create one absence alert per enabled task. Check the live schedule and timezone first. Allow time for timer jitter, the shared run lock and normal execution.

```bash
systemctl list-timers 'reginald-task@*.timer'
systemctl cat reginald-task@research-new-developments.timer
timedatectl show --property=Timezone --value
```

For a research timer scheduled at 09:00 on weekdays in Europe/London, this example allows an hour to finish. From 10:00 until midnight on each weekday it returns one row if no successful completion has arrived since 09:00 that day. A manual rerun that succeeds in that window also clears the alert. Use a 24-hour lookback, check every five minutes, and select **The query starts having results** to notify once per missed deadline. Match the task, timezone, days and hours to the installed timer when adapting this example.

```sql
WITH clock AS (
    SELECT now() AT TIME ZONE 'Europe/London' AS local_now
), successes AS (
    SELECT start_timestamp AT TIME ZONE 'Europe/London' AS finished
    FROM records
    WHERE service_name = 'reginald' AND deployment_environment = 'prod'
      AND span_name = 'run.completed'
      AND attributes->>'task' = 'research-new-developments'
      AND attributes->>'status' = 'success'
)
SELECT 'research-new-developments' AS overdue_task
FROM clock
WHERE EXTRACT(DOW FROM local_now) BETWEEN 1 AND 5
  AND EXTRACT(HOUR FROM local_now) >= 10
  AND NOT EXISTS (
      SELECT 1 FROM successes
      WHERE CAST(finished AS DATE) = CAST(local_now AS DATE)
        AND EXTRACT(HOUR FROM finished) >= 9
  )
```
