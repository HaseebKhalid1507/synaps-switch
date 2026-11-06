# synaps-switch: plan

Owner: Haseeb. Orchestrator: Jawz (havoc). Started Fri 2026-11-06 00:30 EST.

## Why now

Switch's **Flip the Switch** hackathon (WeMakeDevs x DevTools Academy x Switch):
<https://www.wemakedevs.org/hackathons/switch>

- **Submissions close Sat 2026-11-07 23:59 IST = 13:29 EST.**
- Custom agents are explicitly allowed "through the agent protocol". Existing agents
  are fine; the Switch setup and the workflow must be built during the hackathon.
- Judged on: use of Switch (central, project stops without it), technical execution
  (runs, clone-and-reproduce), creativity and impact, presentation (demo video + README).
- Tracks worth entering: Best Business Workflow ($2k) or Best Personal/Creative
  Workflow ($2k); Best Blog (side). Core-contribution issues are 6-10 deep in
  assignees already; skip.
- **Registration, submission, and anything posted on sandbox-quantum/switch are
  Haseeb's.** Workers never post externally.

## Shape

A Synaps daemon extension (Python, stdlib only, like `~/Projects/agent-comms`) that is
a Switch agent client. Switch calls this a "watcher": one per agent, holding the
agent's single connection, routing room events to sessions.

1. **Register once.** `POST /agents/register-known` with a registration token from
   the Gateway → `{id, api_key}` (the key is shown once). Store under the private
   state dir, 0600. Agent type `always_on`.
2. **One event stream.** `GET /agents/{id}/events?connection_id=<uuid>&scope=all&filter=addressed&protocol=..&client=synaps-switch`.
   Connection id generated once and persisted. Read `connection_state` (rooms,
   cursor, heartbeat interval) and use the server's values. Persist the cursor;
   reconnect with `Last-Event-ID`. Handle `gap`, `evicted`, `subscription_changed`,
   `room_released`. Keepalives are SSE comments.
3. **Heartbeat.** `POST /agents/{id}/connection/beat {connection_id, cursor}` every
   `heartbeat_interval_seconds` (2 s; TTL 6 s). 404/409 → reopen the stream, not back off.
4. **Room ⇄ session.** First addressed message in a room → create a daemon session
   named `switch:<room-slug>` over the daemon socket (`attach: create` with a
   SessionConfig: system prompt = Switch skill + room context from
   `connect_to_room`, cwd = a per-room work dir, cost cap), pin it, detach.
   Report the map with `POST /agents/{id}/connection/placements`.
   Map persisted so a daemon restart reuses the same sessions.
5. **Delivery.** Each addressed `message` is delivered to the room's session as an
   event on its session socket (the path agent-comms `wake()` and the subconscious
   use; it unparks a parked session and starts a turn). Include sender name,
   thread id, attachments as pointers.
6. **Tools** (registered by the extension, routed by the calling session's id → room,
   thin over `POST /ops/{name}` with `X-Switch-Connection-Id`):
   `switch_post_message` (with thread), `switch_read_context`, `switch_list_participants`,
   `switch_connect_room`, `switch_list_rooms`. More only when a workflow needs them.
7. **The skill.** Switch's host-neutral agent skill is
   `console/packages/plugins/src/switch-skill/SKILL.md` in their repo. Adapt it for
   Synaps tool names; cite it.

Not in v0: tasks protocol (Switch says "not ready"), media upload, commands (`!reset`)
(need `filter=all`), multi-agent from one extension (one Switch agent per extension
config in v0; the crew idea comes after).

## Milestones

| | Done when |
|---|---|
| **M0 devstack** | `devstack/up.sh` brings up Switch standalone (`deploy/local/standalone-docker-compose.yml`, profiles `collab` + `gateway`, pinned `SWITCH_VERSION`, loopback only, compose project `synaps-switch-dev`). Mattermost reachable, a test room exists, a registration token minted. `down.sh` removes only this project. Documented in `devstack/README.md`. |
| **M1 client** | `switch_client.py`: register, stream (SSE parser, reconnect, cursor, gap), beat loop, ops. Unit tests against a fake bridge (stdlib `http.server`), no network. |
| **M2 extension** | Extension loads in a **private** Synaps daemon, holds the connection, creates/pins room sessions, delivers events, serves the tools. |
| **M3 end to end** | A person @-mentions the agent in Mattermost → the room's Synaps session wakes → reply appears in the channel, threaded. Second message in the same room reuses the session and remembers the first. Daemon restart → same session, cursor resumes, no duplicate replies. Evidence (logs, transcripts, screenshots of Mattermost via the API) in `evidence/`. |
| M4 workflow | Haseeb picks the demo workflow (Fri evening). |
| M5 submit | README for judges, demo video (Haseeb), submission (Haseeb) before Sat 13:29 EST. |

## Rules (every worker gets these verbatim)

- **Never touch the live Synaps daemon or `~/.synaps-cli`.** No `synaps daemon reload`,
  no `reload-extension`, no `pin`/`close` on the main daemon, no installing this
  plugin into `~/.synaps-cli/plugins`. All testing runs on a private base dir
  (`SYNAPS_BASE_DIR`, e.g. `~/.synaps-switch-dev/`) with its own daemon.
- **Never copy `auth.json`** (OAuth refresh tokens rotate; a copy breaks the real one).
  Use a private `synaps auth-broker` on its own port (not 18181, which is the
  synaps-bench campaign's) following `~/Projects/synaps-bench/runner/broker.py`.
- **Docker:** plain `docker compose -p synaps-switch-dev` only. Never `--privileged`,
  never `--cgroupns=host`, never bind `/sys/fs/cgroup`. **Never prune, stop or remove
  any container, network, image or volume outside project `synaps-switch-dev`**:
  wisp's Terminal-Bench trials are running on this machine. Bind ports to 127.0.0.1.
- **No cargo.** This is Python; nothing compiles. (The cargo shim refuses anyway.)
- **No external posts.** No issues, PRs, comments or registrations on any repo or site
  other than branches/PRs in `HaseebKhalid1507/synaps-switch`. Never merge to `main`;
  open the PR with evidence, stop. Haseeb merges.
- **Memory:** bella is shared (TB trials, overnight builds). Keep the stack lean;
  check `free -g` before starting it.
- **Spend:** room sessions use Sonnet, with a session cost cap. Test turns are short.
- **Secrets** never in git, logs or PR bodies: registration tokens, API keys, machine
  tokens. `.gitignore` covers `state/`, `devstack/.env`.

## References

- Switch recon clone (read-only): `/tmp/switch-recon` (head f62ae50, 2026-11-06).
  Agent protocol: `docs/official/internals/agent-protocol.md`. Runtime/watcher:
  `docs/official/internals/connectors-and-runtime.md`. Their TS event stream:
  `console/packages/switch-agent-runtime/src/event-stream.ts`, `sse.ts`.
  Server side: `core/switch_core/bridges/agent/`. Skill:
  `console/packages/plugins/src/switch-skill/SKILL.md`.
- Synaps (`~/Projects/agent-runtime`, read `origin/dev`): `docs/daemon-mode.md`
  (protocol summary, pinning, parking, event delivery), `docs/extensions/*`,
  `docs/events-reactor.md`.
- Working extension to copy patterns from: `~/Projects/agent-comms/main.py`
  (`wake()` delivers to a session socket; tool registration; hooks).
- Daemon session create + pin from Python: `~/Projects/subconscious/main.py`
  (`daemon_socket()`, `daemon_sessions()`, setup steps around line 2300).
