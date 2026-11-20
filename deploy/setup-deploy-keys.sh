#!/usr/bin/env bash
# Provision the two deploy keys reginald needs, before install.sh runs.
#
#   ./deploy/setup-deploy-keys.sh <user@host> <owner/app-repo> <owner/vault-repo>
#
# Run this from your Mac. It generates a keypair on the host for each user that
# clones a repo, registers the public keys with GitHub, and verifies access.
#
# Why two keys: the repos are cloned by different users with separate ~/.ssh —
# root clones /opt/reginald, the reginald user clones and PUSHES the vault — and
# a GitHub deploy key may be attached to only one repository. The vault key
# therefore needs write access; the app key stays read-only.
#
# Credentials: SSH auth is left entirely to ssh (agent, key, or its own password
# prompt) and sudo prompts on the host's tty. Passwords are deliberately never
# accepted as arguments — argv is visible to every process on the machine.
#
# SSH_OPTS is passed to scp as well as ssh, so use the -o long form for anything
# with differing short flags: -o Port=2222, NOT -p 2222 (scp spells that -P).
#   SSH_OPTS='-i ~/.ssh/vps -o Port=2222'
#
# On a password-only host this opens ~7 connections and so prompts ~7 times.
# Authenticate once and reuse it by multiplexing:
#   ssh -M -S /tmp/reginald-ctl -fnNT <user@host>          # prompts once
#   SSH_OPTS='-o ControlPath=/tmp/reginald-ctl' ./deploy/setup-deploy-keys.sh ...
#   ssh -S /tmp/reginald-ctl -O exit <user@host>
# Better still, install a key first: ssh-copy-id <user@host>.
#
# Env knobs: SSH_OPTS, GH=0 (print the keys for manual registration instead).
set -euo pipefail

TARGET="${1:-}"
APP_REPO="${2:-}"
VAULT_REPO="${3:-}"
GH="${GH:-1}"
# Deliberately unquoted at the call sites below so multiple flags split.
# shellcheck disable=SC2206
SSH_OPTS_ARR=(${SSH_OPTS:-})

if [ -z "$TARGET" ] || [ -z "$APP_REPO" ] || [ -z "$VAULT_REPO" ]; then
    cat >&2 <<EOF
usage: $0 <user@host> <owner/app-repo> <owner/vault-repo>

  e.g. $0 ubuntu@vps.example.com jamdev/reginald-agent jamdev/reginald-vault-example

  SSH_OPTS='-i ~/.ssh/vps -o Port=2222'
                                     extra ssh flags. Passed to scp too, so use
                                     the -o long form (-p means something else
                                     to scp). See the header for multiplexing
                                     on a password-only host.
  GH=0                               print the public keys instead of
                                     registering them with the GitHub CLI
EOF
    exit 2
fi

HOST_LABEL="${TARGET#*@}"
REMOTE_SCRIPT="/tmp/reginald-keygen.$$.sh"
LOCAL_TMP="$(mktemp -d)"
trap 'rm -rf "$LOCAL_TMP"' EXIT

say() { printf '>>> %s\n' "$1"; }

# -- local prerequisites ---------------------------------------------------

command -v ssh >/dev/null 2>&1 || { echo "ssh not found" >&2; exit 1; }
if [ "$GH" = "1" ]; then
    if ! command -v gh >/dev/null 2>&1; then
        echo "!!! gh not found — re-run with GH=0 to register the keys by hand" >&2
        exit 1
    fi
    if ! gh auth status >/dev/null 2>&1; then
        echo "!!! gh is not authenticated — run 'gh auth login' first" >&2
        exit 1
    fi
fi

# -- 1. generate the keys on the host --------------------------------------

# Written to a file and run under `sudo bash <file>` rather than piped to
# `sudo bash -s`: piping occupies stdin, leaving sudo unable to prompt for a
# password on hosts that ask for one.
cat > "$LOCAL_TMP/keygen.sh" <<'REMOTE'
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

# install.sh creates this user too; both are guarded, so either order works.
id reginald &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin reginald
REGINALD_HOME="$(getent passwd reginald | cut -d: -f6)"

install -d -m 700 /root/.ssh
install -d -o reginald -g reginald -m 700 "$REGINALD_HOME/.ssh"

# Never regenerate: an existing key is probably already registered upstream,
# and replacing it would silently break the next deploy.
gen() {
    local path="$1" owner="$2" comment="$3"
    if [ -f "$path" ]; then
        echo "    exists  $path"
        return
    fi
    if [ "$owner" = root ]; then
        ssh-keygen -q -t ed25519 -f "$path" -N '' -C "$comment"
    else
        runuser -u "$owner" -- ssh-keygen -q -t ed25519 -f "$path" -N '' -C "$comment"
    fi
    echo "    created $path"
}

gen /root/.ssh/id_ed25519 root "reginald-app@$(hostname)"
gen "$REGINALD_HOME/.ssh/id_ed25519" reginald "reginald-vault@$(hostname)"

# The reginald user has no shell and can never answer a host-key prompt, so
# github.com must be trusted ahead of time or its clone fails under runuser.
for f in /root/.ssh/known_hosts "$REGINALD_HOME/.ssh/known_hosts"; do
    touch "$f"
    if ! ssh-keygen -F github.com -f "$f" >/dev/null 2>&1; then
        ssh-keyscan github.com 2>/dev/null >> "$f"
        echo "    trusted github.com in $f"
    fi
done
chown reginald:reginald "$REGINALD_HOME/.ssh/known_hosts"

# Stage the public halves where the non-root caller can scp them back.
cp /root/.ssh/id_ed25519.pub /tmp/reginald-app.pub
cp "$REGINALD_HOME/.ssh/id_ed25519.pub" /tmp/reginald-vault.pub
chmod 644 /tmp/reginald-app.pub /tmp/reginald-vault.pub
REMOTE

say "generating keys on $HOST_LABEL"
scp "${SSH_OPTS_ARR[@]}" -q "$LOCAL_TMP/keygen.sh" "$TARGET:$REMOTE_SCRIPT"
# -t so sudo can prompt for a password on its own tty.
ssh -t "${SSH_OPTS_ARR[@]}" "$TARGET" "sudo bash $REMOTE_SCRIPT; rm -f $REMOTE_SCRIPT"

say "fetching public keys"
scp "${SSH_OPTS_ARR[@]}" -q \
    "$TARGET:/tmp/reginald-app.pub" "$TARGET:/tmp/reginald-vault.pub" "$LOCAL_TMP/"
ssh "${SSH_OPTS_ARR[@]}" "$TARGET" "rm -f /tmp/reginald-app.pub /tmp/reginald-vault.pub"

# -- 2. register them with GitHub ------------------------------------------

register() {
    local file="$1" repo="$2" title="$3" write="$4" out
    local -a flags=(--repo "$repo" --title "$title")
    [ "$write" = "write" ] && flags+=(--allow-write)

    if out=$(gh repo deploy-key add "$file" "${flags[@]}" 2>&1); then
        echo "    added   $repo ($write)"
        return 0
    fi
    # Re-running after a partial failure must not be an error.
    if grep -qi "already in use\|already exists" <<<"$out"; then
        echo "    present $repo (key already registered)"
        return 0
    fi
    echo "!!! failed to add deploy key to $repo" >&2
    echo "    $out" >&2
    return 1
}

if [ "$GH" = "1" ]; then
    say "registering deploy keys"
    register "$LOCAL_TMP/reginald-app.pub"   "$APP_REPO"   "reginald-app@$HOST_LABEL"   read
    register "$LOCAL_TMP/reginald-vault.pub" "$VAULT_REPO" "reginald-vault@$HOST_LABEL" write
else
    say "add these public keys under each repo's Settings -> Deploy keys"
    echo
    echo "--- $APP_REPO  (read-only) ---"
    cat "$LOCAL_TMP/reginald-app.pub"
    echo
    echo "--- $VAULT_REPO  (TICK 'Allow write access') ---"
    cat "$LOCAL_TMP/reginald-vault.pub"
    echo
    read -rp "press enter once both are registered... " _
fi

# -- 3. verify -------------------------------------------------------------

# The same check install.sh's preflight runs, as the same users: if this
# passes, the real clone will too.
say "verifying access"
cat > "$LOCAL_TMP/verify.sh" <<REMOTE
set -uo pipefail
export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND="ssh -o BatchMode=yes"
fail=0

if git ls-remote --exit-code "git@github.com:${APP_REPO}.git" HEAD >/dev/null 2>&1; then
    echo "    ok   ${APP_REPO} as root"
else
    echo "    FAIL ${APP_REPO} as root"; fail=1
fi

if runuser -u reginald -- env GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes" \\
        git ls-remote --exit-code "git@github.com:${VAULT_REPO}.git" HEAD >/dev/null 2>&1; then
    echo "    ok   ${VAULT_REPO} as reginald"
else
    echo "    FAIL ${VAULT_REPO} as reginald"; fail=1
fi
exit \$fail
REMOTE

scp "${SSH_OPTS_ARR[@]}" -q "$LOCAL_TMP/verify.sh" "$TARGET:$REMOTE_SCRIPT"
if ssh -t "${SSH_OPTS_ARR[@]}" "$TARGET" "sudo bash $REMOTE_SCRIPT; s=\$?; rm -f $REMOTE_SCRIPT; exit \$s"; then
    cat <<EOF

both repositories are reachable. next:

  scp deploy/install.sh $TARGET:/tmp/
  ssh $TARGET
  sudo bash /tmp/install.sh git@github.com:$APP_REPO.git git@github.com:$VAULT_REPO.git
EOF
else
    cat >&2 <<EOF

!!! verification failed. Usual causes:
    * the key was registered on the wrong repository
    * the vault key is missing "Allow write access" (needed: prod pushes)
    * the same key was reused for both repos — GitHub allows a deploy key on
      only one repository, so the second registration silently refers elsewhere
EOF
    exit 1
fi
