# Evidence: the board's outer loop (#11) — reflect → propose → human gate → trial → keep/revert

Code: `company/board/evolve.py` (the LangGraph outer loop), `company/board/stub_quarter.py` (deterministic offline
quarter), `company/lineage_report.py` (the lineage table). Tests: `company/tests/test_evolve.py`.

## 1. Offline, 3 generations (`offline/`)

```
python -m company.board.evolve --env company/envs/env1.yaml --generations 3 --offline --out evidence/m4/evolve/offline
python -m company.lineage_report evidence/m4/evolve/offline/lineage.jsonl
```

Stub quarter (`stub_quarter.run_quarter`, settled by the real `Market.settle` + `fitness`), stub board proposer
(rules over the reflection summary), scripted approver (gen 1: an agent's "approve", ignored, then Haseeb's
"approve"; gen 2: approve; gen 3: reject with a reason). Seeds 1 and 2, common random numbers.
Files: `lineage.jsonl` (one record per generation), `lineage.md` (below), `envs/env1.yaml` + `envs/env2.yaml`
(kept envs only), `run.log`.

### Lineage `evolve:env1:1-2` (seeds [1, 2], mean over seeds)

| gen | env | parent | change | decided by | profit | growth | fix time | LLM $ | fitness | Δ parent | result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | env1 | — | seed | — | $17.0k | -15.1% | 3.08 wk | $2.23 | 11.68 | — | seed |
| 1 | env2 | env1 | link #maintenance ↔ #dev | Haseeb | $17.9k | -11.2% | 2.38 wk | $2.15 | 15.68 | +4.00 | ✅ kept |
| 2 | env3 | env2 | hire qa (testing/testing-test-automation-engineer) into #dev | Haseeb | $17.7k | -12.4% | 2.62 wk | $2.32 | 15.33 | -0.35 | ↩️ reverted (fitness) |
| 3 | env4 | env2 | policy finance_veto_usd: 5000 → 2000 | Haseeb | — | — | — | — | — | — | ❌ rejected (human) |

Best: **env2**, fitness 15.68. Spend: $13.42 *simulated* (the stub quarter prices its imaginary agent calls; no
real LLM call was made).

Why each row came out the way it did (from the QuarterLogs):
- gen 1 kept: with #maintenance ↔ #dev linked, incidents reach synaps-dev the week they are filed instead of a week
  later via #board: mean fix time 3.08 → 2.38 weeks, no incident open at quarter end (severity 1.5 → 0),
  churn 1.87% → 1.50%, fewer #board hops (LLM $2.24 → $2.16).
- gen 2 reverted: env1 already has a reviewer (testing persona) in #dev. A second tester catches more regressions,
  but every merge gets an extra review round (50% slip a week). Seed 1: +0.26 (a reopened #7 avoided). Seed 2:
  −0.97 (five merges slipped, fix time 2.25 → 3.0 weeks). Mean −0.35 on the same seeds → reverted, env2 stays.
  The kind of mixed result two seeds exist for.
- gen 3 rejected by Haseeb: "that puts every weekly campaign on our desk in #board". Not trialed; no fitness.
  Rejected and reverted are separate outcomes in the lineage (`outcome`, `reason`, `approver`, `decision`).

Deterministic: two runs into fresh dirs give identical `lineage.jsonl` (minus `ts`), `lineage.md` and `envs/`
(also `test_offline_run_is_deterministic`).

## 2. Live gate on Switch (`live_gate.py`, `live.log`, `mattermost-m4v-board.json`, `transcript.jsonl`)

```
export COMPANY_SYNAPS_BASE_DIR=~/.synaps-switch-dev/company-llm
export COMPANY_BROKER_TOKEN_FILE=~/.synaps-switch-dev/broker/machine.token   # path only; never printed
export COMPANY_BROKER_URL=http://127.0.0.1:18191
python evidence/m4/evolve/live_gate.py all
```

Switch 127.0.0.1:18400 + Mattermost 127.0.0.1:18465 (shared devstack, untouched apart from what this created):
agents `m4v-board`, `m4v-peer`, room `m4v-board` (Mattermost channel, members admin, m4v-board, m4v-peer, user).
One generation of env1 with the stub quarter; the proposal is real: the board persona (env1's `board` agent, Agency
`project-management/project-manager-senior`) on Haiku via `company.llm.complete_json`, with `read_context` of the
board room in the reflection.

| Step | What happened |
|---|---|
| propose | Haiku proposed `{"op":"link","rooms":["maintenance","dev"]}` with a diagnosis citing the 1-week #board routing delay and 3.08 wk fix time ($0.0102). Valid on the first try. |
| gate post | `m4v-board` posted the proposal in #m4v-board: diagnosis, rationale, parent fitness, the full `env_diff` in a ```diff block, the approve/reject instructions. |
| agent approve | `m4v-peer` (an agent) replied `@m4v-board approve` in the thread. Delivered with `sender_is_agent=True` → **ignored** ("sender is an agent"), the graph paused at the gate again. |
| kill | process 1 exited while paused (exit 3). Checkpoint (SqliteSaver, thread `evolve:env1:1-2:m4v`): `next=['await_gate']`, interrupt `evolve_gate`, the ignored message recorded. |
| resume | process 2 resumed the same thread at `await_gate`. Nothing re-posted (1 gate post in the channel). |
| human approve | `user` posted `@m4v-board approve` in the Mattermost thread (`dev.py mm-post … --as user`). Delivered with `sender_is_agent=False`, `user` on the approvers list → approve. |
| trial + select | env2 on seeds 1, 2: 15.68 > 11.68 → kept. |
| outcome | `m4v-board` replied in the thread: "Kept env2: fitness 15.68 > 11.68 (env1) on seeds [1, 2]." |

`mattermost-m4v-board.json` is the channel as Mattermost has it (4 posts: the gate, the agent's approve, the
person's approve, the outcome). `lineage.jsonl` / `lineage.md` / `envs/` are this live run's lineage.
All files passed through `devstack/dev.py scrub`; a scan for every known credential value (agent API keys, broker
token, registration token, devstack secrets) and for bearer/authorization/cookie/password patterns found nothing.

## Spend

Haiku via `company/llm.py`: one prompt-check call ($0.0103) + the live proposal ($0.0102) = **$0.0205**.
Everything else is offline (stub quarter, stub proposer) or simulated $.
