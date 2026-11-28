# AGENTS.md

Instructions for AI agents working on this repository. Read this before making
changes; it records things that are non-obvious, plus mistakes already made here
so they are not repeated.

`README.md` introduces Reginald. `docs/INSTALL.md` covers installation.
`docs/DEPLOY.md` covers publishing releases and updating an installed host.
`docs/USAGE.md` is how to operate.
This file is how to *work on* the project.

---

## What this is

A headless orchestrator that runs Claude agents against an Obsidian vault on a
schedule, derives content events from `git diff`, and emails them to
subscribers. It runs unattended on a small Linux box — a VPS or a Raspberry Pi.

**Reginald is a contributor to the vault, not its owner.** The vault is a
shared git repository. Humans and (later) other agents contribute under their
**own git identities**, from their own clones, through ordinary git flow.
Reginald's host checkout is just Reginald's working copy: it pulls, does its
own tasks, commits its own output, and pushes. Its extra responsibilities are
stewardship (uuid backfill, escalation repair, DB mirroring, publishing) and
notification (noticing everyone's changes and mailing subscribers about them).
Subscribers are members of the same knowledge-base team, not an external
mailing list — everyone is opted into both new and updated content by default.

Three consequences shape almost every decision:

- **It runs while nobody is watching.** A 09:00 timer failure is discovered by a
  missing brief, not a red build. Prefer failing loudly and early over degrading
  quietly.
- **It spends money and sends real email.** A bug can mail every subscriber, or
  burn API budget in a loop. Dry-run paths and the dev overlay's safety rails
  exist for this reason — do not weaken them for convenience.
- **The vault is the source of truth, not the database.** SQLite holds
  bookkeeping only (events, the escalation mirror, the callout queue, the
  change detection cursor). Never write vault content from the DB.

### How vault changes become emails and callout work

`IdentifyChangesService` scans new commits on `main` from any author. It records
content events under the commit's Git author for the outbox and queues callouts
for `action-edits`. Only `action-edits` selects `identify-changes` as a pre-step.
Every non-dry task run that reaches the end of the commit stage also scans for
changes. Between task runs, `reginald vault identify-changes` and its daily timer
use the same Git cursor. The scan is deterministic, uses no model and is safe
to repeat. Before changing it, read the [change detection guide](docs/USAGE.md#identifying-vault-changes)
and the [callout protocol](docs/USAGE.md#the-callout-protocol).

## Environment

- **Python 3.12**, pinned by `.python-version`. Managed with **uv** against a
  committed `uv.lock`.
- `make dev` / `make test` / `make lint`. All run through `uv run`.
- Keep only `.yaml` files in `config/` and its subdirectories, with one exception
  for the top-level `*.yaml-example` templates. Use one `.yaml` file per MCP
  server in `config/tools.d/` and one per task in `config/tasks.d/`.
- Edit `config/*.yaml-example` when changing shared or environment defaults. Git ignores each checkout's `config/base.yaml`, `config/dev.yaml` and `config/prod.yaml`. Document any settings operators need to apply after an update in the [configuration guide](docs/USAGE.md#configuration).
- No network in tests. No API calls. Tests must stay fast and offline.
- Deployment is **pull-based**: the host fetches and updates itself via
  `deploy/update.sh`, run over SSH on the box. There is no push-from-Mac path
  and nothing depends on mDNS. Do not reintroduce either.

```bash
make dev      # sync the venv from uv.lock
make test     # pytest
make lint     # ruff
```

Changing dependencies means editing `pyproject.toml`, running `uv lock`, and
**committing the lock**. Deploys install it with `--frozen`, so an unlocked
change silently does nothing on the host until someone relocks.

---

## Working rules

### Check the documentation with every change

Before editing, contributors must read the [task steps guide](docs/STEPS.md)
and check all repository documentation for conventions and descriptions the
change affects. Include the README, the guides under `docs/` and the deployment
notes. Follow the documented naming, layout, configuration and writing
conventions in future edits.

Before finishing, check the documentation against the implementation and update
every affected guide, example and link in the same change. For a step change,
check the registry in `build_steps()`, execution in `RunService` and the order in
the shipped task YAML. Keep the steps guide's inventory and examples in sync
with all three.

### Verify, do not assert

The most common failure in this repo has been confident, wrong statements about
behaviour. Before writing that something works a certain way, check it.

- Read the source rather than inferring from a filename or a doc.
- After a fix, **exercise it** — run the command, drive the flow. `make test`
  passing is not evidence that the thing you changed works.
- When you add a regression test, **prove it fails against the bug**. Stash the
  fix, run the test, watch it fail, restore. A regression test that passes
  against broken code is worse than none.
- Beware false passes. A test harness here once reported PASS while silently
  doing nothing, because it ran under `zsh` where a command was not resolving.
  If a check passes suspiciously fast or cleanly, confirm it actually ran.

### Report honestly

- If tests fail, say so and show the output.
- If you could not test something, say which part and why. Several things here
  can only be verified on a real host — say that rather than implying coverage.
- If you gave wrong information earlier, correct it plainly and say what it
  changed. This has happened more than once and owning it immediately is
  cheaper than the confusion.

### Never hardcode a second source of truth

`__version__` was hardcoded in `reginald/__init__.py`, so `release.sh` could bump
`pyproject.toml`, tag, and push a release while `reginald --version` reported the
previous number. It now derives from package metadata.

Before duplicating any value that exists elsewhere, derive it instead. The same
applies to generated output: `install.sh` reads schedules out of the `.timer`
files rather than repeating them, so the printed commands cannot drift.

---

## Writing commands for humans to run

**Every command a script prints, or a doc gives, must be complete and
copy-pasteable.** No placeholders the reader has to resolve, no "etc.", no
"see X for the rest".

This was a real defect. `install.sh` used to end with:

```
4. systemctl enable --now reginald-task@draft-daily-brief.timer  (etc — see TIMERS.md)
```

Two problems: the reader had to go and construct six commands from a table, and
five of the timer units did not exist at all — the table was describing files
someone was expected to hand-write. Now the script enumerates the units it
installed and prints one enable line each, with the schedule as a comment.

When emitting instructions:

- List every command in full, in order.
- Generate them from the real artefacts where possible, so they cannot go stale.
- Annotate anything non-obvious inline (`# Mon..Fri 06:30`).
- State what must be true first ("only once `doctor` exits successfully").
- Say what success looks like, and give the command that checks it.

## Writing deploy scripts

- **Never prompt.** These run over pipes and from other scripts, where a prompt
  hangs forever rather than failing. `GIT_TERMINAL_PROMPT=0` and
  `ssh -o BatchMode=yes` turn credential prompts into immediate errors.
- **Preflight before expensive work.** `install.sh` checks both repos are
  readable before apt, Node, Electron and swap — a missing deploy key used to
  cost ten minutes before surfacing.
- **Check preconditions up front, all of them.** A script that fails halfway
  leaves state to unpick by hand. `release.sh` validates branch, cleanliness,
  sync, and tag availability before touching anything.
- **Make re-runs safe, and say what they do not do.** `install.sh` is safe to
  re-run but guards the clone on `.git` existing, so it never updates anything —
  which is not what "safe to re-run" suggests. That needed spelling out.
- **Stage remote work to a file**, then `sudo bash <file>`. Piping to
  `sudo bash -s` occupies stdin, leaving sudo unable to prompt for a password.
- **Never accept a password as an argument.** `argv` is world-readable via `ps`
  and lands in shell history. Leave auth to ssh; document `ssh-copy-id` or
  ControlMaster multiplexing instead.
- `SSH_OPTS` is passed to `scp` as well as `ssh`, so examples must use the `-o`
  long form (`-o Port=2222`, not `-p 2222`, which `scp` reads differently).

## Operating on the host

Two traps, both hit during the first real install. Both only affect manual
commands — the systemd units set `WorkingDirectory` and `User` correctly.

1. **`cd /opt/reginald` first.** `runuser` switches user but keeps the caller's
   working directory. From `/root` (mode `0700`) the `reginald` user cannot stat
   anything, and the failure surfaces as an opaque
   `PermissionError: 'pyproject.toml'` from inside logfire.
2. **Prefix anything touching the vault, DB or venv with
   `runuser -u reginald --`.** Those paths are owned by the `reginald` user.
   Running git as root gives "dubious ownership" — and git's suggested
   `safe.directory` fix is worse than the error, because root-owned files then
   accumulate and break unattended runs later.

## The vault

`vault_path` must be the vault root **and** the repo root. `Topics/`,
`Setup/Tasks/`, `Setup/Logs/` and `git -C <vault_path>` all hang
off it.

If a vault repo wraps its content in a subfolder, `topics sync` reports
`synced 0 active topic(s)`. Repointing `vault_path` at the subfolder looks like
a fix and is a trap: git reports diff paths relative to the **repo** root, so
they arrive prefixed and never match the event patterns — runs would succeed,
commit, publish, and silently never raise an event or send email. Flatten the
repo instead.

## Releases

```bash
./deploy/release.sh 0.2.0        # -n dry run, -y skip the push prompt
```

Use a GitHub CLI (`gh`) account or token with release write access to `origin`.
The script bumps `pyproject.toml`, refreshes `uv.lock`, runs lint and tests, and
commits and tags the release. Once the push succeeds, it publishes a GitHub
**pre-release** for that tag with empty notes, including for plain version
numbers. You can edit the notes on GitHub later. If publication fails after
the push, the script prints the command to retry for the existing tag.
To finish an interrupted release whose version is already committed, pass
`--force`. It can reuse a local tag and always refuses an existing remote tag.
See the [deployment guide](docs/DEPLOY.md#finish-an-interrupted-release).
Do not bump versions by hand.

- **Never rewrite a pushed tag.** Cut the next patch version instead. A tag that
  has been pushed may already have been fetched.
- Rollback reverts **code, not the database**. Alembic migrations are not
  downgraded; crossing a schema change backwards needs the backup `db migrate`
  takes. Say so whenever rollback is mentioned.

## Testing

- `tests/` is offline and fast. Keep it that way.
- Use real git repositories in `tmp_path` for anything touching `VaultGit` — see
  `tests/test_manual_edits.py`. Set repo-local `user.email`/`user.name` so tests
  do not depend on global git config.
- Runners are stubbed via the `TaskRunner` protocol. `TaskConfig.runner` is a
  `Literal`, so register a stub under an existing key rather than inventing one.
- `topics.sync()` writes UUIDs into `Index.md` frontmatter, so it **dirties the
  vault**. Tests asserting on a clean tree must settle it first.

## Things that are deliberate

Do not "fix" these without asking:

- **Everyone's content raises events — the substantive-change rule decides
  what.** In a multi-author vault, a contributor's push *is* the point; it gets
  picked up by change detection and can reach subscribers. A modified note raises an `updated` event only
  if its body, *with callout blocks stripped*, changed between a commit and its
  parent — so callout dialogue, frontmatter housekeeping (uuid/`last_verified`
  stamps) and pure renames mail nobody, while a real correction by anyone does.
  This **replaces** the old "manual edits raise no events" doctrine and the
  `events ingest` opt-in, both now gone. The rule lives in `publisher/diff.py`
  (`substantive_content`); don't reintroduce a range-based `record_range`.
- **Host-dirty trees are salvaged, not hidden.** A dirty tree at run start is
  committed as `reginald: salvage uncommitted changes` (authored Reginald) and
  evented like any other commit. Editing on the host is an anti-pattern —
  contribute from your own clone — and `doctor` reports recent salvages for
  exactly that reason.
- **A failed run resets the vault** to `pre_commit`. This is why salvage must
  happen first — otherwise `reset --hard` + `clean -fd` would destroy
  uncommitted work.
- **The pull is fetch + rebase, never `--ff-only`.** With contributors pushing
  concurrently, ff-only deadlocks the moment Reginald holds an unpushed commit.
  Reginald rebases *its own* unpushed commits onto `origin/main`; a conflict
  aborts loudly and fails the run (`RebaseConflictError`) — it never
  auto-resolves another author's work.
- **Escalation file moves are code, not agent.** The agent toolset cannot
  delete files, so it cannot move an escalation to `Resolved/`. The agent sets
  `status:` frontmatter; `escalations-sync` performs the move and enforces the
  `status ⟺ folder` invariant in both directions.
- **Escalation numbers are assigned only by sync**, never in filenames.
  Filenames are date-slug (`YYYY-MM-DD - slug.md`) so two clones can't collide;
  `number:` is filled by `EscalationService`. Don't put ESC-NNN in a path.
- **With no arguments, `update.sh` reapplies the selected tag or follows the
  selected branch on `origin`.** Passing a branch or tag explicitly switches
  the deployment target. [Deployment guide](docs/DEPLOY.md#deploy-the-update)
- **`update.sh` installs new timer units but does not enable them.** A deploy
  should not start firing jobs nobody asked for.
- **Deletions raise no content events**, and malformed frontmatter does not drop
  one — losing metadata beats losing the notification. A deleted callout, by
  contrast, marks its work item `not-actioned` (the author withdrew it).
- **Local civil dates** (`date.today()`) are intentional where they appear:
  "today" in a UK/EU regulatory brief means the operator's calendar day, not a
  UTC instant. They carry `# noqa: DTZ011`. Stored timestamps are tz-aware UTC.
- **Lint rules are selected explicitly** in `pyproject.toml` rather than
  inheriting ruff's defaults, which widen between releases. A ruff upgrade
  should not silently change what `make lint` enforces.

## Known gaps

Documented in `docs/USAGE.md`; do not present them as working:

- `subscriber --frequency daily|weekly` is stored but never read — every
  subscriber behaves as `per-run`.
- `reginald serve` is a stub.
- The `--topic` scope restriction is advisory, appended to the prompt. The event
  filter that follows it *is* enforced in code.
- `deploy/install.sh` has never been fully exercised on a fresh host by an
  agent; it needs a real machine. Treat changes to it as untested.
