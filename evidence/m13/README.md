# M4 #13: the live run. env1 baseline vs the evolved org, real PRs, a human gate

Recorded 2026-11-06 on the devstack: Switch 0.31.0 on 127.0.0.1:18400, Mattermost on 127.0.0.1:18465, the private
Synaps daemon (`~/.synaps-switch-dev`) running the extension from `feat/company`, `--transport switch`. The company
process ran from this branch (`m4/13-live-run`, commit `450cd92` for run 1, `1e4f47b` for run 2).

**Approvals in this recorded run were scripted; in the demo video they're typed live.** Every approve and reject
was posted as the devstack's Mattermost user `user`, a real human account, by a script following a policy fixed
and committed before the run ([POLICY.md](POLICY.md), commit `450cd92` at 04:04:34; run 1 started at 04:05:02):

- **Merges:** approve iff the reality-checker review is PASS and the harness pytest on the PR head is green
  (`company.board.live.MattermostApprover`, `--approver mattermost`).
- **Org changes:** approve generations 1 and 2, reject generation 3 ("change freeze: no more org changes before
  the demo; propose it next quarter"). The script ([gate_person.py](gate_person.py)) never reads the proposal.

## Headline

| | env1 (baseline) | best env | |
|---|---|---|---|
| run 1, seed 42 | fitness **13.62** | env3: **16.02** | gen 1 kept (+2.33), gen 2 kept (+0.06), **gen 3 rejected by a person** |
| run 2, seed 1 | fitness **12.85** | env3: **15.12** | gen 1 kept (+1.35), gen 2 kept (+0.93), stopped by its $6 budget guard before gen 3 |

- **12 real PRs** on [HaseebKhalid1507/acme-shop](https://github.com/HaseebKhalid1507/acme-shop/pulls?q=is%3Apr+m13-),
  [#10](https://github.com/HaseebKhalid1507/acme-shop/pull/10) to [#21](https://github.com/HaseebKhalid1507/acme-shop/pull/21),
  every one written by synaps-dev, reviewed PASS, approved in #board by `user` and squash-merged into its
  generation's branch. `main` and the `baseline` tag are untouched ([fork-refs.txt](fork-refs.txt)).
- **The org change that mattered:** `link #maintenance ↔ #dev`, proposed by the board (Haiku) in both runs. Live,
  it took incident fix time from **2.50 to 1.50 weeks**: ops-maint handed the sev1/sev2 incidents to synaps-dev in
  `#dev` the week they came in, instead of waiting a week for the board's relay. Profit +$2.2k, growth +2.3 pts,
  on the same market seed.
- **Cost: $7.21 of model spend for both runs** (run 1 $2.97, run 2 $4.23), 93% of it synaps-dev (Sonnet).
  **Wall time: 47 min 41 s** (21:54 + 25:47).
- **≥1 human reject: yes** (run 1, gen 3). **≥1 fitness revert: no, in neither run.** Why, below. Nothing was
  staged to produce one.

## Lineage

`python -m company.lineage_report evidence/m13/<run>/lineage.jsonl` prints these.

Run 1, seed 42 ([run1-seed42/lineage.md](run1-seed42/lineage.md)):

| gen | env | parent | change | decided by | profit | growth | fix time | LLM $ | fitness | Δ parent | result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | env1 | — | seed | — | $20.8k | -11.2% | 2.50 wk | $1.09 | 13.62 | — | seed |
| 1 | env2 | env1 | link #maintenance ↔ #dev | user | $23.0k | -8.8% | 1.50 wk | $0.96 | 15.96 | +2.33 | ✅ kept |
| 2 | env3 | env2 | hire incident-commander (engineering/engineering-incident-response-commander) into #dev | user | $23.0k | -8.8% | 1.50 wk | $0.90 | 16.02 | +0.06 | ✅ kept |
| 3 | env4 | env3 | policy dev_capacity: — → 3 | user | — | — | — | — | — | — | ❌ rejected (human) |

Run 2, seed 1 ([run2-seed1/lineage.md](run2-seed1/lineage.md)):

| gen | env | parent | change | decided by | profit | growth | fix time | LLM $ | fitness | Δ parent | result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | env1 | — | seed | — | $20.1k | -11.2% | 2.50 wk | $1.18 | 12.85 | — | seed |
| 1 | env2 | env1 | link #maintenance ↔ #dev | user | $22.2k | -8.9% | 1.50 wk | $1.98 | 14.19 | +1.35 | ✅ kept |
| 2 | env3 | env2 | policy dev_capacity_per_quarter: — → 4 | user | $22.2k | -8.9% | 1.50 wk | $1.05 | 15.12 | +0.93 | ✅ kept |

Run 2's log ends: `stop: budget: spent $4.2325, the next generation (~$1.9889) would pass $6.00`.

## KPIs: env1 vs best env, same seed (the quarters' own KPI posts in #board)

| KPI | run 1 env1 | run 1 env2 | run 1 **env3 (best)** | run 2 env1 | run 2 **env3 (best)** |
|---|---|---|---|---|---|
| revenue | $25,766 | $27,958 | $27,958 | $25,081 | $27,215 |
| profit | $20,765 | $22,957 | $22,958 | $20,080 | $22,214 |
| customers | 1,370 | 1,406 | 1,406 | 1,333 | 1,368 |
| churn (avg weekly) | 1.95% | 1.95% | 1.95% | 1.95% | 1.95% |
| growth | -11.15% | -8.82% | -8.82% | -11.19% | -8.86% |
| open incident severity | 3 | 3 | 3 | 3 | 3 |
| mean fix weeks | 2.50 | 1.50 | 1.50 | 2.50 | 1.50 |
| incidents fixed (week) | #1 (2), #2 (3) | #1 (1), #2 (2) | #1 (1), #2 (2) | #1 (2), #2 (3) | #1 (1), #2 (2) |
| LLM cost | $1.0855 | $0.9563 | $0.8971 | $1.1785 | $1.0517 |
| **fitness** | **13.6241** | **15.9571** | **16.0163** | **12.8457** | **15.1180** |

Full tables: `run*/kpi-env<k>-g<k>.md`. Every quarter: 3 weeks, synaps-dev capacity 1/week and 2/quarter, so
#3 (sev2) and #4 (sev3) are never dispatched and open severity stays 3. No features, no campaigns.

## The PRs (all merged, all on the fork)

| run | gen (env) | issue | how it reached synaps-dev | dispatched → merged | synaps-dev | review + harness pytest | PR (squash-merged after `user` approved) |
|---|---|---|---|---|---|---|---|
| 1 (seed 42) | 1 (env1) | #1 | board relay | wk 2 → wk 2 | 116 s | PASS, 35 passed | [acme-shop#10](https://github.com/HaseebKhalid1507/acme-shop/pull/10) `m13-gen-1-fix-1` → `m13-gen-1` |
| 1 (seed 42) | 1 (env1) | #2 | board relay | wk 3 → wk 3 | 155 s | PASS, 42 passed | [acme-shop#11](https://github.com/HaseebKhalid1507/acme-shop/pull/11) `m13-gen-1-fix-2` → `m13-gen-1` |
| 1 (seed 42) | 2 (env2) | #1 | direct line, ops-maint | wk 1 → wk 1 | 109 s | PASS, 37 passed | [acme-shop#12](https://github.com/HaseebKhalid1507/acme-shop/pull/12) `m13-gen-2-fix-1` → `m13-gen-2` |
| 1 (seed 42) | 2 (env2) | #2 | direct line, ops-maint | wk 2 → wk 2 | 124 s | PASS, 43 passed | [acme-shop#13](https://github.com/HaseebKhalid1507/acme-shop/pull/13) `m13-gen-2-fix-2` → `m13-gen-2` |
| 1 (seed 42) | 3 (env3) | #1 | direct line, ops-maint | wk 1 → wk 1 | 100 s | PASS, 36 passed | [acme-shop#14](https://github.com/HaseebKhalid1507/acme-shop/pull/14) `m13-gen-3-fix-1` → `m13-gen-3` |
| 1 (seed 42) | 3 (env3) | #2 | direct line, ops-maint | wk 2 → wk 2 | 148 s | PASS, 42 passed | [acme-shop#15](https://github.com/HaseebKhalid1507/acme-shop/pull/15) `m13-gen-3-fix-2` → `m13-gen-3` |
| 2 (seed 1) | 1 (env1) | #1 | board relay | wk 2 → wk 2 | 96 s | PASS, 36 passed | [acme-shop#16](https://github.com/HaseebKhalid1507/acme-shop/pull/16) `m13-s1-gen-1-fix-1` → `m13-s1-gen-1` |
| 2 (seed 1) | 1 (env1) | #2 | board relay | wk 3 → wk 3 | 191 s | PASS, 44 passed | [acme-shop#17](https://github.com/HaseebKhalid1507/acme-shop/pull/17) `m13-s1-gen-1-fix-2` → `m13-s1-gen-1` |
| 2 (seed 1) | 2 (env2) | #1 | direct line, ops-maint | wk 1 → wk 1 | 105 s | PASS, 35 passed | [acme-shop#18](https://github.com/HaseebKhalid1507/acme-shop/pull/18) `m13-s1-gen-2-fix-1` → `m13-s1-gen-2` |
| 2 (seed 1) | 2 (env2) | #2 | direct line, ops-maint | wk 2 → wk 2 | 370 s | PASS, 46 passed | [acme-shop#19](https://github.com/HaseebKhalid1507/acme-shop/pull/19) `m13-s1-gen-2-fix-2` → `m13-s1-gen-2` |
| 2 (seed 1) | 3 (env3) | #1 | direct line, ops-maint | wk 1 → wk 1 | 57 s | PASS, 34 passed | [acme-shop#20](https://github.com/HaseebKhalid1507/acme-shop/pull/20) `m13-s1-gen-3-fix-1` → `m13-s1-gen-3` |
| 2 (seed 1) | 3 (env3) | #2 | direct line, ops-maint | wk 2 → wk 2 | 253 s | PASS, 42 passed | [acme-shop#21](https://github.com/HaseebKhalid1507/acme-shop/pull/21) `m13-s1-gen-3-fix-2` → `m13-s1-gen-3` |

Each generation's branch is cut fresh from the `baseline` tag, so every generation faces the same bugs; that is
why #1 and #2 are fixed again in each one. synaps-dev pushed each fix branch with the fork's deploy key from a
session with no GitHub login; the company process opened, reviewed and merged the PR with the operator's `gh`
(`gh pr merge --squash --match-head-commit <reviewed sha>`), as in [#26](../m4/github/README.md). PR metadata:
`run*/prs.json`.

## The chats (Mattermost exports, `devstack/dev.py mm-thread`)

Readable: [run1-seed42/threads.md](run1-seed42/threads.md) (and [run 2's](run2-seed1/threads.md)). Raw:
`run*/mattermost/<room>-g<k>.json`, every post in all 18 channels of each run.

- **env1, the board relay** ([threads.md § Generation 1](run1-seed42/threads.md#generation-1-incident-acme-shop1-end-to-end)):
  the market posts *"90 customers report: Applying empty coupon code crashes checkout … (sev1, issue #1)"* in
  `#maintenance-g1` (04:05:21) → ops-maint triages it and routes it to synaps-dev; with no link to `#dev` the route
  lands in `#board-g1` → at the next weekly sync the board posts *"Relaying acme-shop#1 … to synaps-dev in #dev"*
  and dispatches it in `#dev-g1` (04:06:28, week 2) → synaps-dev: *"On it"*, then the fix summary and RESULT
  (04:08:24) → the reviewer: PASS, harness pytest green, PR #10 → `#board-g1`: *"@user merge request: acme-shop#1"*
  → `user`: *"@m13-board approve"* (04:08:45) → board: *"Merged acme-shop#1 (approved by user) as c6998b2e93.
  pytest on the merged base: green (35 passed …). Booked as fixed in week 2."*
- **env2, the direct line** ([threads.md § Generation 2](run1-seed42/threads.md#generation-2-incident-acme-shop1-end-to-end)):
  same market post in `#maintenance-g2` (04:13:04) → ops-maint triages it there → nothing goes through `#board`:
  ops-maint posts the task in `#dev-g2` itself (04:13:26, week 1) → synaps-dev's RESULT (04:15:16) → review PASS,
  PR #12 → `user` approves in `#board-g2` (04:15:33) → merged as 61c58c2ae7, *"Booked as fixed in week 1."*
- **The KPI post**: each quarter's last post in its `#board-g<k>` (in threads.md, and `kpi-*.md`), plus the Ledger
  room document update.
- **The board's gate** ([threads.md § gate](run1-seed42/threads.md#the-boards-org-change-gate-board-g1-reflect--proposal--decision--outcome)),
  all in `#board-g1`: *"Generation 1: the board proposes env1 → env2. Change: link #maintenance ↔ #dev"* with the
  diagnosis from its reflection on the quarter, the rationale and the YAML diff → `user`: approve → *"Kept env2:
  fitness 15.96 > 13.62 (env1) on seeds [42]."* Generation 2 (hire incident-commander) → approve → *"Kept env3:
  fitness 16.02 > 15.96"*. Generation 3 (`policy dev_capacity → 3`) → `user`: *"@m13-board reject change freeze: no
  more org changes before the demo; propose it next quarter"* → *"Not trialed (rejected) … env3 stays."*

## Why there is no fitness revert

The quarter is deterministic for a given seed: the market, the mechanics (links, capacity, QA) and the merge
outcomes. The one term that varies between two runs of the same org is the real LLM bill, and fitness subtracts
it at weight 1. The keep rule is strict `>` with no noise margin (`evolve.py`: `kept = ev["fitness"] >
parent["fitness"]`). So a change that does nothing mechanically is kept iff its trial quarter happened to cost less.

Both generation-2 proposals did nothing mechanically:

- Run 1, *hire incident-commander into #dev*: it is not a tester (the QA mechanic counts testers), and it posted
  only its greeting. Same fixes, same weeks; Δ +0.06 is synaps-dev costing $0.06 less ($0.887 → $0.816).
- Run 2, *policy dev_capacity_per_quarter → 4*: capacity is read from `x-company.dev_capacity.per_week/per_quarter`
  (and the run's `--dev-per-quarter 2` takes precedence over the env anyway), not from `policies`. The validator
  accepted it as a free-form policy. Δ +0.93 is env2's trial being expensive ($1.98: synaps-dev spent 370 s and
  ~$1.5 of session cost re-deriving the double-tax bug before it fixed it) and env3's normal ($1.05).

Run 1's generation 3 (`policy dev_capacity → 3`) was also a no-op knob; it was rejected by the policy, not trialed.
The offline prediction on the real quarter runner reverts *hire QA into #dev* (−0.70: a second tester slips merges
a week), but the live board never proposed QA. Two follow-ups, not changed here because they would change the
recorded run's rules after the fact: reject `set_policy` keys the mechanics don't read, and keep a change only if
it beats the parent by more than the LLM noise (or ties go to the parent).

## Cost and wall time

| | synaps-dev (Sonnet) | reviews (Haiku) | departments + judge (Haiku) | board proposals (Haiku) | total | wall |
|---|---|---|---|---|---|---|
| run 1, seed 42 (3 quarters, 3 proposals) | $2.7151 | $0.0736 | $0.1502 | $0.0351 | **$2.9740** | 21 min 54 s (04:05:02 → 04:26:56) |
| run 2, seed 1 (3 quarters, 2 proposals) | $3.9721 | $0.0739 | $0.1622 | $0.0243 | **$4.2325** | 25 min 47 s (04:28:34 → 04:54:21) |
| **both** | $6.6872 | $0.1475 | $0.3124 | $0.0594 | **$7.2065** | **47 min 41 s** |

Per quarter, per synaps-dev room session (`run*/cost.json`): run 1 $1.012 / $0.887 / $0.816, run 2 $1.098 / $1.893 /
$0.980, under the $3.00 cap each time. One quarter: 6 to 10 minutes, of which synaps-dev is 1 to 6 minutes per fix.
The total is `QuarterLog.llm_cost_usd` over every quarter plus the proposal calls; the reviews' harness pytest and
the Mattermost/Switch traffic are free.

## What misbehaved live (nothing needed a resume)

- No quarter failed, no Mattermost or Switch error, no timeout; the checkpoints were never needed.
- Switch posts *"⚠️ … synaps-dev isn't in this room"* whenever a department @mentions synaps-dev outside `#dev`
  (ops-maint and growth do it in `#maintenance`, `#growth`, `#board`; the board's gen-2 proposal text did too). By
  design: synaps-dev is only in `#dev`. They are in the JSON exports; threads.md leaves them out of the chains.
- Run 2, env2, #2: synaps-dev took 370 s and about $1.5 for one fix (it re-derived the double-tax bug at length).
  Reviewed PASS and merged; it made that quarter the most expensive ($1.98), which is what made run 2's gen 2 look
  like a +0.93 improvement.
- The board proposed no-op knobs (above), and gen 2's `hire` was accepted, trialed and kept without changing any
  outcome.
- The gate script's first poll on run 2 got a 404 (the channel didn't exist yet); it retried.
- My first launch command held the tool shell open; the run itself was unaffected, and the gate script was
  started separately at 04:06, before the first proposal (04:12:27).

## Exact commands

```bash
PY=~/.local/share/synaps-switch/company-venv/bin/python
# run 1 (seed 42, prefix m13-); run 2 is the same with --seeds 1, prefix m13-s1-, --budget-usd 6, db m13-s1
$PY evidence/m13/gate_person.py --channel m13-board-g1 --board m13-board --since-ms <start ms> \
    --log company/state/m13-out/gate-person.jsonl &
tools/live_env.sh $PY -m company.run --env company/envs/env1.yaml --generations 3 --seeds 42 \
  --transport switch --weeks 3 --dev-per-week 1 --dev-per-quarter 2 \
  --agent-prefix m13- --room-prefix m13- --acme-prefix m13- \
  --switch-url http://127.0.0.1:18400 --token-file ~/Projects/synaps-switch/devstack/state/registration.token \
  --issues acme --acme-remote git@github.com:HaseebKhalid1507/acme-shop.git \
  --humans user --approvers user --approver mattermost --approve-timeout 600 \
  --gate-room m13-board-g1 --gate-timeout 1800 --budget-usd 9 \
  --week-wall 90 --quiet 15 --db company/state/m13.db --evolve-db company/state/m13-evolve.sqlite \
  --out company/state/m13-out -v
# evidence (scrubbed: devstack secrets, every agent key, the broker token, $HOME -> ~)
$PY evidence/m13/export.py --raw company/state/m13-out --db company/state/m13.db --prefix m13-gen --out evidence/m13/run1-seed42
python3 evidence/m13/threads.py evidence/m13/run1-seed42 --issue 1 --gens 1,2
```

`tools/live_env.sh` loads the live settings at run time from `~/.synaps-switch-dev`: the broker
(`COMPANY_BROKER_URL`, `COMPANY_BROKER_TOKEN_FILE`, `COMPANY_SYNAPS_BASE_DIR`) and `acme-github.env` (`ACME_REPO`,
`ACME_REMOTE`, `ACME_ALLOW_GITHUB_WRITES`, `ACME_DEPLOY_KEY`, `ACME_ISSUE_AUTHORS`; names and paths only). Nothing
is printed or committed. With those set, `apply_env` cuts `m13-gen-<k>` from `baseline` on the fork (before #13 it
refused any non-`file://` remote; now it allows GitHub when `ACME_ALLOW_GITHUB_WRITES` is set, tested).

## Files

| path | what |
|---|---|
| `POLICY.md`, `gate_person.py` | the approval policy and the scripted person (committed before the run) |
| `run*/console.log` | the full verbose log of the run (both loops, every week, every dispatch and merge) |
| `run2-seed1/run.log`, `run2-seed1/transcript.jsonl` | the quarters' own log and every Switch post (run 1 predates `--generations` passing `--out` to the quarters; its console.log has the same lines) |
| `run*/lineage.jsonl`, `lineage.md` | the lineage records and table |
| `run*/quarters.json` | every quarter's final state: Metrics, fitness, setup (rooms, branches), dev tasks with review and approver, mechanics counters, cost |
| `run*/kpi-env<k>-g<k>.md` | the KPI post of each quarter |
| `run*/envs/env<k>.yaml` | the envs the run produced; run 1's env2 and env3 are also `company/envs/env2.yaml`, `env3.yaml` |
| `run*/mattermost/*.json`, `run*/threads.md` | the Mattermost exports, and the chains rendered |
| `run*/prs.json`, `fork-refs.txt` | the fork's PRs into the run's branches (gh), and `git ls-remote` after both runs |
| `run*/cost.json`, `run*/gate-person.jsonl` | cost and wall time; every gate decision the script posted |
| `export.py`, `threads.py` | how the evidence was collected |
