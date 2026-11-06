# Mattermost threads: run2-seed1
Rendered from `mattermost/*.json` (`devstack/dev.py mm-thread` exports, scrubbed). Switch's own *"synaps-dev isn't in this room"* notices are left out of the incident chains (by design synaps-dev is only in #dev); they are in the JSON.

## Generation 1: incident acme-shop#1, end to end
### #maintenance-g1

### #board-g1: triage and hand-off
> **m13-s1-ops-maint** (04:28:50): (re #maintenance) Sev1: Empty coupon crash blocking checkout. Routing to @synaps-dev for immediate fix. Customer impact: ~90 users unable to complete orders. Owner: @synaps-dev
> 
> ```json
> {"type": "route", "issue": 1, "to": "synaps-dev"}
> ```
> **m13-s1-board** (04:29:52): Relaying acme-shop#1 (incident, sev1: Applying empty coupon code crashes checkout with unhandled exception), raised by ops-maint in #maintenance in week 1, to synaps-dev in #dev.

### #dev-g1: the fix
> **m13-s1-board** (04:29:52): @synaps-dev fix acme-shop#1 (base m13-s1-gen-1) (Applying empty coupon code crashes checkout with unhandled exception). Branch off m13-s1-gen-1, push m13-s1-gen-1-fix-1, and reply here with the RESULT block (pr = the branch you pushed; the PR is opened for you).
> **synaps-dev** (04:29:57, reply): On it: acme-shop#1.
> **synaps-dev** (04:31:28, reply): The crash was a bare `COUPONS[code]` dict lookup in `pricing.py` with no guard — an empty or unknown code raised an unhandled `KeyError` that propagated through `cart.total()` and crashed checkout. Fixed by validating the code first and raising `ValueError` for blank or unrecognised values. Added `tests/test_issue_1.py` (5 cases: empty code, unknown code, valid code, cart with blank/unknown coupon); full suite green (36 passed).
> 
> @m13-s1-board
> RESULT
> ```json
> {"status": "proposed", "summary": "Empty or unknown coupon code no longer crashes checkout — raises ValueError instead of KeyError", "evidence": ["tests/test_issue_1.py: 4 tests fail before fix, all 5 pass after", "pytest -q: 36 passed", "commit 296f083 on m13-s1-gen-1-fix-1"], "pr": "m13-s1-gen-1-fix-1"}
> ```

### #board-g1: the human gate and the merge
> **m13-s1-board** (04:31:41): @user merge request: acme-shop#1 (incident).
> synaps-dev: Empty or unknown coupon code no longer crashes checkout — raises ValueError instead of KeyError
> Review (testing-reality-checker): PASS. PR fixes the root cause (unguarded dictionary access causing KeyError on empty/unknown coupons) by adding explicit validation that raises ValueError instead. Comprehensive regression test added that exercises the exact reported scenario and confirms valid codes still work.
> Harness pytest on the PR head: green (36 passed, 13 warnings in 0.07s).
> PR: https://github.com/HaseebKhalid1507/acme-shop/pull/16
> Reply in this thread with `@m13-s1-board approve` to merge it, or `@m13-s1-board reject`.
> **user** (04:31:44, reply): @m13-s1-board approve
> **m13-s1-board** (04:31:54, reply): Merged acme-shop#1 (approved by user) as 604808450f. pytest on the merged base: green (36 passed, 13 warnings in 0.05s). Booked as fixed in week 2.

### #board-g1: the quarter's KPI post
```
**Quarter 1 of env1, generation 1, seed 1, 3 weeks: KPI**

| KPI | value |
|---|---|
| revenue | $25,081 |
| costs | $5,001 |
| profit | $20,080 |
| customers | 1,333 |
| churn (avg weekly) | 1.95% |
| growth | -11.19% |
| open incident severity | 3 |
| mean fix weeks | 2.50 |
| LLM cost | $1.1785 (depts + judge $0.0544, reviews $0.0257, synaps-dev $1.0984) |
| incidents fixed | #1 (wk 2, tests green), #2 (wk 3, tests green) |
| features shipped | none |
| campaigns | 0 ($0, avg quality 0.00) |
| price | unchanged |
| spend approved / not | $0 / $0 |
| not approved at quarter end | 2 |
| **fitness** | **12.8457** |
```

## Generation 2: incident acme-shop#1, end to end
### #maintenance-g2

### #board-g2: triage and hand-off
(nothing in #board: #maintenance ↔ #dev is linked, so ops-maint triaged it in #maintenance and handed it to synaps-dev in #dev itself, the same week; no relay)

### #dev-g2: the fix
> **m13-s1-ops-maint** (04:36:55): @synaps-dev fix acme-shop#1 (base m13-s1-gen-2) (Applying empty coupon code crashes checkout with unhandled exception). Branch off m13-s1-gen-2, push m13-s1-gen-2-fix-1, and reply here with the RESULT block (pr = the branch you pushed; the PR is opened for you).
> **synaps-dev** (04:36:58, reply): On it: acme-shop#1.
> **synaps-dev** (04:38:40, reply): Empty coupon code (`""`) caused an unhandled `KeyError` in `apply_coupon`, propagating through `cart.total()` and crashing checkout. Fixed by adding a guard in `shop/pricing.py` that raises `ValueError` for any empty or unrecognised code. Added `tests/test_issue_1.py`; full suite green (35 passed).
> 
> @m13-s1-ops-maint
> 
> RESULT
> ```json
> {"status": "proposed", "summary": "Empty/unknown coupon code now raises ValueError instead of crashing with KeyError", "evidence": ["tests/test_issue_1.py: 4 tests fail before fix, all pass after", "pytest -q: 35 passed", "commit 8b22532 on m13-s1-gen-2-fix-1"], "pr": "m13-s1-gen-2-fix-1"}
> ```

### #board-g2: the human gate and the merge
> **m13-s1-board** (04:38:52): @user merge request: acme-shop#1 (incident).
> synaps-dev: Empty/unknown coupon code now raises ValueError instead of crashing with KeyError
> Review (testing-reality-checker): PASS. PR fixes the root cause (unguarded dictionary lookup) by validating coupon code before access and raising ValueError instead of KeyError. Comprehensive regression test added that exercises empty/unknown codes and verifies valid coupons still work; full pytest suite passes.
> Harness pytest on the PR head: green (35 passed, 13 warnings in 0.04s).
> PR: https://github.com/HaseebKhalid1507/acme-shop/pull/18
> Reply in this thread with `@m13-s1-board approve` to merge it, or `@m13-s1-board reject`.
> **user** (04:38:54, reply): @m13-s1-board approve
> **m13-s1-board** (04:38:59, reply): Merged acme-shop#1 (approved by user) as c54abd539e. pytest on the merged base: green (35 passed, 13 warnings in 0.03s). Booked as fixed in week 1.

### #board-g2: the quarter's KPI post
```
**Quarter 1 of env2, generation 2, seed 1, 3 weeks: KPI**

| KPI | value |
|---|---|
| revenue | $27,215 |
| costs | $5,002 |
| profit | $22,213 |
| customers | 1,368 |
| churn (avg weekly) | 1.95% |
| growth | -8.86% |
| open incident severity | 3 |
| mean fix weeks | 1.50 |
| LLM cost | $1.9780 (depts + judge $0.0591, reviews $0.0254, synaps-dev $1.8935) |
| incidents fixed | #1 (wk 1, tests green), #2 (wk 2, tests green) |
| features shipped | none |
| campaigns | 0 ($0, avg quality 0.00) |
| price | unchanged |
| spend approved / not | $0 / $0 |
| not approved at quarter end | 2 |
| **fitness** | **14.1908** |
```

## The board's org-change gate (#board-g1): reflect → proposal → decision → outcome
> **m13-s1-board** (04:35:53): @user Generation 1: the board proposes env1 → env2.
> Change: link #maintenance ↔ #dev
> Diagnosis: Incident backlog bottleneck: 4 incidents raised week 1, only 2 fixed in quarter due to synaps-dev capacity (1/week, 2/quarter) and no direct maintenance↔dev link causing 1-week relay delay. #1 (sev1) and #2 (sev2) got fixed, but #3 and #4 never reached dev, leaving open severity at 3.0. Board relay adds 1 week latency on all incidents.
> Rationale: Direct maintenance↔dev link eliminates 1-week board relay for incidents, letting sev1/sev2 reach synaps-dev immediately. With 4 incidents queued in week 1, faster dispatch saves ~2 weeks of open incident cost (fitness penalty: -2.0*3.0 = -6.0/week). Expected: incidents #3 and #4 dispatch in week 2 instead of week 3, reducing open_severity and churn even within current dev_capacity constraint.
> env1 scored fitness 12.85 on seeds [1]. If approved, env2 runs the same seeds and is kept only if it scores higher.
> ```diff
> --- env1
> +++ env2
> @@ -1,4 +1,5 @@
> -x-name: env1
> +x-name: env2
> +x-parent: env1
>  agents:
>  - name: board
>    persona: project-management/project-manager-senior
> @@ -111,7 +112,9 @@
>    users:
>    - '{$creator}'
>  x-company:
> -  links: []
> +  links:
> +  - - maintenance
> +    - dev
>    budgets:
>      marketing: 10000
>      maintenance: 4000
> ```
> Reply in this thread with `@m13-s1-board approve` or `@m13-s1-board reject <why>`. Only a person on the approvers list decides; no decision in 1800s = auto-reject.
> **user** (04:36:20, reply): @m13-s1-board approve
> **m13-s1-board** (04:46:24, reply): Kept env2: fitness 14.19 > 12.85 (env1) on seeds [1].

> **m13-s1-board** (04:46:34): @user Generation 2: the board proposes env2 → env3.
> Change: policy dev_capacity_per_quarter: — → 4
> Diagnosis: Dev capacity bottleneck (1/week, 2/quarter) caused 2 of 4 wk1 incidents to never reach synaps-dev despite the maintenance↔dev link, leaving open incident severity at 3.0 (fitness penalty -6.0) and churn at 1.9%. Incidents #3 and #4 languished unresolved, depressing growth to -8.9%.
> Rationale: Doubling dev capacity (1→2 items/week, 2→4/quarter) aligns with actual incident volume (4 incidents in wk1). This prevents backlog overflow that leaves incidents open, directly reducing open_incident_severity and churn, which together account for -6.0 of fitness loss. Expect fix time to remain ~1.5 wk and open incidents to resolve faster, improving fitness by ~4-5 points.
> env2 scored fitness 14.19 on seeds [1]. If approved, env3 runs the same seeds and is kept only if it scores higher.
> ```diff
> --- env2
> +++ env3
> @@ -1,5 +1,5 @@
> -x-name: env2
> -x-parent: env1
> +x-name: env3
> +x-parent: env2
>  agents:
>  - name: board
>    persona: project-management/project-manager-senior
> @@ -122,6 +122,7 @@
>    policies:
>      finance_veto_usd: 5000
>      sev1_fix_weeks: 2
> +    dev_capacity_per_quarter: 4
>    token_budget:
>      per_agent: 30000
>      per_quarter: 150000
> ```
> Reply in this thread with `@m13-s1-board approve` or `@m13-s1-board reject <why>`. Only a person on the approvers list decides; no decision in 1800s = auto-reject.
> **user** (04:47:01, reply): @m13-s1-board approve
> **m13-s1-board** (04:54:20, reply): Kept env3: fitness 15.12 > 14.19 (env2) on seeds [1].

