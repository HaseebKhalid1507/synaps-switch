# Progress

Branch `feat/v0`. Updated at each milestone.

## Status

| Milestone | State |
|---|---|
| M0 devstack | done: Switch `switch-v0.31.0` standalone (core + Mattermost + Postgres), project `synaps-switch-dev`, loopback only |
| M0 raw loop | done: `tools/raw_loop.py prove` registers, streams, beats, replies in a Mattermost thread. Evidence: `evidence/m0-raw-loop.log` |
| M1 client | done: `switch_client.py` (stdlib) + 21 unit tests against a fake bridge (`tests/`) |
| M2 extension | done: loads in the private daemon, registers, holds the connection, creates + pins one session per room, delivers, serves the tools incl. `switch_op`. Evidence: `evidence/m2-tools-and-switch-op/` |
| M3 e2e | done: `tools/e2e.py` 12/12 PASS on a fresh room. Evidence: `evidence/m3-e2e-20261106-011427/` |

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
python3 tools/e2e.py                      # M3: fresh room, 3 mentions, daemon restart in between (~2 min, ~$0.10)
python3 tools/transcript.py synaps-lab evidence/<dir>   # scrubbed transcript + extension log for a channel
```

Mattermost UI: http://127.0.0.1:18465, user/password in `devstack/.env` (`MATTERMOST_USER*`).

## M3 result (evidence/m3-e2e-20261106-011427)

- Mention in a fresh room → session created and pinned (Sonnet, $0.50 cap) → reply threaded under the mention in ~6 s.
- Second, separate message → same session, remembers the first.
- Private daemon stopped, a message posted while it is down, daemon started → Synaps brings the pinned session
  back, the extension reopens the same connection at its saved cursor, the message is delivered once and answered
  by the same session, which still remembers. Exactly one reply per message; three deliveries in the log.

## M2 result (evidence/m2-tools-and-switch-op)

- From Mattermost: "who is in this room, and add a room document" → `switch_list_participants` and
  `switch_op("create_room_document", …)` from the room session; Switch posts the document notice, the agent
  answers in the thread.

## Known issues

- **IO-starved Postgres breaks Switch.** Switch's process lease is hard-coded at 15 s. With the host under disk load
  (TB trials), a lease renewal waited 20 s on Postgres, so Switch kept stopping its Mattermost bridge, and posts
  made while it was down were never ingested. Fix in `devstack/compose.override.yml`: dev Postgres runs with
  `fsync=off, synchronous_commit=off, full_page_writes=off`. That was stable afterwards. Dev only.
- `create_room` can still fail (500, or 400 "Bridge not running") when Mattermost is slow. `tools/mkroom.py`
  retries, and adds the agent's bot to the channel itself when Switch skipped it.
- A failed `create_room` can leave an orphan Mattermost channel; adding the bot to it makes Switch adopt it as
  "Mattermost: <name>" (that's how `synaps-lab` ended up as room `Mattermost: synaps-lab`). Harmless in dev.
- When the bot joins a channel, Switch posts a short greeting as the bot ("Hi there! Just @synaps me…"). That
  greeting comes from Switch, not from the Synaps session.
- Room sessions stay pinned until removed; old test rooms leave idle pinned sessions in the private daemon
  (`synapsdev.py sessions`). No cost while idle.

## Next (after M3, not done here)

- M4: the demo workflow (Haseeb). Multi-room: one connection already covers N rooms, each with its own session;
  N agents: add entries to `agents` in `~/.synaps-switch-dev/synaps-switch.json`.
- Unaddressed room chatter is not delivered (filter `addressed`); sessions read it with `switch_read_context`.
- Attachments are listed by name, not downloaded.
