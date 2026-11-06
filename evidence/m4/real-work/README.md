# M4 #12 live: acme-shop#1 through #dev → synaps-dev → review → #board approval → merge

Stack: Switch 0.31.0 (docker project `synaps-switch-dev`, 127.0.0.1:18400; Mattermost 127.0.0.1:18465), reached
over HTTP only. The private Synaps daemon (`~/.synaps-switch-dev`) runs the extension from this branch; synaps-dev
is its agent. No GitHub write: `origin` is a local bare clone of github.com/adityapat958/acme-shop
(`~/.synaps-switch-dev/m12/remote/acme-shop-origin.git`, made with a read-only https clone and no remote back).
Issues were read with `gh issue list` / `gh issue view` (read-only).

Run: `python evidence/m4/real-work/live_real_work.py 1 --gen 2` (setup in the script's docstring).
Result: **13/13 PASS** (`live.log`).

| step | what happened |
|---|---|
| market | `Market(issues=...)` from the 8 real issues: 4 incidents (sev1, sev2, sev2, sev3), 4 features (`issues.json`) |
| reset | the local origin has a `baseline` tag; each generation works on `gen-k` cut from it |
| #dev | `m12-board`: `@synaps-dev fix acme-shop#1 (base gen-2) ...` |
| synaps-dev | Sonnet room session in its own clone (`workdir_repo`). Wrote `tests/test_issue_1.py`, saw 5 tests fail for the reported reason, fixed `shop/pricing.py`, full suite 36 passed, committed `gen-2-fix-1`, plain push, replied in the thread with a summary, `@m12-board` and the RESULT block. 39 s. (`synaps-dev-session.md`: every tool call) |
| review | harness pytest on a fresh clone of the PR head: 36 passed; testing-reality-checker (agency persona, Haiku via company/llm.py): PASS. Posted by `m12-reviewer` in the #dev thread |
| #board | `m12-board` asked `@user` with the summary, the review and the PR ref. `m12-reviewer` (an agent) said "approve": ignored. `user` asked a question: ignored. `user`: `@m12-board approve` → `devworker.merge_pr` (squash into gen-2) → pytest on the merged gen-2: 36 passed → `QuarterLog.fixes = [{issue 1, week_fixed 2, tests_passed true}]` |
| reset check | `gen-3` cut from baseline; synaps-dev's regression test run there: 5 failed (the bug is back for the next trial). `main` and `baseline` untouched |

`live-run1-gen1.log` is the first run, on gen-1: synaps-dev fixed it the same way in 54 s, the review passed, then
the script stopped because `m12-reviewer` was not yet a member of #m12-board (fixed by inviting it; that run's
#board ask stays unanswered and gen-1 unmerged). The gen-2 run is also the reset in action: the same session, given
the same bug on a fresh generation, met the original code again.

| file | what |
|---|---|
| `live.log` | the gen-2 run, PASS/FAIL per check |
| `transcript.jsonl` | SwitchTransport's transcript (posts and deliveries, with `sender_is_agent`) |
| `mattermost-m12-dev.json`, `mattermost-m12-board.json` | the conversation as people see it in Mattermost (`root_id` = thread) |
| `dev-room/`, `board-room/` | full channel transcripts (markdown) and the extension's log lines for the room |
| `synaps-dev-session.md` | the synaps-dev session: every tool call and the head of its result (thinking left out) |
| `pr.diff`, `git-log.txt` | the PR's diff (base...head) and the local origin's refs and history after the merge |
| `quarter-log.json` | RESULT, review, decision and the QuarterLog record |
| `issues.json` | the market's issue set as read from GitHub |

Spend: synaps-dev session $0.38 (both runs, Sonnet, cap $1.00), reviews 2 × $0.012 (Haiku). Total ≈ $0.40.
Everything here went through the shared scrubber (`devstack/dev.py scrub`): no token, key or cookie; `$HOME` is `~`.
