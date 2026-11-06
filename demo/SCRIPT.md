# Demo video: shot list (3:00)

For Haseeb, who records it. Judges score the presentation at 20%, and they want to see from the video and the
README that Switch is central. Keep every shot inside Switch's chat (Mattermost). Use the terminal only where it
proves something real: the Synaps session at work, the merged commit, the lineage table.

You record **one command**: one evolution step on real GitHub PRs, the same command #13 ran
([evidence/m13](../evidence/m13/README.md)) with one generation and your prefix. Its baseline quarter is scenes 2 to 6
(about 7 minutes plus however long you take to approve). Then the board proposes a change and waits for you
(scenes 7 and 8). The trial quarter runs on new `-g2` rooms (about 7 minutes, off camera; you approve its merge
too), then scenes 9 and 10. Record all of it and cut it down. If evolution misbehaves, use the
[fallback cut](#fallback-cut-core-flow-only), which needs only the baseline quarter.

## Names used below

Pick a fresh prefix so the channels and agents are new and the sidebar is clean:

```bash
export P=demo1-                       # any unused prefix; channels become ${P}board-g1, ${P}dev-g1, …
export PY=~/.local/share/synaps-switch/company-venv/bin/python
tools/live_env.sh --check             # every name "set": the broker, the Haiku profile, the fork (acme-github.env)
```

| Thing | Name with `P=demo1-` |
|---|---|
| Channels (generation 1) | `demo1-board-g1`, `demo1-dev-g1`, `demo1-maintenance-g1`, `demo1-growth-g1`, `demo1-marketing-g1`, `demo1-finance-g1` |
| Agents | `demo1-board`, `demo1-ops-maint`, `demo1-reviewer`, `demo1-market`, … ; **`synaps-dev` keeps its own name** |
| acme-shop branches, on the fork [HaseebKhalid1507/acme-shop](https://github.com/HaseebKhalid1507/acme-shop) | base `demo1-gen-1` (cut from `baseline`), fix `demo1-gen-1-fix-1`, the PR `demo1-gen-1-fix-1` → `demo1-gen-1`; the trial: `demo1-gen-2`, … |
| What you type to approve | `@demo1-board approve` (as a **reply in the request's thread**) |

**Where you type:** the Mattermost web UI at <http://127.0.0.1:18465>, logged in as **`user`**. The password is
`MATTERMOST_USER_PASSWORD` in `devstack/.env`; never put that file on screen. The run is started with `--approvers user`,
so only that login counts as a human. **Discord:** Switch's Discord bridge can carry these rooms
(`python3 devstack/dev.py connect-discord --default` before the apply). The company run has only been exercised
on Mattermost, so record on Mattermost unless a full dry run on Discord passes first.

## Pre-stage (before you hit record)

1. **Stack and daemon up** (README steps 1 to 3 if not already done), then check them:
   ```bash
   free -g                                       # ≥ 4 GB available
   python3 devstack/dev.py info                  # Switch 127.0.0.1:18400, Mattermost 127.0.0.1:18465
   python3 devstack/synapsdev.py broker status   # healthz 200
   python3 devstack/synapsdev.py daemon status
   ```
2. **Make the generation-1 rooms now**, so you can arrange the sidebar before recording. The quarter re-applies
   them and only fills gaps:
   ```bash
   $PY -m company.switch_env apply company/envs/env1.yaml --gen 1 --agent-prefix $P --room-prefix $P
   ```
3. **Mattermost:** log in as `user`, star the six `${P}*-g1` channels (they go to the Favorites group at the top),
   set browser zoom to 125%, and close other tabs and bookmarks. Switch posts a greeting from each bot as it joins
   ("Hi! Mention @…"). That's expected; scroll past it.
4. **Terminal 1** (the run): large font, `clear`. Have this command typed but **don't run it until recording**:
   ```bash
   tools/live_env.sh $PY -m company.run --env company/envs/env1.yaml --generations 1 --seeds 42 \
     --transport switch --weeks 3 --dev-per-week 1 --dev-per-quarter 1 \
     --agent-prefix $P --room-prefix $P --acme-prefix $P \
     --switch-url http://127.0.0.1:18400 --token-file devstack/state/registration.token \
     --issues acme --acme-remote git@github.com:HaseebKhalid1507/acme-shop.git \
     --humans user --approvers user --approve-timeout 900 \
     --gate-room ${P}board-g1 --gate-timeout 1800 --budget-usd 5 \
     --week-wall 90 --quiet 15 --db company/state/${P}video.db --evolve-db company/state/${P}video-evolve.sqlite \
     --out /tmp/${P}video -v
   ```
   Leave out `--approver`, so **you** are the approver: of every merge request (900 s each) and of the board's
   proposal (1800 s). `--seeds 42` matters: the default is two seeds, so two quarters per org.
   `--dev-per-quarter 1` keeps it to one fix per quarter. For scale, from #13's live run
   (`evidence/m13/run*/console.log`): incident to RESULT took 57 s to 370 s (usually about 2 min), a fix costs
   synaps-dev about $0.40 to $0.60 (one outlier $1.50), well inside its $3.00 room-session cap, and the departments
   plus the review cost about $0.07 a quarter. Expect about $1.50 and 15 to 20 minutes for the whole command.
   acme-shop is the fork: synaps-dev pushes `${P}gen-1-fix-1` with the deploy key, and the company process opens the
   PR, reviews it, and squash-merges it after your approve. Branches can't be reused, so every take needs a fresh `P`.
5. **Terminal 2** (optional, for scene 4): watch synaps-dev's Synaps session, read-only. Try it once before you
   record, and skip it if attach misbehaves:
   ```bash
   python3 devstack/synapsdev.py sessions                            # the session whose room is ${P}dev-g1
   python3 devstack/synapsdev.py run -- --attach <session-id-prefix> --observe
   ```
   The session only exists after the board's first dispatch to `${P}dev-g1`, so run this during the recording,
   once scene 3 has happened.
6. **Evolution (scenes 7 to 10)** is the same command: after the baseline quarter's KPI post, the board posts
   *"@user Generation 1: the board proposes env1 → env2 …"* in the gate room `${P}board-g1` (seed 42) and **waits**
   up to the gate timeout (1800 s) for a person. Be at the keyboard when that post lands and record the approval
   live. The trial quarter then runs on `${P}*-g2`; approve its merge request in `${P}board-g2`, and let it finish off
   camera. In #13 the board proposed `link #maintenance ↔ #dev` both times
   ([threads](../evidence/m13/run1-seed42/threads.md#the-boards-org-change-gate-board-g1-reflect--proposal--decision--outcome));
   it is the board's call, so yours may differ.

## Shot list

| # | Time | Scene | On screen | You do / type | Voice-over or caption |
|---|---|---|---|---|---|
| 1 | 0:00–0:15 | Hook | GitHub README: the pitch, then the architecture diagram | Scroll slowly | "The Agency is an AI org chart with 167k stars and no office. We gave it one: Switch." |
| 2 | 0:15–0:30 | The office | Mattermost Favorites: the six channels. Open `${P}board-g1` and show the *"📄 created room document “Handbook”"* post (rooms, reporting lines, budgets) | Click `${P}board-g1` | "Every department is a Switch room, every employee is an Agency persona, and the org chart is a Switch template, links and room documents." |
| 3 | 0:30–0:50 | Incident → hand-off | `${P}maintenance-g1`: the market's post *"… Applying empty coupon code crashes checkout … (sev1, issue #1)"*. Then `${P}board-g1`: ops-maint's triage and route *"(re #maintenance) Sev1 confirmed …"*, and a week later the board's *"Relaying acme-shop#1 (incident, sev1: …) … to synaps-dev in #dev."* | Start terminal 1 at the top of the recording. Click between the channels as the posts arrive | "The market is simulated. The bug is a real issue in a real repo." |
| 4 | 0:50–1:20 | The fix | `${P}dev-g1`: *"@synaps-dev fix acme-shop#1 (base ${P}gen-1) …"*, then in the thread *"On it"*, the summary with *"35 passed"* (about that), and the RESULT block. Optional split screen with terminal 2 (test file written, pytest run). Then the reviewer's **PASS** | Open the thread. Speed-ramp the wait (57 s to 370 s in #13) | "synaps-dev is a Synaps session that lives in this room. It writes a regression test, fixes the code, runs the suite and pushes a branch. A Reality Checker reviews the diff and the test run." |
| 5 | 1:20–1:45 | **Human approves** | `${P}board-g1`: *"@user merge request: acme-shop#1 …"* | Open its thread, type **`@demo1-board approve`**, press Enter. Wait for *"Merged acme-shop#1 (approved by user) as <sha>. pytest on the merged base: green …"* | "Nothing merges until a person says so. An agent saying 'approve' is ignored." |
| 5b | (3 s) | The merged PR | Browser: the PR on GitHub, the link at the end of the merge request (`https://github.com/HaseebKhalid1507/acme-shop/pull/<n>`): *Merged*, `${P}gen-1-fix-1` → `${P}gen-1`, the diff with `tests/test_issue_1.py`. In #13 that was [acme-shop#10](https://github.com/HaseebKhalid1507/acme-shop/pull/10) | Open the link | "A real pull request on a real repo, merged because a person said so." |
| 6 | 1:45–2:00 | Quarter KPIs | `${P}board-g1`: *"Quarter 1 of env1 … KPI"* table (revenue, profit, fix weeks, LLM cost, **fitness**) and *"📄 updated room document “Ledger”"* | Scroll the table | "Each quarter the board posts KPIs and writes the Ledger. The revenue is simulated, but the LLM bill is real." |
| 7 | 2:00–2:20 | Board proposes a change | Gate room: *"Generation 1: the board proposes env1 → env2. Change: …"* with the Diagnosis, the Rationale and the `diff` | Zoom into the diff | "The board reads the quarter and proposes one change to the org: here, a direct line between #maintenance and #dev, so incidents stop waiting a week for the board." (In #13 it proposed exactly that. If yours proposes something else, say what it proposed.) |
| 8 | 2:20–2:35 | Approve | Same thread | Type **`@${P}board approve`** in that thread, press Enter. Later, cut to *"Kept env2: fitness X > Y (env1) on seeds [42], Δ +Z > min-gain 0.5."* (#13, before #31 added the margin: *"Kept env2: fitness 15.96 > 13.62 (env1) on seeds [42]."*) | "Approved changes run the same market seeds. A change is kept only if it beats the parent by more than the noise in the LLM bill." |
| 9 | 2:35–2:50 | env2 is real Switch structure | The sidebar shows the `-g2` channels. env2's Handbook shows the change (a new direct line, or the new hire in #dev) | Click the env2 board channel | "env2 is a new set of Switch rooms, and the change is real Switch structure." |
| 10 | 2:50–3:00 | Lineage | Terminal: `$PY -m company.lineage_report /tmp/${P}video/lineage.jsonl` (yours), or #13's `$PY -m company.lineage_report evidence/m13/run1-seed42/lineage.jsonl` (3 generations: kept, kept, rejected by a person; its gen 2 is a no-op that #31's rules refuse, so prefer yours), or the README's Results table. End card: repo URL | — | "env_n is the best org found so far, and every step that produced it is a decision in chat." |

Things you may see:

- Switch posts *"⚠️ … synaps-dev isn't in this room"* in `#board` after a department mentions synaps-dev there.
  That is by design: synaps-dev lives only in `#dev`, and the board dispatches to it. Cut it or say nothing.
- Switch also posts that notice under the board's proposal when its text mentions @synaps-dev. Same thing.
- If the trial's merge request waits on you and you miss it, the trial books the fix as not approved and scores
  lower, so the change looks worse than it is. Approve it.
- The board can only propose a lever the quarter reads (#31). If it proposes anything else, the run log shows
  *"gen 1: … refused before #board, no lever: …; asking once more"*. You only see the second proposal. If that
  one misses too, the generation is skipped and nothing reaches the gate: re-run with a new prefix.
- If the trial's fitness beats the parent's by less than 0.5 (`--min-gain`), the gate thread says *"No effect: …
  env1 stays."* and the lineage row reads *"↔ no effect"*. With one fix a quarter the direct line is worth about
  +1.4 (offline, seed 42, `--dev-per-quarter 1`), so you'd only see this if env2's fix cost synaps-dev about $0.90
  more than env1's.
- If you miss the 900 s approval window, the quarter books the fix as not approved and carries on. Re-run with
  `--fresh` and a new prefix if you want the take.

## Fallback cut: core flow only

Use this if evolution misbehaves live. Scenes 1 to 6 already work: #13 ran this exact chain six times on the fork
with a scripted approver (`evidence/m13/`), and here you are the approver.

| # | Time | Scene |
|---|---|---|
| 1 | 0:00–0:20 | Hook (README pitch + diagram) |
| 2 | 0:20–0:40 | The office (channels + Handbook) |
| 3 | 0:40–1:05 | Incident → ops-maint triage → board routes to synaps-dev |
| 4 | 1:05–1:45 | synaps-dev's fix in the `#dev` thread (+ terminal 2), then the reviewer's PASS |
| 5 | 1:45–2:20 | You type `@demo1-board approve`, the board merges, then the merged commit (5b) |
| 6 | 2:20–2:45 | KPI table + Ledger update in `#board` |
| close | 2:45–3:00 | README "How Switch is used" table and the repo URL. VO: "The board's evolution loop and its evidence are in the repo." Don't show a lineage table you didn't get live |

**Plan C (about 60 s, no company code):** a human gives synaps-dev the task directly.

```bash
python3 tools/mkroom.py ${P}lab --agent synaps-dev      # a Mattermost-bridged room with synaps-dev in it
```

This exact path hasn't been run yet (in the evidence an agent gave synaps-dev its task, never a person), so try it once
before you rely on it. In `~${P}lab`, type `@synaps-dev fix acme-shop#1 (base ${P}gen-1)`, naming a generation branch
that already exists on the fork (one an earlier take cut), never `main`. It replies *"On it"*, then posts the fix
summary and the RESULT block, with `${P}gen-1-fix-1` pushed to the fork by the deploy key. There's no PR, no review
and no merge in this cut: those are the company process's job. Don't use `evidence/m4/real-work/live_real_work.py` as a video take: it posts the approval itself as `user`,
so the approval on screen wouldn't be yours.
