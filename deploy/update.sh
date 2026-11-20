#!/usr/bin/env bash
# Deploy a release on this host. Run it here, not over `ssh <host> ...`.
#
#   update.sh            # newest tag on origin
#   update.sh v0.2.0     # a specific tag (also how you roll back)
#   update.sh main       # a branch, for testing unreleased work
set -euo pipefail

REF="${1:-}"
APP_DIR="/opt/reginald"
VENV="$APP_DIR/.venv"

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo $0 $*)" >&2; exit 1; }
[ -d "$APP_DIR/.git" ] || { echo "$APP_DIR is not a checkout — run install.sh first" >&2; exit 1; }
command -v uv >/dev/null 2>&1 || { echo "uv not found — run install.sh first" >&2; exit 1; }

cd "$APP_DIR"

echo ">>> fetching from origin"
# --prune-tags so a retagged release does not resolve to the stale local object.
git fetch --tags --prune --prune-tags origin

if [ -z "$REF" ]; then
    # Newest by version order rather than commit date: tags are the release
    # record here, and v0.10.0 must sort above v0.9.0.
    REF="$(git tag --sort=-v:refname | head -n1)"
    [ -n "$REF" ] || { echo "no tags on origin — pass a branch, e.g. $0 main" >&2; exit 1; }
    echo "    newest tag: $REF"
fi

echo ">>> checking out $REF"
# Resolve tags and remote branches to one commit before touching the index.
target="$REF"
if git show-ref --verify --quiet "refs/remotes/origin/$REF"; then
    target="refs/remotes/origin/$REF"
fi
target=$(git rev-parse --verify --end-of-options "$target^{commit}")
previous=$(git rev-parse HEAD)

# Older releases tracked these files. Make their index entries match the target
# so checkout leaves each existing working copy in place in either direction.
local_configs=()
for path in config/{base,dev,prod}.yaml; do
    if [[ -f "$path" || -L "$path" ]]; then
        local_configs+=("$path")
        if ! git check-ignore --no-index -q -- "$path"; then
            # Some older releases also predate the committed ignore rules.
            exclude_file=$(git rev-parse --git-path info/exclude)
            mkdir -p "$(dirname "$exclude_file")"
            printf '\n/%s\n' "$path" >> "$exclude_file"
        fi
    fi
done

saved_index=
if (( ${#local_configs[@]} )) && ! git diff --cached --quiet "$target" -- "${local_configs[@]}"; then
    saved_index=$(git write-tree)
    git reset -q "$target" -- "${local_configs[@]}"
fi
if ! git checkout -q --detach "$target"; then
    if [[ -n "$saved_index" ]]; then
        git reset -q "$saved_index" -- "${local_configs[@]}"
    fi
    exit 1
fi
git --no-pager log -1 --format='    %h %s'
echo "WARNING: Review $APP_DIR/docs/DEPLOY.md (Updating) and $APP_DIR/docs/USAGE.md (Configuration) for settings to apply to your local YAML files." >&2
git diff --name-only "$previous" "$target" -- 'config/*.yaml-example' | sed 's/^/    Changed template: /' >&2

echo ">>> syncing dependencies"
# --frozen installs uv.lock exactly and never re-resolves: a dependency change
# is a reviewed commit, not something an unattended deploy discovers by itself.
# Unconditional on purpose — a deploy must not vary with what the host has.
export UV_PYTHON_INSTALL_DIR=/opt/uv/python
uv python install
uv sync --frozen --no-default-groups --extra agents --extra observability
chmod -R a+rX /opt/uv/python "$APP_DIR/.venv"

# The systemd units get these via EnvironmentFile=; the direct invocations below
# need them too, or `db migrate` dies on the missing LOGFIRE_TOKEN and `doctor`
# reports every key absent. runuser (util-linux) passes the environment through —
# plain `sudo -u` strips it and sudo's --preserve-env is ignored on Ubuntu 26.04.
set -a
# shellcheck disable=SC1091
[ -f /etc/reginald/env ] && . /etc/reginald/env
set +a
export REGINALD_ENV=prod

echo ">>> backing up + migrating database"
runuser -u reginald -- "$VENV/bin/reginald" db migrate

echo ">>> operator wrapper"
# Guarded: a rollback to a release from before the wrapper existed would
# otherwise abort the deploy here, under set -e, having already checked out and
# synced. A stale wrapper left behind is harmless — it only execs the venv.
if [ -f "$APP_DIR/deploy/reginald-wrapper.sh" ]; then
    install -m 0755 "$APP_DIR/deploy/reginald-wrapper.sh" /usr/local/bin/reginald
else
    echo "    no wrapper in this release; leaving /usr/local/bin/reginald as it is"
fi

echo ">>> installing systemd units"
cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
# Enabled timers keep their old OnCalendar until restarted.
mapfile -t timers < <(systemctl list-units --type=timer --state=active --no-legend 'reginald-*' \
                      | awk '{print $1}')
[ ${#timers[@]} -eq 0 ] || systemctl restart "${timers[@]}"

echo ">>> self-check"
runuser -u reginald -- "$VENV/bin/reginald" doctor

echo ">>> deployed $REF"
