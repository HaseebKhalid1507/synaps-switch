# M4 evidence

| dir | what |
|---|---|
| [`../m13/`](../m13/README.md) | **#13, the live run**: env1 baseline vs the evolved org on the same seeds, 12 real PRs on HaseebKhalid1507/acme-shop (#10 to #21) merged after a person approved in #board, the lineage tables, KPIs, Mattermost exports, cost ($7.21) and wall time (47 min 41 s) |
| [`github/`](github/README.md) | #26: the first real PR on the fork (acme-shop#9), the deploy-key security model, the probe of the room session's environment |
| [`quarter/`](quarter/README.md) | #10: one live quarter, local stand-in origin |
| [`evolve/`](evolve/) | #11: the human gate for org changes (kill + resume at the gate), offline evolution |
| [`real-work/`](real-work/) | #12: synaps-dev → review → #board → merge, 13/13 |
| [`dept-agents/`](dept-agents/) | #9: the department agents on Haiku |
| [`apply-env/`](apply-env/) | #8: an org applied to Switch |
| [`transport/`](transport/) | the Switch transport |

Headline from #13 (seed 42): `link #maintenance ↔ #dev`, proposed by the board and approved by a person, took
incident fix time from 2.50 to 1.50 weeks and fitness from 13.62 to 15.96 (best env3: 16.02); a person rejected
generation 3. No fitness revert happened live; [why](../m13/README.md#why-there-is-no-fitness-revert).
