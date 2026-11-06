# synaps-switch

Put [Synaps](https://github.com/HaseebKhalid1507/SynapsCLI) agents into
[Switch](https://github.com/sandbox-quantum/switch) rooms: Slack, Teams,
Discord, Telegram and Mattermost.

A Synaps **daemon extension** that speaks the Switch
[agent protocol](https://docs.switchagents.ai/flintai/switch/internals/agent-protocol)
directly: one event stream per agent, one long-lived Synaps session per room.
No Switch Console, no adapter in Switch, no changes to Synaps.

Status: **pre-alpha, being built.** See [PLAN.md](PLAN.md).

## How it works

```
Mattermost / Slack / Discord ─▶ Switch ─▶ agent bridge
                                              │  SSE down: room events
                                              │  HTTP up:  POST /ops/{name}
                                              ▼
                          synaps daemon ── synaps-switch extension (one process)
                               │              holds the connection, beats every 2 s,
                               │              maps room ⇄ session, serves switch_* tools
                               ├── session switch:<room A>   (pinned, wakes on a message)
                               └── session switch:<room B>
```

- The extension is daemon-wide (one process for every session), which matches
  Switch's rule of one live connection per agent.
- A message that addresses the agent is delivered to that room's session as an
  event. The session wakes, runs a turn, and answers with `switch_post_message`.
- Room sessions are ordinary Synaps sessions: you can `/sessions` into one and
  watch it work, steer it, or take it over.

## License

MIT
