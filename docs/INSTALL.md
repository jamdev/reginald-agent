# Install and deploy Reginald

For a scheduled installation on Ubuntu, start with [One-time install](#one-time-install). To try Reginald locally, follow [Local development](#local-development). For an existing host, go to [Updating](#updating).

## One-time install

- **Your workstation** is where you prepare the vault and connect to the server. You need Bash, Git, the GitHub CLI (`gh`), `ssh` and `scp`. On Windows, use a WSL2 Linux terminal for these commands.
- **The server** runs Reginald. The installer targets Ubuntu Server 26.04 LTS. You need Git on the server and an SSH login with sudo access.

### 1. Clone the Reginald application

On your workstation, open a terminal in the directory where you keep your projects. Clone the application into a folder called `reginald`, enter it, and check that it contains the installer.

```bash
git clone https://github.com/jamdev/reginald-agent.git reginald
cd reginald
ls deploy/install.sh
```

Once you see `deploy/install.sh`, keep this terminal open for step 2, which creates the vault alongside the `reginald` directory.

### 2. Create your private vault

Create a private copy of the example vault in your GitHub account. It includes the topic folders and task prompts Reginald uses. Your team’s notes will live in this repository. [Example vault](https://github.com/jamdev/reginald-vault-example)

On your workstation, sign in to your own GitHub account with `gh auth login` if needed. Run this block in the same terminal. It creates a sibling folder named `reginald-vault` and a private repository of the same name in your account.

```bash
(
  set -e
  gh repo clone jamdev/reginald-vault-example ../reginald-vault
  cd ../reginald-vault
  git remote remove origin
  gh repo create reginald-vault --private --source=. --remote=origin --push
  gh repo view --json sshUrl,visibility,defaultBranchRef
)
```

Check that the output shows `PRIVATE`, the `main` branch and an `sshUrl` for your own account. Keep that URL for step 4.

### 3. Give the server access to your vault

The server downloads the application from public GitHub over HTTPS. It needs an SSH key to push updates to your private vault.

**On your workstation**, copy the installer to the server and log in.

```sh
{
  printf 'Ubuntu SSH login: '
  read -r reginald_host
  scp deploy/install.sh "$reginald_host:reginald-install.sh" && ssh "$reginald_host"
}
```

**On the server**, create the service account and its SSH key. The following commands reuse an existing key and print the public key on the last line.

```bash
id reginald >/dev/null 2>&1 || sudo useradd --system --create-home --shell /usr/sbin/nologin reginald
sudo -H -u reginald sh -ec '
  cd "$HOME"
  install -d -m 700 .ssh
  if [ ! -f .ssh/id_ed25519 ]; then
    ssh-keygen -q -t ed25519 -N "" -f .ssh/id_ed25519 -C "Reginald vault"
  fi
  cat .ssh/id_ed25519.pub
'
```

**In GitHub**, open your private vault's **Settings → Deploy keys → Add deploy key**. Enter `Reginald server` as the title, paste the public key, select **Allow write access**, and click **Add key**. [GitHub deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)

### 4. Run the installer

In the same **server** session, paste the vault's `sshUrl` from step 2. On the first connection, SSH may ask you to confirm GitHub's host key. Check the fingerprint before accepting. A successful check prints a commit hash followed by `HEAD`. [GitHub SSH fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)

```sh
{
  cd /tmp
  printf 'Private vault SSH URL: '
  read -r reginald_vault_url
  sudo -H -u reginald git ls-remote --exit-code "$reginald_vault_url" HEAD
}
```

Once that succeeds, install Reginald from the public application repository.

```bash
sudo bash "$HOME/reginald-install.sh" https://github.com/jamdev/reginald-agent.git "${reginald_vault_url:?Enter the vault SSH URL first}"
```

The installer checks repository access before installing the runtime, clones the application into `/opt/reginald` and the vault into `/srv/vault/agentic-gc`, then installs the CLI wrapper and systemd units. You can rerun it to finish an interrupted install. It reuses existing clones. Use [Updating](#updating) to deploy a newer release.

### 5. Configure the host

Copy the configuration templates and edit the shared and production settings. The loop preserves existing settings if you repeat this step.

```bash
cd /opt/reginald
for template in config/*.yaml-example; do
  config_file=${template%-example}
  if [ ! -e "$config_file" ]; then
    sudo cp "$template" "$config_file"
  fi
done
ls -l config/base.yaml config/dev.yaml config/prod.yaml
sudoedit config/base.yaml config/prod.yaml
```

Set `email.from_address` to your verified Postmark sender and `site.base_url` to the published vault's address. Check the production paths for the vault and database. If you want to run without publishing or email delivery, set `site.enabled` to `false` or `email.provider` to `console`, respectively, in the production overlay. [Configuration reference](USAGE.md#configuration)

#### Set the server timezone and local time

Reginald’s timers follow the server’s local time. On the Ubuntu server, choose the timezone your team uses for its schedule and enable network time synchronisation (NTP). Press Enter for `Europe/London`, which follows GMT and British Summer Time automatically, or enter another zone from `timedatectl list-timezones`.

```bash
(
  set -e
  read -r -p 'Schedule timezone [Europe/London]: ' reginald_timezone
  sudo timedatectl set-timezone "${reginald_timezone:-Europe/London}"
  sudo timedatectl set-ntp true
  timedatectl status
)
```

Check that `Time zone` is correct and `Local time` agrees with a trusted clock in that zone. Keep NTP enabled so the clock stays in sync. Once synchronisation completes, `timedatectl status` should show `NTP service: active` and `System clock synchronized: yes`. When finding a run in Logfire, compare `Universal time` with UTC timestamps. Keep the timezone in any missed-task alert in sync with the server’s schedule. [Ubuntu clock commands](https://manpages.ubuntu.com/manpages/resolute/man1/timedatectl.1.html) · [Logfire alert examples](LOGGING.md#task-completion-alerts)

If the server cannot reach its time source and its clock needs correcting immediately, enter the current date and time from a trusted clock in the selected timezone. This block pauses NTP for the manual adjustment and re-enables it on exit.

```bash
(
  set -e
  read -r -p 'Current local date and time (YYYY-MM-DD HH:MM:SS): ' reginald_local_time
  : "${reginald_local_time:?Enter the current local date and time}"
  sudo timedatectl set-ntp false
  trap 'sudo timedatectl set-ntp true' EXIT
  sudo timedatectl set-time "$reginald_local_time"
)
```

Run `timedatectl status` again to verify the local time and, once the time source is reachable, confirm that the clock has synchronised.

#### Secrets

```bash
sudoedit /etc/reginald/env
```

Replace the installer placeholders. For the shipped tasks, set `ANTHROPIC_API_KEY` for API billing or `CLAUDE_CODE_OAUTH_TOKEN` for a subscription. Remove the example API key line when using the subscription token because the API key takes precedence. Set `BRAVE_API_KEY` for Brave search and `POSTMARK_SERVER_TOKEN` for Postmark delivery. Other runners have their own credential requirements. [Provider credentials](USAGE.md#secrets) · [Runner authentication](RUNNERS.md#checking-runner-credentials)

Use `sudo reginald` for CLI commands on the host. The wrapper enters the application directory, loads the protected environment file and runs the CLI as the service user with the production configuration.

#### Configure Logfire

The production template enables Logfire tracing in the EU region and captures up to 100 characters per conversation field. [Conversation settings](LOGGING.md#read-the-agent-conversation) If startup reports `No user token was found matching` the EU URL, Reginald continues with journal logging. The SDK has fallen back to looking for a user login. For an unattended server, configure a project write token as follows.

1. In your browser, open or create a project in [Logfire EU](https://logfire-eu.pydantic.dev). Open **Settings → Write tokens → New write token** and copy the token. If your project is in the US, use its US dashboard and set `base_url: https://logfire-us.pydantic.dev` in the existing `logfire` section of `/opt/reginald/config/prod.yaml`. The token and endpoint must belong to the same region. [Write tokens](https://pydantic.dev/docs/logfire/manage/create-write-tokens/) · [Data regions](https://pydantic.dev/docs/logfire/manage/data-regions/)
2. On the server, run `sudoedit /etc/reginald/env`. Find `# LOGFIRE_TOKEN=REPLACE-ME`, remove the leading `#` and space, and replace `REPLACE-ME` with the copied token. Save the file. Each new CLI invocation and scheduled service reads it at startup.
3. Run `sudo reginald doctor`. The `logfire` row shows `[OK]` with the project URL. It shows `[WARN]` if SDK setup fails or the project URL is unavailable when checked. To confirm delivery, open the project's **Live** view after the next scheduled task, select **prod**, and look for a new record with `service_name = 'reginald'`. [Inspect a run in Logfire](LOGGING.md#find-individual-tool-calls-in-logfire)

**To use journal logging alone**, run `sudoedit /opt/reginald/config/prod.yaml` and change `enabled: true` to `enabled: false` under `logfire`. Run `sudo reginald doctor` again. The Logfire configuration warning and the `logfire` row should both disappear.

### 6. Connect Obsidian Publish

If publishing is enabled, sign in as the service user so its scheduled jobs can use the saved login. Start from the application directory, which that account can read.

```bash
cd /opt/reginald
sudo runuser -u reginald -- ob login
```

After login succeeds, connect the installed vault to your Publish site.

```bash
{
  printf 'Obsidian Publish site: '
  read -r reginald_site
  sudo runuser -u reginald -- ob publish-setup --site "$reginald_site" --path /srv/vault/agentic-gc
}
```

### 7. Initialise and verify

Run the first change scan against the fresh clone, before topic sync or escalation migration can edit vault files.

```bash
sudo reginald vault identify-changes
sudo reginald topics sync
sudo reginald escalations migrate
sudo reginald doctor
```

On a new database, the scan sets the cursor to the current commit and skips earlier history. Topic sync should report the number of active topic folders you expect. Escalation migration splits the legacy log if present or seeds the database from existing escalation notes. Check that `doctor` reports `prod` and resolve any `[FAIL]` rows. An unused runner warning is expected. [Change detection](USAGE.md#identifying-vault-changes) · [Doctor checks](RUNNERS.md#checking-runner-credentials)

### 8. Enable scheduled work

Once `doctor` exits successfully, choose which timers to enable. Tasks can use paid model credentials and change the vault. The outbox timer can send real email to subscribers. The installer prints one enable command per installed timer with its schedule. Run the selected commands as root, or use the sudo commands in the [timer guide](TIMERS.md#enabling).

```bash
systemctl list-timers 'reginald-*'
```

Compare each enabled timer’s `NEXT` with the day and time you expect in the server’s timezone. To check the first scheduled research run, inspect its journal and run history.

```bash
sudo journalctl -u reginald-task@research-new-developments.service --since today --no-pager
sudo reginald task run-history --task research-new-developments
```

To run that check immediately, start the service with `sudo systemctl start --no-block reginald-task@research-new-developments.service`. This starts a real research task. Check for a completed run with status `success` in the history. [Reading the host journal](LOGGING.md#read-the-host-journal) · [Managing subscribers](USAGE.md#subscribers)

## Updating

<a id="back-up-the-database-before-updating"></a>
<a id="deploy-the-update"></a>
<a id="rolling-back"></a>

For an existing host, follow the [deployment guide](DEPLOY.md#updating). It covers [taking a database backup](DEPLOY.md#back-up-the-database-before-updating), [deploying and verifying a release](DEPLOY.md#deploy-the-update) and [returning to an earlier release](DEPLOY.md#rolling-back).

<a id="local-development-mac"></a>

## Local development

On Linux, macOS or Windows using WSL2, [clone the Reginald application](#1-clone-the-reginald-application) and run the local setup commands from its directory. You need Git, Make, uv and Node.js in that environment. uv installs the Python version pinned in `.python-version`. [Install uv](https://docs.astral.sh/uv/getting-started/installation/) · [Install Node.js](https://nodejs.org/en/download)

```bash
make dev
```

### Copy the configuration templates

```bash
for template in config/*.yaml-example; do
  config_file=${template%-example}
  if [ ! -e "$config_file" ]; then
    cp "$template" "$config_file"
  fi
done
ls -l config/base.yaml config/dev.yaml config/prod.yaml
```

For a first-time local setup, [create your private vault](#2-create-your-private-vault). Set `vault_path` in `config/dev.yaml` to the root of a **scratch clone** of that vault. Dev tasks can edit files and commit locally. The shipped dev overlay disables pushing and publishing, uses console email and restricts recipients to its allowlist. [Development settings](USAGE.md#the-dev-safety-rails)

### Local development secrets

Tests and lint can run without credentials. For CLI use, keep your development credentials in the Git-ignored `.env` beside `pyproject.toml`. Add one `NAME=value` entry per line for the providers you use. The default console email provider needs no Postmark token. [Provider credentials](USAGE.md#secrets) · [Claude subscription setup](USAGE.md#agent-authentication)

Development tracing is off by default. To enable it, set `enabled: true` under `logfire` in `config/dev.yaml` and put the matching project's `LOGFIRE_TOKEN` in your local `.env`. [Configure Logfire](#configure-logfire)

```bash
touch .env
chmod 600 .env
nano .env
```

Load the file for each CLI invocation. Check that `doctor` reports `dev` and the scratch vault path. With the shipped task prompts in place, the dry run renders the digest prompt without calling the model.

```bash
uv run --env-file .env reginald --env dev doctor
uv run --env-file .env reginald --env dev task run key-dates-digest --dry-run
```

For day-to-day commands, see the [usage guide](USAGE.md). To find your way around the code, see the [repository layout](LAYOUT.md). For tests, dependency changes and cutting releases, see the [contributor guide](../AGENTS.md).
