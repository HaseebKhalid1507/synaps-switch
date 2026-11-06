# M4 #8: an env applied to Switch (switch-v0.31.0, devstack)

Full evidence, scripts and traces of every Switch call are in [`m4/apply-env/`](m4/apply-env/README.md). This page has
the op responses and the channel list that "Done when" asks for.

Command (agents and rooms prefixed `m4e-` so the shared stack's real `synaps-dev` and the other workers' rooms are
untouched; without prefixes the command is `python -m company.switch_env apply company/envs/env1.yaml`):

```
python -m company.switch_env apply company/envs/env1.yaml --gen 95 --stand-in-synaps --agent-prefix m4e- --room-prefix m4e-
switch ops: all 22 ops apply/teardown use are present (53 in the registry)
template: group 'm4e-env1-g95', 6 rooms
applied env1 generation 95: 6 rooms, 5 links, documents ['Handbook', 'Ledger']
```

Op responses (from `m4/apply-env/apply-env1-g95.trace.jsonl`):

```
create_room_from_yaml -> {"group_id": "ab2f280e-…", "group_name": "m4e-env1-g95", "errors": [],
                          "rooms": [{"room_id": "db64d6d2-…", "room_name": "m4e-board-g95", …}, … 6 rooms]}
list_linked_rooms @m4e-dev-g95 -> [{"target_room_name": "m4e-board-g95", "label": "reports to", "access": "member"}]
   (the same for maintenance, growth, marketing, finance)
create_room_document @m4e-board-g95 {"name": "Handbook", …} -> {"document_id": "c0167a54-…"}
create_room_document @m4e-board-g95 {"name": "Ledger", …}   -> {"document_id": "9fac16c5-…"}
```

Channels, from Switch (`get_room_detail`) and Mattermost (`/channels/{id}/members`):

| Mattermost channel | agents in the room | channel members | link |
|---|---|---|---|
| m4e-board-g95 | m4e-board | admin, m4e-board, user | (Handbook + Ledger documents) |
| m4e-dev-g95 | m4e-synaps-dev, m4e-reviewer, m4e-board | admin, m4e-board, m4e-reviewer, m4e-synaps-dev, user | → board (reports to) |
| m4e-maintenance-g95 | m4e-ops-maint, m4e-board | admin, m4e-board, m4e-ops-maint, user | → board (reports to) |
| m4e-growth-g95 | m4e-growth, m4e-board | admin, m4e-board, m4e-growth, user | → board (reports to) |
| m4e-marketing-g95 | m4e-marketing, m4e-board | admin, m4e-board, m4e-marketing, user | → board (reports to) |
| m4e-finance-g95 | m4e-finance, m4e-board | admin, m4e-board, m4e-finance, user | → board (reports to) |

Ledger after `update_ledger` with one settled quarter (the seeded market, no actions):

```
| quarter | seed | revenue | costs | profit | customers | churn_rate | growth | open_incident_severity | mean_fix_weeks | llm_cost_usd | fitness |
| smoke (no actions) | 7 | 30,412.28 | 5,000.00 | 25,412.28 | 1202 | 0.035 | -0.2024 | 8.00 | 4.00 | 0 | 9.31 |
```

Teardown:

```
python -m company.switch_env teardown company/envs/env1.yaml --gen 95 --agent-prefix m4e- --room-prefix m4e-
  -> archive_room x6; list_rooms(include_archived) shows m4e-{board,dev,maintenance,growth,marketing,finance}-g95 archived
```

Also verified live: an env with a direct link (env2 = env1 + `link maintenance↔dev`) applied through the per-room
`create_room` + `link_rooms` path (7 links), re-applying changes nothing, and applying through a running
SwitchTransport. There, a post in env room `maintenance` reaches @board and the reply comes back in the same thread.
Op gaps in 0.31.0: none. Behaviour that shapes the design is listed in `m4/apply-env/README.md`.
