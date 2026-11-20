#!/usr/bin/env bash
# Installed as /usr/local/bin/reginald — the operator entry point on a host.
#
#   sudo reginald doctor
#   sudo reginald task run-history
#   sudo reginald -v task run research-new-developments
#
# Collapses the four things every manual command needs, each of which has its
# own way of failing confusingly when forgotten:
#
#   cd /opt/reginald        runuser keeps the caller's cwd, and the reginald
#                           user cannot read /root — anything resolving a
#                           relative path dies with PermissionError
#   . /etc/reginald/env     without it, API keys and tokens are absent
#   REGINALD_ENV=prod       without it the CLI defaults to dev, silently
#                           reading a different database and vault
#   runuser -u reginald     the vault, database and venv are owned by that user
#
# REGINALD_ENV from the caller wins, so `REGINALD_ENV=dev sudo -E reginald ...`
# still works for a one-off.
set -euo pipefail

APP_DIR="/opt/reginald"
ENV_FILE="/etc/reginald/env"
BIN="$APP_DIR/.venv/bin/reginald"

if [ "$(id -u)" -ne 0 ]; then
    echo "reginald: must run as root (sudo reginald $*)" >&2
    echo "  it reads $ENV_FILE (mode 600) and runs as the reginald user" >&2
    exit 1
fi

[ -x "$BIN" ] || { echo "reginald: $BIN not found — is the install complete?" >&2; exit 1; }

# Remember any explicit choice before the env file can overwrite it.
_caller_env="${REGINALD_ENV:-}"

set -a
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
set +a

export REGINALD_ENV="${_caller_env:-${REGINALD_ENV:-prod}}"

cd "$APP_DIR"
exec runuser -u reginald -- "$BIN" "$@"
