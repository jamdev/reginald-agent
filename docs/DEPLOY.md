# Releasing and deploying Reginald

Maintainers publish a version from the application checkout on their workstation. An operator then deploys that Git tag on the server. For a fresh server, start with the [installation guide](INSTALL.md). For an installed server, go straight to [Updating](#updating).

## Publish a new version

Start on a clean `main` branch in the application repository. You need Git, Bash, Python, Make, uv and the GitHub CLI. Your Git credentials must allow pushes to `origin`, and `gh` needs release write access to the same repository. Check the destination and your GitHub login first.

```bash
git status --short --branch
git remote get-url --push origin
gh auth status
```

Choose the next unused version. For example, to preview and then publish `1.0.3`, run each command in order.

```bash
./deploy/release.sh 1.0.3 --dry-run
./deploy/release.sh 1.0.3
```

The script bumps `pyproject.toml`, updates `uv.lock`, runs lint and tests, and creates a commit and an annotated tag. Confirm the push when prompted, or add `--yes` for unattended use. After the push, it creates a GitHub **Pre-release** with empty notes. You can edit the notes on GitHub later.

## Finish an interrupted release

If `pyproject.toml` already contains the intended version, check **both** the remote tag and the GitHub Release entry. The following example uses `v1.0.2` in the public Reginald repository.

```bash
git tag --list v1.0.2
git ls-remote --refs origin refs/tags/v1.0.2
gh release view v1.0.2 --repo jamdev/reginald-agent --json tagName,isPrerelease,url
```

**If the remote tag is missing**, the second command succeeds with no output. From clean `main`, preview the recovery and then run it.

```bash
./deploy/release.sh 1.0.2 --force --dry-run
./deploy/release.sh 1.0.2 --force
```

`--force` (or `-f`) allows the version already recorded in the project. If a local tag exists, the script checks its version and ancestry, verifies the tagged code, and pushes the original tag. Later local commits stay out of the push. If there is no local tag, it verifies and tags the current commit on `main`. In both cases the lockfile must be current and lint and tests must pass. Recovery creates no additional version commit and refuses a tag that already exists on GitHub.

**If the tag is on GitHub and only the Release entry is missing**, retry publication for the existing tag. The script prints the command after a publication failure. For this example, run the following command.

```bash
gh release create v1.0.2 --repo jamdev/reginald-agent --verify-tag --prerelease --title v1.0.2 --notes ""
```

Run the `gh release view` command above again. Success shows `tagName` equal to `v1.0.2`, `isPrerelease` equal to `true`, and the Release URL. If a published tag points to code that needs changing, cut the next patch version.

## Updating

### Back up the database before updating

On the host, take a snapshot of the production database while the current release is still installed. This gives you a copy to restore if you need to roll back across a schema change. Each run reserves a new filename under `/var/lib/reginald/`, so you can keep several pre-upgrade backups from the same day.

```bash
(
  set -e
  cd /opt/reginald
  backup=$(sudo -u reginald mktemp "/var/lib/reginald/pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
  sudo reginald db backup --to "$backup"
  sudo -u reginald sqlite3 "$backup" 'PRAGMA integrity_check;'
)
```

The backup command prints `backed up to` followed by the full path. The integrity check should print `ok`. Record that path with the release you are updating from.

### Deploy the update

For hosts using Brave Search, run the following check before updating. It should print `function`. If it fails, upgrade Node.js to an LTS release with built-in SQLite (22.13 or later). If a host overlay sets `tools.brave-search.args` to launch the old upstream package directly, remove that field so the shipped tool can load its rate limiter. An override of `command` alone can stay. [Brave Search rate limits](MCPSERVERS.md#brave-search-rate-limits)

```bash
node -p "typeof require('node:sqlite').DatabaseSync"
```

Once the backup checks out, run the updater on the host. With no argument, it selects the highest Git tag in version order. GitHub’s **Pre-release** label does not exclude a tag from deployment.

```bash
sudo /opt/reginald/deploy/update.sh
```

On upgrading to conversation capture, hosts with Logfire enabled start exporting up to 100 characters per conversation field. To choose another limit or disable capture for the first run after updating, set `logfire.capture_chars` in the host overlay before updating. [Conversation settings](LOGGING.md#read-the-agent-conversation)

On the next real run, task grants are checked against the selected runner and its connected MCP servers. Correct any stale grant named in a startup error. Existing Codex tasks can opt into live native search by adding `web_search` alongside their sandbox grant. [Tool grants and search examples](TOOLS.md)

After updating, all three runners export complete Brave Search result URLs to Logfire when conversation capture is enabled. The existing `capture_chars: false` setting keeps these URLs out of telemetry. [View search result URLs](LOGGING.md#find-individual-tool-calls-in-logfire)

The updater installs the locked dependencies, backs up and migrates the database, refreshes the wrapper and units, restarts active timers and runs `doctor`. Check the release row for the version and tag you intended. Review any `Changed template` lines and apply the relevant settings to your host's YAML files. New timers need to be enabled individually. [Configuration](USAGE.md#configuration) · [Enabling timers](TIMERS.md#enabling)

When enabling Codex on an existing Ubuntu host, [verify the Codex sandbox](INSTALL.md#verify-the-codex-sandbox). The one-time installer provisions Bubblewrap and its AppArmor profile. The linked check and repair also work on hosts provisioned with an older installer.

To deploy a specific tag, for example `v1.0.2`, use this command instead. After the updater finishes, verify the installed version and active schedules.

```bash
sudo /opt/reginald/deploy/update.sh v1.0.2
sudo reginald --version
systemctl list-timers 'reginald-*'
```

The updater should finish with `deployed v1.0.2`, and the CLI should report `1.0.2`.

To deploy unreleased work from the main branch, run `sudo /opt/reginald/deploy/update.sh main`. The update starts with the script already installed on the host. If a release changes the updater's installation steps, rerun the command after checkout to apply those steps.

### Rolling back

The updater switches the code to the release tag you choose and runs migrations forward only. If the upgrade changed the database schema, restoring the earlier schema requires the [snapshot taken before the upgrade](#back-up-the-database-before-updating). If you already upgraded without taking a separate snapshot, the updater's migration also writes a dated backup beside the database. Preserve that file before another migration or default backup on the same day can overwrite it.

For a compatible database, choose the desired release from the tags and check the updater's final `doctor` report.

```bash
sudo git -C /opt/reginald tag --sort=-v:refname
printf 'Release tag to restore: '
read -r reginald_release
sudo /opt/reginald/deploy/update.sh "${reginald_release:?A release tag is required}"
```
