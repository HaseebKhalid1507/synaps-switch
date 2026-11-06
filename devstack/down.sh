#!/usr/bin/env bash
# Stop the devstack. Touches only compose project synaps-switch-dev.
#   devstack/down.sh          stop + remove this project's containers and network (data kept)
#   devstack/down.sh --wipe   also remove this project's volumes and devstack/state
set -euo pipefail
cd "$(dirname "$0")"
P=synaps-switch-dev
args=()
[ "${1:-}" = --wipe ] && args+=(-v)
docker compose -p "$P" --env-file .env -f vendor/standalone-docker-compose.yml -f compose.override.yml \
  --profile collab --profile gateway down "${args[@]}"
if [ "${1:-}" = --wipe ]; then rm -rf state; echo "wiped volumes + devstack/state (devstack/.env kept)"; fi
