# Task steps

A **task** is a job Reginald can schedule or run by name. 

A **step** is a named Python operation that the task asks Reginald to run before or after its agent.  It identifies shared code that might be useful to more than one task, e.g. to ensure consistency in the vault.

A **service** implements the underlying operation in code (note services can be broader, and also implement CLI commands directly).

For example, the `backfill-uuids` step calls `VaultService.backfill_uuids()`, so every task can give new notes a UUID without spending a model turn. The task's runner handles the model call. [Task definitions](TASKS.md) · [Runners](RUNNERS.md)

A task might call steps across multiple services.  The task short names are resolved to the relevant service by AppContext - see below.

## How a task calls a step

The `pre` and `post` lists in a task's YAML select steps by name. For example, `triage-inbox` asks Reginald to run three operations after the agent finishes. The same three appear at the end of every shipped task. [Triage task definition](../config/tasks.d/triage-inbox.yaml)

```yaml
post: [normalize-frontmatter, backfill-uuids, set-publish]
```

`action-edits` also needs to find work before the agent starts and check its answers afterwards. It is the only shipped task with `pre` steps. [Action edits task definition](../config/tasks.d/action-edits.yaml)

```yaml
pre: [identify-changes, escalations-sync]
post: [callouts-verify, escalations-sync, normalize-frontmatter, backfill-uuids, set-publish]
```

Both lists default to empty. An override in `dev.yaml` or `prod.yaml` replaces the whole list for that phase. [Configuration loader](../src/reginald/core/config.py)

At startup, `AppContext` passes the name-to-function mapping from `build_steps()` to `RunService`. For immediate and queued runs, `RunService._execute_locked()` calls `_run_steps()` under the vault's run lock. It looks up and calls each step in the order shown in the task YAML. An unknown name raises `RunError` when the run reaches it. [Run service](../src/reginald/services/runs.py) · [Step registry](../src/reginald/core/context.py)

1. **Before the agent**, Reginald runs the `pre` list after the Git sync stage. It joins any manifest text the steps return and inserts it wherever the prompt uses `{{MANIFEST}}`.
2. **Once all pre-steps finish**, Reginald calls the task's runner unless a step returned `skip_agent=True`. An `action-edits` run with an empty manifest therefore uses zero model turns.
3. **After the agent succeeds or is skipped**, Reginald runs the `post` list before the optional Git commit, so repairs can land in the same commit as the agent's edits. A runner-reported failure bypasses the post-steps.

A task dry run skips both step lists and the agent. Every non-dry task run that reaches the end of the commit stage scans for new commits. The standalone `reginald vault identify-changes` command uses the same scan and cursor. Only the `action-edits` pre-step also prepares a manifest for the agent. [Run pipeline](USAGE.md#the-run-pipeline) · [Identifying vault changes](USAGE.md#identifying-vault-changes)

## Registered steps

| YAML name | Used by the shipped tasks | What it does | Service method |
| --- | --- | --- | --- |
| `identify-changes` | `action-edits` pre | Walks new commits for content events and callout work. Builds the prompt manifest from pending callouts addressed to Reginald or left unaddressed. Saves their IDs for verification and asks to skip the agent if the list is empty. | `IdentifyChangesService.run(pull=False)` and `IdentifyChangesService.manifest()` |
| `escalations-sync` | `action-edits` pre and post | Assigns escalation numbers, reconciles each note's status with its folder, and updates the SQLite mirror from the vault. Running it again after the agent picks up newly resolved escalations. | `EscalationService.sync()` |
| `callouts-verify` | `action-edits` post | Rereads the notes for this run's manifest. Marks a pending item as actioned for this run only when it finds the matching callout with a nested reply. | `IdentifyChangesService.verify_actioned()` |
| `normalize-frontmatter` | All tasks, post | Collapses duplicate top-level YAML keys in vault notes, keeping the last value. This prepares the frontmatter for UUID backfill and publish flags. | `VaultService.normalize_frontmatter()` |
| `backfill-uuids` | All tasks, post | Gives each eligible Markdown note with a missing or invalid `uuid` a fresh UUID. Preserves valid UUIDs. | `VaultService.backfill_uuids()` |
| `set-publish` | All tasks, post | Sets `publish: false` on notes matched by `.publish-ignore`. Elsewhere it supplies `publish: true` when the flag is missing and preserves explicit settings. The change detection service publishes the site when it processes new commits. | `VaultService.set_publish_flags()` |

## Adding or changing a step

Keep the vault or database operation on the service that owns it. In `build_steps()`, register a small function under a lowercase, hyphenated YAML name. Each function receives a `StepContext` and returns a `StepOutcome`. The run service owns these types. [Step registry](../src/reginald/core/context.py) · [Step types](../src/reginald/services/runs.py)

- Use `StepContext.run_id` to identify the run. Its `shared` dictionary lasts for that run only. For example, `identify-changes` saves the manifest item IDs there and `callouts-verify` reads them.
- Return a short `detail` for the INFO log. Pre-steps can also return `manifest` text or set `skip_agent`. With Logfire enabled, each invocation has a `run.step` span whose `step` attribute is the YAML name. [Logging guide](LOGGING.md)
- Select the step in each task that needs it, in dependency order. Keep repairs safe to repeat and update the examples and table above when the wiring changes. [Task definitions](../config/tasks.d/)

After `make dev`, run the following command from the repository root. All pipeline tests should pass, including verification of the manifest items and skipping the agent on an empty queue. [Pipeline tests](../tests/test_pipeline_steps.py)

```bash
uv run pytest -q tests/test_pipeline_steps.py
```
