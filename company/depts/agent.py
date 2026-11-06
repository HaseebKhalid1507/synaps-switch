"""company/depts/agent.py — department agents: Agency personas working the company transport.

    log = QuarterLog()
    t = FakeTransport(rooms=transport_rooms(env))      # or a SwitchTransport over the same rooms
    deps = Departments(env, t, log)                     # registers one DeptAgent per env agent
    t.start()
    deps.week = 2
    room, text = deps.event_message(event)              # market event, @-addressed to the room's dept
    t.say("market", room, text)                         # (FakeTransport; on Switch someone posts it)
    ...
    metrics = market.settle(log)

Agents. One DeptAgent per env agent except synaps-dev (provider synaps; #12 drives it) and, by
default, board (the board graph, #10/#11, owns it; pass skip=() for a Haiku stand-in). Its system
prompt is `roster.instructions_for(persona, role_note)` (role_note = the env agent's `instructions`)
plus the house protocol below. Every call is one stateless `llm.complete_json`, so the prompt
carries the context: week, price, the dept's budget, shipped features, and the conversation so far.
The agent answers in the thread of the message it got and may @-address other agents.

Rooms. `transport_rooms(env)` is the membership the agents assume: each room's own agents and
humans, the board agent in every room, every agent in #board (the hub: in env1, with no links,
departments reach each other through #board), and `x-company.links` pairs share their agents.
A reply whose @-targets are not all in the current room goes to a room that has them all (a
linked room, else #board), as a new thread prefixed "(re #room)"; it stays the same conversation.

ACTION blocks. Every reply ends with a fenced JSON ACTION block (`render`), parsed back from the
posted text (`parse_action`) into the QuarterLog:
    {"type":"campaign","text":"…","spend":800,"cites_feature":true}   -> campaigns (+ quality) + spend
    {"type":"spend_request","dept":"maintenance","usd":2000}           -> spend, after finance
    {"type":"price","value":26.0}                                      -> price_changes, after finance
    {"type":"route","issue":3,"to":"synaps-dev"}                       -> Departments.routes + decisions
    {"type":"none"}
Finance's verdicts carry the same block plus "decision": approve|deny|escalate|reject.

Finance. Price changes and spend requests are always reviewed by @finance (cc'd if the author
didn't mention it); finance itself never raises them (its own are ignored, so nothing is booked twice). A campaign is booked at once when its spend is within the veto limit and the
dept's budget, else it is held for finance. cites_feature is what the copy shows: a shipped
feature named in it (#issue or title words), whatever the ACTION claims. The policy check is code, not the LLM's call:
over `policies.finance_veto_usd` (a spend, or a price change's revenue at risk over the weeks left)
-> escalate to the #board humans, who answer in that thread "@finance approve" / "@finance reject"
(only messages from people count); over the dept's remaining budget -> deny; else finance's LLM
approves or denies on the merits. Approved campaigns get a quality score from CampaignJudge
(Haiku, fixed rubric, cached by text hash). With no finance agent in the env, items within policy
are booked and the rest denied.

Budgets. `x-company.token_budget: {per_agent: N | {name: N, default: N}, per_quarter: M}`
(estimated tokens, 4 chars each, reserved before every call). An agent over its own or the
quarter's budget does not call the LLM: it says so in the thread, once per conversation, with no
@-mentions. All real costs (failed calls too, and the judge) go to QuarterLog.llm_cost_usd.

Loop guard. An agent ignores its own messages, messages that don't @-address it, and messages it
already handled. Per conversation it answers at most `max_hops` (3) agent messages in a row; a
message from a person or the market resets the count. Refusals and escalations mention no agent.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

from company.market.sim import Market, QuarterLog, default_issues
from company.transport import addresses, parse_mentions

ACTION_TYPES = ("campaign", "spend_request", "price", "route", "none")
VERDICTS = ("approve", "deny", "escalate", "reject")
DEFAULT_TOKEN_BUDGET = {"per_agent": 30_000, "per_quarter": 150_000}
DEFAULT_HUMANS = ("Aditya", "Haseeb")
DEFAULT_VETO_USD = 5000.0
HUB = "board"
MAX_HOPS = 3
HISTORY_LINES = 8
CHARS_PER_TOKEN = 4
REPLY_TOKENS_EST = 300


# ───────────────────────── ACTION blocks ─────────────────────────

_FENCE = re.compile(r"```[ \t]*([A-Za-z]*)[ \t]*\n(.*?)\n?[ \t]*```", re.S)


def render(say: str, action: dict) -> str:
    """Chat text that ends with the fenced JSON ACTION block."""
    block = "```json\n" + json.dumps(action, ensure_ascii=False) + "\n```"
    say = (say or "").strip()
    return f"{say}\n\n{block}" if say else block


def parse_action(text: str) -> dict | None:
    """The fenced JSON object with a "type" that ends the message, or None."""
    blocks = list(_FENCE.finditer(text or ""))
    if not blocks:
        return None
    last = blocks[-1]
    if text[last.end():].strip() or last.group(1).lower() not in ("", "json", "action"):
        return None
    try:
        obj = json.loads(last.group(2))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and "type" in obj else None


def _usd(v) -> float:
    if isinstance(v, str):
        v = v.replace("$", "").replace(",", "").strip()
    x = float(v)
    if not x == x or x < 0 or x > 1e7:
        raise ValueError(f"bad amount {v!r}")
    return round(x, 2)


def _bool(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() == "true")


def validate_action(a, agents=None) -> dict:
    """Normalize an ACTION; ValueError if it is not one of ACTION_TYPES with usable fields."""
    if not isinstance(a, dict):
        raise ValueError("ACTION is not a JSON object")
    t = str(a.get("type") or "").strip().lower()
    if t == "none":
        out = {"type": "none"}
    elif t == "campaign":
        text = str(a.get("text") or "").strip()
        if not text:
            raise ValueError("campaign without text")
        out = {"type": t, "text": text[:1200], "spend": _usd(a.get("spend") or 0),
               "cites_feature": _bool(a.get("cites_feature"))}
    elif t == "spend_request":
        usd = _usd(a.get("usd"))
        if usd <= 0:
            raise ValueError("spend_request needs usd > 0")
        out = {"type": t, "dept": str(a.get("dept") or "").strip().lstrip("#"), "usd": usd}
    elif t == "price":
        value = _usd(a.get("value"))
        if not 0 < value < 10_000:
            raise ValueError(f"price {value} out of range")
        out = {"type": t, "value": value}
    elif t == "route":
        issue = int(str(a.get("issue")).strip().lstrip("#"))
        to = str(a.get("to") or "").strip().lstrip("@")
        if not to or (agents is not None and to not in agents):
            raise ValueError(f"route to unknown agent {to!r}")
        out = {"type": t, "issue": issue, "to": to}
    else:
        raise ValueError(f"unknown ACTION type {a.get('type')!r}")
    d = str(a.get("decision") or "").strip().lower()
    if d in VERDICTS:
        out["decision"] = d
    return out


def _strip_fences(s: str) -> str:
    return _FENCE.sub("", s or "").strip()


# ───────────────────────── rooms ─────────────────────────


def transport_rooms(env, humans=DEFAULT_HUMANS, hub=HUB) -> dict[str, list[str]]:
    """Room membership the company runs on (see the module docstring)."""
    names = [a["name"] for a in env.agents]
    own = {r["name"]: list(r.get("agents") or []) for r in env.rooms}
    rooms: dict[str, list[str]] = {}
    for r in env.rooms:
        users = []
        for u in r.get("users") or []:
            users += list(humans) if u == "{$creator}" else [u]
        rooms[r["name"]] = list(own[r["name"]]) + users
    for pair in (env.company or {}).get("links") or []:
        a, b = list(pair)[:2]
        if a in rooms and b in rooms:
            rooms[a] += own[b]
            rooms[b] += own[a]
    rooms.setdefault(hub, list(humans))
    for name, members in rooms.items():
        if "board" in names:
            members.append("board")
        if name == hub:
            members += names
    return {k: list(dict.fromkeys(v)) for k, v in rooms.items()}


# ───────────────────────── budgets ─────────────────────────


def _tokens(*parts: str) -> int:
    return sum(len(p or "") for p in parts) // CHARS_PER_TOKEN


class TokenBudget:
    """Estimated-token budget per agent and per quarter (the judge counts toward the quarter)."""

    def __init__(self, cfg=None):
        cfg = dict(DEFAULT_TOKEN_BUDGET, **(cfg or {}))
        pa = cfg["per_agent"]
        self.per_agent = dict(pa) if isinstance(pa, dict) else {"default": int(pa)}
        self.per_agent.setdefault("default", DEFAULT_TOKEN_BUDGET["per_agent"])
        self.per_quarter = int(cfg["per_quarter"])
        self.used: dict[str, int] = {}
        self.usd: dict[str, float] = {}

    def limit(self, agent):
        return int(self.per_agent.get(agent, self.per_agent["default"]))

    def total(self):
        return sum(self.used.values())

    def over(self, agent, est) -> str | None:
        """Why `agent` may not spend `est` more tokens, or None."""
        u, lim, tot = self.used.get(agent, 0), self.limit(agent), self.total()
        if u + est > lim:
            return f"{agent} has {max(0, lim - u):,} of its {lim:,} tokens left this quarter; a reply needs about {est:,}"
        if tot + est > self.per_quarter:
            return (f"the company has {max(0, self.per_quarter - tot):,} of its {self.per_quarter:,} tokens left "
                    f"this quarter; a reply needs about {est:,}")
        return None

    def charge(self, agent, tokens, usd=0.0):
        self.used[agent] = self.used.get(agent, 0) + int(tokens)
        self.usd[agent] = round(self.usd.get(agent, 0.0) + float(usd), 6)


# ───────────────────────── LLM, roster ─────────────────────────


def _default_llm(system, prompt):
    from company.llm import complete_json
    return complete_json(system, prompt)


def _llm_error():
    from company.llm import LLMError
    return LLMError


def load_personas(root=None) -> dict:
    """The Agency roster, loaded once per dir (company.core.roster.get_roster): $COMPANY_ROSTER_DIR, else
    company/vendor/agency-agents (company/fetch_roster.sh). Empty dict when it is missing."""
    from company.core.roster import get_roster
    return get_roster(root)


def instructions_of(spec: dict, roster: dict) -> str:
    from company.core.roster import instructions_for
    note = spec.get("instructions") or spec.get("description") or f"You are {spec['name']}."
    persona = roster.get(spec.get("persona") or "")
    return instructions_for(persona, note) if persona else note.strip()


# ───────────────────────── campaign judge ─────────────────────────

JUDGE_VERSION = "rubric-v1"
RUBRIC = ("clarity", "specificity", "benefit", "cta", "honesty")
JUDGE_SYSTEM = """You judge marketing campaign copy for acme-shop, a small online shop, on a fixed rubric.
Score each criterion 0, 1 or 2 (2 = excellent, be strict):
clarity: one clear message a customer gets in five seconds
specificity: names a concrete product feature, price or offer, not generic hype
benefit: says what the customer gains
cta: ends with a clear call to action
honesty: no invented numbers, features or claims beyond the facts given
Reply {"clarity":n,"specificity":n,"benefit":n,"cta":n,"honesty":n,"note":"<ten words or fewer>"}."""


class CampaignJudge:
    """Campaign quality 0..1 = rubric points / 10, from one Haiku call, cached by text hash."""

    def __init__(self, llm=None, cache_path=None):
        self.llm = llm or _default_llm
        self.cache_path = Path(cache_path) if cache_path else None
        self.cache: dict[str, dict] = {}
        self.calls = 0
        self._lock = threading.Lock()
        if self.cache_path and self.cache_path.exists():
            try:
                self.cache = json.loads(self.cache_path.read_text())
            except ValueError:
                self.cache = {}

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(f"{JUDGE_VERSION}\n{text.strip()}".encode()).hexdigest()

    def score(self, text: str, facts: str = "") -> tuple[dict, float]:
        """({quality, scores, note, cached}, cost_usd). Raises LLMError (with .cost_usd)."""
        k = self.key(text)
        with self._lock:
            hit = self.cache.get(k)
        if hit:
            return {**hit, "cached": True}, 0.0
        prompt = f"Facts: {facts or 'none given'}\n\nCampaign copy:\n{text.strip()}"
        obj, cost = self.llm(JUDGE_SYSTEM, prompt)
        self.calls += 1
        scores = {}
        for c in RUBRIC:
            try:
                scores[c] = max(0, min(2, int(round(float(obj.get(c, 0))))))
            except (TypeError, ValueError):
                scores[c] = 0
        res = {"quality": round(sum(scores.values()) / (2 * len(RUBRIC)), 2), "scores": scores,
               "note": str(obj.get("note") or "")[:120], "tokens": _tokens(JUDGE_SYSTEM, prompt, json.dumps(obj))}
        with self._lock:
            self.cache[k] = res
            if self.cache_path:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.cache_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.cache, indent=1, sort_keys=True))
                os.replace(tmp, self.cache_path)
        return {**res, "cached": False}, cost


# ───────────────────────── prompts ─────────────────────────

PROTOCOL = """\
## How you work at acme-shop
You are @{name}, the {dept} department agent of acme-shop, working in Switch chat rooms.
People and other agents @-mention you; you answer in their thread.
Agents you can address: {agents}.
Humans: {humans} in #board. Only they approve spend over ${veto:,.0f} and code merges; never approve on their behalf.

Reply with one JSON object: {{"say": "<your chat message, under 70 words>", "action": <ACTION>}}
ACTION is exactly one of:
{{"type":"campaign","text":"<customer-facing copy, under 50 words>","spend":<usd>,"cites_feature":<true only if the copy names a feature shipped this quarter>}}
{{"type":"spend_request","dept":"<department>","usd":<amount>}}
{{"type":"price","value":<new monthly price in usd>}}
{{"type":"route","issue":<acme-shop issue number>,"to":"<the agent who will fix or build it, usually synaps-dev>"}}
{{"type":"none"}}
Rules:
- Decide now with the facts given. No more data, meetings or time are coming: never ask for analysis or wait; estimate and act.
- Take one concrete action when the message calls for one; otherwise {{"type":"none"}}.
- @-mention an agent only when it must act next, at most two, written "@name " with a space after the name. Never mention yourself. Never reply just to thank or acknowledge.
- Price changes and spend requests go to @finance, which books or escalates them.
- Never invent shipped features, numbers or customer quotes.
- If there is nothing useful to add, reply {{"say": "", "action": {{"type": "none"}}}}."""

REVIEW = """\
Review request from {by} ({dept}): {what}.
Policy check (computed, binding): {check}: {why}.
- "escalate": write a short note to the #board humans saying what is asked and why it needs them; decision "escalate".
- "deny": explain in one or two sentences; decision "deny".
- "ok": approve or deny on the merits (margin, budget left, timing); decision "approve" or "deny".
Mention an agent only if it must act next (e.g. @marketing to announce an approved price); don't mention the requester just to inform it.
Reply as JSON {{"say": "...", "decision": "approve|deny|escalate"}}."""

PAST = {"approve": "Approved", "deny": "Denied", "reject": "Rejected", "escalate": "Escalated"}
# What each market event asks of the department it lands in (appended by event_message).
EVENT_ASKS = {
    "competitor": "Decide our price now: a price ACTION with the price you set (the current one if you hold).",
    "incident": "Triage it now and route it to whoever fixes it.",
    "feature_request": "Decide now whether we build it this quarter; if so, route it to whoever builds it.",
    "invoice": "Fixed costs are booked automatically; flag anything unusual, otherwise no action.",
    "lead": "Decide whether a campaign should follow up.",
}

_APPROVE = re.compile(r"\b(approve[ds]?|yes|lgtm)\b", re.I)
_REJECT = re.compile(r"\b(reject(?:ed|s)?|den(?:y|ied)|no|veto(?:ed)?)\b", re.I)


# ───────────────────────── runtime ─────────────────────────


class Departments:
    """All dept agents of one quarter, their shared state, and the QuarterLog they fill."""

    def __init__(self, env, transport, log: QuarterLog | None = None, *, llm=None, judge=None, roster=None,
                 rooms=None, humans=DEFAULT_HUMANS, approvers=None, issues=None, week=1,
                 max_hops=MAX_HOPS, skip=("board",), finance="finance", hub=HUB):
        self.env, self.t = env, transport
        self.log = log if log is not None else QuarterLog()
        self.llm = llm or _default_llm
        self.judge = judge or CampaignJudge(llm=self.llm)
        self.company = env.company or {}
        self.humans = tuple(humans)
        self.approvers = {a.lower() for a in approvers} if approvers else None
        self.rooms = rooms if rooms is not None else transport_rooms(env, humans, hub)
        self.hub = hub
        self.issues = issues or default_issues()
        self.week = week
        self.weeks = int(self.company.get("weeks_per_quarter", 4))
        self.max_hops = max_hops
        self.budget = TokenBudget(self.company.get("token_budget"))
        self.lock = threading.RLock()
        self.agent_names = [a["name"] for a in env.agents]
        self.hops: dict[str, int] = {}
        self.conv_of: dict[str, str] = {}
        self.history: dict[str, list[str]] = {}
        self.seen: set = set()
        self.refused: set = set()
        self.pending: dict[str, dict] = {}       # requesting message id -> item
        self.escalations: dict[str, list] = {}   # #board thread root -> item ids
        self.routes: list[dict] = []
        roster = roster if roster is not None else load_personas()
        specs = [a for a in env.agents
                 if a.get("provider") != "synaps" and a["name"] != "synaps-dev" and a["name"] not in skip]
        self.finance = finance if any(a["name"] == finance for a in specs) else None
        self.agents: dict[str, DeptAgent] = {a["name"]: DeptAgent(self, a, instructions_of(a, roster)) for a in specs}
        for n, a in self.agents.items():
            transport.on_message(n, a.handle)

    # facts
    def dept_of(self, agent):
        for r in self.env.rooms:
            if agent in (r.get("agents") or []):
                return r["name"]
        return agent

    def veto(self):
        return float((self.company.get("policies") or {}).get("finance_veto_usd", DEFAULT_VETO_USD))

    def price(self):
        ch = self.log.price_changes
        return float(ch[-1]["price"]) if ch else float(self.company.get("price", 29.0))

    def budget_line(self, dept):
        b = (self.company.get("budgets") or {}).get(dept)
        return None if b is None else float(b)

    def spent(self, dept):
        return sum(s["usd"] for s in self.log.spend if s.get("dept") == dept and s.get("approved"))

    def shipped(self):
        titles = {f["issue"]: f["title"] for f in self.issues.get("features", [])}
        return [f"#{f['issue']} {titles.get(f['issue'], '')} (week {f.get('week_shipped')})".replace("  ", " ")
                for f in self.log.features]

    def facts(self, agent=None):
        dept = self.dept_of(agent) if agent else None
        lines = [f"Week {self.week} of {self.weeks} at acme-shop. Current price: ${self.price():.2f}/month."]
        if dept:
            b = self.budget_line(dept)
            lines.append(f"Your department: {dept}. " + (
                f"Budget ${b:,.0f}, approved spend so far ${self.spent(dept):,.0f}." if b is not None
                else "No budget line."))
        if agent is not None and agent == self.finance:
            led = [f"{d} ${self.spent(d):,.0f} of ${b:,.0f}" for d, b in (self.company.get("budgets") or {}).items()]
            lines.append("Ledger, approved spend vs budget: " + ("; ".join(led) or "no budgets") + ". "
                         "Approved items are already booked; never book them again.")
        lines.append(f"Finance veto: any single spend over ${self.veto():,.0f} needs the #board humans. "
                     "Fixed costs (hosting, the infrastructure invoice) are booked automatically.")
        lines.append("Shipped this quarter: " + ("; ".join(self.shipped()) or "nothing yet") + ".")
        fixed = {f["issue"] for f in self.log.fixes}
        shipped = {f["issue"] for f in self.log.features}
        inc = [f"#{i['issue']} {i['title']} (sev{i['severity']})" for i in self.issues.get("incidents", [])
               if i["issue"] not in fixed]
        feat = [f"#{f['issue']} {f['title']} (demand {f['demand']:.1f})" for f in self.issues.get("features", [])
                if f["issue"] not in shipped]
        lines.append("Open incidents: " + ("; ".join(inc) or "none") + ".")
        lines.append("Feature backlog: " + ("; ".join(feat) or "empty") + ".")
        return "\n".join(lines)

    def event_message(self, ev) -> tuple[str, str]:
        """(room, text) for a market event, @-addressed to the dept agents of its room, with its ask."""
        own = next((r.get("agents") or [] for r in self.env.rooms if r["name"] == ev.room), [])
        tags = " ".join(f"@{a}" for a in own if a in self.agents)
        return ev.room, " ".join(x for x in (tags, ev.text, EVENT_ASKS.get(ev.kind, "")) if x)

    def usage(self):
        return {"tokens": dict(self.budget.used), "usd": dict(self.budget.usd),
                "total_tokens": self.budget.total(), "llm_cost_usd": round(self.log.llm_cost_usd, 6)}

    # bookkeeping (call with self.lock held where noted)
    def decide(self, **kw):
        with self.lock:
            self.log.decisions.append({"week": self.week, **kw})

    def book_cost(self, agent, tokens, usd):
        with self.lock:
            self.budget.charge(agent, tokens, usd)
            self.log.llm_cost_usd = round(self.log.llm_cost_usd + float(usd or 0), 6)

    def conv_for(self, msg):
        key = msg.thread or msg.id
        return self.conv_of.get(key) or self.conv_of.get(msg.id) or key

    def note(self, conv, room, sender, text):
        h = self.history.setdefault(conv, [])
        h.append(f"[#{room}] {sender}: {' '.join((text or '').split())[:500]}")
        del h[:-HISTORY_LINES]

    def reach(self, agent, targets, room):
        """A room with `agent` and every target: `room`, else a linked/own room, else the hub."""
        def ok(r):
            m = self.rooms.get(r, ())
            return agent in m and all(x in m for x in targets)
        if ok(room):
            return room
        for r in [*(r for r in self.rooms if r != self.hub), self.hub]:
            if ok(r):
                return r
        return room

    def check(self, item) -> tuple[str, str]:
        """Finance policy: ("escalate"|"deny"|"ok", why). Code, not the LLM's call."""
        a, veto = item["action"], self.veto()
        if a["type"] == "price":
            old, new = self.price(), a["value"]
            left = max(1, self.weeks - self.week + 1)
            risk = abs(old - new) * Market.BASE_CUSTOMERS * Market.BASE_WEEKLY_PURCHASE_RATE * left
            why = f"${old:.2f} -> ${new:.2f} puts about ${risk:,.0f} of revenue at stake over {left} week(s)"
            return ("escalate", why + f", over the ${veto:,.0f} veto limit") if risk > veto else \
                ("ok", why + f", within the ${veto:,.0f} veto limit")
        usd = a.get("usd", a.get("spend", 0.0))
        if usd > veto:
            return "escalate", f"${usd:,.0f} is over the ${veto:,.0f} veto limit"
        dept, b = item["dept"], self.budget_line(item["dept"])
        if b is not None and self.spent(dept) + usd > b:
            return "deny", f"${usd:,.0f} would take {dept} past its ${b:,.0f} budget (${self.spent(dept):,.0f} spent)"
        return "ok", f"${usd:,.0f} is within the ${veto:,.0f} veto limit and {dept}'s budget"

    def needs_finance(self, agent, action) -> bool:
        t = action["type"]
        if t in ("price", "spend_request"):
            return True
        if t == "campaign" and action["spend"] > 0:
            return self.check({"action": action, "dept": self.dept_of(agent)})[0] != "ok"
        return False

    def submit(self, agent, mid, action, conv, room):
        """Book the ACTION parsed from `agent`'s post `mid` (pending ones wait for finance)."""
        t = action["type"]
        if t == "none" or "decision" in action:
            return None
        if t == "route":
            r = {"week": self.week, "issue": action["issue"], "to": action["to"], "by": agent, "msg": mid}
            with self.lock:
                self.routes.append(r)
            self.decide(type="route", issue=action["issue"], to=action["to"], by=agent, msg=mid)
            return r
        if agent == self.finance and t in ("price", "spend_request"):
            self.decide(type="ignored_action", by=agent, msg=mid, action=action,
                        why="finance reviews price and spend requests, it does not raise them")
            return None
        item = self.item(agent, mid, action, conv, room)
        if t == "campaign" and not self.needs_finance(agent, action):
            self.settle(item, True, by="policy", why="within the veto limit and budget")
        elif self.finance is None:
            chk, why = self.check(item)
            self.settle(item, chk == "ok", by="policy", why=f"no finance agent: {why}")
        return item

    def item(self, agent, mid, action, conv, room):
        with self.lock:
            if mid not in self.pending:
                dept = action.get("dept") if action["type"] == "spend_request" else None
                self.pending[mid] = {"id": mid, "kind": action["type"], "action": action, "by": agent,
                                     "dept": dept or self.dept_of(agent), "week": self.week,
                                     "status": "pending", "conv": conv, "room": room}
            return self.pending[mid]

    def settle(self, item, approved, *, by, why="", status=None):
        """Book a decided item into the QuarterLog (once)."""
        with self.lock:
            if item["status"] not in ("pending", "escalated"):
                return item
            item["status"] = status or ("approved" if approved else "denied")
        a, w = item["action"], self.week
        entry = {"type": "verdict", "item": item["id"], "kind": item["kind"], "status": item["status"],
                 "by": by, "why": why}
        if a["type"] == "campaign":
            if approved:
                q = self.score(a, item["by"])
                with self.lock:
                    self.log.campaigns.append({"week": w, "text": a["text"], "spend": a["spend"],
                                               "cites_feature": q["cites_feature"], "quality": q["quality"],
                                               "by": item["by"]})
                entry.update(quality=q["quality"], scores=q.get("scores"))
            if a["spend"] > 0:
                with self.lock:
                    self.log.spend.append({"week": w, "dept": item["dept"], "usd": a["spend"], "approved": approved})
        elif a["type"] == "spend_request":
            with self.lock:
                self.log.spend.append({"week": w, "dept": item["dept"], "usd": a["usd"], "approved": approved})
        elif a["type"] == "price" and approved:
            with self.lock:
                self.log.price_changes.append({"week": w, "price": a["value"]})
        self.decide(**entry)
        return item

    def cites_shipped(self, text) -> bool:
        """Whether the copy names a feature shipped this quarter (by #issue or its title words)."""
        titles = {f["issue"]: f["title"] for f in self.issues.get("features", [])}
        low = (text or "").lower()
        words = set(re.findall(r"[a-z0-9]+", low))
        for f in self.log.features:
            i = f["issue"]
            sig = [w for w in re.findall(r"[a-z0-9]+", titles.get(i, "").lower()) if len(w) >= 4]
            if f"#{i}" in low or (sig and sum(w in words for w in sig) >= max(1, len(sig) - 1)):
                return True
        return False

    def score(self, a, agent):
        """Judge a campaign. cites_feature is what the copy shows: a shipped feature named in it."""
        cites = self.cites_shipped(a["text"])
        titles = {f["issue"]: f["title"] for f in self.issues.get("features", [])}
        shipped = [(i, titles.get(i, "")) for i in (f["issue"] for f in self.log.features)]
        facts = f"price ${self.price():.2f}/month; shipped this quarter: " + (
            ", ".join(t for _, t in shipped) or "nothing")
        try:
            res, cost = self.judge.score(a["text"], facts)
        except Exception as e:  # noqa: BLE001 - a failed judge books its cost, quality falls to neutral
            self.book_cost("judge", 0, getattr(e, "cost_usd", 0.0))
            self.decide(type="judge_failed", by=agent, error=f"{type(e).__name__}: {e}"[:300])
            return {"quality": 0.5, "cites_feature": cites}
        if not res.get("cached"):
            self.book_cost("judge", res.get("tokens", 0), cost)
        return {"quality": res["quality"], "scores": res["scores"], "cites_feature": cites}


class DeptAgent:
    """One department agent: answers messages that @-address it, in the thread, via one LLM call."""

    def __init__(self, deps: Departments, spec: dict, instructions: str):
        self.deps, self.spec, self.name = deps, spec, spec["name"]
        self.dept = deps.dept_of(self.name)
        others = [f"@{a['name']} ({(a.get('description') or a['name']).rstrip('.')})"
                  for a in deps.env.agents if a["name"] != self.name]
        self.instructions = instructions
        self.system = instructions.rstrip() + "\n\n" + PROTOCOL.format(
            name=self.name, dept=self.dept, agents="; ".join(others),
            humans=", ".join(f"@{h}" for h in deps.humans), veto=deps.veto())
        if deps.finance == self.name:
            self.system += ("\n- You are finance: requests reach you as review prompts. Never raise a price or "
                            "spend_request ACTION yourself; outside a review use {\"type\":\"none\"}.")
        self._known = {n.lower(): n for n in [*deps.agent_names, *deps.humans]}

    @property
    def is_finance(self):
        return self.deps.finance == self.name

    # entry point (transport handler)
    def handle(self, msg):
        d = self.deps
        if msg.sender == self.name or not addresses(msg.mentions, self.name):
            return                                     # loop guard 1: only what addresses me
        with d.lock:
            if (self.name, msg.id) in d.seen:
                return
            d.seen.add((self.name, msg.id))
            conv = d.conv_for(msg)
            d.note(conv, msg.room, msg.sender, msg.text)
            if msg.sender_is_agent:
                hops = d.hops.get(conv, 0)
                if hops >= d.max_hops:                 # loop guard 2: max agent<->agent hops
                    d.log.decisions.append({"week": d.week, "type": "loop_guard", "agent": self.name,
                                            "msg": msg.id, "from": msg.sender, "conv": conv, "hops": hops})
                    return
                d.hops[conv] = hops + 1
            else:
                d.hops[conv] = 0
        if self.is_finance:
            if not msg.sender_is_agent and self.human_verdict(msg, conv):
                return
            item = d.pending.get(msg.id)
            if item is None:
                a = self._parsed(msg.text)
                if a and "decision" not in a and (a["type"] in ("price", "spend_request") or
                                                  (a["type"] == "campaign" and d.needs_finance(msg.sender, a))):
                    item = d.item(msg.sender, msg.id, a, conv, msg.room)
            if item is not None and item["status"] == "pending":
                return self.review(msg, item, conv)
        return self.respond(msg, conv)

    # pieces
    def _parsed(self, text):
        try:
            a = parse_action(text)
            return validate_action(a, self.deps.agent_names) if a else None
        except (ValueError, TypeError):
            return None

    def clean(self, say: str, *, agents=True) -> str:
        """No fences, no self-mention, `@name.` -> `@name`; agents=False un-mentions every agent."""
        s = _strip_fences(say)[:1200]

        def fix(m):
            tok = m.group(1)
            n = self._known.get(tok.lower()) or self._known.get(tok.rstrip("._-").lower())
            if n is None:
                return m.group(0)
            if n == self.name or (not agents and n in self.deps.agent_names):
                return n
            return "@" + n
        return re.sub(r"@([A-Za-z0-9._-]+)", fix, s)

    def ask(self, prompt, msg, conv) -> dict | None:
        """One budgeted LLM call; refuses (once per conversation) when over budget."""
        d, est = self.deps, _tokens(self.system, prompt) + REPLY_TOKENS_EST
        with d.lock:
            why = d.budget.over(self.name, est)
            if why is None:
                d.budget.charge(self.name, est)        # reserve; trued up below
        if why:
            self.refuse(msg, conv, why)
            return None
        try:
            obj, cost = d.llm(self.system, prompt)
        except _llm_error() as e:
            d.book_cost(self.name, -REPLY_TOKENS_EST, e.cost_usd)
            d.decide(type="llm_error", agent=self.name, msg=msg.id, error=str(e)[:300])
            return None
        d.book_cost(self.name, _tokens(json.dumps(obj)) - REPLY_TOKENS_EST, cost)
        return obj if isinstance(obj, dict) else {}

    def refuse(self, msg, conv, why):
        d = self.deps
        with d.lock:
            if (self.name, conv) in d.refused:
                return
            d.refused.add((self.name, conv))
        text = (f"{self.name} here: I'm over my LLM budget ({why}), so I won't act on this. "
                "The board can raise x-company.token_budget.")
        d.decide(type="budget_refusal", agent=self.name, msg=msg.id, why=why)
        self.post(msg.room, text, msg.thread or msg.id, conv)

    def post(self, room, text, thread, conv):
        d = self.deps
        mid = d.t.post(self.name, room, text, thread=thread)
        with d.lock:
            d.conv_of[mid] = conv
            d.note(conv, room, self.name, text)
        return mid

    def deliver(self, msg, conv, say, action):
        """Post say + ACTION where every @-target is, then book the ACTION parsed from the post."""
        d = self.deps
        targets = [m for m in parse_mentions(say) if m in d.agent_names and m != self.name]
        extra = []
        if action["type"] == "route" and action["to"] not in targets and action["to"] != self.name:
            extra.append(f"@{action['to']} please take issue #{action['issue']}.")
            targets.append(action["to"])
        if d.finance and d.finance != self.name and d.finance not in targets and \
                "decision" not in action and d.needs_finance(self.name, action):
            extra.append(f"cc @{d.finance} for review.")
            targets.append(d.finance)
        say = " ".join([say, *extra]).strip()
        room = d.reach(self.name, targets, msg.room)
        thread = msg.thread or msg.id
        if room != msg.room:
            say, thread = f"(re #{msg.room}) {say}", None
        text = render(say, action)
        mid = self.post(room, text, thread, conv)
        parsed = self._parsed(text)                    # the QuarterLog gets what the chat shows
        if parsed is not None:
            d.submit(self.name, mid, parsed, conv, room)
        return mid

    def respond(self, msg, conv):
        d = self.deps
        with d.lock:
            hist = "\n".join(d.history.get(conv, [])[:-1]) or "(none)"
            facts = d.facts(self.name)
        prompt = (f"{facts}\n\nConversation so far:\n{hist}\n\n"
                  f"New message for you in #{msg.room} from {msg.sender} "
                  f"({'an agent' if msg.sender_is_agent else 'a person or the market'}):\n{msg.text}\n\n"
                  'Reply as JSON {"say": "...", "action": {...}}.')
        obj = self.ask(prompt, msg, conv)
        if obj is None:
            return None
        say = self.clean(str(obj.get("say") or ""))
        try:
            action = validate_action(obj.get("action") or {"type": "none"}, d.agent_names)
            action.pop("decision", None)
        except (ValueError, TypeError) as e:
            d.decide(type="invalid_action", agent=self.name, msg=msg.id, error=str(e)[:200],
                     action=obj.get("action"))
            action = {"type": "none"}
        if not say and action["type"] == "none":
            return None                                # nothing to add: stay quiet
        return self.deliver(msg, conv, say, action)

    def review(self, msg, item, conv):
        """Finance: policy check (binding) + the persona's note; approve, deny or escalate."""
        d = self.deps
        chk, why = d.check(item)
        a = item["action"]
        what = {"price": lambda: f"change the price to ${a['value']:.2f}",
                "spend_request": lambda: f"spend ${a['usd']:,.0f} for {item['dept']}",
                "campaign": lambda: f"a ${a['spend']:,.0f} campaign: {a['text'][:200]!r}"}[a["type"]]()
        with d.lock:
            hist = "\n".join(d.history.get(conv, [])[:-1]) or "(none)"
            facts = d.facts(self.name)
        prompt = (f"{facts}\n\nConversation so far:\n{hist}\n\nMessage in #{msg.room} from {msg.sender}:\n"
                  f"{msg.text}\n\n" + REVIEW.format(by=item["by"], dept=item["dept"], what=what, check=chk, why=why))
        obj = self.ask(prompt, msg, conv)
        if obj is None:
            return None
        llm_says = str(obj.get("decision") or "").strip().lower()
        decision = chk if chk != "ok" else ("deny" if llm_says == "deny" else "approve")
        action = {**a, "decision": decision}
        if decision == "escalate":
            say = self.clean(str(obj.get("say") or ""), agents=False)
            lead = "|".join(re.escape(h) for h in d.humans)
            say = re.sub(rf"^(?:\s*@(?:{lead})\b[,:]?)+\s*", "", say, flags=re.I) if lead else say
            say = say or f"{item['by']} asks to {what}."
            text = render(" ".join(f"@{h}" for h in d.humans) + f" {say} ({why}.) "
                          "Reply in this thread: approve or reject, addressed to @finance", action)
            room = d.hub
            thread = (msg.thread or msg.id) if msg.room == d.hub else None
            mid = self.post(room, text, thread, conv)
            with d.lock:
                item["status"], item["escalation"] = "escalated", mid
                d.escalations.setdefault(thread or mid, []).append(item["id"])
            d.decide(type="verdict", item=item["id"], kind=item["kind"], status="escalated", by=self.name, why=why)
            return mid
        say = self.clean(str(obj.get("say") or "")) or f"{PAST[decision]}: {what} ({why})."
        mid = self.deliver(msg, conv, say, action)
        d.settle(item, decision == "approve", by=self.name, why=why)
        return mid

    def human_verdict(self, msg, conv) -> bool:
        """A person answering an escalation: book it. True when the message was a verdict."""
        d = self.deps
        yes, no = bool(_APPROVE.search(msg.text)), bool(_REJECT.search(msg.text))
        if yes == no:
            return False
        with d.lock:
            ids = d.escalations.get(msg.thread or "", [])
            if not ids and msg.room == d.hub:
                open_ = [i for i, it in d.pending.items() if it["status"] == "escalated"]
                ids = open_ if len(open_) == 1 else []
            items = [d.pending[i] for i in ids if d.pending[i]["status"] == "escalated"]
        if not items:
            return False
        if d.approvers is not None and msg.sender.lower() not in d.approvers:
            self.post(msg.room, f"{msg.sender} is not a board approver; this stays escalated.",
                      msg.thread or msg.id, conv)
            return True
        for it in items:
            verdict = "approve" if yes else "reject"
            d.settle(it, yes, by=msg.sender, why=f"{msg.sender} replied {verdict!r} in #{msg.room}",
                     status="approved" if yes else "rejected")
            a = {**it["action"], "decision": verdict}
            self.post(msg.room, render(f"{PAST[verdict]} by {msg.sender}; booked for week {d.week}.", a),
                      msg.thread or msg.id, conv)
        return True
