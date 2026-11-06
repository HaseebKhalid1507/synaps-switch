# M4 #26 live: acme-shop#3 fixed by synaps-dev → a REAL PR on the fork → #board human → squash-merged into the generation

**PR: https://github.com/HaseebKhalid1507/acme-shop/pull/9** (`m26-gen-1-fix-3` → `m26-gen-1`, opened and merged by the
company process with the operator's gh; the room session has no GitHub login).

Stack: Switch 0.31.0 (127.0.0.1:18400, Mattermost 127.0.0.1:18465), the private Synaps daemon (`~/.synaps-switch-dev`)
running the extension from branch `m4/github-fork`. Fork: `HaseebKhalid1507/acme-shop` (issues #1-#8 mirrored from
`adityapat958/acme-shop`, which is never written: read-only issue reads only). `main` and `baseline` untouched
(`fork-refs.txt`).

## Setup (exact commands)

```bash
# once: the fork's deploy key (write access to that repo only), private, never in git:
#   ~/.synaps-switch-dev/m12/deploy/acme-shop (0600); its public half is a deploy key on the fork
source ~/.local/share/synaps-switch/company-venv/bin/activate
python tools/synaps_dev.py setup --github --gen 1 --prefix m26-
#   pushes the `baseline` tag + `m26-gen-1` to the fork with the deploy key, writes the synaps-dev entry
#   (gh-none GH_CONFIG_DIR, GIT_SSH_COMMAND with only the deploy key, no agent, cap $3) into
#   ~/.synaps-switch-dev/synaps-switch.json (backup next to it), and ~/.synaps-switch-dev/acme-github.env
#   (0600, names and paths only)
python3 devstack/synapsdev.py daemon stop && python3 devstack/synapsdev.py daemon start
python evidence/m4/github/live_github.py register                     # m26-board, m26-reviewer (creds 0600)
python3 tools/mkroom.py m26-dev   --agent synaps-dev --with m26-board --with m26-reviewer
python3 tools/mkroom.py m26-board --agent synaps-dev --with m26-board --with m26-reviewer
#   room ids -> ~/.synaps-switch-dev/m26/rooms.json
set -a; . ~/.synaps-switch-dev/acme-github.env; set +a                 # ACME_REPO/REMOTE/ALLOW_GITHUB_WRITES/DEPLOY_KEY/ISSUE_AUTHORS
export COMPANY_SYNAPS_BASE_DIR=~/.synaps-switch-dev/company-llm \
       COMPANY_BROKER_TOKEN_FILE=~/.synaps-switch-dev/broker/machine.token COMPANY_BROKER_URL=http://127.0.0.1:18191
python evidence/m4/github/live_github.py run 3
```

## Result: the merge path 20/20 PASS, plus the probe of the session's own environment (5/5) via the daemon's record

| step | what happened |
|---|---|
| market | 8 issues from the fork (4 incidents, 4 features), **only allowlisted authors** (`ACME_ISSUE_AUTHORS`, default `HaseebKhalid1507,adityapat958`). Live check: the same fork with `ACME_ISSUE_AUTHORS=adityapat958` gives 0 and 0 (the mirrored issues are by HaseebKhalid1507), so a stranger's issue on the public fork never reaches the market |
| #dev | `m26-board`: `@synaps-dev fix acme-shop#3 (base m26-gen-1) ...` |
| synaps-dev | Sonnet room session, cloned `git@github.com:HaseebKhalid1507/acme-shop.git` over ssh with the deploy key. Wrote `tests/test_issue_3.py` (failed for the reported reason: stock 10 → 7), added `self._release()` in `place_order`'s `except PaymentError`, 32 passed, committed, `git push -u origin m26-gen-1-fix-3` (deploy key). RESULT `pr: "m26-gen-1-fix-3"`: the branch, no PR (`synaps-dev-session.md`: every tool call; no `gh` call at all) |
| PR | this process (operator's gh): `gh pr create -R HaseebKhalid1507/acme-shop --base m26-gen-1 --head m26-gen-1-fix-3` → **#9**, author `HaseebKhalid1507`, not cross-repo |
| review | harness pytest on a fresh clone of the PR head `b82cfc7` (no credentials in its env): 32 passed. testing-reality-checker (Haiku) on `gh pr diff`: PASS, $0.0114 (`pr.diff`) |
| #board | `m26-board` asked `@user`. `m26-reviewer` (an agent) said "approve": ignored. `user` asked a question: ignored, PR still OPEN. **Scripted** human approval: `devstack/dev.py mm-post m26-board '@m26-board approve' --root <thread> --as user` (a script posting as the seeded Mattermost user, not a live person) → `gh pr merge --squash --match-head-commit b82cfc7` → merged as `2fea4ca` into `m26-gen-1`, pytest on the merged base 32 passed → `QuarterLog.fixes = [{issue 3, week_fixed 2, tests_passed true}]` |

## The room session has no GitHub login

The env the private daemon records for this session (`env` in `~/.synaps-switch-dev/sessions/<id>.json`; its
bash tool runs with exactly that env, after `env_clear()`), names only:
`GH_CONFIG_DIR GIT_AUTHOR_* GIT_COMMITTER_* GIT_SSH_COMMAND HOME LANG PATH PYTHONDONTWRITEBYTECODE TERM`.
No `SSH_AUTH_SOCK` (the extension now refuses to pass it: `main.session_env`, tested), no token, no gh config.

`probe-session-env.txt` (`probe_session_env.py <session>`: `session_probe.sh` under `env -i` + that recorded
env, in the session's own clone):

```
GH_CONFIG_DIR=~/.synaps-switch-dev/m12/gh-none (files: 0)
SSH_AUTH_SOCK=<unset>
gh auth status exit=1        You are not logged into any GitHub hosts.
gh api user exit=4
ssh-add -l exit=2 (2 = no agent)
ls-remote origin exit=0      (the deploy key reads the fork)
ERROR: Permission to HaseebKhalid1507/synaps-switch.git denied to deploy key
push other repo exit=128     (a --dry-run push to another repo: denied)
```

`probe-company-side.txt` is the same probe with the env `main.session_env` builds from the live config (same
result). Asking synaps-dev to run the probe itself in its room failed in an instructive way: it **declined twice**
as credential reconnaissance (`probe-in-session.txt`), which is the behaviour we want from it; the live script's
five "probe run by synaps-dev" checks are FAIL for that reason only (`live.log`), and the recorded-env probe above
stands in for them.

## How the live run went (not first try; `live-attempts.log`)

1. The first dispatch: synaps-dev fixed the bug, then its turn ended at the daemon's default **24 provider rounds
   per event turn** (`turn_budget_exhausted` in the daemon log) before it pushed. Fix: `turn_budget.event.max_provider_rounds = 80`
   in the private daemon config (`devstack/synapsdev.py setup` now writes it); daemon restarted, board nudged in
   the thread, synaps-dev pushed `m26-gen-1-fix-3` and replied.
2. Its reply put `RESULT` as plain text, then re-posted in the skill's format but with `status: "done"`. `land_pr`
   now treats `done` + `pr` = exactly the expected fix branch as `proposed` (tested); the skill says what
   `proposed` means in GitHub mode.
3. The review needed the private broker's settings (`COMPANY_BROKER_*`, above). The resume run
   (`run 3 --thread <dispatch> --result-from-mm`) took synaps-dev's last RESULT from the thread and did the rest.

## Spend

synaps-dev session `20261106-082735-aecb`: **$0.65** (Sonnet, cap $3: the fix ≈ $0.60, plus the nudges and two
refusals). Review: $0.0114 (Haiku). Total ≈ **$0.66**.

| file | what |
|---|---|
| `live.log` | the final run, PASS/FAIL per check |
| `live-attempts.log` | the earlier runs (turn limit, the plain-text RESULT, the missing broker settings) |
| `pr.json`, `pr.diff`, `fork-refs.txt` | the PR (title, body, author, commits, state after), its diff, the fork's refs after the merge |
| `synaps-dev-session.md` | the synaps-dev session: every tool call (thinking left out) |
| `transcript.jsonl`, `mattermost-m26-dev.json`, `mattermost-m26-board.json` | Switch transcript and the Mattermost view |
| `quarter-log.json` | RESULT, review, decision, QuarterLog |
| `issues.json` | the market's issue set from the fork (allowlisted authors) |
| `session_probe.sh`, `probe_session_env.py`, `probe-*.txt` | the no-GitHub-login probe and its outputs |
| `live_github.py` | the run |

Everything here went through the shared scrubber (`devstack/dev.py scrub`): no token, key or cookie; `$HOME` is `~`.
