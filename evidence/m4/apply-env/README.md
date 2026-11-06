# M4 #8 live check: apply an env to Switch (rooms, agents, links, Ledger doc)

Stack: Switch 0.31.0 (docker project `synaps-switch-dev`, bridge on 127.0.0.1:18400, Mattermost on 127.0.0.1:18465).
HTTP only; nothing restarted. Everything made here is named `m4e-*`: agents `m4e-<env agent>` (registered with the
dev registration token, credentials in `company/state/agents/`, 0600, gitignored), rooms `m4e-<room>-g<k>` in room
group `m4e-<env>-g<k>`. `--stand-in-synaps` registers a plain `m4e-synaps-dev` so the real `synaps-dev` (the Synaps
extension's agent, used by #12) is never touched.

Runs (no model calls, $0):

```
python evidence/m4/apply-env/live_apply_env.py          # M4E_GEN=95: env1 as g95 (template), env2 as g96 (per room)
python evidence/m4/apply-env/live_with_transport.py     # M4E_TGEN=97: apply through a live SwitchTransport
```

## Result

| check | result |
|---|---|
| op registry (`GET /agents/{id}/ops`): 53 ops; every op apply/teardown use (10 required + 12 optional) present | `ops-check.json`, no gap |
| `python -m company.switch_env apply company/envs/env1.yaml --gen 95 ...`: one `create_room_from_yaml` makes the group, 6 rooms, 5 links | `apply-env1-g95.log`, `.trace.jsonl` (15 Switch calls) |
| the 6 Mattermost channels exist with the right agents' bots and the user | table below, `env1-g95.switch-and-mattermost.json` |
| #board holds the Handbook and the Ledger, created by the operator | `env1-g95.handbook.md`, `env1-g95.ledger.md` |
| apply again: nothing created, only the Handbook refreshed, Ledger kept | `reapply-env1-g95.log` (13 calls: reads + one `update_room_document`) |
| `update_ledger` writes a quarter row (the seeded market settled with no actions, seed 7) | `env1-g95.ledger-after-update.md` |
| `teardown`: 6 × `archive_room`; `list_rooms(include_archived)` shows all six archived | `teardown-env1-g95.*` |
| env2 = env1 + `link maintenance↔dev`, `--mode rooms`: 6 × `create_room`, `create_room_group`, 7 × `link_rooms` (5 reporting lines + the direct line both ways), docs, teardown | `apply-env2-g96-rooms.*`, `env2-g96.*`, `teardown-env2-g96.*` |
| through SwitchTransport: `apply_env(env1, t._link("m4e-board"))` → `t.pin_rooms(rooms)` → ops-maint posts in env room `maintenance` to @board, board answers in the thread, both see room `maintenance`; `update_ledger` and `teardown` on the same link | `transport-g97.log`: 5/5 PASS |

env1 as generation 95 (`env1-g95.switch-and-mattermost.json`):

| env room | Switch room | Switch agents | users | links | Mattermost channel members |
|---|---|---|---|---|---|
| board | m4e-board-g95 | m4e-board | user | - | admin, m4e-board, user |
| dev | m4e-dev-g95 | m4e-synaps-dev, m4e-reviewer, m4e-board | user | → m4e-board-g95 (reports to) | admin, m4e-board, m4e-reviewer, m4e-synaps-dev, user |
| finance | m4e-finance-g95 | m4e-finance, m4e-board | user | → m4e-board-g95 (reports to) | admin, m4e-board, m4e-finance, user |
| growth | m4e-growth-g95 | m4e-growth, m4e-board | user | → m4e-board-g95 (reports to) | admin, m4e-board, m4e-growth, user |
| maintenance | m4e-maintenance-g95 | m4e-ops-maint, m4e-board | user | → m4e-board-g95 (reports to) | admin, m4e-board, m4e-ops-maint, user |
| marketing | m4e-marketing-g95 | m4e-marketing, m4e-board | user | → m4e-board-g95 (reports to) | admin, m4e-board, m4e-marketing, user |

`user` is `{$creator}`: the owner's Mattermost username, from `get_my_context`. `admin` is in every channel because
Switch's Mattermost admin account creates them.

## What 0.31.0 does and doesn't do (found live)

- **No op is missing.** `create_room_from_yaml`, `run_template`, `create_room`, `invite_agent_to_room`,
  `add_users_to_room`, `link_rooms`/`list_linked_rooms`, `create_room_document`/`update_room_document`,
  `archive_room`/`unarchive_room`, `create_room_group` are all there.
- **Template agents are managed agents.** A v2 `type: agent` resource is created on a machine with a provider
  (claude/codex/opencode), so our registration-token agents can't come from a template. The template therefore
  references existing agents by name and creates only the group, the rooms and the links. `run_template` adds nothing
  over `create_room_from_yaml` for us and isn't used. `core/env.py:to_switch_template()` emits top-level
  `agents:`/`rooms:`, which 0.31.0 doesn't read as a v2 room template, so `switch_template(plan)` builds the v2 document.
- **`archive_room` is metadata-only.** The Mattermost channel stays open and unarchived. There's no op to delete or
  archive a room group, so each generation leaves its group, with only archived rooms in it.
- **Room documents can only be updated by the agent that created them.** The operator (board) owns the Ledger.
- **Membership gates everything.** `get_room_detail`, `archive_room` and `list_linked_rooms` all need the caller in
  the room, so the operator joins every room.
- **A room op needs a live stream.** A connection id without an open event stream is refused for any op
  (`409 connection … is not open`). Connecting a second session of the same agent to a room evicts the first from it.
  The warning is visible in `reapply-env1-g95.log`, where the second CLI run took #board from the first run's closed
  connection.
- `create_room` was not flaky this time: no 500 or "bridge not running", so no retry fired and the devstack
  Mattermost check found every bot already in its channel. The retry path is covered by the unit tests.

## Left on the stack

All of the following were made by these runs and are prefixed `m4e-`. All 42 rooms are archived: 7 generations
(g90–g93 and g95–g97, since g94 stopped at the CLI check) × 6. Their 7 room groups and the Mattermost channels stay,
because 0.31.0 has no op to remove them. Test agents: `m4e-board`,
`m4e-ops-maint`, `m4e-growth`, `m4e-marketing`, `m4e-finance`, `m4e-synaps-dev`, `m4e-reviewer`. The `m4e-nosuch-*`
agents came from checking the CLI's "synaps-dev not registered" error, before it was changed to check first and
register after.
