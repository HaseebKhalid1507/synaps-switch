# #13 run policy (written before the run)

Written 2026-11-06 before the live run started, and committed before it: commit `450cd92` at 04:04:34 EST; run 1
started at 04:05:02. (Corrected after the run: this line first said "~04:20", a typo; the commit time is the record.)

**Approvals in this recorded run are scripted; in the demo video they're typed live.** The scripted person posts
as the devstack's Mattermost user `user` (`devstack/dev.py mm-post --as user`), a real human account. The gates
accept it because Switch reports the sender as a person and `user` is the only name on `--approvers`.

| Gate | Policy (fixed) | Who posts |
|---|---|---|
| Merge requests in #board (every PR) | approve iff the reality-checker review is PASS **and** the harness pytest on the PR head is green; else reject | `company.board.live.MattermostApprover` (`company.run --approver mattermost`) |
| Org change, generation 1 | approve | `evidence/m13/gate_person.py` |
| Org change, generation 2 | approve | `evidence/m13/gate_person.py` |
| Org change, generation 3 | reject: "change freeze: no more org changes before the demo; propose it next quarter" | `evidence/m13/gate_person.py` |

The policy does not look at what the board proposes. Whether an approved change is kept or reverted is decided
by fitness on the same seed (42), not by the person. If no approved change is reverted, that is reported as is;
no revert is staged.

Settings: env1, seed 42, 3 weeks per quarter, synaps-dev capacity 1/week and 2/quarter, 3 generations,
prefix `m13-` (agents, rooms, acme-shop branches `m13-gen-<k>` on HaseebKhalid1507/acme-shop), budget stop $9.

## Run 2 (seed 1), written before it started

Written 2026-11-06 after run 1 (seed 42) finished and before run 2 started: commit `1e4f47b` at 04:28:23 EST; run 2
started at 04:28:34. (Corrected after the run: this line first said "~04:35", a typo.) Run 1 finished with gen 1 kept, gen 2 kept, gen 3 rejected, so
**no fitness revert**. The issue allows a second short run on seed 1. Same command, same policy, same settings;
only the seed (`--seeds 1`) and the prefix (`m13-s1-`: agents, rooms, acme-shop branches `m13-s1-gen-<k>`) change.

| Gate | Policy (fixed, unchanged from run 1) |
|---|---|
| Merge requests | approve iff review PASS and harness pytest green, else reject (`MattermostApprover`) |
| Org change, generation 1 | approve |
| Org change, generation 2 | approve |
| Org change, generation 3 | reject: "change freeze: no more org changes before the demo; propose it next quarter" |

If run 2 has no revert either, that is reported as is. Budget stop for run 2: $6 (run 1 spent $2.97; total cap $10).
