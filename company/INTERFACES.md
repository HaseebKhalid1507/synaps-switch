# company/: module contracts

The design is in `docs/DESIGN-company.md`. This file pins the interfaces so modules can be built in parallel.
Python 3.10. Venv: `company/.venv` (langgraph, langgraph-checkpoint-sqlite, pyyaml, pytest).
Run tests: `company/.venv/bin/python -m pytest company/tests -q`.
Package root: `company/` is a package (`company/__init__.py`); import as `from company.market.sim import Market`.
Run from the repo root.

## Real vs simulated

The product is a real GitHub repo, `adityapat958/acme-shop`: a small Python shop with pytest
tests, planted bugs filed as issues labelled `incident`, and feature requests filed as issues labelled `feature`.
Engineering work is real: a branch, a PR, CI-like local pytest, and a merge after the board approves.
Only the **market** (customers, demand, churn, revenue) is simulated, and it is seeded.

## company/core/env.py  (owner: A)

```python
@dataclass
class Env:
    name: str                      # "env1"
    parent: str | None
    agents: list[dict]             # Switch template agents: {name, description, addressing, instructions}
    rooms: list[dict]              # {name, agents: [...], users: [...]}
    company: dict                  # the x-company block: links, budgets, policies, weeks_per_quarter, price
def load_env(path) -> Env
def save_env(env, path)            # YAML, stable key order
def apply_mutation(env, m: dict) -> Env   # returns a NEW env, name/parent set by caller
#   m = {"op": "set_instructions", "agent": "marketing", "text": "..."}
#     | {"op": "add_agent", "agent": {...}, "room": "dev"} | {"op": "remove_agent", "agent": "rnd"}
#     | {"op": "link", "rooms": ["maintenance","dev"]} | {"op": "unlink", ...}
#     | {"op": "set_policy", "key": "finance_veto_usd", "value": 2000}
#     | {"op": "set_budget", "dept": "marketing", "value": 8000}
#     | {"op": "set_price", "value": 29.0}
def env_diff(a: Env, b: Env) -> str      # human-readable unified diff of the YAML
def to_switch_template(env) -> str       # YAML Switch's create_room_from_yaml accepts (agents/room(s)/kickoff), drops x-company
```

`company/envs/env1.yaml` is the seed: rooms board, dev, maintenance, growth, marketing, finance.
Agents: board, ops-maint, growth, marketing, finance (LangGraph), synaps-dev (the engineering agent).
`x-company.links: []` means every department reports only via #board.

## company/market/sim.py  (owner: A)

Pure python, deterministic given (seed, env.company, actions).

```python
@dataclass
class MarketEvent:
    week: int; room: str; kind: str        # kind: incident|feature_request|competitor|invoice|lead
    text: str                              # chat-ready line, e.g. "40 customers report checkout 500s"
    payload: dict                          # e.g. {"issue": 3, "severity": 1} — incidents/features reference acme-shop issue numbers

@dataclass
class QuarterLog:                          # what the company actually did (filled by the board run)
    fixes: list[dict]        # {issue, week_fixed, pr_url, tests_passed}
    features: list[dict]     # {issue, week_shipped, pr_url, tests_passed}
    campaigns: list[dict]    # {week, text, spend, cites_feature: bool, quality: 0..1}
    price_changes: list[dict]# {week, price}
    spend: list[dict]        # {week, dept, usd, approved: bool}
    llm_cost_usd: float
    decisions: list[dict]    # free-form timeline, not used for scoring

@dataclass
class Metrics:
    revenue: float; costs: float; profit: float; customers: int; churn_rate: float
    growth: float            # customers end/start - 1
    open_incident_severity: float; mean_fix_weeks: float; llm_cost_usd: float

class Market:
    def __init__(self, seed: int, company: dict, issues: dict): ...
        # issues: {"incidents": [{issue, title, severity}], "features": [{issue, title, demand}]}
    def events_for_week(self, week: int) -> list[MarketEvent]
    def settle(self, log: QuarterLog) -> Metrics
```

Model (keep it simple and explainable, document it in the module docstring):
- Customers start at N(seed). Each open incident of severity s adds churn every week it stays open.
- A shipped feature raises conversion from that week on, scaled by its `demand`.
- Campaign leads = spend × base_rate × (0.5 + quality) × (1.5 if cites_feature else 1).
- Price trades conversion against margin, and a competitor price cut shifts demand.
- Costs = budgets actually spent + llm_cost_usd + fixed costs.
- Same seed + same actions → same metrics (test this).

## company/core/fitness.py, lineage.py  (owner: A)

```python
def fitness(m: Metrics, weights: dict) -> float     # weights from company/fitness.yaml
def append_lineage(path, record: dict)              # jsonl: {gen, env, parent, mutation, rationale, approver, fitness, parent_fitness, kept, ts}
def read_lineage(path) -> list[dict]
```

## company/llm.py  (owner: C)

```python
def complete(system: str, prompt: str, *, profile="company", timeout=120) -> tuple[str, float]
    # runs: synaps chat --profile <profile> --no-extensions -s <system>, prompt on stdin
    # env SYNAPS_MACHINE_TOKEN from ~/.synaps-cli/company/broker/token (broker on 127.0.0.1:18282)
    # returns (text without banner lines, cost_usd parsed from "session saved: … ($0.0011)")
def complete_json(system, prompt, **kw) -> tuple[dict, float]   # strips ```json fences, json.loads, one retry
class LLMError(RuntimeError)   # missing token, timeout, non-zero exit, empty reply, no JSON after the retry; .cost_usd = spent anyway
```

Env overrides (defaults above): `COMPANY_SYNAPS_PROFILE` (the name the "company" profile has here), `COMPANY_BROKER_TOKEN_FILE`,
`COMPANY_BROKER_URL`, `COMPANY_SYNAPS_BASE_DIR`. `python -m company.llm setup` writes the profile into a private base dir
(Haiku, thinking off, tiny system.md, empty mcp.json, all built-in tools disabled, `auth.remote_endpoint` = the broker);
`python -m company.llm ping` makes one live JSON call. The prompt is sent as one stdin line (newlines become `\n`).

## company/devworker.py  (owner: B)

The fallback when synaps-dev (the extension's room session, below) does not answer.

```python
def work_issue(issue: int, kind: str, *, repo="adityapat958/acme-shop", workdir="company/state/work",
               remote=None, base=None) -> dict
    # clone/fetch the remote (ACME_REMOTE, default the GitHub repo) at base (default main), branch
    # issue-<n> (or <base>-fix-<n> on a gen-k base), run `synaps chat --profile $COMPANY_DEV_PROFILE
    # --no-extensions` with the whole prompt as ONE stdin line, pytest, commit, plain push,
    # then the PR: `gh pr create` with "Closes #n", or a local PR ref in local mode.
    # returns {issue, pr_url, tests_passed, summary, cost_usd}
def merge_pr(pr_url, *, head_sha=None) -> bool   # only after a human approves in #board (realwork.MergeGate)
    # local PR ref: squash-merge into its base on the local remote; GitHub: gh pr merge --squash
    # [--match-head-commit head_sha], refused unless ACME_ALLOW_GITHUB_WRITES=1 and acme.check_mergeable
    # (on ACME_REPO, open, same repo, <gen-k>-fix-N into gen-k)
```
Machine settings, the company/llm.py scheme (#6, #16): `COMPANY_DEV_PROFILE`, `COMPANY_BROKER_TOKEN_FILE`,
`COMPANY_BROKER_URL`, `COMPANY_SYNAPS_BASE_DIR`, `COMPANY_SYNAPS_BIN`; nothing machine-specific in the code.
Pushes are plain. Only a branch this process created may be rewritten, and then only with `--force-with-lease`.
`python -m company.devworker setup` writes the `company-dev` profile (Sonnet, `max_session_cost`, broker only).

## company/acme.py  (#12)

The product repo: issues, the remote, PR refs, reset between trials. Config by env:
`ACME_REPO` (issues, default `adityapat958/acme-shop`), `ACME_REMOTE` (where branches go; a `file://` bare repo =
local mode), `ACME_ALLOW_GITHUB_WRITES=1` (without it every GitHub write is refused before git/gh runs; the upstream
`adityapat958/acme-shop` and any remote that is not `ACME_REPO` are refused always), `ACME_DEPLOY_KEY` (git uses only
that key, no ssh agent), `ACME_ISSUE_AUTHORS` (issue authors the market reads; default `HaseebKhalid1507,adityapat958`).
GitHub mode (#26) = `tools/synaps_dev.py setup --github`, which writes these for the fork.

```python
load_issues(repo=None, *, cache=None) -> {"incidents": [{issue, title, severity}], "features": [{issue, title, demand}]}
    # read-only `gh issue list --label incident|feature --state all`; severity from the sevN label,
    # demand from the body's **Priority:** (high .9, medium .6, low .3); cached so every trial sees the same set
build_market(seed, company, *, issues=None) -> Market     # Market(seed, company, issues=load_issues())
ensure_baseline(cfg) -> sha            # tag `baseline` on the remote at main, once
start_generation(k, cfg) -> "gen-k"    # gen-k cut from baseline: env_k and env_k+1 face the same bugs
work_branch(issue, base) -> "<base>-fix-<issue>"
local_pr_ref(remote, head, base) / parse_pr_ref(ref)       # "file:///…/origin.git?head=gen-1-fix-1&base=gen-1"
pr_diff(pr), pr_files(pr), run_tests_at(remote, ref), merge_local(pr, head_sha=None), tests_after_merge(pr)
open_pr(cfg, head, base, *, issue, title, body) -> ref|url  # gh pr create -R <repo> --base gen-k --head gen-k-fix-N
check_mergeable(pr, cfg) / pr_info(pr) / fetch_issue(repo, n)   # fetch_issue refuses a non-allowlisted author
```

## company/realwork.py  (#12)

#dev → synaps-dev → review → #board approval → merge, over the Transport contract.

```python
dispatch_text(DevTask) -> "@synaps-dev fix acme-shop#1 (base gen-1) ..."
parse_result(text) -> {status: proposed|blocked|done, summary, evidence[], pr|None, valid} | None
dispatch(transport, DevTask, waiter, *, timeout, fallback=devworker.work_issue) -> RESULT dict + via/thread
land_pr(res, DevTask) -> res            # (inside dispatch) RESULT pr = the pushed branch -> acme.open_pr, here, never
                                        # in the room session; any asker (board or ops-maint), keyed on issue + base
review_pr(pr, *, issue, issue_text, tests=None) -> {verdict: PASS|NEEDS WORK, summary, concerns, tests, files, cost_usd}
    # testing-reality-checker persona on Haiku; failing harness pytest or no tests/ file forces NEEDS WORK
class MergeGate(transport, *, board, room, approvers, week)  # ask(MergeRequest) -> thread; handle(msg); wait(thread)
apply_decision(req, text, *, sender, sender_is_agent, approvers, week) -> record | None   # for a LangGraph interrupt()
record_into(quarter_log, record)       # merged incident -> fixes[{issue, week_fixed, pr_url, tests_passed}]
```
Only a person on `approvers` (`COMPANY_APPROVERS`, default haseeb,aditya) can approve; an agent never can.
`week_fixed` is the week of the merge; `tests_passed` is pytest on the merged base.

## synaps-dev (the extension's agent, #12)

An agent entry in the extension config (`tools/synaps_dev.py setup` writes it): Sonnet with a `max_session_cost`,
`skill_file: skill/synaps-dev.md` (the task procedure and the RESULT block), `workdir_repo` (each room session runs
in its own clone), `session_env`/`session_path` (git identity, gh logged out and no ssh in local mode, a venv with pytest).
Addressed with `@synaps-dev fix acme-shop#N (base gen-k)`, it reproduces with a failing test, fixes, runs the suite,
commits on `gen-k-fix-N`, pushes, and replies in the thread with a summary, `@<asker>`, and

    RESULT
    ```json
    {"status": "proposed", "summary": "...", "evidence": ["..."], "pr": "<PR ref or URL>"}
    ```

## company/transport.py  (owner: C)

```python
@dataclass
class Msg: id: str; room: str; thread: str | None; sender: str; sender_is_agent: bool; text: str; mentions: list[str]
class Transport(Protocol):
    def post(self, as_agent: str, room: str, text: str, thread: str | None = None) -> str
    def on_message(self, agent: str, handler: Callable[[Msg], None]) -> None   # addressed messages for that agent
    def start(self) -> None
class FakeTransport      # in-memory, for tests and offline runs; records a transcript
class SwitchTransport    # one switch_client.Connection per agent identity, credentials from company/state/agents/<name>.json
```

## company/switch_env.py  (owner: C)

```python
def apply_env(env, bridge, *, generation=1, agent_names=None, room_prefix="", mode="auto",
              state_dir="company/state", fixup=None, extra_members=None) -> dict[str, str]  # {env room: Switch room id}
    # extra_members: {env room: [Switch agent names]} on top of the env's agents (the quarter loop: every dept
    # agent in #board, the market everywhere)
def teardown(env, bridge, *, generation=1, room_prefix="", state_dir="company/state") -> dict  # archives them
def update_ledger(env, bridge, rows, *, generation=1, ...)   # rewrites the "Ledger" doc in #board, one row per quarter
def check_ops(bridge) -> {missing_required, missing_optional, ...}   # GET /agents/{id}/ops, logs gaps
# bridge: op(name, args, room_id=None) + connect(room_id) + .name, i.e. transport._link("board"), or Ops(sc.Bridge)
```

Generation k = Switch rooms `<room>-g<k>` in room group `<env>-g<k>`, made by one `create_room_from_yaml` (v2 template,
existing agents by name) with a per-room `create_room` fallback. The operator (`bridge`'s agent, the board) joins every
room. Links: every room → #board ("reports to"), each `x-company.links` pair both ways ("direct line"). Docs in
#board: Handbook + Ledger, owned by the operator. State: `<state_dir>/switch_env/<group>.json`.
CLI: `python -m company.switch_env apply|teardown ENV.yaml [--gen K] [--agent-prefix P] [--room-prefix P]`, `ops`.

## company/depts/agent.py  (owner: C, #9)

```python
def transport_rooms(env, humans=("Aditya","Haseeb"), hub="board") -> dict[str, list[str]]
    # membership the agents assume: room agents + humans, board agent in every room, every agent in #board,
    # x-company.links pairs share agents. Build FakeTransport(rooms=...) with it. On Switch, apply_env puts only
    # the env's agents + the operator in each room: invite the dept agents into #board too, or pass rooms= with
    # the real membership (then cross-room @-mentions without a shared room are not delivered).
class Departments:
    def __init__(self, env, transport, log: QuarterLog, *, llm=complete_json, judge=None, roster=None, rooms=None,
                 humans=("Aditya","Haseeb"), approvers=None, issues=None, week=1, max_hops=3, skip=("board",))
        # registers one DeptAgent handler per env agent except synaps-dev (and board unless skip=())
    week: int                                  # set by the quarter loop before posting that week's events
    def event_message(self, ev: MarketEvent) -> tuple[str, str]   # (room, "@<dept agents> <text> <ask>")
    routes: list[dict]                         # {week, issue, to, by, msg}: work to dispatch (#12)
    pending: dict[str, dict]                   # every price/spend/campaign item by requesting msg id, with status
    def usage(self) -> dict                    # est. tokens + usd per agent, llm_cost_usd
class CampaignJudge(llm=None, cache_path=None).score(text, facts) -> ({quality, scores, note, cached}, cost)
def parse_action(text) -> dict | None; def validate_action(a, agents=None) -> dict; def render(say, action) -> str
```

Every agent reply ends with a fenced JSON ACTION block (`campaign | spend_request | price | route | none`,
finance verdicts add `decision`), parsed from the posted text into the QuarterLog. Finance policy
(`policies.finance_veto_usd`, dept budgets) is code; over the veto goes to the #board humans, who answer
`@finance approve|reject` in the thread. Budgets: `x-company.token_budget: {per_agent, per_quarter}`.
Loop guard: only messages that @-address the agent, at most 3 agent<->agent hops per conversation.
Demo/evidence: `python -m company.depts.demo --out evidence/m4/dept-agents [--stub]`.

## company/board/  (owner: C)

The board's LangGraph graphs (inner quarter loop, outer evolution loop with interrupt() approval gates), the router
(which paused run a message resumes, and who is allowed to resume it), and the department agents in `company/depts/`.
Entry point: `python -m company.run --env company/envs/env1.yaml [--quarters N | --generations N] --transport fake|switch`.

### quarter.py (#10): the inner loop, one quarter in LangGraph

```python
def run_quarter(env, *, seed, generation=1, **opts) -> tuple[Metrics, QuarterLog]   # the runner evolve (#11) injects
def run_quarter_full(env, *, seed, generation=1, config: QuarterConfig | None = None, **opts) -> QuarterResult
    # QuarterResult(metrics, log, fitness, report, thread_id, resumed: "new"|"resumed"|"cached", state)
class QuarterConfig:   # every opts key; the defaults run offline: FakeTransport + stub LLM/synaps-dev/person
    transport="fake"|"switch"; llm=None (stub offline, pass company.llm.complete_json for Haiku); weeks=None;
    db="company/state/board.db"; thread_id=None ("<env>:<seed>"); fresh=False; issues=None (market default;
    live: acme.load_issues()); humans; approvers; approver=None|"stub"|"mattermost"|callable(rt, thread, req);
    dev_per_week / dev_per_quarter (else x-company.dev_capacity {per_week, per_quarter}, else unlimited);
    dev_timeout=900; approve_timeout=180; week_wall_s=60; quiet_s=12; review/merge/verify/dev_cost/fallback
    injectables; agent_prefix/room_prefix/acme_prefix; acme_remote (live: local file:// remote only);
    ledger_rows (earlier quarters); quarter_label; weights; hook(node, week) (tests); log(line)
def kpi_table(...) -> str; def ledger_row(label, seed, metrics, fitness) -> dict
```

Graph (thread "<env>:<seed>", SqliteSaver after every node, `durability="sync"`):
`open_quarter → [operate(w) → work(w) → close_week(w)] × weeks → settle → report`.
- open_quarter: live only: agents registered (board, dept agents, `market`; synaps-dev must exist), `apply_env`
  generation k on the board's link with `extra_members` (dept agents into #board, market everywhere; synaps-dev only
  in #dev), `pin_rooms`, acme-shop `<acme_prefix>gen-k` cut from `baseline` on the local remote.
- operate(w): `deps.week = w`; the market identity posts each `events_for_week(w)` (`deps.event_message`); live, the
  week ends when the rooms were quiet `quiet_s` or after `week_wall_s`. The market counts as a person for the hop guard.
- work(w): `deps.routes` to synaps-dev → dev queue (incidents by severity, then features by demand, deduped, capped)
  → `realwork.dispatch` in #dev → review (harness pytest + testing-reality-checker) posted in #dev → `MergeGate` in
  #board → a person's approve → merge on the local remote → `QuarterLog.fixes/features` (week = the merge's week).
- close_week(w): late decisions booked, synaps-dev's session cost booked, a week summary in #board.
- settle: everything still pending (finance escalations, merge requests, undispatched routes) is not approved and
  logged (`verdict status=not_approved by=quarter_end`, `merge_not_approved`, `route_not_dispatched`);
  `Market.settle` → Metrics; fitness (company/fitness.yaml).
- report: KPI table + fitness posted to #board, the Ledger doc rewritten (`update_ledger`; offline
  `<state_dir>/ledger/<group>.md`).

`llm_cost_usd` = dept agents + judge (Departments) + PR reviews + synaps-dev's room session (`live.SessionCost`).
The board records its @-mail (`state["board_inbox"]`) and never approves: offline `stubs.StubApprover`, live
`live.MattermostApprover` (a scripted person posting via `dev.py mm-post`). Killed: rerun with the same arguments
resumes the node it died in; a finished thread returns its result; a changed env/config (fingerprint) or
`fresh=True` starts over. Other modules: `wire.py` (CompanyTransport: env names ↔ prefixed Switch names, activity for
`wait_quiet`; `company_rooms`, `extra_members`), `stubs.py`, `live.py` (approver, synaps-dev cost, local-only merge,
evidence export + scrub).

### mechanics.py (#25): how the env changes a quarter

Pure rules, shared by FakeTransport and Switch; each random draw is keyed by (seed, item), so env_k and env_k+1
face the same luck on a seed. Every effect is a `QuarterLog.decisions` entry with a readable `what`
(types `org`, `route_direct`, `route_relay`, `dispatch`, `regression`, `regression_caught`, `review_slip`,
`route_not_dispatched`, `mechanics` {stats}, `market_inputs`); evolve's kept/reverted reason quotes
`mechanics.why_moved(parent_eval, trial_eval)`.
- links: a sev1/sev2 incident routed by a dept whose room is linked to #dev (`x-company.links`) is dispatched the
  same week by that dept agent in #dev (it must be a #dev member: `company_rooms` / `extra_members` put it there);
  synaps-dev answers it. Otherwise (no link, sev3, features) the board relays it a week later (`RELAY_WEEKS`).
- capacity: `QuarterConfig.dev_per_week/dev_per_quarter`, else `x-company.dev_capacity {per_week, per_quarter}`
  (env1: 3/week), else unlimited. Waits and never-dispatched routes are logged.
- testers: agents in #dev with a `testing/*` persona. An incident fix has a latent regression with p 0.25; one
  tester catches it with p 0.45, two or more 0.9; uncaught, the incident reopens and its re-fix lands 2 weeks later.
  Each tester beyond the first is another (paid) review round; with p 0.5 the merge slips a week.
- policies/budgets: enforced by Departments; approved campaigns + spend reach `Market.settle` unchanged.
Market (#16): sev1 = 3 severity points (4 − s); each open point costs 0.5% weekly churn and 3% of that week's sales;
features raise conversion from their ship week; GOOD > OK > BAD on profit and growth (`test_market`).
Roster (#16): `company/fetch_roster.sh` clones agency-agents at the pinned commit into `$COMPANY_ROSTER_DIR`
(default `company/vendor/agency-agents`, gitignored); `core.roster.get_roster()` loads it once.

### company/run.py

`python -m company.run --env company/envs/env1.yaml --quarters 1 --transport fake` prints the Metrics and fitness
(deterministic for a seed; `--report` adds the KPI table, `--json` one object per quarter). Live:
`--transport switch --llm haiku --approver mattermost --agent-prefix P --room-prefix P --acme-prefix P
--acme-remote file:///…git --issues acme --humans user --approvers user --weeks 3 --generation K`
(see evidence/m4/quarter/live_quarter.py). `--generations N` is reserved for #11 (`evolve.evolve`).

### company/board/evolve.py: the outer loop (#11)

```python
evolve(env, *, generations=3, seeds=(1, 2), run_quarter, propose, gate, lineage_path, envs_dir,
       budget_usd=25.0, approvers=None, gate_timeout_s=3600, checkpoint="company/state/evolve.sqlite",
       thread_id=None, read_rooms=None, roster=None, fresh=False, log=None) -> dict
       # {run, best, best_fitness, records, stop_reason, spent_usd, generations}; resumes the thread if it exists
run_quarter(env, *, seed, generation) -> (Metrics, QuarterLog)
       # injected: stub_quarter.run_quarter offline; load_quarter_runner("quarter", **QuarterConfig fields)
       # wraps #10's company.board.quarter.run_quarter, imported lazily
propose(summary, env, *, history, error) -> ({"diagnosis", "mutation", "rationale"}, cost_usd)
       # LLMProposer (board persona on Haiku via company.llm) | StubProposer; re-asked once with the error
gate: ask(payload) -> thread; wait(thread, deadline) -> msg dict | None; announce(thread, text); [release()]
       # TransportGate(transport= | factory=, board, room, approvers) | ScriptedGate(script)
```

Graph (thread `evolve:<env>:<seeds>`, SqliteSaver): baseline → reflect → propose → post_gate → await_gate
(`interrupt()`) → trial → select → record → reflect …; reject / timeout / invalid → record without a trial.
Only a message with `sender_is_agent=False` from an approver saying approve/reject (`realwork.parse_decision`)
decides; anything else is logged in the record's `gate.ignored` and the gate waits again; no decision by the
deadline = `timeout` (auto-reject). Kept iff mean fitness over the seeds > the parent's (same seeds).
Lineage record: `{run, runner, gen, env, parent, mutation, change, diagnosis, rationale, approver, decision,
outcome: seed|kept|reverted|rejected|timeout|invalid, reason, fitness, parent_fitness, delta, kept, seeds, metrics,
eval, gate, cost_usd: {quarters, proposal}, diff, ts}`; kept envs → `<envs_dir>/env<k>.yaml`.
Budget: stops when spent (sum of QuarterLog.llm_cost_usd + proposals) ≥ budget or the next generation would pass it.
CLI: `python -m company.board.evolve --env … --generations N [--offline]`; table: `python -m company.lineage_report`.
