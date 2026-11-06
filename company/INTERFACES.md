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

```python
def work_issue(issue: int, kind: str, *, repo="adityapat958/acme-shop", workdir="company/state/work") -> dict
    # clone/fetch acme-shop, branch issue-<n>, run `synaps chat --profile company-dev --no-extensions`
    # in the checkout with a prompt to fix/implement the issue and run pytest; then commit, push,
    # `gh pr create` with "Closes #n". Verify by running pytest itself.
    # returns {issue, pr_url, tests_passed, summary, cost_usd}
def merge_pr(pr_url) -> bool          # gh pr merge --squash; called only after a human approves in #board
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
              state_dir="company/state", fixup=None) -> dict[str, str]     # {env room name: Switch room id}
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

## company/board/  (owner: C)

The board's LangGraph graphs (inner quarter loop, outer evolution loop with interrupt() approval gates), the router
(which paused run a message resumes, and who is allowed to resume it), and the department agents in `company/depts/`.
Entry point: `python -m company.run --env company/envs/env1.yaml --generations 3 --transport fake|switch`.
