### Lineage `evolve:env1:42` (seeds [42], mean over seeds)

| gen | env | parent | change | decided by | profit | growth | fix time | LLM $ | fitness | Δ parent | result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | env1 | — | seed | — | $20.8k | -11.2% | 2.50 wk | $1.09 | 13.62 | — | seed |
| 1 | env2 | env1 | link #maintenance ↔ #dev | user | $23.0k | -8.8% | 1.50 wk | $0.96 | 15.96 | +2.33 | ✅ kept |
| 2 | env3 | env2 | hire incident-commander (engineering/engineering-incident-response-commander) into #dev | user | $23.0k | -8.8% | 1.50 wk | $0.90 | 16.02 | +0.06 | ✅ kept |
| 3 | env4 | env3 | policy dev_capacity: — → 3 | user | — | — | — | — | — | — | ❌ rejected (human) |

Best: **env3**, fitness 16.02.
Spend (QuarterLog.llm_cost_usd over all quarters + proposals): $2.9740; quarters by `company.board.quarter.run_quarter`.

Why:
- gen 1 (env2, link #maintenance ↔ #dev): kept — fitness 15.957 > parent 13.624 on seeds [42] (Δ +2.333): fix time 2.50 wk → 1.50 wk, profit $20.8k → $23.0k, growth -11.2% → -8.8%, LLM $1.085 → $0.956; across the seeds: items relayed by the board 2 → 0, weeks waited for the board's relay 2 → 0, items handed to synaps-dev directly 0 → 2. Board: Direct #maintenance→#dev link eliminates the 1-week board relay delay for incidents, allowing sev1/sev2 issues to reach synaps-dev immediately (same week). This cuts incident latency by 50% (from 2 weeks to 1 week for sev1), directly reducing open_incident_severity impact on fitness (-2.0 weight) and churn. With 2 out of 4 incidents currently stuck in queue, faster dispatch also better utilizes existing dev capacity
- gen 2 (env3, hire incident-commander (engineering/engineering-incident-response-commander) into #dev): kept — fitness 16.016 > parent 15.957 on seeds [42] (Δ +0.059): profit $23.0k → $23.0k, LLM $0.956 → $0.897. Board: Incident Response Commander dequeues backlog (incidents #3, #4) by coordinating dispatch timing with synaps-dev's 1/week capacity, reducing mean fix time and closing severity faster. Expected effect: fitness gain from (0.5*growth upside + incident severity reduction) as fewer open incidents improve NPS and retention
- gen 3 (env4, policy dev_capacity: — → 3): rejected — rejected by user: change freeze: no more org changes before the demo; propose it next quarter. Board: Increasing dev capacity from 1 to 3 items/week allows synaps-dev to handle the full incident backlog (sev1 + sev2 + sev3) without dropping items, closing the open severity gap from 3.0 toward 0. This unblocks incident-commander's effectiveness (added last gen) and should recover the ~2% churn leaking to competitors, improving fitness via reduced open_incident_severity component (-2.0 × 3.0 penalty)
