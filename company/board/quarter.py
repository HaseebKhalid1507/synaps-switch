"""company/board/quarter.py — the board's inner loop: one quarter of the company under a fixed env, in LangGraph.

    from company.board.quarter import run_quarter
    metrics, log = run_quarter(env, seed=42, generation=1)              # FakeTransport + stub LLM, offline
    metrics, log = run_quarter(env, seed=42, generation=7, transport="switch", agent_prefix="m10-",
                               room_prefix="m10-", approver="mattermost", weeks=3)   # live (company/run.py)

The graph (one thread per quarter, SqliteSaver checkpoints after every node, thread_id "<env>:<seed>"):

    open_quarter ─▶ operate(w) ─▶ work(w) ─▶ close_week(w) ─┬─▶ operate(w+1) …
                                                            └─▶ settle ─▶ report ─▶ END

- open_quarter: Switch only: register every agent (dept agents, the board, the `market` identity; synaps-dev
  must already exist), apply_env generation k on the board's own link with every dept agent invited into
  #board and the market into every room, pin the rooms, cut the acme-shop branch gen-k from baseline on
  the local remote. The board says the quarter is open in #board.
- operate(w): deps.week = w; the market posts each `Market.events_for_week(w)` event into its room,
  @-addressed to that room's dept agents (`Departments.event_message`); the dept agents act (ACTION blocks
  into the QuarterLog, finance reviews). FakeTransport is synchronous; on Switch the week ends when the
  rooms have been quiet for `quiet_s`, or after `week_wall_s`.
- work(w): `Departments.routes` to synaps-dev become the board's dev queue (deduped, incidents by severity
  first, then features by demand; at most dev_per_week / dev_per_quarter, else x-company.dev_capacity). How a
  route reaches #dev depends on the org (company/board/mechanics.py, #25): a sev1/sev2 incident from a room
  linked to #dev is dispatched the week it was raised by the department agent itself (ops-maint addresses
  synaps-dev in #dev; synaps-dev answers ops-maint); everything else waits for the board's weekly relay.
  testing/* agents in #dev review each PR (regressions caught or reopened, extra rounds can slip a merge).
  Each of these effects is a QuarterLog decision with a readable "what". Each task goes through realwork
  (#12): dispatch in #dev → synaps-dev's RESULT → review (testing-reality-checker on Haiku + harness pytest)
  → MergeGate in #board → a human's "approve" → merge on the local remote → QuarterLog.fixes / .features
  (week_fixed = the week of the merge). The board never approves: on FakeTransport a stub person answers,
  live a scripted person posts as the devstack's Mattermost user (live.MattermostApprover).
- close_week(w): decisions that arrived late are booked; the board posts a short week summary in #board.
- settle: anything still pending (finance escalations nobody answered, merge requests with no decision,
  routes the dev queue never reached) counts as not approved and is logged as such; synaps-dev's session cost
  is booked; Market.settle(QuarterLog) → Metrics; fitness from company/fitness.yaml.
- report: the KPI table + fitness posted to #board and written to the generation's Ledger document
  (switch_env.update_ledger; offline, a file under state_dir/ledger/).

QuarterLog.llm_cost_usd is every real cost: dept agents and the campaign judge (Departments books them),
the PR reviews, and synaps-dev's room session.

Killed and resumed: the board's state (QuarterLog, Departments' pending items/routes/budgets, the judge
cache, the dev queue, open merge requests) is in the checkpoint, so a run killed in week w restarts the
node it was in, with the same thread id. A thread already finished returns its stored result; a thread
whose env/seed/config changed (fingerprint) or `fresh=True` starts over.

The board's own @-messages are recorded (state["board_inbox"]); proposing org changes is #11.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, TypedDict

import yaml

from company import realwork as rw
from company.board import mechanics as mech
from company.board.stubs import StubApprover, StubDev, stub_llm, stub_merge, stub_review, stub_verify
from company.board.wire import DEV, MARKET, CompanyTransport, company_rooms, extra_members
from company.core.env import Env
from company.core.fitness import fitness as fitness_of
from company.depts.agent import DEFAULT_HUMANS, CampaignJudge, Departments
from company.market.sim import Market, Metrics, QuarterLog, default_issues

ROOT = Path(__file__).resolve().parents[2]
BOARD = "board"
HUB = "board"
DEFAULT_DB = "company/state/board.db"
INBOX_KEEP = 200


def load_weights(path=None) -> dict:
    p = Path(path or ROOT / "company" / "fitness.yaml")
    data = yaml.safe_load(p.read_text()) or {}
    return data.get("weights", data)


# ───────────────────────── config ─────────────────────────


@dataclass
class QuarterConfig:
    transport: str = "fake"                  # fake | switch
    llm: Callable | None = None              # complete_json stand-in; None: stub on fake, company.llm live
    llm_label: str | None = None             # "stub" | "haiku" (fingerprint, report)
    weeks: int | None = None                 # override x-company.weeks_per_quarter
    db: str = DEFAULT_DB
    thread_id: str | None = None             # default "<env>:<seed>"
    fresh: bool = False
    state_dir: str = "company/state"
    out_dir: str | None = None               # transcript.jsonl, run.log
    issues: dict | None = None               # market issue set; None: market.default_issues()
    humans: tuple = DEFAULT_HUMANS
    approvers: tuple | None = None           # who may approve a merge (lowercase); None: realwork's default
    approver: Any = None                     # callable(rt, thread, MergeRequest) | "stub" | "mattermost" | "none"
    dev_per_week: int | None = None
    dev_per_quarter: int | None = None
    dev_timeout: float = 900.0
    approve_timeout: float = 180.0
    week_wall_s: float = 60.0
    quiet_s: float = 12.0
    review: Callable | None = None           # (pr, issue=, issue_text=) -> review dict
    merge: Callable | None = None            # (pr) -> bool
    verify: Callable | None = None           # (pr) -> {passed, summary, sha}
    dev_cost: Callable | None = None         # () -> synaps-dev's cumulative usd
    fallback: Callable | None = None         # realwork.dispatch fallback when synaps-dev is silent
    agent_prefix: str = ""                   # Switch registry name = prefix + env agent name (not synaps-dev)
    room_prefix: str = ""
    switch_url: str | None = None
    token_file: str | None = None
    devstack: str | None = None
    acme_remote: str | None = None           # local bare repo (file://...); live runs refuse anything else
    acme_prefix: str = ""                    # branch gen-k becomes <prefix>gen-k
    ledger_rows: list = field(default_factory=list)   # earlier quarters' rows, kept in the Ledger
    quarter_label: Any = None
    weights: dict | None = None
    hook: Callable | None = None             # hook(node, week) at the start of every node (tests: crash here)
    log: Callable | None = None


class QState(TypedDict, total=False):
    fingerprint: str
    env: dict
    seed: int
    generation: int
    weeks: int
    week: int
    log: dict
    deps: dict
    work: dict
    board_inbox: list
    current: dict
    weeks_done: list
    setup: dict
    fake_n: int
    metrics: dict
    fitness: float
    report: str
    ledger_row: dict
    pending_at_end: list


@dataclass
class QuarterResult:
    metrics: Metrics
    log: QuarterLog
    fitness: float
    report: str
    thread_id: str
    resumed: str                 # "new" | "resumed" | "cached"
    state: dict


def env_dict(env: Env) -> dict:
    return {"name": env.name, "parent": env.parent, "agents": env.agents, "rooms": env.rooms, "company": env.company}


def _with_weeks(env: Env, weeks) -> Env:
    e = Env(env.name, env.parent, copy.deepcopy(env.agents), copy.deepcopy(env.rooms), copy.deepcopy(env.company or {}))
    if weeks:
        e.company["weeks_per_quarter"] = int(weeks)
    return e


# ───────────────────────── runtime (live objects, rebuilt from a checkpoint) ─────────────────────────


class Runtime:
    """Everything a node needs that can't live in the checkpoint: the transport, Departments, the merge
    gate. Built on the first node this process runs, from that node's state."""

    def __init__(self, env: Env, seed: int, generation: int, cfg: QuarterConfig):
        self.env, self.seed, self.k, self.cfg = env, seed, generation, cfg
        self.weeks = int(env.company.get("weeks_per_quarter", 4))
        self.issues = cfg.issues or default_issues()
        self.market = Market(seed, env.company, issues={"incidents": self.issues["incidents"],
                                                        "features": self.issues["features"]})
        self.board, self.hub = BOARD, HUB
        self.live = cfg.transport == "switch"
        self.humans = tuple(cfg.humans)
        self.approvers = tuple(a.lower() for a in (cfg.approvers or rw.approvers_from_env()))
        self.ready = False
        self.week = 1
        self.lock = threading.RLock()
        self.gate_asked: dict[str, float] = {}
        self.t = self.deps = self.gate = self.waiter = self.judge = self.stub_dev = None
        self.approver = None
        self.base = None
        self.dev_room_name = None
        self._log_lines: list[str] = []
        self.dev_names = [a["name"] for a in env.agents if a["name"] != DEV and a.get("provider") != "synaps"]
        self.has_reviewer = "reviewer" in self.dev_names

    # logging
    def log(self, *a):
        line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
        self._log_lines.append(line)
        if self.cfg.log:
            self.cfg.log(line)
        if self.cfg.out_dir:
            with open(Path(self.cfg.out_dir) / "run.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def hook(self, node, week=None):
        if self.cfg.hook:
            self.cfg.hook(node, week)

    # names
    def reg_map(self) -> dict[str, str]:
        p = self.cfg.agent_prefix
        names = {n: p + n for n in [*self.dev_names, MARKET]}
        if BOARD not in names:
            names[BOARD] = p + BOARD
        return names

    # build / restore
    def ensure(self, state: dict):
        if self.ready:
            return
        cfg = self.cfg
        out = Path(cfg.out_dir) if cfg.out_dir else None
        if out:
            out.mkdir(parents=True, exist_ok=True)
        rooms = company_rooms(self.env, self.humans, hub=self.hub)
        self.rooms = rooms
        transcript = out / "transcript.jsonl" if out else None
        if self.live:
            from company.transport import SwitchTransport
            names = self.reg_map()
            inner = SwitchTransport(cfg.switch_url, agents=list(names.values()), state_dir=cfg.state_dir,
                                    registration_token_file=cfg.token_file, transcript_path=transcript,
                                    log=lambda *a: self.log("transport:", *a))
            self.t = CompanyTransport(inner, names)
        else:
            from company.transport import FakeTransport
            inner = FakeTransport(rooms=rooms, transcript_path=transcript, log=lambda *a: self.log("transport:", *a))
            inner._n = int(state.get("fake_n") or 0)      # message ids continue where the checkpoint left off
            self.t = CompanyTransport(inner)
        self.qlog = QuarterLog(**copy.deepcopy(state["log"])) if state.get("log") else QuarterLog()
        llm = cfg.llm or (None if self.live else stub_llm)
        self.judge = CampaignJudge(llm=llm)
        d = state.get("deps") or {}
        self.judge.cache = dict(d.get("judge_cache") or {})
        self.deps = Departments(self.env, self.t, self.qlog, llm=llm, judge=self.judge, rooms=rooms,
                                humans=self.humans, issues=self.issues, week=int(state.get("week") or 1))
        self.deps.pending = copy.deepcopy(d.get("pending") or {})
        self.deps.escalations = copy.deepcopy(d.get("escalations") or {})
        self.deps.routes = copy.deepcopy(d.get("routes") or [])
        self.deps.budget.used = dict((d.get("budget") or {}).get("used") or {})
        self.deps.budget.usd = dict((d.get("budget") or {}).get("usd") or {})
        self.deps.refused = {tuple(x) for x in d.get("refused") or []}
        self.work = copy.deepcopy(state.get("work") or {"queue": [], "done": {}, "tasks": [], "gate": {},
                                                          "routes_seen": 0, "review_usd": 0.0, "dev_usd": 0.0,
                                                          "dev_cost_seen": 0.0, "skipped": []})
        for k, v in (("held", []), ("quality", {}), ("mech", {})):
            self.work.setdefault(k, v)
        self.inbox = list(state.get("board_inbox") or [])
        self.waiter = rw.Waiter()
        merge = cfg.merge or (None if self.live else stub_merge)
        verify = cfg.verify or (None if self.live else stub_verify)
        if self.live and merge is None:
            from company.board.live import local_merge
            merge = local_merge
        self.gate = rw.MergeGate(self.t, board=self.board, room=self.hub, approvers=self.approvers,
                                 week=lambda: self.week, merge=merge, verify=verify, log=lambda *a: self.log(*a))
        for tid, req in (self.work.get("gate") or {}).items():
            self.gate.pending[tid] = rw.MergeRequest(**req)
        self.t.on_message(self.board, self.board_handler)
        for n in self.dev_names:            # a direct line: synaps-dev answers the dept agent that asked
            if n != self.board:
                self.t.on_message(n, self.dev_reply)
        if not self.live:
            self.stub_dev = StubDev(self.t)
            self.stub_dev.spent = float(self.work.get("dev_cost_seen") or 0.0)   # cumulative, like a session
        self.t.start()
        setup = state.get("setup") or {}
        if setup.get("rooms") and self.live:
            self.t.pin_rooms(setup["rooms"])
        self.base = setup.get("base")
        self.dev_room_name = setup.get("dev_room_name")
        self._make_approver(setup)
        self.ready = True

    def _make_approver(self, setup):
        a = self.cfg.approver
        if callable(a):
            self.approver = a
        elif a in (None, "stub") and not self.live:
            self.approver = StubApprover(human=self.humans[-1] if self.humans else "Haseeb")
        elif a == "mattermost" and self.live and setup.get("channels"):
            from company.board.live import MattermostApprover, devstack_dir
            self.approver = MattermostApprover(devstack_dir(self.cfg.devstack), setup["channels"][self.hub],
                                               human=self.humans[0], log=self.log)
        else:
            self.approver = None

    def close(self):
        if self.t is not None:
            try:
                self.t.stop()
            except Exception as e:  # noqa: BLE001
                self.log(f"transport stop: {e}")

    # the board's own handler: record, hand replies to the waiter, approvals to the gate
    def board_handler(self, msg):
        with self.lock:
            self.inbox.append({"week": self.week, "id": msg.id, "room": msg.room, "thread": msg.thread,
                               "sender": msg.sender, "agent": msg.sender_is_agent, "text": msg.text[:300]})
            del self.inbox[:-INBOX_KEEP]
        if msg.room == self.hub and (msg.thread or "") in self.gate.pending:
            self.gate.handle(msg)
            return
        self.waiter.offer(msg)

    def dev_reply(self, msg):
        """synaps-dev's RESULT to a department agent that handed it work over a direct line: the board's
        work loop is waiting for it (realwork.dispatch), exactly as for its own dispatches."""
        if msg.sender == DEV:
            self.waiter.offer(msg)

    def mech_count(self, key, n=1):
        m = self.work.setdefault("mech", {})
        m[key] = m.get(key, 0) + n

    def explain(self, type_, what, **kw):
        """One mechanics decision, with the human-readable reason the board and the demo read back."""
        self.deps.decide(type=type_, who=kw.pop("who", self.board), what=what, **kw)
        self.log(f"week {self.week}: {what}")

    def post_board(self, text, thread=None):
        return self.t.post(self.board, self.hub, text, thread=thread)

    # costs
    def book(self, who, usd, kind):
        usd = round(float(usd or 0.0), 6)
        if not usd:
            return
        with self.deps.lock:
            self.qlog.llm_cost_usd = round(self.qlog.llm_cost_usd + usd, 6)
        self.work[kind] = round(self.work.get(kind, 0.0) + usd, 6)

    def dev_cost(self) -> float:
        if self.cfg.dev_cost:
            return float(self.cfg.dev_cost() or 0.0)
        if self.stub_dev is not None:
            return self.stub_dev.cost()
        if self.live and self.dev_room_name:
            from company.board.live import SessionCost
            return SessionCost(self.dev_room_name)()
        return 0.0

    def book_dev_cost(self):
        """synaps-dev's spend since the last look (its room session's cumulative cost)."""
        now = self.dev_cost()
        delta = round(now - self.work.get("dev_cost_seen", 0.0), 6)
        if delta > 0:
            self.work["dev_cost_seen"] = now
            self.book(DEV, delta, "dev_usd")
        return delta

    # checkpoint
    def snapshot(self) -> dict:
        d = self.deps
        with d.lock:
            deps = {"pending": copy.deepcopy(d.pending), "escalations": copy.deepcopy(d.escalations),
                    "routes": copy.deepcopy(d.routes),
                    "budget": {"used": dict(d.budget.used), "usd": dict(d.budget.usd)},
                    "refused": sorted([list(x) for x in d.refused]), "judge_cache": dict(self.judge.cache)}
            log = copy.deepcopy(asdict(self.qlog))
        with self.lock:
            inbox = list(self.inbox)
        snap = {"log": log, "deps": deps, "work": copy.deepcopy(self.work), "board_inbox": inbox}
        if not self.live:
            snap["fake_n"] = self.t.inner._n
        return snap

    # ── dev queue ──
    def issue_info(self, n):
        for i in self.issues.get("incidents", []):
            if i["issue"] == n:
                return {"kind": "incident", "title": i["title"], "severity": i["severity"]}
        for f in self.issues.get("features", []):
            if f["issue"] == n:
                return {"kind": "feature", "title": f["title"], "demand": f["demand"]}
        return None

    def queue_routes(self):
        w = self.work
        routes = self.deps.routes
        queued = {q["issue"] for q in w["queue"]} | {int(k) for k in w["done"]}
        for r in routes[w.get("routes_seen", 0):]:
            n = int(r["issue"])
            info = self.issue_info(n)
            if r.get("to") != DEV or info is None:
                why = "not routed to synaps-dev" if r.get("to") != DEV else "not an acme-shop issue in this market"
                w["skipped"].append({"issue": n, "by": r.get("by"), "week": r.get("week"), "why": why})
                self.deps.decide(type="route_skipped", issue=n, by=r.get("by"), to=r.get("to"), why=why)
                continue
            plan = mech.route_plan(self.env, r.get("by") or self.board, info, hub=self.hub, members=self.rooms)
            ready = int(r["week"]) + plan["delay"]
            if n in queued:
                q = next((x for x in w["queue"] if x["issue"] == n), None)
                if q is not None and ready < q["ready"]:      # a second, faster route for the same issue
                    q.update(ready=ready, path=plan["path"], asker=plan["asker"], by=r.get("by"),
                             origin=plan["room"], msg=r.get("msg"))
                continue
            queued.add(n)
            w["queue"].append({"issue": n, "week": r["week"], "by": r.get("by"), "msg": r.get("msg"), **info,
                               "ready": ready, "path": plan["path"], "asker": plan["asker"], "origin": plan["room"]})
            tag = f"sev{info['severity']}" if info["kind"] == "incident" else f"demand {info['demand']:.1f}"
            self.explain("route_direct" if plan["path"] == "direct" else "route_relay",
                         f"#{n} ({tag}) raised wk{r['week']} by {r.get('by')} in #{plan['room']}: {plan['why']}"
                         + ("" if plan["path"] == "direct" or not plan["delay"] else f" (wk{ready})"),
                         issue=n, by=r.get("by"), path=plan["path"], ready_week=ready)
        w["routes_seen"] = len(routes)

        def prio(q):
            return (0, q["severity"], q["week"], q["issue"]) if q["kind"] == "incident" else \
                (1, -q.get("demand", 0), q["week"], q["issue"])
        w["queue"].sort(key=prio)

    def caps(self):
        return mech.capacity(self.env.company, self.cfg.dev_per_week, self.cfg.dev_per_quarter)

    def capacity(self, done_this_week):
        """synaps-dev's capacity: QuarterConfig.dev_per_week/_quarter, else x-company.dev_capacity
        {per_week, per_quarter}, else unlimited."""
        per_week, per_q = self.caps()
        if per_week is not None and done_this_week >= int(per_week):
            return False
        if per_q is not None and len(self.work["tasks"]) >= int(per_q):
            return False
        return True

    def dispatch_week(self, week) -> int:
        """Dispatch what is ready this week (routed, relayed), in priority order, up to synaps-dev's capacity."""
        n = 0
        for q in list(self.work["queue"]):
            if q.get("ready", q["week"]) > week:
                continue
            if not self.capacity(n):
                break
            self.work["queue"].remove(q)
            self.run_task(q, week)
            n += 1
        return n

    def dispatch_reason(self, q, week) -> str:
        n, ready = q["issue"], q.get("ready", q["week"])
        tag = f"sev{q['severity']}" if q["kind"] == "incident" else f"demand {q['demand']:.1f}"
        relay, wait = ready - int(q["week"]), week - ready
        if q.get("path") == "direct":
            why = f"#{n} ({tag}) routed direct by {q['by']} (#{q.get('origin')} ↔ #dev): raised wk{q['week']}, " \
                  f"dispatched wk{week}"
        elif relay:
            why = f"#{n} ({tag}) waited for board relay: raised wk{q['week']} by {q['by']} in #{q.get('origin')}, " \
                  f"dispatched wk{week}"
        else:
            why = f"#{n} ({tag}) dispatched by the board: raised wk{q['week']}, dispatched wk{week}"
        if wait > 0:
            why += f" (+{wait} wk waiting for dev capacity, {mech.cap_text(*self.caps())})"
        return why

    def run_task(self, q, week):
        n, kind = q["issue"], q["kind"]
        tag = f"sev{q['severity']}" if kind == "incident" else f"demand {q['demand']:.1f}"
        path, asker = q.get("path", "board_relay"), q.get("asker") or self.board
        ready = q.get("ready", q["week"])
        if path == "direct":
            self.mech_count("direct")
        else:
            self.mech_count("relayed")
            self.mech_count("relay_weeks", ready - int(q["week"]))
        self.mech_count("capacity_weeks", week - ready)
        self.explain("dispatch", self.dispatch_reason(q, week), who=asker, issue=n, kind=kind, path=path,
                     raised_week=q["week"], ready_week=ready)
        if path == "direct":
            self.log(f"week {week}: {asker} hands acme-shop#{n} to synaps-dev in #dev over the direct line")
        else:
            self.post_board(f"Relaying acme-shop#{n} ({kind}, {tag}: {q['title']}), raised by {q['by']} in "
                            f"#{q.get('origin') or self.hub} in week {q['week']}, to synaps-dev in #dev.")
        task = rw.DevTask(n, kind, base=self.base or f"gen-{self.k}", asker=asker, dev=DEV, room="dev",
                          title=q["title"])
        self.log(f"week {week}: dispatch acme-shop#{n} ({kind}) on {task.base} as {asker}")
        fallback = self.cfg.fallback
        if fallback is None:
            from company.board.live import no_fallback
            fallback = no_fallback
        res = rw.dispatch(self.t, task, self.waiter, timeout=self.cfg.dev_timeout if self.live else 5.0,
                          fallback=fallback)
        self.book_dev_cost()
        if res.get("via") == "devworker" and getattr(fallback, "__name__", "") == "no_fallback":
            res["via"] = "timeout"             # nobody did the work: realwork labels its fallback path "devworker"
        if res.get("via") == "devworker":
            self.book(DEV, res.get("cost_usd", 0.0), "dev_usd")
        rec = {"issue": n, "kind": kind, "week": week, "by": q["by"], "status": res["status"], "via": res.get("via"),
               "pr": res.get("pr"), "summary": res.get("summary", "")[:300], "thread": res.get("thread"),
               "seconds": res.get("seconds"), "path": path, "asker": asker}
        self.work["tasks"].append(rec)
        self.work["done"][str(n)] = rec
        self.log(f"week {week}: RESULT acme-shop#{n} via {res.get('via')} after {res.get('seconds')}s: "
                 f"{res['status']} pr={res.get('pr')}")
        if res["status"] != "proposed" or not res.get("pr"):
            rec["outcome"] = "no_pr"
            self.deps.decide(type="dev_task", issue=n, kind=kind, status=res["status"], outcome="no_pr")
            return rec
        review_fn = self.cfg.review or (None if self.live else stub_review)
        if review_fn is None:
            from company.board.live import live_review
            review_fn = live_review({i["issue"]: i["title"] for i in [*self.issues["incidents"], *self.issues["features"]]})
        try:
            review = review_fn(res["pr"], issue=n, issue_text=q["title"])
        except Exception as e:  # noqa: BLE001 - a failed review is NEEDS WORK, never a merge
            self.book("reviewer", getattr(e, "cost_usd", 0.0), "review_usd")
            review = {"verdict": "NEEDS WORK", "summary": f"review failed: {type(e).__name__}: {e}"[:200],
                      "concerns": ["the review did not run"], "tests": {"passed": False, "summary": "not run"},
                      "files": [], "cost_usd": 0.0}
        self.book("reviewer", review.get("cost_usd", 0.0), "review_usd")
        rec["review"] = {"verdict": review["verdict"], "tests": (review.get("tests") or {}).get("summary"),
                         "files": review.get("files"), "concerns": review.get("concerns", [])[:5]}
        text = rw.review_text(review, res["pr"], board=self.board, dev=DEV)
        text = "\n".join(l for l in text.splitlines() if not l.startswith(f"@{DEV} "))   # no rework loop
        self.t.post("reviewer" if self.has_reviewer else self.board, "dev", text, thread=res.get("thread"))
        review = self.review_rounds(q, res, review, review_fn, week)
        req = rw.MergeRequest(n, kind, res["pr"], res.get("summary", ""), review, res.get("evidence") or [])
        slip = (self.work["quality"].get(str(n)) or {}).get("slip", 0)
        if slip:
            self.work["held"].append({"req": asdict(req), "week": week + slip})
            rec["outcome"] = "extra_review"
            self.mech_count("slipped")
            self.explain("review_slip", f"#{n}: another review round in #dev; the merge slips to wk{week + slip}",
                         who=", ".join(self.work["quality"][str(n)]["testers"][1:]), issue=n, merge_week=week + slip)
            return rec
        return self.ask_merge(req, rec, week)

    def review_rounds(self, q, res, review, review_fn, week):
        """Testers in #dev (mechanics.quality): latent regression caught or not, extra review rounds.
        Returns the review the merge request carries (the worst verdict of all rounds)."""
        n, kind = q["issue"], q["kind"]
        ql = mech.quality(self.env, self.seed, n, kind)
        self.work["quality"][str(n)] = ql
        tst = ql["testers"]
        for t in tst[1:]:                         # every tester beyond the first: another real review round
            try:
                extra = review_fn(res["pr"], issue=n, issue_text=q["title"])
            except Exception as e:  # noqa: BLE001
                self.book(t, getattr(e, "cost_usd", 0.0), "review_usd")
                extra = {"verdict": "NEEDS WORK", "summary": f"review failed: {type(e).__name__}"[:200],
                         "tests": {"passed": False, "summary": "not run"}, "cost_usd": 0.0}
            self.book(t, extra.get("cost_usd", 0.0), "review_usd")
            self.mech_count("review_rounds")
            if t in self.rooms.get("dev", ()):
                self.t.post(t, "dev", f"{t} review of {res['pr']}: {extra.get('verdict')} — "
                                      f"{str(extra.get('summary', ''))[:200]}", thread=res.get("thread"))
            if extra.get("verdict") != "PASS" and review.get("verdict") == "PASS":
                review = {**extra, "files": review.get("files"), "concerns": extra.get("concerns") or [f"{t}: NEEDS WORK"]}
        if ql["caught_by"]:
            self.mech_count("caught")
            self.explain("regression_caught", f"#{n}: {ql['caught_by']} caught a regression in review before the "
                         f"merge (synaps-dev fixed it on the branch)", who=ql["caught_by"], issue=n)
        return review

    def ask_merge(self, req, rec, week):
        """#board's merge gate for a reviewed PR: ask, let the person decide, book the decision."""
        n = req.issue
        tid = self.gate.ask(req)
        self.gate_asked[tid] = time.time()
        self.work["gate"][tid] = asdict(req)
        rec["gate_thread"] = tid
        decision = None
        if self.approver is not None:
            try:
                decision = self.approver(self, tid, req)
            except Exception as e:  # noqa: BLE001
                self.log(f"approver failed: {type(e).__name__}: {e}")
            recs = getattr(self.approver, "decisions", None)
            self.work.setdefault("approvals", []).append(
                recs[-1] if recs and recs[-1].get("issue") == n else
                {"issue": n, "decision": decision, "as": type(self.approver).__name__, "scripted": True})
        dec = self.gate.wait(tid, timeout=self.cfg.approve_timeout if (decision and self.live) else 0.5)
        if dec:
            self.book_decision(tid, dec)
        else:
            rec["outcome"] = "awaiting_approval"
            self.log(f"week {week}: acme-shop#{n} awaits a #board decision")
        return rec

    def release_held(self, week):
        """PRs whose extra review round ends this week go to the #board merge gate now."""
        for h in list(self.work.get("held") or []):
            if h["week"] <= week:
                self.work["held"].remove(h)
                req = rw.MergeRequest(**h["req"])
                rec = self.work["done"].get(str(req.issue)) or {"issue": req.issue}
                rec["outcome"] = None
                self.ask_merge(req, rec, week)

    def regress(self, dec):
        """A merged incident fix whose latent regression nobody caught: customers hit it again, the incident
        reopens, and the re-fix lands REOPEN_WEEKS later (past the quarter: still open at quarter end)."""
        n = dec["issue"]
        ql = self.work["quality"].get(str(n)) or {}
        if dec.get("kind") != "incident" or not dec.get("merged") or not ql.get("latent") or ql.get("caught_by"):
            return
        fix = next((f for f in self.qlog.fixes if f["issue"] == n), None)
        if fix is None:
            return
        merged, refix = fix["week_fixed"], fix["week_fixed"] + mech.REOPEN_WEEKS
        self.mech_count("reopened")
        who = ", ".join(ql.get("testers") or []) or "nobody"
        if refix > self.weeks:
            self.qlog.fixes.remove(fix)
            what = (f"#{n}: fix regressed after the wk{merged} merge (review by {who} missed it); reopened, "
                    f"not re-fixed this quarter")
        else:
            fix["week_fixed"] = refix
            what = (f"#{n}: fix regressed after the wk{merged} merge (review by {who} missed it); reopened, "
                    f"re-fix lands wk{refix}")
        self.explain("regression", what, who="market", issue=n, merged_week=merged, refix_week=refix)

    def book_decision(self, tid, dec):
        req = self.work["gate"].pop(tid, None)
        with self.deps.lock:
            rw.record_into(self.qlog, dec)
        rec = self.work["done"].get(str(dec["issue"]))
        outcome = "merged" if dec.get("merged") else ("rejected" if dec["decision"] != "approve" else "merge_failed")
        if rec is not None:
            rec.update(outcome=outcome, approver=dec.get("approver"), merged_week=dec.get("week"),
                       tests_passed=dec.get("tests_passed"))
            for t in self.work["tasks"]:
                if t["issue"] == dec["issue"]:
                    t.update(outcome=outcome, approver=dec.get("approver"), merged_week=dec.get("week"),
                             tests_passed=dec.get("tests_passed"))
        self.deps.decide(type="merge", issue=dec["issue"], kind=dec["kind"], decision=dec["decision"],
                         approver=dec.get("approver"), merged=dec.get("merged"), tests_passed=dec.get("tests_passed"),
                         pr=dec.get("pr_url"))
        self.log(f"merge decision acme-shop#{dec['issue']}: {dec['decision']} by {dec.get('approver')}, "
                 f"merged={dec.get('merged')} tests_passed={dec.get('tests_passed')}")
        self.regress(dec)
        return req

    def market_inputs(self):
        """What the policies and budgets let through to Market.settle, as one readable line."""
        lg = self.qlog
        camps = lg.campaigns
        spent = {}
        for x in lg.spend:
            if x.get("approved", True):
                spent[x.get("dept", "?")] = spent.get(x.get("dept", "?"), 0.0) + float(x.get("usd") or 0)
        verdicts = [d for d in lg.decisions if d.get("type") == "verdict"]
        denied = sum(1 for d in verdicts if d.get("status") == "denied")
        price = lg.price_changes[-1]["price"] if lg.price_changes else self.env.company.get("price")
        q = sum(float(c.get("quality", 0.5)) for c in camps) / len(camps) if camps else 0.0
        what = (f"to the market: {len(camps)} campaign(s), ${sum(float(c.get('spend') or 0) for c in camps):,.0f} "
                f"spend, mean quality {q:.2f}, {sum(1 for c in camps if c.get('cites_feature'))} citing a shipped "
                f"feature; approved spend " + (", ".join(f"{k} ${v:,.0f}" for k, v in sorted(spent.items())) or "none")
                + f"; {len(verdicts) - denied} item(s) approved, {denied} denied by finance/policy; "
                f"price ${float(price or 0):.2f}; "
                f"fixes {sorted((f['issue'], f['week_fixed']) for f in lg.fixes)}, "
                f"features {sorted((f['issue'], f['week_shipped']) for f in lg.features)}")
        self.explain("market_inputs", what, who="policy")

    def collect_gate(self):
        for tid in list(self.work.get("gate") or {}):
            dec = self.gate.decided.get(tid)
            if dec:
                self.book_decision(tid, dec)

    # ── quarter end ──
    def expire(self):
        """Everything still pending counts as not approved, and is logged as such."""
        out = []
        d = self.deps
        for it in list(d.pending.values()):
            if it["status"] in ("pending", "escalated"):
                was = it["status"]
                d.settle(it, False, by="quarter_end", why=f"still {was} at quarter end: not approved",
                         status="not_approved")
                out.append({"what": it["kind"], "id": it["id"], "by": it["by"], "was": was,
                            "action": it["action"]})
        for tid, req in list((self.work.get("gate") or {}).items()):
            d.decide(type="merge_not_approved", issue=req["issue"], kind=req["kind"], pr=req["pr"],
                     why="no #board decision by quarter end")
            rec = self.work["done"].get(str(req["issue"]))
            if rec is not None:
                rec["outcome"] = "not_approved"
            for t in self.work["tasks"]:
                if t["issue"] == req["issue"] and t.get("outcome") == "awaiting_approval":
                    t["outcome"] = "not_approved"
            out.append({"what": "merge", "issue": req["issue"], "pr": req["pr"], "was": "awaiting approval"})
        self.work["gate"] = {}
        for h in self.work.get("held") or []:
            req = h["req"]
            d.decide(type="merge_not_approved", issue=req["issue"], kind=req["kind"], pr=req["pr"],
                     why="its extra review round ran past the quarter", who="qa",
                     what=f"#{req['issue']}: the extra review round ran past the quarter (merge wk{h['week']}): not merged")
            out.append({"what": "merge", "issue": req["issue"], "pr": req["pr"], "was": "in review"})
        self.work["held"] = []
        for q in self.work.get("queue") or []:
            ready = q.get("ready", q["week"])
            how = (f"board relay → wk{ready}" if q.get("path") == "board_relay" and ready > int(q["week"])
                   else "routed direct" if q.get("path") == "direct" else "queued")
            d.decide(type="route_not_dispatched", issue=q["issue"], kind=q["kind"], by=q["by"],
                     why="the dev queue did not reach it this quarter", who=self.board,
                     what=f"#{q['issue']} ({q['kind']}) never reached synaps-dev: raised wk{q['week']} by {q['by']}, "
                          f"{how}, then dev capacity ({mech.cap_text(*self.caps())}) ran out")
            self.mech_count("not_dispatched")
            out.append({"what": "route", "issue": q["issue"], "kind": q["kind"], "was": "queued"})
        return out


# ───────────────────────── report ─────────────────────────


def _money(x):
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def kpi_table(env_name, seed, generation, weeks, m: Metrics, fit: float, qlog: QuarterLog, work: dict,
              pending: list, label=None) -> str:
    fixes = ", ".join(f"#{f['issue']} (wk {f['week_fixed']}{', tests green' if f.get('tests_passed') else ''})"
                      for f in qlog.fixes) or "none"
    feats = ", ".join(f"#{f['issue']} (wk {f['week_shipped']})" for f in qlog.features) or "none"
    camp = qlog.campaigns
    spend_ok = sum(s["usd"] for s in qlog.spend if s.get("approved"))
    spend_no = sum(s["usd"] for s in qlog.spend if not s.get("approved"))
    price = f"${qlog.price_changes[-1]['price']:.2f} (wk {qlog.price_changes[-1]['week']})" if qlog.price_changes \
        else "unchanged"
    rev, dev = work.get("review_usd", 0.0), work.get("dev_usd", 0.0)
    rest = round(qlog.llm_cost_usd - rev - dev, 6)
    rows = [("revenue", _money(m.revenue)), ("costs", _money(m.costs)), ("profit", _money(m.profit)),
            ("customers", f"{m.customers:,}"), ("churn (avg weekly)", f"{m.churn_rate:.2%}"),
            ("growth", f"{m.growth:+.2%}"), ("open incident severity", f"{m.open_incident_severity:g}"),
            ("mean fix weeks", f"{m.mean_fix_weeks:.2f}"),
            ("LLM cost", f"${m.llm_cost_usd:.4f} (depts + judge ${rest:.4f}, reviews ${rev:.4f}, synaps-dev ${dev:.4f})"),
            ("incidents fixed", fixes), ("features shipped", feats),
            ("campaigns", f"{len(camp)} (${sum(c['spend'] for c in camp):,.0f}, avg quality "
                          f"{(sum(c['quality'] for c in camp) / len(camp)) if camp else 0:.2f})"),
            ("price", price), ("spend approved / not", f"${spend_ok:,.0f} / ${spend_no:,.0f}"),
            ("not approved at quarter end", str(len(pending))),
            ("**fitness**", f"**{fit:.4f}**")]
    head = f"Quarter {label if label is not None else 1} of {env_name}, generation {generation}, seed {seed}, {weeks} weeks"
    return "\n".join([f"**{head}: KPI**", "", "| KPI | value |", "|---|---|", *[f"| {k} | {v} |" for k, v in rows]])


def ledger_row(label, seed, m: Metrics, fit: float) -> dict:
    return {"quarter": label, "seed": seed, **{k: getattr(m, k) for k in (
        "revenue", "costs", "profit", "customers", "churn_rate", "growth", "open_incident_severity",
        "mean_fix_weeks", "llm_cost_usd")}, "fitness": round(fit, 4)}


# ───────────────────────── graph ─────────────────────────


def build_graph(rt: Runtime):
    from langgraph.graph import END, START, StateGraph

    def open_quarter(state: QState):
        rt.hook("open_quarter", None)
        rt.ensure(state)
        setup = dict(state.get("setup") or {})
        if rt.live and not setup.get("rooms"):
            setup.update(live_setup(rt))
            rt.base = setup["base"]
            rt.dev_room_name = setup.get("dev_room_name")
            rt._make_approver(setup)
        elif not setup:
            setup = {"base": f"{rt.cfg.acme_prefix}gen-{rt.k}", "rooms": None}
            rt.base = setup["base"]
        rt.post_board(f"Quarter {rt.cfg.quarter_label or 1} opens: {rt.env.name} generation {rt.k}, seed {rt.seed}, "
                      f"{rt.weeks} weeks. Engineering base branch {rt.base}. The Handbook and the Ledger are in this room.")
        if not state.get("setup"):
            rt.explain("org", mech.org_text(rt.env, *rt.caps(), hub=rt.hub))
        rt.log(f"open_quarter: {rt.env.name} g{rt.k} seed {rt.seed}, {rt.weeks} weeks, base {rt.base}")
        return {**rt.snapshot(), "setup": setup, "week": state.get("week") or 1}

    def operate(state: QState):
        w = state["week"]
        rt.hook("operate", w)
        rt.ensure(state)
        rt.week = rt.deps.week = w
        events = []
        for ev in rt.market.events_for_week(w):
            room, text = rt.deps.event_message(ev)
            if room not in rt.rooms:            # an evolved env without that room: the hub hears it
                room = rt.hub
            mid = rt.t.post(MARKET, room, text)
            events.append({"room": room, "kind": ev.kind, "payload": ev.payload, "id": mid})
            rt.log(f"week {w}: market -> #{room}: {text[:110]}")
        quiet, waited = (True, 0.0)
        if rt.live:
            quiet, waited = rt.t.wait_quiet(rt.cfg.quiet_s, rt.cfg.week_wall_s)
        rt.log(f"week {w}: {len(events)} events; rooms {'quiet' if quiet else 'still busy'} after {waited}s; "
               f"llm ${rt.qlog.llm_cost_usd:.4f}; routes {len(rt.deps.routes)}")
        return {**rt.snapshot(), "current": {"week": w, "events": events, "quiet": quiet, "waited_s": waited}}

    def work(state: QState):
        w = state["week"]
        rt.hook("work", w)
        rt.ensure(state)
        rt.week = rt.deps.week = w
        rt.collect_gate()
        rt.release_held(w)
        rt.queue_routes()
        n = rt.dispatch_week(w)
        cur = dict(state.get("current") or {})
        cur["dispatched"] = n
        return {**rt.snapshot(), "current": cur}

    def close_week(state: QState):
        w = state["week"]
        rt.hook("close_week", w)
        rt.ensure(state)
        rt.week = rt.deps.week = w
        rt.collect_gate()
        rt.book_dev_cost()
        q = rt.qlog
        fixed = [f"#{f['issue']}" for f in q.fixes if f["week_fixed"] == w]
        shipped = [f"#{f['issue']}" for f in q.features if f["week_shipped"] == w]
        camps = [c for c in q.campaigns if c["week"] == w]
        summary = {"week": w, "events": len((state.get("current") or {}).get("events") or []),
                   "dispatched": (state.get("current") or {}).get("dispatched", 0), "fixed": fixed,
                   "shipped": shipped, "campaigns": len(camps), "price": rt.deps.price(),
                   "queue": [x["issue"] for x in rt.work["queue"]], "llm_cost_usd": q.llm_cost_usd}
        rt.post_board(f"Week {w} of {rt.weeks} closed. Fixed: {', '.join(fixed) or 'none'}; shipped: "
                      f"{', '.join(shipped) or 'none'}; campaigns: {len(camps)}; price ${rt.deps.price():.2f}; "
                      f"dev queue: {', '.join('#%d' % x for x in summary['queue']) or 'empty'}; "
                      f"LLM spend so far ${q.llm_cost_usd:.4f}.")
        return {**rt.snapshot(), "week": w + 1, "weeks_done": [*(state.get("weeks_done") or []), summary]}

    def settle(state: QState):
        rt.hook("settle", None)
        rt.ensure(state)
        rt.week = rt.deps.week = rt.weeks
        rt.collect_gate()
        pending = rt.expire()
        if rt.live and rt.work["tasks"]:        # synaps-dev's last turn may still be saving
            for _ in range(10):
                time.sleep(3)
                if not rt.book_dev_cost():
                    break
        rt.book_dev_cost()
        stats = {k: rt.work["mech"].get(k, 0) for k in mech.STATS}
        rt.explain("mechanics", "quarter mechanics: " + (mech.stats_text(stats) or "nothing waited, nothing reopened"),
                   stats=stats, labels=dict(mech.STATS))
        rt.market_inputs()
        m = rt.market.settle(rt.qlog)
        fit = fitness_of(m, rt.cfg.weights or load_weights())
        rt.log(f"settle: {m}; fitness {fit:.4f}; not approved at quarter end: {len(pending)}")
        return {**rt.snapshot(), "metrics": asdict(m), "fitness": fit, "pending_at_end": pending}

    def report(state: QState):
        rt.hook("report", None)
        rt.ensure(state)
        m = Metrics(**state["metrics"])
        label = rt.cfg.quarter_label if rt.cfg.quarter_label is not None else 1
        table = kpi_table(rt.env.name, rt.seed, rt.k, rt.weeks, m, state["fitness"], rt.qlog, rt.work,
                          state.get("pending_at_end") or [], label)
        rt.post_board(table)
        row = ledger_row(label, rt.seed, m, state["fitness"])
        write_ledger(rt, [*rt.cfg.ledger_rows, row])
        return {"report": table, "ledger_row": row}

    def after_week(state: QState):
        return "operate" if state["week"] <= rt.weeks else "settle"

    g = StateGraph(QState)
    for name, fn in (("open_quarter", open_quarter), ("operate", operate), ("work", work),
                     ("close_week", close_week), ("settle", settle), ("report", report)):
        g.add_node(name, fn)
    g.add_edge(START, "open_quarter")
    g.add_edge("open_quarter", "operate")
    g.add_edge("operate", "work")
    g.add_edge("work", "close_week")
    g.add_conditional_edges("close_week", after_week, {"operate": "operate", "settle": "settle"})
    g.add_edge("settle", "report")
    g.add_edge("report", END)
    return g


def write_ledger(rt: Runtime, rows):
    from company.switch_env import ledger_markdown, update_ledger
    if rt.live:
        update_ledger(rt.env, rt.t.link(rt.board), rows, generation=rt.k, room_prefix=rt.cfg.room_prefix,
                      state_dir=rt.cfg.state_dir)
        rt.log(f"report: Ledger of {rt.cfg.room_prefix}{rt.env.name}-g{rt.k} updated ({len(rows)} row(s))")
        return
    p = Path(rt.cfg.state_dir) / "ledger" / f"{rt.cfg.room_prefix}{rt.env.name}-g{rt.k}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ledger_markdown(rt.env, rt.k, rows))
    rt.log(f"report: Ledger written to {p}")


# ───────────────────────── live setup ─────────────────────────


def live_setup(rt: Runtime) -> dict:
    """Switch: every agent registered (SwitchTransport.start did), synaps-dev present, the generation's rooms
    applied on the board's link with the extra members, rooms pinned, acme-shop gen-k cut on the local remote."""
    from company import acme
    from company.board.live import devstack_dir
    from company.switch_env import apply_env, devstack_fixup, load_devstack, load_state

    cfg = rt.cfg
    names = rt.reg_map()
    link = rt.t.link(rt.board)
    found = {a.get("name") for a in link.op("list_agents", {"name_contains": DEV}) or []}
    if DEV not in found:
        raise RuntimeError(f"{DEV} is not registered in Switch: it is the Synaps extension's agent, start it first")
    amap = {a["name"]: names.get(a["name"], a["name"]) for a in rt.env.agents}
    extra = extra_members(rt.env, rt.rooms, lambda a: names.get(a, a))
    d = devstack_dir(cfg.devstack)
    dev = load_devstack(d) if d else None
    fix = devstack_fixup(dev, lambda *a: rt.log("apply:", *a)) if dev else None

    def fixup(r, detail):                  # Mattermost membership is best effort (its adapter is flaky)
        try:
            fix(r, detail)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - dev.die raises SystemExit
            rt.log(f"apply: Mattermost fixup of {r['switch_name']} failed: {type(e).__name__}: {e}")
    rooms = apply_env(rt.env, link, generation=rt.k, agent_names=amap, room_prefix=cfg.room_prefix,
                      state_dir=cfg.state_dir, log=lambda *a: rt.log("apply:", *a), fixup=fixup if fix else None,
                      extra_members=extra)
    rt.t.pin_rooms(rooms)
    st = load_state(cfg.state_dir, f"{cfg.room_prefix}{rt.env.name}-g{rt.k}") or {}
    channels = {}
    if dev is not None:
        e = dev.env()
        for name, r in (st.get("rooms") or {}).items():
            if r.get("channel_id"):
                try:
                    channels[name] = dev.mm(e, "GET", f"/channels/{r['channel_id']}", who="admin")["name"]
                except (Exception, SystemExit):  # noqa: BLE001
                    rt.log(f"apply: no Mattermost channel name for {name}")
    remote = cfg.acme_remote or os.environ.get("ACME_REMOTE")
    acfg = acme.config(remote=remote) if remote else acme.config()
    if not acfg.local:
        raise acme.GitHubWriteRefused("the live quarter runs local-remote only: set acme_remote / ACME_REMOTE "
                                      "to the local bare clone (file://...)")
    base = acme.start_generation(rt.k, acfg, prefix=cfg.acme_prefix)
    rt.log(f"apply: rooms {json.dumps(rooms)}; Mattermost {json.dumps(channels)}; acme-shop base {base} on {acfg.remote}")
    dev_room = (st.get("rooms") or {}).get("dev", {}).get("switch_name") or f"{cfg.room_prefix}dev-g{rt.k}"
    return {"rooms": rooms, "channels": channels, "base": base, "remote": acfg.remote, "dev_room_name": dev_room,
            "agents": names, "group": f"{cfg.room_prefix}{rt.env.name}-g{rt.k}"}


# ───────────────────────── entry points ─────────────────────────


def fingerprint(env: Env, seed, generation, cfg: QuarterConfig, issues) -> str:
    blob = json.dumps({"env": env_dict(env), "seed": seed, "generation": generation, "transport": cfg.transport,
                       "llm": cfg.llm_label or ("stub" if (cfg.llm is None and cfg.transport == "fake") else "custom"),
                       "issues": issues, "agent_prefix": cfg.agent_prefix, "room_prefix": cfg.room_prefix,
                       "acme_prefix": cfg.acme_prefix, "caps": [cfg.dev_per_week, cfg.dev_per_quarter],
                       "mechanics": mech.MECHANICS_VERSION,
                       "label": cfg.quarter_label}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def checkpointer(db):
    from langgraph.checkpoint.sqlite import SqliteSaver
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(db), check_same_thread=False))


def run_quarter_full(env: Env, *, seed: int, generation: int = 1, config: QuarterConfig | None = None,
                     **kw) -> QuarterResult:
    cfg = config or QuarterConfig()
    for k, v in kw.items():
        if not hasattr(cfg, k):
            raise TypeError(f"run_quarter: unknown option {k!r}")
        setattr(cfg, k, v)
    env = _with_weeks(env, cfg.weeks)
    issues = cfg.issues or default_issues()
    cfg.issues = issues
    tid = cfg.thread_id or f"{env.name}:{seed}"
    fp = fingerprint(env, seed, generation, cfg, issues)
    saver = checkpointer(cfg.db)
    rt = Runtime(env, seed, generation, cfg)
    graph = build_graph(rt).compile(checkpointer=saver)
    conf = {"configurable": {"thread_id": tid}, "recursion_limit": 20 + 5 * rt.weeks}
    try:
        snap = graph.get_state(conf)
        if snap.values and (cfg.fresh or snap.values.get("fingerprint") != fp):
            rt.log(f"thread {tid}: {'fresh run requested' if cfg.fresh else 'env/config changed'}; starting over")
            saver.delete_thread(tid)
            snap = graph.get_state(conf)
        if snap.values and not snap.next:
            how, final = "cached", snap.values
            rt.log(f"thread {tid}: already finished; returning the stored result")
        elif snap.values:
            how = "resumed"
            rt.log(f"thread {tid}: resuming at {list(snap.next)} (week {snap.values.get('week')})")
            final = graph.invoke(None, conf, durability="sync")
        else:
            how = "new"
            init = {"fingerprint": fp, "env": env_dict(env), "seed": seed, "generation": generation,
                    "weeks": rt.weeks, "week": 1, "weeks_done": [], "board_inbox": []}
            final = graph.invoke(init, conf, durability="sync")
    finally:
        rt.close()
        saver.conn.close()
    m = Metrics(**final["metrics"])
    qlog = QuarterLog(**final["log"])
    return QuarterResult(m, qlog, float(final["fitness"]), final.get("report", ""), tid, how, dict(final))


def run_quarter(env: Env, *, seed: int, generation: int = 1, **kw) -> tuple[Metrics, QuarterLog]:
    """One quarter of env on the seeded market → (Metrics, QuarterLog). The runner #11's evolve loop injects.
    Keyword options are QuarterConfig fields (transport="fake"|"switch", llm, weeks, db, thread_id, fresh, ...)."""
    r = run_quarter_full(env, seed=seed, generation=generation, **kw)
    return r.metrics, r.log
