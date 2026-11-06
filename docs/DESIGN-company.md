# Design: an AI company that redesigns itself (env1 → env_n)

Author: Aditya. Status: proposal for M4 (the workflow). Builds on M0–M3 (#1, #2, #3) and changes nothing in them.
Track: **Best Business Workflow**.

## Update: staffed by The Agency

Employees are hired from [The Agency](https://github.com/msitarzewski/agency-agents) (MIT, 167k stars):
about 270 agent personas in divisions (engineering, marketing, finance, support, testing, product, project management),
plus written runbooks (`strategy/runbooks/`) that say which persona leads which kind of work.
The Agency has no runtime: personas are copied into one coding tool at a time, never work together, and have no humans in the loop.

- **env1 = The Agency's own published runbooks, staffed as written** (incident response, marketing campaign).
- Mutations gain `hire` (a persona from the roster), `swap_persona`, and `fire`.
- The headline result: **an evolved org that beats The Agency's published runbook** on the same seeded quarter.
- `acme-shop` (github.com/adityapat958/acme-shop) is the agency's client: real code, real incidents, real PRs.

Module contracts: `company/INTERFACES.md`. Roster loader: `company/core/roster.py`.

## One paragraph

We run a small mock company inside Switch. Every department is a chat room
(`#dev`, `#maintenance`, `#rnd`, `#marketing`, `#finance`, `#growth`), staffed by AI
agents, with Aditya and Haseeb sitting in `#board` as the humans. A company
design is called an **env**: which agents exist, what each one is told to do,
which rooms talk to which, the budgets, and the approval rules. The company runs
a **quarter** under that env and gets scored (revenue, profit, growth, incidents,
and the real LLM bill). After each quarter the **board agent** studies what
happened, proposes a change to the org design, and the humans approve or reject
it in `#board`. Accepted, the company runs the next quarter as `env2`, and so on.
An env is kept only if it beats its parent on the same simulated market. So
`env_n` is the best org design found so far, and every step that produced it is
a readable decision in a chat channel.

## Why this is a Switch project, not a script

| Company concept | Switch primitive it maps to |
|---|---|
| Department | Room (channel in Mattermost/Slack/Discord) |
| Employee | Agent (Synaps or LangGraph-backed), joined to rooms |
| Org chart / env | A **room template YAML** (`create_room_from_yaml`, `run_template`, `save_template`) |
| Job description | Agent `instructions` + room **roles** (`define_role`, `assume_role`) |
| Reporting lines | **Linked rooms** / room groups (`link_rooms`, `create_room_group`) |
| Company handbook, KPIs, ledger | **Room documents** (`create_room_document`, `update_room_document`) |
| Board decision | A human message in `#board`, recorded in the decision log |

Remove Switch and nothing is left: the rooms, the hand-offs, the approvals and
the record all live there. That covers the "use of Switch" criterion, which is 30% of the score.

## Who is who

```
 humans:  Aditya, Haseeb  ── #board (approve / reject env changes, approve big spend)

 agents:
   board      (LangGraph)  runs the quarter loop and the evolution loop, scores, proposes envs
   synaps-dev (Synaps)     the ONLY agent that touches real code: demo-repo PRs, fixes, tests
   dept agents (LangGraph, one Switch agent each, one Python process):
     ops-maint  #maintenance  triages incidents, asks synaps-dev for fixes
     rnd        #rnd          proposes features from customer signals
     marketing  #marketing    writes campaigns (real copy, scored)
     finance    #finance      budget, ledger, vetoes spend over limit
     growth     #growth       pricing, funnel, picks which feature to push
```

Real work vs simulated world: the work products are real (code in `demo/shop`,
marketing copy, budget docs, incident fixes). Only the **market** is simulated:
customers, demand, churn. That keeps runs reproducible and keeps the stakes honest.

## The two loops (LangGraph)

### Inner loop: one quarter under a fixed env

```
 START
   │
 load_env ── run_template(env_k.yaml): rooms, agents, roles, links, handbook doc
   │
 open_quarter ── market.tick(seed) emits events into rooms:
   │               customer complaints → #maintenance, feature requests → #rnd,
   │               competitor price cut → #growth, invoice due → #finance
   │
 operate ×W weeks ── each week: dept agents act in their rooms and @-address each other;
   │                  synaps-dev does real fixes/features; finance books costs
   │                  (incl. the real token spend of every agent, read from usage)
   │
 close_quarter ── market.settle(shipped features, fixes, campaigns, prices) → metrics
   │
 report ── post the KPI table to #board, write it to the "Ledger" room document
   │
 END
```

### Outer loop: evolve the company

```
 env1 ──run quarter──▶ score1
   │
 reflect ── board reads the quarter's rooms (read_context) + metrics:
   │          "where did value leak?" (slow fix times? spend wasted? wrong feature?)
 propose ── ONE mutation to the env (small, explainable diff of the YAML)
   │
 gate ── interrupt(): post the diff + rationale to #board,
   │       resumed only by a human "approve" / "reject"
 trial ── run env_{k+1} on the SAME market seeds as env_k (fair comparison)
   │
 select ── fitness(env_{k+1}) > fitness(env_k)?  keep : revert (log why)
   │
 loop until budget (generations or $) ──▶ env_n = best so far
```

Fitness (one number, so selection is unambiguous):

```
fitness = profit + 0.5·revenue_growth − 2·open_incidents·severity − llm_cost
```

Weights live in `company/fitness.yaml` and are set by the board, so the humans
decide what "better company" means.

### Mutation types (what can change between envs)

| Mutation | Example |
|---|---|
| Edit instructions | marketing: "every campaign must cite one shipped feature" |
| Add / remove agent | hire `qa` into `#dev`; fire `rnd` and fold it into growth |
| Rewire | link `#maintenance` ↔ `#dev` directly instead of via `#board` |
| Policy | finance veto threshold $5k → $2k; incidents sev1 must be fixed in 1 week |
| Budget | move 20% of marketing budget to maintenance |
| Ritual | weekly growth→rnd "top 3 customer asks" post |

One mutation per generation, so every improvement can be traced to a cause.

## Worked example (what the demo shows)

**env1 (seed design).** Five departments, everyone reports only to `#board`, no direct links.

Quarter 1 in chat, abridged:

```
#maintenance  market      checkout 500s reported by 40 customers (sev1)
#maintenance  ops-maint   @board sev1 checkout outage, need dev time
#board        board       @synaps-dev please take the checkout outage (routed from #maintenance)
#dev          synaps-dev  repro'd: cart total NaN on empty coupon. PR #12, tests green. RESULT {...}
#board        Haseeb      approve merge
#growth       growth      competitor cut price 15%. propose match
#finance      finance     veto: margin goes negative at current costs
#marketing    marketing   campaign "Spring sale" (no shipped feature to point at)
```

Score Q1: revenue $41k, profit −$3k, fix time 3.1 weeks, LLM cost $6.20 → fitness **−11.4**.

**reflect → propose (gen 1).**
`board`: "Fixes waited 2 weeks in #board before reaching dev. Mutation: link
#maintenance ↔ #dev, so ops-maint can address synaps-dev directly for sev1/sev2."
Posted as a YAML diff in `#board`. Aditya: `approve`.

**env2.** Same market seeds. Fix time 3.1 → 0.8 weeks, churn down,
profit +$4k → fitness **+6.9**. Kept.

**gen 2.** `board`: "Marketing spend converts at 0.4%; campaigns never mention
shipped work. Mutation: marketing must cite one feature shipped this quarter."
→ env3 fitness **+9.8**. Kept.

**gen 3.** `board`: "Hire a QA agent." env4 fitness **+8.1** (QA's token cost
outweighed fewer regressions). **Reverted**, logged as "QA not worth it at this
scale". The demo should show this rejection: it proves the selection step is real.

**gen 4.** Finance veto threshold lowered → env5 **+12.3**. env_n = env5.

| env | change | profit | growth | fix time | LLM $ | fitness | kept |
|---|---|---|---|---|---|---|---|
| 1 | seed | −3k | — | 3.1 wk | 6.2 | −11.4 | — |
| 2 | link maint↔dev | +4k | +6% | 0.8 wk | 6.0 | 6.9 | ✅ |
| 3 | marketing cites shipped work | +6k | +11% | 0.8 wk | 6.4 | 9.8 | ✅ |
| 4 | hire QA | +6k | +11% | 0.6 wk | 9.9 | 8.1 | ❌ |
| 5 | finance veto $2k | +8k | +12% | 0.8 wk | 6.3 | 12.3 | ✅ |

(Numbers are illustrative; the real ones come from the run.)

## Files

```
company/
  envs/env1.yaml          seed org (a Switch room template + our extras block)
  envs/env_k.yaml         written by the board on every accepted mutation (git-tracked lineage)
  fitness.yaml            weights, set by humans
  market/sim.py           seeded customer/demand/churn model, pure python, unit-tested
  board/graph.py          outer + inner LangGraph loops, SqliteSaver checkpoints
  board/router.py         Switch frames → the right paused run (who may resume what)
  depts/agents.py         dept agents: one switch_client connection each, one process
  depts/prompts/*.md      job descriptions (also the mutation target)
  lineage.jsonl           env_k → env_{k+1}: diff, rationale, approver, fitness, kept
demo/shop/                the toy product synaps-dev works on (planted bugs)
```

`env1.yaml` sketch (Switch template fields plus an `x-company` block Switch ignores):

```yaml
agents:
  - name: ops-maint
    addressing: anyone
    provider: langgraph
    instructions: |
      You run maintenance. Triage every incident: severity, owner, ETA.
      Escalate sev1/sev2 to @board.
  # ... rnd, marketing, finance, growth, board
rooms:
  - { name: maintenance, agents: [ops-maint],  users: ["{$creator}"] }
  - { name: dev,         agents: [synaps-dev], users: ["{$creator}"] }
  - { name: board,       agents: [board],      users: ["{$creator}"] }
x-company:
  links: []                       # env2 adds [maintenance, dev]
  budgets: { marketing: 10000, maintenance: 4000, rnd: 6000 }
  policies: { finance_veto_usd: 5000 }
  weeks_per_quarter: 4
```

## What this needs from the Synaps side (Haseeb)

Nothing beyond M1–M3, plus three small asks:

1. `switch_client.py` usable as a library (`on_message` callback). The board and the dept
   agents each import it (#1).
2. `synaps-dev` ends task replies with `@<asker>` and a fenced JSON RESULT block:
   `{"status":"proposed|blocked|done","summary":"…","evidence":["…"],"pr":"url|null"}` (#2 skill).
3. Agents talking to each other must not loop: every agent acts only on frames that address it,
   and the board's router only resumes a paused run from the expected sender (#3).

The extension stays stdlib-only. LangGraph lives in `company/` with its own
`requirements.txt`.

## Scope against the deadline (Sat 13:29 EST)

**MVP (must ship):** 4 rooms (`#board`, `#dev`, `#maintenance`, `#growth`), the board plus
two dept agents plus synaps-dev, a 3-week quarter, 3 generations with at least one rejection,
the lineage table in the README.

**Stretch:** all six departments, finance ledger as a room document, Slack bridge
for the video.

**Cost guard:** dept agents use a small model, synaps-dev uses Sonnet with a session cap,
each quarter takes about 5 minutes of real time, and the market runs off seeds so trials are cheap
to rerun.

## Risks

- **"It's a toy sim."** Answer: the work is real (PRs, fixes, copy) and only the
  market is modeled. Show a real merged PR in the video.
- **Noise.** One quarter on one seed can fool selection. Compare envs on the same
  seeds (common random numbers), 2–3 seeds if time allows.
- **Agent chatter loops / cost blowup.** The router's resume rules handle loops, and a
  per-quarter token budget hard-stops a runaway quarter.
