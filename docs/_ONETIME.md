# Breaking changes - onetime upgrade

Prior to open-sourcing, I'd released 21 versions of this code - from v0.1.0 through to v0.21.0

My production copy was stuck on v0.17.0.  Upgrading was failing due to breaking changes - this was expected.  At v0.19.0 I had to take steps to bring my production copy back up to date.  This is the procedure I used.  This is left in the docs as it might be useful for future breaking changes or debugging.

It installed a fresh v0.19.0 checkout and virtual environment at `/opt/reginald`. It uses the existing vault, service account and credentials in `/etc/reginald/env`. It carries over subscribers, subscriptions, deliveries, run history and the last scanned vault commit.

Run the blocks in order in **one root Bash session on the server**. They assume the standard configuration directory `/opt/reginald/config` and production vault and database paths outside the application checkout. Allow space for a second virtual environment and two copies of the SQLite database.

## 1. Prepare the replacement

The first two checks confirm that the checkout and the installed CLI both reached v0.19.0. Record the recovery directory and the command printed for restoring your shell variables if the SSH session ends.

```bash
set -euo pipefail
umask 022
cd /opt/reginald

test "$(git rev-parse HEAD)" = "$(git rev-parse 'refs/tags/v0.19.0^{commit}')"
test "$(runuser -u reginald -- .venv/bin/reginald --version)" = "reginald 0.19.0"
saved=$(mktemp -d /opt/reginald-reinstall.XXXXXXXX)
fresh=$(mktemp -d /opt/reginald-new.XXXXXXXX)
chmod 755 "$fresh"
printf 'saved=%q\nfresh=%q\n' "$saved" "$fresh" > "$saved/paths"
printf 'Recovery directory: %s\nFresh checkout: %s\n' "$saved" "$fresh"
printf 'To restore the variables in a root Bash session: source %q\n' "$saved/paths"

git clone --branch v0.19.0 "$(git remote get-url origin)" "$fresh"
test "$(git -C "$fresh" rev-parse HEAD)" = "$(git rev-parse HEAD)"

for name in base dev prod; do
    cp "$fresh/config/$name.yaml-example" "$fresh/config/$name.yaml"
done

grep -nE '^(vault_path|db_path):' config/base.yaml config/prod.yaml
nano "$fresh/config/base.yaml" "$fresh/config/prod.yaml" "$fresh/config/dev.yaml"
```

Use the original files in `/opt/reginald/config` as your reference. In the fresh files, enter the **verified Postmark sender, published site URL and existing production vault and database paths**. Carry over any customised allowlists, budget, Git identity and environment settings. The release supplies all seven standard tasks in `config/tasks.d/`. Put any customised task permissions in the environment overlay under `tasks.<name>.tools`. The old `allowed_tools` key causes the validation failure.

## 2. Pause scheduled work

Run this block once. It records which Reginald timers are enabled and which are active, then waits for any running service to finish. Keep manual Reginald jobs paused through the transfer. If you reconnect, use the saved timer lists when resuming. Taking a second snapshot after stopping the timers would record an empty schedule.

```bash
systemctl list-unit-files --type=timer --state=enabled --no-legend --no-pager 'reginald-*.timer' |
    awk '{print $1}' > "$saved/enabled-timers"
systemctl list-units --type=timer --state=active --plain --no-legend --no-pager 'reginald-*.timer' |
    awk '{print $1}' > "$saved/active-timers"

xargs -r -a "$saved/enabled-timers" systemctl disable
xargs -r -a "$saved/active-timers" systemctl stop

while true; do
    running=$(systemctl list-units --type=service --state=active,activating,deactivating \
        --plain --no-legend --no-pager 'reginald-*')
    if [[ -z "$running" ]]; then break; fi
    sleep 5
done
```

## 3. Transfer the database

v0.17.0 used Alembic revision `0005`. v0.19.0 starts a new migration history at `0001` and removes `subscribers.manage_token`. This block takes a consistent SQLite backup, creates the v0.19.0 schema in a separate file and copies each application table with its original IDs. It also transfers the old `intake` cursor to `identify-changes` if that cursor has yet to be created.

```bash
cd /opt/reginald
runuser -u reginald -- env REGINALD_CONFIG_DIR="$fresh/config" .venv/bin/python - <<'PY'
import os
import sqlite3
import subprocess
from pathlib import Path
from tempfile import mkdtemp

import yaml
from reginald.core.config import load_config
from reginald.db.schema import Base

cfg = load_config("prod")
old = {}
for name in ("base", "prod"):
    old.update(yaml.safe_load(Path(f"config/{name}.yaml").read_text()) or {})

for key in ("db_path", "vault_path"):
    before = Path(old[key]).expanduser().resolve()
    after = getattr(cfg, key).resolve()
    if after != before or after.is_relative_to(Path.cwd()) or not after.exists():
        raise SystemExit(f"Check {key}: existing {before}, replacement {after}")

path = cfg.db_path.resolve()
live = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
work = Path(mkdtemp(prefix="reginald-reinstall-", dir=path.parent))
backup = work / "original.db"
with sqlite3.connect(backup) as original:
    live.backup(original)
print(f"Original database backup: {backup}", flush=True)

replacement = work / "v0.19.0.db"
subprocess.run(
    [".venv/bin/alembic", "upgrade", "head"],
    env={**os.environ, "REGINALD_DB_PATH": str(replacement)},
    check=True,
)

new = sqlite3.connect(replacement, uri=True)
new.execute("ATTACH DATABASE ? AS old", (backup.as_uri() + "?mode=ro",))
new.execute("PRAGMA foreign_keys=ON")
with new:
    new.execute("BEGIN")
    new.execute("PRAGMA defer_foreign_keys=ON")
    for table in Base.metadata.sorted_tables:
        name = table.name
        columns = ", ".join(column.name for column in table.columns)
        new.execute(f"INSERT INTO main.{name} ({columns}) SELECT {columns} FROM old.{name}")
        count = new.execute(f"SELECT count(*) FROM main.{name}").fetchone()[0]
        if count != new.execute(f"SELECT count(*) FROM old.{name}").fetchone()[0]:
            raise SystemExit(f"Row count differs for {name}")
        print(f"{name}: {count}")

    new.execute("""UPDATE cursors SET name='identify-changes' WHERE name='intake'
                   AND NOT EXISTS (SELECT 1 FROM cursors WHERE name='identify-changes')""")
    if new.execute("PRAGMA foreign_key_check").fetchone():
        raise SystemExit("Foreign key check failed")
    if new.execute("PRAGMA integrity_check").fetchone() != ("ok",):
        raise SystemExit("Database integrity check failed")

new.backup(live)
new.close()
live.close()
print(f"Transferred the verified database to {path}")
PY
```

Record the printed path to `original.db`. The final line confirms that the row counts, foreign keys and integrity checks passed and the verified copy was written to the live database path.

## 4. Switch to the fresh checkout

After the database transfer succeeds, move the previous installation into the recovery directory and deploy the exact release tag. The updater builds the virtual environment, installs the wrapper and systemd units, and runs `doctor`.

```bash
cd /opt
test ! -e "$saved/previous-install"
mv -T /opt/reginald "$saved/previous-install"
mv -T "$fresh" /opt/reginald
cd /opt/reginald

./deploy/update.sh refs/tags/v0.19.0
reginald --version
reginald --env prod subscriber list --all
reginald --env prod task run-history
```

Check for `reginald 0.19.0`, a successful production `doctor` report, the expected subscribers (including inactive ones) and familiar recent runs. If the updater stops after the directory switch, correct the reported issue and rerun `./deploy/update.sh refs/tags/v0.19.0` from `/opt/reginald` before continuing.

## 5. Resume the previous schedule

Once the checks pass, restore the enabled and active timer lists. The former `reginald-intake.timer` now runs as `reginald-identify-changes.timer`.

```bash
sed 's/^reginald-intake\.timer$/reginald-identify-changes.timer/' "$saved/enabled-timers" |
    sort -u | xargs -r systemctl enable
sed 's/^reginald-intake\.timer$/reginald-identify-changes.timer/' "$saved/active-timers" |
    sort -u | xargs -r systemctl start
systemctl list-timers --all 'reginald-*'
```

Check the next firing times. After the first scheduled task, run `reginald --env prod task run-history` and confirm it succeeded. You can then remove the archived application if you want to reclaim disk space. Keep the pre-transfer `original.db` backup. A return to v0.17.0 would also require restoring that database because a code rollback does not downgrade the schema.

```bash
rm -rI -- "${saved:?}/previous-install"
```
