# Timers

Reginald ships one timer per task and separate timers for vault change detection and the email outbox. `install.sh` copies the units into `/etc/systemd/system/` and prints the commands to enable them.

`reginald-task@.service` is a systemd *template*: `%i` becomes the task name,
so `reginald-task@triage-inbox.service` runs `reginald task run triage-inbox`. The
service is templated, but each timer is a real file because every task needs
its own `OnCalendar`.

| Unit | Schedule |
|---|---|
| `reginald-task@research-new-developments.timer` | `Mon..Fri 09:00` |
| `reginald-task@triage-inbox.timer` | `Mon..Fri 10:00` |
| `reginald-task@update-trackers.timer` | `Mon 08:30` |
| `reginald-task@key-dates-digest.timer` | `Mon 09:30` |
| `reginald-task@archive-sweep.timer` | `Mon *-*-01..07 10:00` (first Monday) |
| `reginald-task@source-verification.timer` | `Mon *-*-15..21 10:00` (third Monday) |
| `reginald-task@action-edits.timer` | `*-*-* 01:00` (daily, incl. weekends) |
| `reginald-identify-changes.timer` | `*-*-* 11:30` (daily) |
| `reginald-outbox.timer` | `Mon..Fri 12:00` |

## Enabling

```bash
sudo systemctl enable --now reginald-task@research-new-developments.timer
sudo systemctl enable --now reginald-task@triage-inbox.timer
sudo systemctl enable --now reginald-task@update-trackers.timer
sudo systemctl enable --now reginald-task@key-dates-digest.timer
sudo systemctl enable --now reginald-task@archive-sweep.timer
sudo systemctl enable --now reginald-task@source-verification.timer
sudo systemctl enable --now reginald-task@action-edits.timer
sudo systemctl enable --now reginald-identify-changes.timer
sudo systemctl enable --now reginald-outbox.timer

systemctl list-timers 'reginald-*'
```

Each timer can be enabled independently. Once `reginald doctor` exits successfully, enable the timers you want to run unattended.

Task runs and `reginald vault identify-changes` take the same run lock. If a task holds the lock, the scheduled scan waits, then resumes from the saved Git cursor. The outbox can send mail while a scan runs.

The `action-edits` task scans the vault in its `identify-changes` pre-step. Every non-dry task run that completes the commit stage also scans afterwards. The daily `reginald-identify-changes.timer` picks up contributor pushes between task runs, ahead of the weekday noon outbox drain.

## Changing a schedule

Edit the `.timer` here, then re-deploy. `update.sh` reinstalls the units and
restarts any that are already active, because **an enabled timer keeps its old
`OnCalendar` until restarted**. To change one by hand:

```bash
sudo systemctl edit --full reginald-task@triage-inbox.timer
sudo systemctl daemon-reload
sudo systemctl restart reginald-task@triage-inbox.timer
```

Check a calendar expression before trusting it — systemd will happily accept a
spec that never fires:

```bash
systemd-analyze calendar 'Mon *-*-01..07 10:00'   # prints the next elapse
```

## Renaming a task

`update.sh` copies units in but never removes one, so a rename leaves the old
timer enabled on the host, firing a task the config no longer defines. Retire it
by hand, on the host, after deploying the release that renames it:

```bash
sudo systemctl disable --now reginald-task@draft-daily-brief.timer
sudo rm /etc/systemd/system/reginald-task@draft-daily-brief.timer
sudo systemctl daemon-reload
sudo systemctl enable --now reginald-task@research-new-developments.timer

systemctl list-timers 'reginald-*'    # old unit gone, new one has a NEXT
```

Run history keeps the old name: `task run-history --task draft-daily-brief` still
finds everything from before the rename.

## Notes

* All timers set `Persistent=true`, so a run missed while the host was down
  executes on next boot rather than being skipped.
* Task timers set `RandomizedDelaySec=300` to avoid every job starting exactly
  on the minute.
* `reginald-task@.service` allows `TimeoutStartSec=3h`; a long agent run holds
  the lock for that whole period.

## Adding a task

1. Create a YAML file in `config/tasks.d/` with the task fields at the top level.
   Reginald registers the task under the filename without its extension.
   From the project checkout, run `uv run reginald --env dev task list` to see it listed.
2. Create its task note in the vault under `Setup/Tasks/` and set `prompt_file` to its vault-relative path.
3. Copy an existing `.timer` here, rename it to `reginald-task@<task>.timer`,
   and set `Description` and `OnCalendar`.
4. Deploy, then `systemctl enable --now reginald-task@<task>.timer`.

No new `.service` file is needed — the template covers every task.
