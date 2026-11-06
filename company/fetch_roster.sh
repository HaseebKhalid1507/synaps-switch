#!/usr/bin/env bash
# Fetch The Agency's personas (msitarzewski/agency-agents, MIT) at the pinned commit, for company/core/roster.py.
#   company/fetch_roster.sh            -> $COMPANY_ROSTER_DIR, default company/vendor/agency-agents (gitignored)
#   company/fetch_roster.sh DIR        -> DIR (then export COMPANY_ROSTER_DIR=DIR)
# Idempotent: a clone already at the pin is left alone. The pin matches agency/build_plugin.py.
set -euo pipefail
REPO="${AGENCY_REPO:-https://github.com/msitarzewski/agency-agents.git}"
PINNED="b859c515d813efcfc4755d3471dd9e7686c7706d"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-${COMPANY_ROSTER_DIR:-$HERE/vendor/agency-agents}}"

if [ -d "$DEST/.git" ] && [ "$(git -C "$DEST" rev-parse HEAD 2>/dev/null)" = "$PINNED" ]; then
  echo "roster: $DEST already at $PINNED"
  exit 0
fi
if [ ! -d "$DEST/.git" ]; then
  mkdir -p "$(dirname "$DEST")"
  git clone --quiet --no-checkout "$REPO" "$DEST"
fi
git -C "$DEST" fetch --quiet origin "$PINNED" 2>/dev/null || git -C "$DEST" fetch --quiet origin
git -C "$DEST" -c advice.detachedHead=false checkout --quiet "$PINNED"
echo "roster: $DEST at $(git -C "$DEST" rev-parse HEAD) ($(find "$DEST" -mindepth 2 -name '*.md' | wc -l) persona files)"
