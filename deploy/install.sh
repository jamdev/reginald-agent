#!/usr/bin/env bash
# First-time host setup. Run as root on Ubuntu Server 26.04 LTS (a Raspberry Pi,
# a VPS — anything with apt + systemd).
#
# Ubuntu-specific quirks handled below:
#   - needrestart prompts interactively mid-apt          -> NEEDRESTART_MODE=a
#   - curl / gnupg are not preinstalled                  -> installed below
#   - AppArmor restricts unprivileged user namespaces    -> Bubblewrap and Electron profiles
#   - the server images ship with no swap                -> optional swapfile
# Env knobs: SWAP_SIZE (default 2G), SKIP_SWAP=1, NODE_MAJOR (default 22).
set -euo pipefail

APP_DIR="/opt/reginald"
REPO_URL="${1:?usage: install.sh <reginald-repo-url> <vault-repo-url>}"
VAULT_URL="${2:?usage: install.sh <reginald-repo-url> <vault-repo-url>}"
NODE_MAJOR="${NODE_MAJOR:-22}"
SWAP_SIZE="${SWAP_SIZE:-2G}"

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo $0 ...)" >&2; exit 1; }

# needrestart (Ubuntu-only) otherwise opens a whiptail dialog during apt.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

# Never block on a credential prompt: this script is routinely run over a pipe
# or from another script, where an interactive prompt hangs forever instead of
# failing. BatchMode does the same for ssh, including unknown host keys.
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes"

. /etc/os-release
if [ "${ID:-}" != "ubuntu" ]; then
    echo "!!! expected Ubuntu, found ${PRETTY_NAME:-unknown} — continuing, but untested" >&2
fi
echo ">>> ${PRETTY_NAME:-unknown} on $(uname -m)"

echo ">>> system user + directories"
id reginald &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin reginald
mkdir -p /var/lib/reginald /etc/reginald /srv/vault
chown reginald:reginald /var/lib/reginald /srv/vault

# Fail on unreadable repositories BEFORE the expensive work below (apt,
# build-essential, NodeSource, Electron, swapfile) — otherwise a missing deploy
# key costs ten minutes before it surfaces. Each repo is checked as the user
# that will actually clone it, through the same runuser path used further down,
# so a passing preflight means the real clone will work.
echo ">>> preflight: repository access"
if ! command -v git >/dev/null 2>&1; then
    apt-get update
    apt-get install -y git
fi

check_repo() {
    local user="$1" label="$2" url="$3" out status=0
    if [ "$user" = root ]; then
        out=$(git ls-remote --exit-code "$url" HEAD 2>&1) || status=$?
    else
        out=$(runuser -u "$user" -- git ls-remote --exit-code "$url" HEAD 2>&1) || status=$?
    fi
    if [ "$status" -eq 0 ]; then
        echo "    ok   ${label} as ${user}"
        return 0
    fi
    echo "!!! cannot read the ${label} repository as '${user}': ${url}" >&2
    echo "    git: $(echo "$out" | tail -n1)" >&2
    return 1
}

repo_check_failed=0
check_repo root "reginald" "$REPO_URL" || repo_check_failed=1
check_repo reginald "vault" "$VAULT_URL" || repo_check_failed=1
if [ "$repo_check_failed" -ne 0 ]; then
    cat >&2 <<'EOF'

GitHub removed password authentication for git in 2021, so an https:// URL on a
private repo cannot work here. Use SSH with a per-repo deploy key, generated for
the user that clones that repo — they have separate ~/.ssh:

  reginald repo -> root           /root/.ssh/id_ed25519           read-only
  vault repo    -> reginald user  ~reginald/.ssh/id_ed25519       needs WRITE
                                  (prod pushes the vault after every run)

  ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ''
  install -d -o reginald -g reginald -m 700 ~reginald/.ssh
  runuser -u reginald -- ssh-keygen -t ed25519 -f ~reginald/.ssh/id_ed25519 -N ''

Add each PUBLIC key under that repository's Settings -> Deploy keys, ticking
"Allow write access" for the vault only. A deploy key works on one repo only,
so the two keys must differ. Then seed known_hosts for both users — the
reginald user has no shell and can never answer a host-key prompt:

  ssh-keyscan github.com | tee -a /root/.ssh/known_hosts ~reginald/.ssh/known_hosts
  chown reginald:reginald ~reginald/.ssh/known_hosts

Then re-run this script with git@github.com:owner/repo.git URLs.
EOF
    exit 1
fi

echo ">>> system packages"
apt-get update
# python3-venv is in universe on Ubuntu (it is in main on Raspberry Pi OS), so the
# component has to be enabled before the install below, not after.
if ! apt-cache policy | grep -q "c=universe"; then
    apt-get install -y software-properties-common
    add-apt-repository -y universe
    apt-get update
fi
# curl/ca-certificates/gnupg: absent from Ubuntu Server images, needed for NodeSource.
# build-essential/python3-dev: sdist fallbacks when no wheel is published for the arch.
apt-get install -y \
    git python3 python3-venv python3-dev build-essential \
    sqlite3 curl ca-certificates gnupg bubblewrap

# Codex uses bwrap on PATH. Ubuntu's profile lets that binary set up loopback
# inside its network namespace while keeping the host's userns restrictions.
if [ "$ID" = ubuntu ] && [ "$(cat /sys/module/apparmor/parameters/enabled 2>/dev/null)" = Y ]; then
    echo ">>> Ubuntu AppArmor profile for the Codex sandbox"
    apt-get install -y apparmor
    profile=/etc/apparmor.d/bwrap-userns-restrict
    if [ ! -f "$profile" ]; then
        apt-get install -y apparmor-profiles
        install -m 0644 /usr/share/apparmor/extra-profiles/bwrap-userns-restrict "$profile"
    fi
    apparmor_parser -r "$profile"
fi

echo ">>> node ${NODE_MAJOR}+ (obsidian-headless + agent tooling)"
node_major() {
    if command -v node >/dev/null 2>&1; then
        node -v | sed 's/^v//' | cut -d. -f1
    else
        echo 0
    fi
}
# Deliberately NOT the distro packages: Ubuntu's nodejs omits npm, and the npm deb
# pulls ~690 packages (eslint, X11, mesa, alacritty) onto a headless Pi. NodeSource
# ships one self-contained package with npm included. Its setup_*.x script refuses
# Ubuntu releases it predates, so use the distro-agnostic "nodistro" suite directly.
if [ "$(node_major)" -lt "$NODE_MAJOR" ] || ! command -v npm >/dev/null 2>&1 ||
        ! node -e "require('node:sqlite')" >/dev/null 2>&1; then
    install -d -m 0755 /usr/share/keyrings
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor --yes -o /usr/share/keyrings/nodesource.gpg
    echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list
    apt-get update
    apt-get install -y nodejs
fi
node -e "require('node:sqlite')" || { echo "Brave Search requires Node.js with built-in SQLite (22.13+ LTS)" >&2; exit 1; }
echo "    node $(node -v), npm $(npm -v)"
npm install -g obsidian-headless

# Ubuntu 24.04+ sets kernel.apparmor_restrict_unprivileged_userns=1, which breaks
# Electron's sandbox (obsidian-headless) with "No usable sandbox!". Grant the
# capability to that binary only, rather than disabling the hardening globally.
if [ -d /etc/apparmor.d ] && command -v apparmor_parser >/dev/null 2>&1; then
    echo ">>> apparmor profile for obsidian-headless (Electron userns)"
    cat > /etc/apparmor.d/obsidian-headless <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile obsidian-headless /usr/{lib,local/lib}/node_modules/obsidian-headless/**
        flags=(unconfined) {
  userns,
  include if exists <local/obsidian-headless>
}
EOF
    apparmor_parser -r -W /etc/apparmor.d/obsidian-headless \
        || echo "    !!! profile failed to load; if 'ob' reports a sandbox error, see docs/INSTALL.md (Host notes)" >&2
fi

# Ubuntu Server images have no swap (no dphys-swapfile as on Raspberry Pi OS);
# a long agent run alongside Electron will OOM on a 4GB Pi without it.
if [ "${SKIP_SWAP:-0}" != "1" ] && [ -z "$(swapon --show --noheadings)" ]; then
    echo ">>> ${SWAP_SIZE} swapfile"
    fallocate -l "$SWAP_SIZE" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Persistent=true timers replay missed runs, so the clock must be right on a
# Pi with no RTC.
timedatectl set-ntp true || true

echo ">>> uv"
# Installed to /usr/local/bin so root and the reginald user share one binary
# (it is on sudo's secure_path by default).
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh
fi
echo "    $(uv --version)"

echo ">>> code"
[ -d "$APP_DIR/.git" ] || git clone "$REPO_URL" "$APP_DIR"
cd "$APP_DIR"
# uv symlinks the venv to its managed interpreter, and the reginald user has to
# be able to execute it — so the install dir must be shared, not root's home
# (the default ~/.local/share/uv/python is mode 0700 and unreadable to it).
export UV_PYTHON_INSTALL_DIR=/opt/uv/python
# Honours .python-version: the pinned interpreter is fetched rather than
# inherited from whatever python3 the distro happens to ship.
uv python install
uv sync --frozen --no-default-groups --extra agents --extra observability
chmod -R a+rX /opt/uv/python "$APP_DIR/.venv"

echo ">>> vault"
[ -d /srv/vault/agentic-gc/.git ] || runuser -u reginald -- git clone "$VAULT_URL" /srv/vault/agentic-gc

echo ">>> secrets file (edit this!)"
if [ ! -f /etc/reginald/env ]; then
    cat > /etc/reginald/env <<'EOF'
# Which config overlay to use. The systemd units set this themselves; it is
# here so `reginald` on the command line agrees with them.
REGINALD_ENV=prod

# Claude auth (see docs/USAGE.md). Set ONE: the API key for
# Console billing, or a `claude setup-token` OAuth token for a Max/Pro plan.
# If both are set the API key wins (the CLI's own precedence).
ANTHROPIC_API_KEY=sk-ant-REPLACE-ME
# CLAUDE_CODE_OAUTH_TOKEN=REPLACE-ME
# For a Codex task, set OPENAI_API_KEY or sign in as the service user.
# OPENAI_API_KEY=
# Pydantic AI credentials follow the task's model provider, for example OpenRouter.
# OPENROUTER_API_KEY=
POSTMARK_SERVER_TOKEN=REPLACE-ME
BRAVE_API_KEY=REPLACE-ME
# prod.yaml sets logfire.enabled: true — uncomment to ship traces, otherwise
# observability stays a logged no-op.
# LOGFIRE_TOKEN=REPLACE-ME
# Optional log level override for the host's YAML settings.
# Each scheduled run reads this file at startup.
# REGINALD_LOG_LEVEL=DEBUG
EOF
    chmod 600 /etc/reginald/env
fi

echo ">>> operator wrapper (/usr/local/bin/reginald)"
# Guarded: a rollback to a release from before the wrapper existed would
# otherwise abort the deploy here, under set -e, having already checked out and
# synced. A stale wrapper left behind is harmless — it only execs the venv.
if [ -f "$APP_DIR/deploy/reginald-wrapper.sh" ]; then
    install -m 0755 "$APP_DIR/deploy/reginald-wrapper.sh" /usr/local/bin/reginald
else
    echo "    no wrapper in this release; leaving /usr/local/bin/reginald as it is"
fi

echo ">>> systemd units"
cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload

echo
echo "installed on $(hostname) — next steps:"
cat <<'EOF'
  Use 'sudo reginald' when running the Reginald CLI on this host. The wrapper at
  /usr/local/bin/reginald handles the working directory, the secrets file,
  REGINALD_ENV and the reginald user for you — all four fail confusingly
  when forgotten.

  1. On a fresh install, copy the configuration templates before editing
     the settings. On an existing install, copy only files that are missing.
     cd /opt/reginald
EOF
for template in config/*.yaml-example; do
    [ -f "$template" ] || continue
    printf '     sudo cp %q %q\n' "$template" "${template%-example}"
done
cat <<'EOF'
     ls -l config/*.yaml

     Review the shared and production settings, then enter your credentials.
     sudoedit config/base.yaml config/prod.yaml
     sudoedit /etc/reginald/env
  2. runuser -u reginald -- ob login    # Obsidian account
     runuser -u reginald -- ob publish-setup --site <site> --path /srv/vault/agentic-gc
  3. sudo reginald topics sync
     sudo reginald doctor
  4. Enable the timers — copy-paste the block below. Only do this once
     'doctor' exits successfully. An enabled timer will fire unattended.
EOF

# Generated from the units actually installed, so this cannot drift from
# deploy/systemd/ the way a hand-written list would.
echo
for f in "$APP_DIR"/deploy/systemd/*.timer; do
    unit="$(basename "$f")"
    sched="$(awk -F= '/^OnCalendar=/{printf "%s%s", sep, $2; sep="; "}' "$f")"
    printf '     systemctl enable --now %-44s # %s\n' "$unit" "$sched"
done
echo

cat <<'EOF'
     systemctl list-timers 'reginald-*'      # confirm the next run times

To update later: SSH in and run 'sudo /opt/reginald/deploy/update.sh'.

Note: unattended-upgrades may reboot the host; timers are Persistent=true so
missed runs replay, but check /var/log/unattended-upgrades/ if a run vanishes.
EOF
