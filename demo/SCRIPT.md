# Demo video: shot list (3:00)

For Haseeb, who records it. Judges score the presentation at 20%, and they want to see from the video and the
README that Switch is central. Keep every shot inside Switch's chat (Mattermost). Use the terminal only where it
proves something real: the Synaps session at work, the merged commit, the lineage table.

You record two things. The first is **one live quarter**: a few minutes of real time plus however long you take
to approve. Record all of it and cut it down to scenes 2 to 6. The second is **one evolution step**: scenes 7 to 10, taken from #13's run.
If evolution misbehaves, use the [fallback cut](#fallback-cut-core-flow-only), which needs only the quarter.

## Names used below

Pick a fresh prefix so the channels and agents are new and the sidebar is clean:

```bash
export P=demo1-                       # any unused prefix; channels become ${P}board-g1, ${P}dev-g1, …
export PY=~/.local/share/synaps-switch/company-venv/bin/python
export COMPANY_SYNAPS_BASE_DIR=$HOME/.synaps-switch-dev/company-llm \
       COMPANY_BROKER_TOKEN_FILE=$HOME/.synaps-switch-dev/broker/machine.token \
       COMPANY_BROKER_URL=http://127.0.0.1:18191
```

| Thing | Name with `P=demo1-` |
|---|---|
| Channels (generation 1) | `demo1-board-g1`, `demo1-dev-g1`, `demo1-maintenance-g1`, `demo1-growth-g1`, `demo1-marketing-g1`, `demo1-finance-g1` |
| Agents | `demo1-board`, `demo1-ops-maint`, `demo1-reviewer`, `demo1-market`, … ; **`synaps-dev` keeps its own name** |
| acme-shop branches (local stand-in origin) | base `demo1-gen-1`, fix `demo1-gen-1-fix-1` |
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
4. **Terminal 1** (the quarter): large font, `clear`. Have this command typed but **don't run it until recording**:
   ```bash
   $PY -m company.run --env company/envs/env1.yaml --transport switch --generation 1 --weeks 3 \
     --agent-prefix $P --room-prefix $P --acme-prefix $P \
     --switch-url http://127.0.0.1:18400 --token-file devstack/state/registration.token \
     --issues acme --acme-remote "file://$HOME/.synaps-switch-dev/m12/remote/acme-shop-origin.git" \
     --humans user --approvers user --approve-timeout 900 --dev-per-week 1 --dev-per-quarter 1 \
     --week-wall 90 --quiet 15 --out /tmp/demo-q1 --report -v
   ```
   Leave out `--approver`, so **you** are the approver. You have 900 s to approve each merge request.
   `--dev-per-quarter 1` keeps synaps-dev to one fix, well inside its $1.00 room-session cap. For scale, from the
   evidence run (`evidence/m4/quarter/run.log`): incident to RESULT took 2 min, synaps-dev's fix of #1 cost $0.60, and
   the departments plus reviews cost $0.06. Of that run's 19 minutes, 15 were a second fix that hit the cost cap and
   waited out its 900 s timeout. With one fix you avoid that.
   **[FILL FROM #26: once PRs go to GitHub, replace --acme-remote with what #26 documents]**
5. **Terminal 2** (optional, for scene 4): watch synaps-dev's Synaps session, read-only. Try it once before you
   record, and skip it if attach misbehaves:
   ```bash
   python3 devstack/synapsdev.py sessions                            # the session whose room is ${P}dev-g1
   python3 devstack/synapsdev.py run -- --attach <session-id-prefix> --observe
   ```
   The session only exists after the board's first dispatch to `${P}dev-g1`, so run this during the recording,
   once scene 3 has happened.
6. **Evolution (scenes 7 to 10):** **[FILL FROM #13: the exact `--generations` command, its gate room, seeds and gate timeout]**
   The board posts its proposal in the gate room and **waits** up to the gate timeout for a person. Be at the
   keyboard when that post lands and record the approval live. Trials re-run whole quarters, so let them finish
   off camera.

## Shot list

| # | Time | Scene | On screen | You do / type | Voice-over or caption |
|---|---|---|---|---|---|
| 1 | 0:00–0:15 | Hook | GitHub README: the pitch, then the architecture diagram | Scroll slowly | "The Agency is an AI org chart with 167k stars and no office. We gave it one: Switch." |
| 2 | 0:15–0:30 | The office | Mattermost Favorites: the six channels. Open `${P}board-g1` and show the *"📄 created room document “Handbook”"* post (rooms, reporting lines, budgets) | Click `${P}board-g1` | "Every department is a Switch room, every employee is an Agency persona, and the org chart is a Switch template, links and room documents." |
| 3 | 0:30–0:50 | Incident → hand-off | `${P}maintenance-g1`: the market's post *"… Applying empty coupon code crashes checkout … (sev1, issue #1)"*. Then `${P}board-g1`: ops-maint's triage and route, and the board's *"Routing acme-shop#1 … to synaps-dev in #dev"* | Start terminal 1 at the top of the recording. Click between the channels as the posts arrive | "The market is simulated. The bug is a real issue in a real repo." |
| 4 | 0:50–1:20 | The fix | `${P}dev-g1`: *"@synaps-dev fix acme-shop#1 (base ${P}gen-1) …"*, then in the thread *"On it"*, the summary with *"36 passed"*, and the RESULT block. Optional split screen with terminal 2 (test file written, pytest run). Then the reviewer's **PASS** | Open the thread. Speed-ramp the wait (39 s to 101 s in the evidence runs) | "synaps-dev is a Synaps session that lives in this room. It writes a regression test, fixes the code, runs the suite and pushes a branch. A Reality Checker reviews the diff and the test run." |
| 5 | 1:20–1:45 | **Human approves** | `${P}board-g1`: *"@user merge request: acme-shop#1 …"* | Open its thread, type **`@demo1-board approve`**, press Enter. Wait for *"Merged acme-shop#1 (approved by user) as <sha>. pytest on the merged base: green …"* | "Nothing merges until a person says so. An agent saying 'approve' is ignored." |
| 5b | (2 s) | The merged code | Terminal: `git -C ~/.synaps-switch-dev/m12/remote/acme-shop-origin.git log --oneline -2 ${P}gen-1` **[FILL FROM #26: the GitHub PR page instead]** | — | — |
| 6 | 1:45–2:00 | Quarter KPIs | `${P}board-g1`: *"Quarter 1 of env1 … KPI"* table (revenue, profit, fix weeks, LLM cost, **fitness**) and *"📄 updated room document “Ledger”"* | Scroll the table | "Each quarter the board posts KPIs and writes the Ledger. The revenue is simulated, but the LLM bill is real." |
| 7 | 2:00–2:20 | Board proposes a change | Gate room: *"Generation 1: the board proposes env1 → env2. Change: …"* with the Diagnosis, the Rationale and the `diff` | Zoom into the diff | "The board reads the quarter and proposes one change to the org: **[FILL FROM #13: e.g. 'hire a QA engineer from The Agency' or 'link #maintenance and #dev']**." |
| 8 | 2:20–2:35 | Approve | Same thread | Type **`@<P>board approve`** (the board of #13's run), press Enter. Later, cut to *"Kept env2: fitness X > Y (env1) on seeds […]"* | "Approved changes run the same market seeds. A change is kept only if fitness beats the parent." |
| 9 | 2:35–2:50 | env2 is real Switch structure | The sidebar shows the `-g2` channels. env2's Handbook shows the change (a new direct line, or the new hire in #dev) | Click the env2 board channel | "env2 is a new set of Switch rooms, and the change is real Switch structure." |
| 10 | 2:50–3:00 | Lineage | Terminal: `$PY -m company.lineage_report <lineage.jsonl>` **[FILL FROM #13: the path]**, or the README's Results table. End card: repo URL | — | "env_n is the best org found so far, and every step that produced it is a decision in chat." |

Things you may see:

- Switch posts *"⚠️ … synaps-dev isn't in this room"* in `#board` after a department mentions synaps-dev there.
  That is by design: synaps-dev lives only in `#dev`, and the board dispatches to it. Cut it or say nothing.
- Until #26, the merge request shows a local `file://…` PR ref with your home path. That's harmless and holds no secret.
- If you miss the 900 s approval window, the quarter books the fix as not approved and carries on. Re-run with
  `--fresh` and a new prefix if you want the take.

## Fallback cut: core flow only

Use this if evolution misbehaves live. Scenes 1 to 6 already work: `evidence/m4/quarter/` ran this exact chain
with a scripted approver, and here you are the approver.

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
before you rely on it. In `~${P}lab`, type `@synaps-dev fix acme-shop#1 (base main)`. It replies *"On it"*, then posts the fix summary
and the RESULT block, with the branch pushed to the local stand-in origin. There's no review and no merge in this
cut. Don't use `evidence/m4/real-work/live_real_work.py` as a video take: it posts the approval itself as `user`,
so the approval on screen wouldn't be yours.
