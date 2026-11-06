#!/usr/bin/env bash
# Bring up the synaps-switch devstack: Switch standalone 0.31.0 (core + Mattermost),
# loopback only, compose project synaps-switch-dev. Idempotent.
#   WITH_GATEWAY=1 devstack/up.sh   also start the Gateway dashboard (UI, ~200 MB)
set -euo pipefail
cd "$(dirname "$0")"
P=synaps-switch-dev
TAG=switch-v0.31.0
SHA=cba238878078cf3d77fd9e136ef432f1ce5fd1bfe597acd172b9faa6720f0121
COMPOSE=vendor/standalone-docker-compose.yml

avail=$(free -g | awk '/^Mem:/{print $7}')
if [ "${avail:-0}" -lt 4 ]; then
  echo "up.sh: only ${avail} GB available; the stack wants ~3 GB. Refusing (FORCE=1 overrides)." >&2
  [ "${FORCE:-0}" = 1 ] || exit 1
fi

mkdir -p vendor
if ! echo "$SHA  $COMPOSE" | sha256sum -c --status 2>/dev/null; then
  curl -fsSL "https://raw.githubusercontent.com/sandbox-quantum/switch/$TAG/deploy/local/standalone-docker-compose.yml" -o "$COMPOSE.tmp"
  echo "$SHA  $COMPOSE.tmp" | sha256sum -c --status || { echo "up.sh: compose sha256 mismatch" >&2; rm -f "$COMPOSE.tmp"; exit 1; }
  mv "$COMPOSE.tmp" "$COMPOSE"
fi

python3 dev.py init-env
mkdir -p state && chmod 700 state

profiles=(--profile collab)
[ "${WITH_GATEWAY:-0}" = 1 ] && profiles+=(--profile gateway)

# Bare-key settings in the compose file read the shell env.
export TELEMETRY_ENABLED=false THIRD_PARTY_AVATARS_ENABLED=false
docker compose -p "$P" --env-file .env -f "$COMPOSE" -f compose.override.yml "${profiles[@]}" up -d
python3 dev.py wait
# The setup job seeds the Mattermost team/users and registers the bridge.
for _ in $(seq 1 90); do
  st=$(docker compose -p "$P" --env-file .env -f "$COMPOSE" -f compose.override.yml --profile collab ps -a --format '{{.Service}} {{.State}} {{.ExitCode}}' setup 2>/dev/null || true)
  case "$st" in *"exited 0"*) break ;; esac
  sleep 2
done
docker compose -p "$P" --env-file .env -f "$COMPOSE" -f compose.override.yml --profile collab logs --no-log-prefix setup 2>/dev/null | grep -E "Setup complete|ERROR|WARNING|Registered|already" | tail -8 || true
python3 dev.py mint-token
python3 dev.py info
