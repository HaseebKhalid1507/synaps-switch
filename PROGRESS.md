# Progress

Branch `feat/v0`. Updated at each milestone.

## Status

| Milestone | State |
|---|---|
| M0 devstack | done: Switch `switch-v0.31.0` standalone (core + Mattermost + Postgres), project `synaps-switch-dev`, loopback only |
| M0 raw loop | done: `tools/raw_loop.py prove` registers, streams, beats, replies in a Mattermost thread. Evidence: `evidence/m0-raw-loop.log` |
| M1 client | done: `switch_client.py` (stdlib) + 21 unit tests against a fake bridge (`tests/`) |
| M2 extension | in progress: loads in a private daemon, registers the agent, holds the connection. Room session creation not yet exercised end to end |
| M3 e2e | not started |

## What works

- `devstack/up.sh` / `down.sh`: compose file fetched at the pinned tag and sha256-checked; refuses to start with < 4 GB available.
- `devstack/dev.py`: env init, wait for health, mint a registration token, Mattermost helpers (post, read thread, join).
- `devstack/synapsdev.py`: private base dir `~/.synaps-switch-dev` with its own `synaps auth-broker` on 127.0.0.1:18191
  (machine token file 0600, no copy of auth.json) and its own daemon. `daemon start` refuses to proceed unless the
  extension reports the private base dir.
- `switch_client.py`: register, event stream (SSE parser, Last-Event-ID / cursor resume, gap handling, eviction codes,
  backoff), heartbeat thread (beats from the moment the stream opens, before `connection_state`, so a slow server cannot
  expire the connection), ops with connection + session selector headers.
- `main.py` (extension): N agents from a JSON list; per agent one connection; per room one pinned daemon session created
  over the daemon socket; addressed messages delivered to that session as events; tools `switch_post_message`,
  `switch_read_context`, `switch_list_participants`, `switch_room_info`, `switch_list_rooms`, `switch_list_ops`,
  generic `switch_op(name, args)`, `switch_status`.

## Extension API gaps (worked around, Synaps not patched)

- An extension cannot create a session: `main.py` uses the daemon socket (hello, attach create, bye; then pin), as
  subconscious does.
- Extensions get a scrubbed environment (HOME, LANG, PATH, TERM): no `SYNAPS_BASE_DIR`, so the base dir is the
  `base_dir` config key. Without it the extension would write state under `~/.synaps-cli` (seen once in dev, moved out).
- Delivery into a session is the session's event socket (as agent-comms' wake).

## Reproduce

```bash
free -g                                   # want >= 4 GB available
devstack/up.sh                            # Switch + Mattermost on 127.0.0.1
python3 devstack/dev.py mint-token        # registration token -> devstack/state/registration.token (0600)
python3 tools/raw_loop.py prove           # M0 raw loop
python3 -m unittest tests.test_switch_client
python3 devstack/synapsdev.py broker start
python3 devstack/synapsdev.py daemon start
python3 tools/mkroom.py synaps-lab        # create a Mattermost-bridged room with the agent
```

## Known issues / next

- Under heavy machine load (load average ~80 from TB trials) Switch's calls to Mattermost time out (10 s) and
  `create_room` returns 500. Retry when load drops, or create the room once and reuse it.
- Next: create the room, mention the agent in Mattermost, confirm a room session is created, pinned, and replies in
  the thread via `switch_post_message`; then `switch_op` and restart/resume; evidence in `evidence/`.
