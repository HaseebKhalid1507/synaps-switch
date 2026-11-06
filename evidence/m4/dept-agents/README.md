# Evidence: department agents on FakeTransport with real Haiku calls (#9)

```
export COMPANY_SYNAPS_BASE_DIR=~/.synaps-switch-dev/company-llm
export COMPANY_BROKER_TOKEN_FILE=~/.synaps-switch-dev/broker/machine.token   # path only; never printed
export COMPANY_BROKER_URL=http://127.0.0.1:18191
python -m company.depts.demo --out evidence/m4/dept-agents
```

env1 (no links, so departments reach each other through #board), FakeTransport over
`transport_rooms(env1)`, every LLM call one `company.llm.complete_json` (Haiku 4.5) via the private broker.
Files: `summary.md` (chat, parsed ACTION blocks, QuarterLog, decisions, usage), `transcript.jsonl`
(every post with its recipients), `quarterlog.json` (QuarterLog + Market.settle metrics + usage),
`judge-cache.json` (the campaign judge's cache, by text hash), `run.log`.

## What the run shows (final run, 33 s, $0.0504, 0 transport errors)

| Scene | Result |
|---|---|
| Competitor cuts price 18% in #growth | growth posts a **price ACTION** (`{"type":"price","value":29.0}`, a hold) to #board, finance **approves** in-thread (policy: $0 revenue at stake); `price_changes=[{week 2, $29}]` |
| Haseeb: "$1,500 campaign for One-click reorder" | marketing posts a **campaign ACTION**, booked at once (within veto + budget), judged **quality 0.7** (honesty 0: the copy says the feature "ships this week", it shipped in week 1); `cites_feature` verified from the copy |
| ...marketing also asks @finance to "book the spend" | finance (Haiku) raised its own spend_request anyway; the runtime **ignored** it, so nothing was booked twice |
| Aditya: "$7,000 campaign" | held, cc @finance; finance **escalates** to @Aditya @Haseeb in #board (mentions no agent; over the $5,000 veto); Aditya replies `@finance reject` in the thread; spend booked `approved: false`, no campaign |
| sev1 incident to ops-maint (budget 200 tokens) | **budget refusal**: ops-maint says so in-thread, no LLM call, no @-mention |
| synaps-dev (simulated agent) pings growth 4 times in one thread | growth answers 3 (two **route ACTIONs**: #3 and #23 to synaps-dev, now in `Departments.routes`); the 4th is stopped by the **loop guard** (`hops: 3`) |
| synaps-dev posts "growth: FYI ..." without an @-mention | delivered to nobody (`to: []`) |

## Spend

Five live runs while tuning the prompts: $0.0378 + $0.0281 + $0.0408 + $0.0474 + $0.0504 = **$0.2045** total,
about $0.004 per call (the persona system prompt is ~1,500 tokens). What the earlier runs taught, now fixed:

- Haiku-growth asked finance for "elasticity data" instead of pricing: the protocol now says decide now with the facts
  given, and `event_message` adds what each market event asks for (a competitor cut asks for a price ACTION).
- growth routed incident #3 to ops-maint: the route ACTION now says "the agent who will fix or build it".
- finance re-booked an auto-booked campaign's spend: finance's own price/spend ACTIONs are ignored (logged), and its
  prompt carries the ledger.
- the escalation repeated "@Aditya @Haseeb": leading human mentions in finance's text are dropped before the prefix.
