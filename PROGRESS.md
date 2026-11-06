# Progress

Branch `feat/v0`. Updated at each milestone.

## Status

| Milestone | State |
|---|---|
| M0 devstack | done: Switch `switch-v0.31.0` standalone (core + Mattermost + Postgres), project `synaps-switch-dev`, loopback only |
| M0 raw loop | done: `tools/raw_loop.py prove` registers, streams, beats, replies in a Mattermost thread. Evidence: `evidence/m0-raw-loop.log` |
| M1 client | done: `switch_client.py` (stdlib) + 21 unit tests against a fake bridge (`tests/`) |
| M2 extension | done: loads in the private daemon, registers, holds the connection, creates + pins one session per room, delivers, serves the tools incl. `switch_op`. Evidence: `evidence/m2-tools-and-switch-op/` |
| M3 e2e | done: `tools/e2e.py` 12/12 PASS on a fresh room, twice (before and after the library refactor). Evidence: `evidence/m3-e2e-20261106-011427/`, `evidence/m3-e2e-20261106-012214/` |
| DESIGN-company ask (1) | done: `switch_client.Agent`, a library with an `on_message` callback; the extension runs on it too. Live: `examples/echo_agent.py` |
| DESIGN-company ask (3) | done: only addressed frames, never its own, each message id once (persisted), per-thread cap. Live: `tools/loopcheck.py`, evidence `evidence/loopcheck-20261106-012541/` |
| agency plugin (PR #15) | built into the private base dir: `~/.synaps-switch-dev/plugins/agency`, real dir, 283 agents / 18 divisions; daemon loads `plugins=2 skills=19` |

## M4 #12: real work (synaps-dev → review → #board → merge)

Live, 13/13 PASS: `evidence/m4/real-work/` (README there). acme-shop#1 fixed by synaps-dev in its own clone,
reviewed (harness pytest + testing-reality-checker on Haiku), approved by a human in #board, squash-merged, booked as
`week_fixed`. Against a **local bare clone standing in for origin**: the account has no push access to acme-shop yet.

- `company/acme.py`: issues → `Market(issues=...)` (read-only gh), `baseline` tag + `gen-k` branches (reset between
  trials), local PR refs, diff/tests/merge on the local remote, `ACME_REMOTE` / `ACME_ALLOW_GITHUB_WRITES` config.
- `company/realwork.py`: dispatch + RESULT parsing, review, `MergeGate` / `apply_decision` (the board's approval gate).
- `company/devworker.py`: the prompt is one stdin line (headless chat made every line a turn); plain push, force only
  with a lease on a branch this run created; GitHub writes refused without the flag; local mode.
- Extension: agent keys `skill_file`, `workdir_repo`, `session_env`, `session_path`; `skill/synaps-dev.md`;
  `tools/synaps_dev.py setup` writes the synaps-dev entry; `tools/session_trace.py` dumps a session's tool calls.
- Once push access is granted: `tools/synaps_dev.py setup --github`, `ACME_REMOTE=git@github.com:adityapat958/acme-shop.git`,
  `ACME_ALLOW_GITHUB_WRITES=1`, push `baseline` + `gen-k` to the real repo, PR refs become GitHub URLs (`gh pr create`
  in the skill's instructions), `merge_pr` uses `gh pr merge --squash`.
- Test agents/rooms: `synaps-dev`, `m12-board`, `m12-reviewer`; rooms `m12-dev`, `m12-board`. The private plugin link
  `~/.synaps-switch-dev/plugins/synaps-switch` pointed at the #12 worktree for the run.

## M4 #11: the board's outer loop (reflect → propose → human gate → trial → keep/revert)

`company/board/evolve.py`: one LangGraph graph, SqliteSaver-checkpointed (`evolve:<env>:<seeds>`), resumable also
while paused at the human gate. The board persona proposes ONE mutation (Haiku, or a stub), validated by applying
it, re-asked once; the env_diff + rationale go to #board; only a person on the approvers list resumes it; approved
changes are trialed on the same seeds and kept only if mean fitness improves. `company/board/stub_quarter.py` is a
deterministic quarter for offline runs/tests (settled by the real market); #10's `quarter.run_quarter` plugs in
via `load_quarter_runner("quarter", ...)`. `company/lineage_report.py` renders the lineage table.
Evidence: `evidence/m4/evolve/` (offline 3 generations: kept, reverted, rejected; live gate on Switch: an agent's
approve ignored, killed + resumed at the gate, a person's Mattermost reply decides).
Test agents/rooms: `m4v-board`, `m4v-peer`, room `m4v-board`; private state `~/.synaps-switch-dev/m4v/`.

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

## Shared with the M4 workers (don't break)

- The Switch stack (`docker compose -p synaps-switch-dev`) and the private broker on 127.0.0.1:18191 are used by the
  M4 workers (#6, #7), who register `m4t-*` agents. Don't restart the broker or recreate the stack without a very
  good reason; if you must, log it here with the time.
- Restarts so far: 2026-11-06 01:12 EST, `devstack/up.sh` recreated only the postgres container to apply
  the dev no-fsync settings (before the M4 workers started). Since then nothing has been restarted except the
  private Synaps daemon (`synapsdev.py daemon stop|start`, used by `tools/e2e.py`; it does not affect Switch or the broker).
- `switch_client.py` public API is stable: `register`, `Bridge` (`op`, `ops`, `beat`, `health`), `Connection`,
  `SSEParser`, `SwitchError`, `Fatal`. New things are additive (`Agent`, `Message`, `Redeliver`).
- Test agents I registered: `synaps` (the extension), `ss-echo`, `ss-ping`, `ss-pong`. Rooms: `synaps-lab`,
  `e2e-*`, `ss-echo-room`, `loop-*`.
- #8 (`company/switch_env.py`) registered `m4e-*` agents and made rooms `m4e-<room>-g90…g97` in room groups
  `m4e-env{1,2}-g9x`. All of those rooms are archived; 0.31.0 has no op to remove the groups. Evidence:
  `evidence/m4/apply-env/`.

## switch_client as a library (ask 1)

```python
from switch_client import Agent, Redeliver

def handle(msg):                 # msg: room_id, message_id, thread, sender_name, body, attachments, gap
    if msg.gap:                  # history was lost before this message: read before acting
        ctx = msg.agent.read_context(msg.room_id)
    return "on it"               # a returned string is posted as a reply in msg's thread
    # or: msg.reply("...", targets=["synaps-dev"]); raise Redeliver() to get it again later

agent = Agent("http://127.0.0.1:18400", "ops-maint", on_message=handle,
              credentials_file="state/ops-maint.json",          # registers once; written 0600
              registration_token_file="devstack/state/registration.token",
              state_file="state/ops-maint.state.json",          # cursor + handled ids: resume, never twice
              max_thread_deliveries=12, thread_window_s=600)    # the per-thread loop cap
agent.start(); agent.wait_connected()
agent.post(room_id, "hello", thread_id=None, targets=["board"])  # targets -> send_targeted_message
agent.op("create_room", {...})                                   # any catalog op; room_id=... runs it in that room
agent.run_forever()
```

`on_message` runs on the stream thread, one message at a time, in order; the cursor only moves after it returns
(raise `Redeliver` to leave the message unconsumed). Other exceptions are logged and the message counts as handled,
so a broken handler can't loop. Several rooms share the one connection; each room gets its own session selector
(`connect_to_room` once, lazily, rebound if Switch drops the binding).

No loops (ask 3), enforced in `Agent` before `on_message`:
- only frames with `addressed: true` (the connection also opens with `filter=addressed`);
- never the agent's own messages;
- each `message_id` at most once, also across restarts (the last 500 ids are kept in `state_file`);
- at most `max_thread_deliveries` messages per (room, thread) per `thread_window_s`; the rest are dropped and logged.
Live finding: Switch delivers an agent's `send_targeted_message` to the target agent with `addressed: true`, so two
agents that always answer each other *will* rally; in `tools/loopcheck.py` the cap stopped it at 4 each. The board's
router should still keep its own resume rules (only the expected sender resumes a paused run).

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
python3 devstack/synapsdev.py setup       # base dir, config, plugin link, agency plugin (real dir)
python3 devstack/synapsdev.py broker start
python3 devstack/synapsdev.py daemon start
python3 tools/mkroom.py synaps-lab        # create a Mattermost-bridged room with the agent
python3 tools/e2e.py                      # M3: fresh room, 3 mentions, daemon restart in between (~2 min, ~$0.10)
python3 tools/transcript.py synaps-lab evidence/<dir>   # scrubbed transcript + extension log for a channel
python3 tools/loopcheck.py                # ask (3): two agents that always answer each other stop at the cap (no model, $0)
python3 examples/echo_agent.py --name my-echo --url http://127.0.0.1:18400 \
    --token-file devstack/state/registration.token --state /tmp/my-echo   # smallest library agent
python3 tools/mkroom.py my-room --agent my-echo --creds /tmp/my-echo/credentials.json [--with other-agent]
```

Mattermost UI: http://127.0.0.1:18465, user/password in `devstack/.env` (`MATTERMOST_USER*`).

## M3 result (evidence/m3-e2e-20261106-011427, rerun after the refactor: evidence/m3-e2e-20261106-012214)

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
