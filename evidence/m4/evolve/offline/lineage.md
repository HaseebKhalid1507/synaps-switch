### Lineage `evolve:env1:1-2` (seeds [1, 2], mean over seeds)

| gen | env | parent | change | decided by | profit | growth | fix time | LLM $ | fitness | Δ parent | result |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | env1 | — | seed | — | $17.0k | -15.1% | 3.08 wk | $2.23 | 11.68 | — | seed |
| 1 | env2 | env1 | link #maintenance ↔ #dev | Haseeb | $17.9k | -11.2% | 2.38 wk | $2.15 | 15.68 | +4.00 | ✅ kept |
| 2 | env3 | env2 | hire qa (testing/testing-test-automation-engineer) into #dev | Haseeb | $17.7k | -12.4% | 2.62 wk | $2.32 | 15.33 | -0.35 | ↩️ reverted (fitness) |
| 3 | env4 | env2 | policy finance_veto_usd: 5000 → 2000 | Haseeb | — | — | — | — | — | — | ❌ rejected (human) |

Best: **env2**, fitness 15.68.
Spend (QuarterLog.llm_cost_usd over all quarters + proposals): $13.4200; quarters by `company.board.stub_quarter.run_quarter` (simulated $).

Why:
- gen 1 (env2, link #maintenance ↔ #dev): kept — fitness 15.680 > parent 11.684 on seeds [1, 2] (Δ +3.996). Board: Link #maintenance ↔ #dev so ops-maint hands sev1/sev2 incidents to synaps-dev directly; ignored at the gate: ops-maint (sender is an agent)
- gen 2 (env3, hire qa (testing/testing-test-automation-engineer) into #dev): reverted — fitness 15.326 <= parent 15.680 on seeds [1, 2] (Δ -0.354). Board: Hire a QA agent (Agency testing-test-automation-engineer) into #dev to catch regressions before the merge
- gen 3 (env4, policy finance_veto_usd: 5000 → 2000): rejected — rejected by Haseeb: that puts every weekly campaign on our desk in #board. Board: Lower the finance veto to $2,000 so each weekly campaign needs a #board sign-off
