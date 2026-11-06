### Lineage `evolve:env1:1` (seeds [1], mean over seeds)

| gen | env | parent | change | decided by | profit | growth | fix time | LLM $ | fitness | Δ parent | result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | env1 | — | seed | — | $20.1k | -11.2% | 2.50 wk | $1.18 | 12.85 | — | seed |
| 1 | env2 | env1 | link #maintenance ↔ #dev | user | $22.2k | -8.9% | 1.50 wk | $1.98 | 14.19 | +1.35 | ✅ kept |
| 2 | env3 | env2 | policy dev_capacity_per_quarter: — → 4 | user | $22.2k | -8.9% | 1.50 wk | $1.05 | 15.12 | +0.93 | ✅ kept |

Best: **env3**, fitness 15.12.
Spend (QuarterLog.llm_cost_usd over all quarters + proposals): $4.2325; quarters by `company.board.quarter.run_quarter`.

Why:
- gen 1 (env2, link #maintenance ↔ #dev): kept — fitness 14.191 > parent 12.846 on seeds [1] (Δ +1.345): fix time 2.50 wk → 1.50 wk, profit $20.1k → $22.2k, growth -11.2% → -8.9%, LLM $1.179 → $1.978; across the seeds: items relayed by the board 2 → 0, weeks waited for the board's relay 2 → 0, items handed to synaps-dev directly 0 → 2. Board: Direct maintenance↔dev link eliminates 1-week board relay for incidents, letting sev1/sev2 reach synaps-dev immediately. With 4 incidents queued in week 1, faster dispatch saves ~2 weeks of open incident cost (fitness penalty: -2.0*3.0 = -6.0/week). Expected: incidents #3 and #4 dispatch in week 2 instead of week 3, reducing open_severity and churn even within current dev_capacity constraint
- gen 2 (env3, policy dev_capacity_per_quarter: — → 4): kept — fitness 15.118 > parent 14.191 on seeds [1] (Δ +0.927): profit $22.2k → $22.2k, LLM $1.978 → $1.052. Board: Doubling dev capacity (1→2 items/week, 2→4/quarter) aligns with actual incident volume (4 incidents in wk1). This prevents backlog overflow that leaves incidents open, directly reducing open_incident_severity and churn, which together account for -6.0 of fitness loss. Expect fix time to remain ~1.5 wk and open incidents to resolve faster, improving fitness by ~4-5 points
