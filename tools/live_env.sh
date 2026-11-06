#!/usr/bin/env bash
# Run a command with the company process's live settings, read at run time from the private Synaps dir
# (~/.synaps-switch-dev, or $SYNAPS_SWITCH_DEV_BASE). Nothing is printed, copied or written.
#
#   tools/live_env.sh $PY -m company.run --transport switch ...        broker + Haiku profile + GitHub mode
#   tools/live_env.sh --local $PY -m company.run --transport switch ...   broker + Haiku profile only
#                                                                         (acme-shop on the local stand-in origin)
#   tools/live_env.sh --check                                          which settings are set (names only)
#
# Always:      COMPANY_SYNAPS_BASE_DIR, COMPANY_BROKER_TOKEN_FILE, COMPANY_BROKER_URL (the private broker that
#              company/llm.py and the PR review call; the token stays in its file, only its path is exported).
# GitHub mode: everything in <base>/acme-github.env (written by `tools/synaps_dev.py setup --github`):
#              ACME_REPO, ACME_REMOTE, ACME_ALLOW_GITHUB_WRITES, ACME_DEPLOY_KEY, ACME_ISSUE_AUTHORS.
#              Without it, or with --local, acme-shop stays on the local remote (--acme-remote file://...).
# Values already in the environment win over the defaults above (not over acme-github.env).
set -euo pipefail

base=${SYNAPS_SWITCH_DEV_BASE:-$HOME/.synaps-switch-dev}
mode=github
case "${1:-}" in
  --local) mode=local; shift ;;
  --check) mode=check; shift ;;
esac

export COMPANY_SYNAPS_BASE_DIR=${COMPANY_SYNAPS_BASE_DIR:-$base/company-llm}
export COMPANY_BROKER_TOKEN_FILE=${COMPANY_BROKER_TOKEN_FILE:-$base/broker/machine.token}
export COMPANY_BROKER_URL=${COMPANY_BROKER_URL:-http://127.0.0.1:18191}
[ -r "$COMPANY_BROKER_TOKEN_FILE" ] || { echo "live_env: no broker token at $COMPANY_BROKER_TOKEN_FILE (python3 devstack/synapsdev.py broker start)" >&2; exit 2; }

envf=$base/acme-github.env
if [ "$mode" != local ] && [ -f "$envf" ]; then
  set -a; . "$envf"; set +a
elif [ "$mode" = local ]; then
  unset ACME_ALLOW_GITHUB_WRITES ACME_REMOTE ACME_DEPLOY_KEY
fi

if [ "$mode" = check ]; then
  for n in COMPANY_SYNAPS_BASE_DIR COMPANY_BROKER_TOKEN_FILE COMPANY_BROKER_URL \
           ACME_REPO ACME_REMOTE ACME_ALLOW_GITHUB_WRITES ACME_DEPLOY_KEY ACME_ISSUE_AUTHORS; do
    if [ -n "${!n:-}" ]; then echo "$n set"; else echo "$n -"; fi
  done
  exit 0
fi
[ $# -gt 0 ] || { echo "usage: tools/live_env.sh [--local|--check] COMMAND [ARGS...]" >&2; exit 2; }
exec "$@"
