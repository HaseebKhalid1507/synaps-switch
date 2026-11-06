# M4 #10: one live quarter of acme-shop on Switch

`python evidence/m4/quarter/live_quarter.py --generation 1 --weeks 3` (run 2026-11-06 02:36–02:55, 1112 s wall).
env1, seed 42, **3 weeks**, generation 1 (fresh: rooms `m10-<room>-g1`, group `m10-env1-g1`, acme-shop branch
`m10-gen-1` cut from `baseline` on the **local** remote). Real dept agents (Haiku via `company/llm.py`), the market
as its own `m10-market` agent, the extension's `synaps-dev` (Sonnet, its room session in `#m10-dev-g1`), the
testing-reality-checker review on Haiku, synaps-dev capacity 1/week, 2/quarter.

**The #board approval was a SCRIPTED human.** `company/board/live.MattermostApprover` posted `@m10-board approve`
in the merge-request thread as the devstack's seeded Mattermost user `user` (`dev.py mm-post … --as user`), by a
fixed rule: approve iff the review is PASS and the harness pytest on the PR head is green. The board never approves
anything itself; the MergeGate only accepted it because it came from a person (`sender_is_agent=False`) on the
approvers list.

## KPI (posted to #m10-board-g1 and written to the Ledger doc via `update_ledger`)

| KPI | value |
|---|---|
| revenue | $30,295 |
| costs | $5,001 |
| profit | $25,294 |
| customers | 1,322 |
| churn (avg weekly) | 3.25% |
| growth | -14.27% |
| open incident severity | 7 |
| mean fix weeks | 1.00 |
| LLM cost | $1.0766 (depts + judge $0.0513, reviews $0.0114, synaps-dev $1.0139) |
| incidents fixed | #1 (wk 1, tests green) |
| features shipped | none |
| campaigns | 0 ($0, avg quality 0.00) |
| price | unchanged |
| spend approved / not | $0 / $0 |
| not approved at quarter end | 2 |
| **fitness** | **10.1460** |

## The chain (Mattermost, `mattermost-*.json`)

1. **market → dept** `#m10-maintenance-g1`, m10-market: `@m10-ops-maint 30 customers report: Applying empty coupon
   code crashes checkout with unhandled exception (sev1, issue #1) Triage it now and route it to whoever fixes it.`
2. **dept → board** `#m10-board-g1`, m10-ops-maint: `(re #maintenance) Sev1 confirmed: checkout crash from empty
   coupon code. … Routing to @synaps-dev for immediate fix.` + ACTION `{"type": "route", "issue": 1, "to": "synaps-dev"}`
3. **board → synaps-dev** `#m10-board-g1`: `Routing acme-shop#1 … to synaps-dev in #dev.`; `#m10-dev-g1`:
   `@synaps-dev fix acme-shop#1 (base m10-gen-1) …`
4. **synaps-dev fix** (thread): `On it: acme-shop#1.`, then `Empty coupon code in apply_coupon did a bare
   COUPONS[code] dict lookup, raising an unhandled KeyError …; it now validates the code up-front and raises
   ValueError … Added tests/test_issue_1.py; full suite green (36 passed).` + RESULT (status proposed, branch
   `m10-gen-1-fix-1`, local PR ref)
5. **review** (thread), m10-reviewer: `Review (testing-reality-checker): PASS. … Harness pytest on the PR head: green
   (36 passed). Files: shop/pricing.py, tests/test_issue_1.py.`
6. **merge request** `#m10-board-g1`, m10-board: `@user merge request: acme-shop#1 (incident). … Reply in this
   thread with @m10-board approve to merge it, or @m10-board reject.`
7. **approval** (thread), user: `@m10-board approve`  ← the scripted human
8. **merge** (thread), m10-board: `Merged acme-shop#1 (approved by user) as 56afbdd567. pytest on the merged base:
   green (36 passed). Booked as fixed in week 1.` (`acme-git.txt`: the squash commit on `m10-gen-1` and its diff)

## What else happened

- Week 1: ops-maint triaged all four incidents (#1 sev1, #2 #3 sev2, #4 sev3) and routed each to synaps-dev.
  The board queued them by severity; #1 went out at once (above).
- Week 2: growth heard four feature requests (#5 demand 0.9, #6 #7 0.6, #8) and decided to fix the sev2 incidents
  first and defer the features. The board dispatched #2. synaps-dev found the double tax, ran the suite green, and
  was **cancelled by its own session cost cap** (`max_session_cost` 1.0 in the private daemon config) while
  committing: #1 had cost $0.60 of the room session's $1.00. No RESULT, so after `dev_timeout` (900 s) the board
  logged `#2: no PR` (run.log says `via devworker … blocked`: realwork's label for the fallback path; the quarter
  uses no fallback, and that label is now `timeout`).
- Week 3: the infrastructure invoice; nothing to dispatch (capacity 2/quarter used).
- Quarter end: #3 and #4 were still queued: **not approved**, logged as `route_not_dispatched`. Open severity 7
  (#2 + #3 + #4) drives churn to 3.25% and fitness to 10.15.
- Switch's own notice "synaps-dev isn't in this room" follows each dept post that @-mentions synaps-dev in #board.
  By design synaps-dev is only in #dev and gets work from the board's dispatch; the depts' mention is just their
  route ACTION's text. Cosmetic.

## Cost

| | USD |
|---|---|
| dept agents + campaign judge (Haiku) | 0.0513 |
| PR review (Haiku) | 0.0114 |
| synaps-dev (Sonnet, room session `sw-synaps-dev-m10-dev-g1-…`, 96 messages) | 1.0139 |
| **total (QuarterLog.llm_cost_usd)** | **1.0766** |

## Files

`run.log` (the board's log), `transcript.jsonl` (every Switch message the quarter's agents saw or sent),
`mattermost-<room>.json` (channel exports, `dev.py mm-thread`), `kpi.md`, `quarter.json` (Metrics, QuarterLog, dev
tasks, approvals, pending-at-end, setup), `cost.json`, `acme-git.txt` (what landed on `m10-gen-1`). All went through
the evidence scrubber (`company/board/live.scrubber`, the `devstack/dev.py scrub` values + `$HOME` → `~`); a scan for
every known credential value (agent keys, registration and broker tokens, devstack secrets) found 0 hits.
