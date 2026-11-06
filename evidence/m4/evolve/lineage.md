### Lineage `evolve:env1:1-2:m4v` (seeds [1, 2], mean over seeds)

| gen | env | parent | change | decided by | profit | growth | fix time | LLM $ | fitness | Δ parent | result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | env1 | — | seed | — | $17.0k | -15.1% | 3.08 wk | $2.23 | 11.68 | — | seed |
| 1 | env2 | env1 | link #maintenance ↔ #dev | user | $17.9k | -11.2% | 2.38 wk | $2.15 | 15.68 | +4.00 | ✅ kept |

Best: **env2**, fitness 15.68.
Spend (QuarterLog.llm_cost_usd over all quarters + proposals): $8.7902; quarters by `company.board.stub_quarter.run_quarter` (simulated $).

Why:
- gen 1 (env2, link #maintenance ↔ #dev): kept — fitness 15.680 > parent 11.684 on seeds [1, 2] (Δ +3.996). Board: Direct link between #maintenance and #dev eliminates 1-week board queue for incident routing, allowing ops-maint to escalate sev1/sev2 fixes immediately. This should reduce mean fix time from 3.08 weeks and unblock critical path work, improving fitness through faster incident resolution and reducing open_incident_severity weight in the formula; ignored at the gate: m4v-peer (sender is an agent)
