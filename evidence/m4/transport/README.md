# M4 #7 live check: SwitchTransport on the local Switch stack

Stack: Switch 0.31.0 (docker project `synaps-switch-dev`, bridge on 127.0.0.1:18400, Mattermost on
127.0.0.1:18465; container ports 8000/8065). Reached over HTTP only.
Created for this test, nothing else touched: agents `m4t-alpha`, `m4t-beta`, room `m4t-room` (Mattermost-bridged).
Credentials are in `company/state/agents/*.json` (0600, gitignored). No secret appears in these files.

Run: `python evidence/m4/transport/live_transport.py` from the repo root.

| file | what |
|---|---|
| `live.log` | the run: every Msg each agent received, then PASS/FAIL per check |
| `transcript.jsonl` | SwitchTransport's own transcript (posts and deliveries) |
| `mattermost-thread.json` | the same conversation as people see it in Mattermost (`root_id` = threading) |
| `live_transport.py` | the script |

Result: 6/6 PASS. The checks are agent to agent @-mention delivery, a threaded reply in the root's thread,
a human message flagged as human (top level and in the thread), no redelivery after a restart, and a message
sent while the transport was down delivered once on restart.

## What Switch 0.31.0 actually sends (probe, trimmed)

```
agent  → payload.sender = "@switch-agent-5bb07abc:localhost"                                    sender_kind = null  sender_role = null
person → payload.sender = "@switch-mattermost-83e18cb6-16e7-413d-94f7-c8eab53a0583-user:localhost" sender_kind = null  sender_role = "owner"
```

`sender_kind` is null for both. The person's id does not have the `switch-user-*` shape the protocol doc
describes, which is the doc's own warning ("treat the whole id as opaque") playing out. So `sender_is_agent`
is decided by matching ids rather than parsing them: ids of messages this transport posted, agent ids learned
that way or from the room's participant list (`connect_to_room` → `participants[].type == "agent"`), and
`sender_role` (set only for a person) as a positive human signal. If the lookup fails, the sender counts as
an agent (fail closed).
