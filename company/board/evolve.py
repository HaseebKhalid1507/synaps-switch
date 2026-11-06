"""company/board/evolve.py — the board's outer loop: reflect → propose ONE mutation → human gate → trial → keep/revert.

    from company.board.evolve import evolve, ScriptedGate, TransportGate, StubProposer, LLMProposer
    from company.board.stub_quarter import run_quarter            # or #10's company.board.quarter runner
    out = evolve("company/envs/env1.yaml", generations=3, seeds=(1, 2), run_quarter=run_quarter,
                 propose=LLMProposer(), gate=TransportGate(transport, board="board", room="board"),
                 lineage_path="company/lineage.jsonl", envs_dir="company/envs", budget_usd=1.0)

One LangGraph StateGraph, checkpointed (SqliteSaver, thread `evolve:<env>:<seeds>`), so a killed evolution
resumes where it stopped, also while it waits at the human gate:

    baseline ─▶ reflect ─▶ propose ─▶ post_gate ─▶ await_gate ─┬▶ trial ─▶ select ─▶ record ─┬▶ reflect …
       │                     │ invalid twice                    │ reject / timeout ─────────▶│
       └─ (budget) ─▶ END    └──────────────────────────────────┼───────────────────────────▶│ (G or $) ─▶ END
                                                     ignored ◀──┘ (agent, non-approver, not a decision)

baseline   run_quarter(env_k) on every seed → mean fitness (company/fitness.yaml weights). Lineage gen 0.
reflect    a compact summary: mean Metrics, per-seed fitness, QuarterLog highlights (fixes, regressions,
           escalations, spend), and on Switch `read_context` of every room (`read_rooms`).
propose    the board persona (env's `board` agent + its Agency persona) proposes exactly ONE mutation of
           the ops `core.env.apply_mutation` supports (incl. hire / swap_persona / fire from the roster),
           with a diagnosis and a rationale. Validated by applying it; an invalid one is re-asked once
           with the error; invalid twice → recorded as `invalid`, no gate.
post_gate  the env_diff + rationale go to #board (`gate.ask`), once: the thread id is checkpointed.
await_gate interrupt(): the run pauses until the driver resumes it with a message from the gate's thread.
           Only a person on `approvers` (msg.sender_is_agent False) saying approve/reject decides
           (realwork.parse_decision, the MergeGate rules); anything else is logged as ignored and the run
           pauses again. No decision before the deadline (`gate_timeout_s`) → auto-reject, logged.
trial      the candidate runs on the SAME seeds (common random numbers).
select     kept iff mean fitness > the parent's; else reverted (the parent stays current).
record     append_lineage (once per (run, gen), also across resumes); kept envs → <envs_dir>/env<k>.yaml.
           Stops after `generations` or when the $ budget (QuarterLog.llm_cost_usd + proposal costs) is spent.

The quarter runner is injected: `run_quarter(env, *, seed, generation) -> (Metrics, QuarterLog)`.
`company.board.stub_quarter.run_quarter` is the deterministic offline one; `load_quarter_runner("quarter")`
imports #10's `company.board.quarter.run_quarter` lazily (this module never imports it at import time).

CLI (offline Done-when: stub quarter, stub proposer, scripted approver):
    python -m company.board.evolve --env company/envs/env1.yaml --generations 3 --offline --out DIR
    python -m company.lineage_report DIR/lineage.jsonl
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, TypedDict

import yaml

from company.core.env import Env, apply_mutation, env_diff, load_env, save_env
from company.core.fitness import fitness
from company.core.lineage import append_lineage, read_lineage
from company.board.mechanics import why_moved
from company.realwork import approvers_from_env, parse_decision

DEFAULT_SEEDS = (1, 2)
DEFAULT_GATE_TIMEOUT_S = float(os.environ.get("COMPANY_GATE_TIMEOUT_S", 3600))
DEFAULT_BUDGET_USD = 25.0
WEIGHTS_PATH = Path(__file__).resolve().parent.parent / "fitness.yaml"
OPS = ("set_instructions", "add_agent", "hire", "remove_agent", "fire", "swap_persona",
       "link", "unlink", "set_policy", "set_budget", "set_price")
NO_FIRE = ("board", "synaps-dev")
NO_SWAP = ("synaps-dev",)
METRIC_KEYS = ("revenue", "costs", "profit", "customers", "churn_rate", "growth", "open_incident_severity",
               "mean_fix_weeks", "llm_cost_usd")
SHORTLIST = (
    "testing/testing-test-automation-engineer", "testing/testing-api-tester", "testing/testing-reality-checker",
    "testing/testing-performance-benchmarker", "support/support-infrastructure-maintainer",
    "support/support-support-responder", "support/support-analytics-reporter",
    "engineering/engineering-incident-response-commander", "engineering/engineering-code-reviewer",
    "engineering/engineering-devops-automator", "engineering/engineering-finops-engineer",
    "finance/finance-fpa-analyst", "finance/finance-bookkeeper-controller", "marketing/marketing-growth-hacker",
    "marketing/marketing-content-creator", "marketing/marketing-email-strategist",
    "product/product-sprint-prioritizer", "product/product-feedback-synthesizer", "product/product-manager",
    "project-management/project-manager-senior", "project-management/project-management-experiment-tracker",
)
GATE_KIND = "evolve_gate"


class Paused(Exception):
    """Raised by a gate to stop the driver while the run is paused (the checkpoint keeps it)."""


# ───────────────────────── env / metrics helpers ─────────────────────────


def env_to_dict(env: Env) -> dict:
    return {"name": env.name, "parent": env.parent, "agents": copy.deepcopy(env.agents),
            "rooms": copy.deepcopy(env.rooms), "company": copy.deepcopy(env.company)}


def env_from_dict(d: dict) -> Env:
    return Env(name=d["name"], parent=d.get("parent"), agents=copy.deepcopy(d["agents"]),
               rooms=copy.deepcopy(d["rooms"]), company=copy.deepcopy(d["company"]))


def load_weights(path=None) -> dict:
    with open(path or WEIGHTS_PATH) as f:
        return yaml.safe_load(f)["weights"]


def _metrics_dict(m) -> dict:
    return asdict(m) if is_dataclass(m) else dict(m)


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def env_index(name: str) -> int:
    m = re.search(r"(\d+)$", name or "")
    return int(m.group(1)) if m else 1


def _rooms(env: Env) -> list[str]:
    return [r["name"] for r in env.rooms]


def _agent(env: Env, name: str) -> dict | None:
    return next((a for a in env.agents if a.get("name") == name), None)


def _links(env: Env) -> list[list[str]]:
    return [list(p) for p in (env.company.get("links") or [])]


def _linked(env: Env, a: str, b: str) -> list[str] | None:
    return next((p for p in _links(env) if sorted(p) == sorted([a, b])), None)


def compact_log(log) -> dict:
    """The QuarterLog fields the board reads back, JSON-safe."""
    d = asdict(log) if is_dataclass(log) else dict(log)
    return {"fixes": d.get("fixes", []), "features": d.get("features", []),
            "campaigns": [{k: c.get(k) for k in ("week", "spend", "cites_feature", "quality")}
                          for c in d.get("campaigns", [])],
            "price_changes": d.get("price_changes", []), "spend": d.get("spend", []),
            "llm_cost_usd": float(d.get("llm_cost_usd") or 0.0),
            "decisions": [x for x in d.get("decisions", []) if isinstance(x, dict)][-40:]}


def evaluate(env: Env, seeds, run_quarter, weights: dict, generation: int) -> dict:
    """run_quarter on every seed → {fitness (mean), metrics (mean), per_seed: [...], cost_usd}."""
    per = []
    for s in seeds:
        m, log = run_quarter(env, seed=int(s), generation=generation)
        md = _metrics_dict(m)
        from company.market.sim import Metrics
        f = fitness(Metrics(**{k: md[k] for k in METRIC_KEYS}), weights)
        per.append({"seed": int(s), "fitness": round(f, 4), "metrics": md, "log": compact_log(log)})
    n = len(per)
    mean = {k: round(sum(p["metrics"][k] for p in per) / n, 4) for k in METRIC_KEYS}
    return {"fitness": round(sum(p["fitness"] for p in per) / n, 4), "metrics": mean, "per_seed": per,
            "cost_usd": round(sum(p["log"]["llm_cost_usd"] for p in per), 6), "generation": generation}


# ───────────────────────── reflect ─────────────────────────


def summarize(env: Env, ev: dict, rooms: dict | None = None) -> dict:
    """What the board reads: numbers, the quarter's highlights per seed, the org, and the rooms."""
    highlights, spend, denied, escalated, regressed = [], {}, 0, 0, 0
    for p in ev["per_seed"]:
        lg = p["log"]
        for d in lg["decisions"]:
            what = str(d.get("what", ""))
            if "regressed" in what:
                regressed += 1
            if "veto" in what or "escalat" in what:
                escalated += 1
            line = f"seed {p['seed']} wk{d.get('week')} {d.get('who')}: {what}"
            if d.get("type") in ("org", "mechanics"):          # the quarter's mechanics: read these first
                highlights.insert(sum(1 for h in highlights if h.startswith("! ")), "! " + line)
            elif what:
                highlights.append(line)
        for s in lg["spend"]:
            key = s.get("dept", "?")
            if s.get("approved", True):
                spend[key] = round(spend.get(key, 0.0) + float(s.get("usd", 0)) / len(ev["per_seed"]), 2)
            else:
                denied += 1
    p0 = ev["per_seed"][0]["log"] if ev["per_seed"] else {}
    org = [{"name": a["name"], "persona": a.get("persona"), "provider": a.get("provider"),
            "rooms": [r["name"] for r in env.rooms if a["name"] in (r.get("agents") or [])]} for a in env.agents]
    return {
        "env": env.name, "seeds": [p["seed"] for p in ev["per_seed"]], "fitness": ev["fitness"],
        "per_seed_fitness": {str(p["seed"]): p["fitness"] for p in ev["per_seed"]},
        "metrics": ev["metrics"],
        "counts": {"fixes_per_seed": [len(p["log"]["fixes"]) for p in ev["per_seed"]],
                   "features_per_seed": [len(p["log"]["features"]) for p in ev["per_seed"]],
                   "campaigns": len(p0.get("campaigns", [])),
                   "campaigns_citing_shipped": sum(1 for c in p0.get("campaigns", []) if c.get("cites_feature")),
                   "regressions": regressed, "veto_escalations": escalated, "denied_spend": denied},
        "spend_per_quarter": spend,
        "highlights": [h.removeprefix("! ") for h in highlights][:40],
        "org": {"agents": org, "rooms": _rooms(env), "links": _links(env),
                "budgets": env.company.get("budgets") or {}, "policies": env.company.get("policies") or {},
                "price": env.company.get("price"), "weeks_per_quarter": env.company.get("weeks_per_quarter")},
        "rooms": {k: str(v)[-800:] for k, v in (rooms or {}).items()},
    }


def summary_text(s: dict) -> str:
    m = s["metrics"]
    lines = [f"Quarter under {s['env']} on seeds {s['seeds']}: fitness {s['fitness']:.2f} "
             f"(per seed {s['per_seed_fitness']}).",
             f"Mean metrics: revenue ${m['revenue']:,.0f}, costs ${m['costs']:,.0f}, profit ${m['profit']:,.0f}, "
             f"customers {m['customers']:.0f}, growth {m['growth']:+.1%}, churn {m['churn_rate']:.1%}, "
             f"open incident severity {m['open_incident_severity']:.1f}, mean fix time {m['mean_fix_weeks']:.2f} wk, "
             f"LLM ${m['llm_cost_usd']:.2f}.",
             f"Counts: {json.dumps(s['counts'])}. Approved spend per quarter: {json.dumps(s['spend_per_quarter'])}.",
             "Org: " + json.dumps(s["org"], separators=(",", ":")),
             "Highlights:"] + [f"- {h}" for h in s["highlights"]]
    for room, ctx in (s.get("rooms") or {}).items():
        lines.append(f"#{room} (read_context, tail): {ctx}")
    return "\n".join(lines)


# ───────────────────────── mutations: validate + describe ─────────────────────────


def _canon(m) -> str:
    return json.dumps(m, sort_keys=True, default=str)


def validate_mutation(env: Env, m, *, roster: dict | None = None, tried=()) -> tuple[Env | None, str | None]:
    """(new env, None) or (None, why it is not a valid single mutation of env)."""
    if not isinstance(m, dict) or not isinstance(m.get("op"), str):
        return None, 'mutation must be a JSON object with an "op"'
    op = m["op"]
    if op not in OPS:
        return None, f"unknown op {op!r}; allowed: {', '.join(OPS)}"
    rooms, names = _rooms(env), [a["name"] for a in env.agents]
    roster = roster or {}
    if op == "set_instructions":
        if m.get("agent") not in names:
            return None, f"no agent {m.get('agent')!r}; agents: {names}"
        if not isinstance(m.get("text"), str) or len(m["text"].strip()) < 20:
            return None, "set_instructions needs the full new instructions in \"text\""
    elif op in ("add_agent", "hire"):
        name = m.get("name") if op == "hire" else (m.get("agent") or {}).get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", name):
            return None, "the new agent needs a lowercase name ([a-z0-9-])"
        if name in names:
            return None, f"agent {name!r} already exists"
        if m.get("room") not in rooms:
            return None, f"\"room\" must be one of {rooms}"
        if op == "hire":
            if not isinstance(m.get("persona"), str):
                return None, "hire needs a persona id from the roster"
            if roster and m["persona"] not in roster:
                return None, f"persona {m['persona']!r} is not in the Agency roster"
    elif op in ("remove_agent", "fire"):
        if m.get("agent") not in names:
            return None, f"no agent {m.get('agent')!r}; agents: {names}"
        if m["agent"] in NO_FIRE:
            return None, f"{m['agent']} cannot be fired"
    elif op == "swap_persona":
        if m.get("agent") not in names or m["agent"] in NO_SWAP:
            return None, f"swap_persona needs an agent with a persona, one of {[n for n in names if n not in NO_SWAP]}"
        if not isinstance(m.get("persona"), str) or (roster and m["persona"] not in roster):
            return None, f"persona {m.get('persona')!r} is not in the Agency roster"
        if (_agent(env, m["agent"]) or {}).get("persona") == m["persona"]:
            return None, f"{m['agent']} already has persona {m['persona']}"
    elif op in ("link", "unlink"):
        pair = m.get("rooms")
        if not (isinstance(pair, list) and len(pair) == 2 and all(r in rooms for r in pair) and pair[0] != pair[1]):
            return None, f"\"rooms\" must be two different rooms of {rooms}"
        have = _linked(env, *pair)
        if op == "link" and have:
            return None, f"{pair} are already linked"
        if op == "unlink":
            if not have:
                return None, f"{pair} are not linked"
            m = {**m, "rooms": have}
    elif op == "set_policy":
        if not isinstance(m.get("key"), str) or not _num(m.get("value")) or m["value"] < 0:
            return None, "set_policy needs a \"key\" and a non-negative number \"value\""
    elif op == "set_budget":
        if not isinstance(m.get("dept"), str) or not _num(m.get("value")) or m["value"] < 0:
            return None, "set_budget needs a \"dept\" and a non-negative number \"value\""
        if m["dept"] not in rooms and m["dept"] not in (env.company.get("budgets") or {}):
            return None, f"no department {m['dept']!r}"
    elif op == "set_price":
        if not _num(m.get("value")) or not (1 <= m["value"] <= 1000):
            return None, "set_price needs a number \"value\" (1..1000)"
    for t in tried:
        if t.get("mutation") and _canon(t["mutation"]) == _canon(m):
            return None, f"already tried in generation {t['gen']} ({t['outcome']}): propose something else"
    try:
        new = apply_mutation(env, m)
    except (KeyError, ValueError, TypeError) as e:
        return None, f"apply_mutation failed: {type(e).__name__}: {e}"
    if env_diff(env, new) == "":
        return None, "the mutation changes nothing"
    return new, None


def describe(m: dict | None, env: Env | None = None) -> str:
    """One short line for the lineage table."""
    if not m:
        return "seed"
    op, env = m.get("op"), env
    pol = (env.company.get("policies") or {}) if env else {}
    bud = (env.company.get("budgets") or {}) if env else {}
    if op in ("link", "unlink"):
        return f"{op} #{m['rooms'][0]} ↔ #{m['rooms'][1]}"
    if op == "hire":
        return f"hire {m.get('name')} ({m.get('persona')}) into #{m.get('room')}"
    if op == "add_agent":
        return f"add {(m.get('agent') or {}).get('name')} to #{m.get('room')}"
    if op in ("fire", "remove_agent"):
        return f"fire {m.get('agent')}"
    if op == "swap_persona":
        old = (_agent(env, m.get("agent")) or {}).get("persona") if env else None
        return f"{m.get('agent')}: persona {old or '?'} → {m.get('persona')}"
    if op == "set_policy":
        return f"policy {m.get('key')}: {pol.get(m.get('key'), '—')} → {m.get('value')}"
    if op == "set_budget":
        return f"budget {m.get('dept')}: {bud.get(m.get('dept'), '—')} → {m.get('value')}"
    if op == "set_price":
        return f"price: {env.company.get('price') if env else '?'} → {m.get('value')}"
    if op == "set_instructions":
        return f"rewrite {m.get('agent')}'s instructions"
    return op or "?"


# ───────────────────────── proposers ─────────────────────────


class StubProposer:
    """Deterministic board for offline runs and tests: a short list of rules over the summary, first that
    applies and was not tried yet. Same contract as LLMProposer:
        (summary, env, *, history, error) -> ({"diagnosis", "mutation", "rationale"}, cost_usd)"""

    def __init__(self, roster: dict | None = None):
        self.roster = roster

    def candidates(self, s: dict, env: Env):
        m, c = s["metrics"], s["counts"]
        if not _linked(env, "maintenance", "dev") and m["mean_fix_weeks"] >= 1.5:
            yield ({"op": "link", "rooms": ["maintenance", "dev"]},
                   f"Mean fix time is {m['mean_fix_weeks']:.2f} weeks and every incident waited a week in #board "
                   "before reaching #dev.",
                   "Link #maintenance ↔ #dev so ops-maint hands sev1/sev2 incidents to synaps-dev directly.")
        n_test = sum(1 for r in env.rooms if r["name"] == "dev" for a in (r.get("agents") or [])
                     if str((_agent(env, a) or {}).get("persona", "")).startswith("testing/"))
        if c["regressions"] and n_test < 2:
            yield ({"op": "hire", "persona": "testing/testing-test-automation-engineer", "name": "qa", "room": "dev",
                    "role_note": "You are QA for acme-shop in #dev: review every synaps-dev PR for regressions "
                                 "before @board is asked to merge."},
                   f"{c['regressions']} fix(es) regressed after merge across the seeds and reopened incidents.",
                   "Hire a QA agent (Agency testing-test-automation-engineer) into #dev to catch regressions "
                   "before the merge.")
        veto = float((env.company.get("policies") or {}).get("finance_veto_usd", 5000))
        mk = s["spend_per_quarter"].get("marketing", 0.0)
        if mk and veto > 2000:
            yield ({"op": "set_policy", "key": "finance_veto_usd", "value": 2000},
                   f"Marketing booked ${mk:,.0f} a quarter with no review: every weekly campaign is under the "
                   f"${veto:,.0f} veto, and campaigns bring few customers.",
                   "Lower the finance veto to $2,000 so each weekly campaign needs a #board sign-off.")
        if not _linked(env, "growth", "dev") and sum(c["features_per_seed"]) == 0:
            yield ({"op": "link", "rooms": ["growth", "dev"]},
                   "No feature shipped this quarter; requests wait in #board behind incidents.",
                   "Link #growth ↔ #dev so growth hands the top feature to synaps-dev directly.")
        b = float((env.company.get("budgets") or {}).get("marketing", 0))
        if b > 0:
            yield ({"op": "set_budget", "dept": "marketing", "value": int(b * 0.8)},
                   f"Marketing spends ${b:,.0f} a quarter for few customers.", "Cut the marketing budget by 20%.")

    def __call__(self, summary: dict, env: Env, *, history=(), error=None):
        for mut, diag, why in self.candidates(summary, env):
            _, err = validate_mutation(env, mut, roster=self.roster, tried=history)
            if err is None:
                return {"diagnosis": diag, "mutation": mut, "rationale": why}, 0.0
        return {"diagnosis": "no rule applies", "mutation": {"op": "noop"}, "rationale": ""}, 0.0


EVOLVE_PROTOCOL = """You are the board of acme-shop's AI company, reviewing the quarter that just ended.
Find where value leaked and propose exactly ONE change to the org design (the env) for the next quarter.
The humans in #board approve or reject it; an approved change is trialed on the same market seeds and kept
only if fitness improves. fitness = {w_profit}*profit + {w_growth}*growth - {w_inc}*open_incident_severity
- {w_llm}*llm_cost_usd.

How the org shapes a quarter (the highlights' "org:" and "quarter mechanics:" lines count it): a department's
sev1/sev2 incident reaches synaps-dev the same week only over a direct link between its room and #dev; otherwise
the board relays it at its next weekly sync, a week later. synaps-dev takes at most dev_capacity items a week.
Every testing/* agent in #dev reviews each PR: more reviewers catch more regressions before the merge (an uncaught
one reopens the incident), but each extra reviewer is another review round that costs tokens and may slip the
merge a week. Open incidents cost churn and sales every week they stay open.

Allowed mutations (exactly one, as JSON):
{{"op":"link","rooms":["<room>","<room>"]}}            direct line between two rooms (else they talk via #board)
{{"op":"unlink","rooms":["<room>","<room>"]}}
{{"op":"hire","persona":"<roster id>","name":"<new-agent-name>","room":"<room>","role_note":"<duties>"}}
{{"op":"fire","agent":"<agent>"}}                        never board or synaps-dev
{{"op":"swap_persona","agent":"<agent>","persona":"<roster id>"}}
{{"op":"set_instructions","agent":"<agent>","text":"<the full new instructions>"}}
{{"op":"set_policy","key":"<policy>","value":<number>}}   e.g. finance_veto_usd, sev1_fix_weeks
{{"op":"set_budget","dept":"<dept>","value":<number>}}
{{"op":"set_price","value":<number>}}

Rules: one small, explainable change; cite the numbers that justify it; do not repeat a change already
tried (listed under History); hire / swap only personas from the Roster shortlist.
Reply with {{"diagnosis":"<where value leaked, 1-2 sentences with numbers>","mutation":{{...}},
"rationale":"<why this change and its expected effect, 1-2 sentences>"}}"""


def board_persona(env: Env, roster: dict | None) -> str:
    spec = _agent(env, "board") or {"name": "board", "persona": "project-management/project-manager-senior",
                                    "instructions": "You are the board agent for acme-shop."}
    from company.depts.agent import instructions_of
    return instructions_of(spec, roster or {})


class LLMProposer:
    """The board persona proposing one mutation with company.llm.complete_json (Haiku, via the broker)."""

    def __init__(self, llm: Callable | None = None, roster: dict | None = None, weights: dict | None = None,
                 shortlist=SHORTLIST):
        self.llm = llm
        self.roster = roster
        self.weights = weights or load_weights()
        self.shortlist = shortlist

    def _llm(self, system, prompt):
        if self.llm is not None:
            return self.llm(system, prompt)
        from company.llm import complete_json
        return complete_json(system, prompt)

    def system(self, env: Env) -> str:
        w = self.weights
        return (board_persona(env, self.roster) + "\n\n" +
                EVOLVE_PROTOCOL.format(w_profit=w.get("profit", 0.001), w_growth=w.get("growth", 0.5),
                                       w_inc=w.get("incident_penalty", 2.0), w_llm=w.get("llm_cost", 1.0)))

    def prompt(self, summary: dict, env: Env, history=(), error=None) -> str:
        roster = self.roster or {}
        short = [f"- {pid}: {roster[pid]['name']}: {roster[pid]['description'][:90]}" if pid in roster else f"- {pid}"
                 for pid in self.shortlist if not roster or pid in roster]
        hist = [f"- gen {h['gen']}: {h.get('change')} → {h.get('outcome')} ({h.get('reason') or ''})" for h in history]
        parts = [summary_text(summary), "", "History:", *(hist or ["- none yet"]), "", "Roster shortlist:", *short]
        if error:
            parts += ["", f"Your previous proposal was invalid: {error}. Propose a different, valid single mutation."]
        return "\n".join(parts)

    def __call__(self, summary: dict, env: Env, *, history=(), error=None):
        obj, cost = self._llm(self.system(env), self.prompt(summary, env, history, error))
        mut = obj.get("mutation")
        if mut is None and "op" in obj:
            mut = {k: v for k, v in obj.items() if k not in ("diagnosis", "rationale")}
        return {"diagnosis": str(obj.get("diagnosis", "")), "mutation": mut,
                "rationale": str(obj.get("rationale", ""))}, float(cost or 0.0)


# ───────────────────────── the gate ─────────────────────────


def gate_text(p: dict, approvers, board: str = "board", max_diff: int = 6000) -> str:
    who = " ".join(f"@{a}" for a in approvers)
    diff = p.get("diff") or ""
    if len(diff) > max_diff:
        diff = diff[:max_diff] + "\n… (diff truncated)"
    return "\n".join([
        f"{who} Generation {p['gen']}: the board proposes {p['parent']} → {p['candidate']}.",
        f"Change: {p['change']}",
        f"Diagnosis: {p.get('diagnosis') or '-'}",
        f"Rationale: {p.get('rationale') or '-'}",
        f"{p['parent']} scored fitness {p['parent_fitness']:.2f} on seeds {p['seeds']}. If approved, {p['candidate']} "
        "runs the same seeds and is kept only if it scores higher.",
        "```diff", diff.rstrip(), "```",
        f"Reply in this thread with `@{board} approve` or `@{board} reject <why>`. Only a person on the approvers "
        f"list decides; no decision in {int(p['timeout_s'])}s = auto-reject.",
    ])


def outcome_text(rec: dict) -> str:
    o = rec["outcome"]
    if o == "kept":
        return (f"Kept {rec['env']}: fitness {rec['fitness']:.2f} > {rec['parent_fitness']:.2f} "
                f"({rec['parent']}) on seeds {rec['seeds']}.")
    if o == "reverted":
        return f"Reverted {rec['env']}: {rec['reason']}. {rec['parent']} stays."
    return f"Not trialed ({o}): {rec['reason']}. {rec['parent']} stays."


def _reason_text(text: str) -> str:
    t = re.sub(r"^(\s*@[A-Za-z0-9._-]+[\s,:]*)+", "", text or "").strip()
    t = re.sub(r"^(approve|approved|lgtm|reject|rejected|no|deny|denied)\b[\s,:.-]*", "", t, flags=re.I)
    return t.strip()


def classify(v: Any, *, thread: str | None, approvers, timeout_s: float) -> dict:
    """The resume value → {"decision": approve|reject|timeout|None, ...}. None = ignored, keep waiting."""
    if not isinstance(v, dict):
        return {"decision": None, "why": "not a message"}
    if v.get("timeout"):
        return {"decision": "timeout", "approver": None,
                "reason": f"approval timeout after {int(timeout_s)}s: auto-reject"}
    base = {"id": v.get("id"), "sender": v.get("sender"), "sender_is_agent": v.get("sender_is_agent", True),
            "text": str(v.get("text", ""))[:200]}
    if thread and v.get("thread") not in (None, thread):
        return {"decision": None, **base, "why": "another thread"}
    if v.get("sender_is_agent", True):      # fail closed: no flag = not a person
        return {"decision": None, **base, "why": "sender is an agent"}
    if str(v.get("sender") or "").lstrip("@").lower() not in {a.lower() for a in approvers}:
        return {"decision": None, **base, "why": "not on the approvers list"}
    d = parse_decision(v.get("text") or "")
    if d is None:
        return {"decision": None, **base, "why": "not approve/reject"}
    return {"decision": d, "approver": v.get("sender"), "message": v.get("id"),
            "reason": _reason_text(v.get("text") or "")}


class ScriptedGate:
    """Offline approver. script[gen-1] is that generation's thread replies, in order: "approve",
    "reject: <why>" (from `human`), or a full message dict (e.g. an agent's approve, to be ignored).
    An exhausted script waits forever → the driver's deadline → timeout."""

    def __init__(self, script=(), human="Haseeb", board="board", kill_after_ask: set | None = None):
        self.script = [list(s) if isinstance(s, (list, tuple)) else [s] for s in script]
        self.human, self.board = human, board
        self.asked: list[dict] = []
        self.announced: list[tuple[str, str]] = []
        self._pos: dict[str, int] = {}
        self.kill_after_ask = set(kill_after_ask or ())

    def ask(self, payload: dict) -> str:
        self.asked.append(payload)
        return f"script-{payload['run']}-g{payload['gen']}"

    def wait(self, thread: str, deadline: float):
        gen = int(thread.rsplit("-g", 1)[1])
        if gen in self.kill_after_ask:
            self.kill_after_ask.discard(gen)
            raise Paused(f"killed while paused at the gate (gen {gen})")
        items = self.script[gen - 1] if gen - 1 < len(self.script) else []
        i = self._pos.get(thread, 0)
        if i >= len(items) or items[i] in (None, "timeout"):
            return None
        self._pos[thread] = i + 1
        it = items[i]
        if isinstance(it, dict):
            return {"thread": thread, "id": f"{thread}-m{i}", **it}
        return {"id": f"{thread}-m{i}", "thread": thread, "sender": self.human, "sender_is_agent": False,
                "text": f"@{self.board} {it}"}

    def announce(self, thread: str, text: str):
        self.announced.append((thread, text))


class TransportGate:
    """The gate over a Transport (FakeTransport offline, SwitchTransport live): posts the proposal to the
    board room as the board agent, buffers every threaded reply addressed to the board, hands them to the
    driver one by one (the graph decides; agents and non-approvers are ignored there). Like
    realwork.MergeGate, but the decision is applied inside the LangGraph run, not here.

    `transport=` an open transport, or `factory=` a () -> transport the gate opens on first use and closes
    in release() (called before every trial quarter): on Switch, quarter.py's runner opens the same board
    identity (same credentials, cursor and connection id), so the two must never be connected at once."""

    def __init__(self, transport=None, *, factory: Callable | None = None, board="board", room="board",
                 approvers=None, log=None, keep=50):
        if (transport is None) == (factory is None):
            raise ValueError("TransportGate needs exactly one of transport= or factory=")
        self.board, self.room, self.factory, self.keep = board, room, factory, keep
        self.approvers = tuple(approvers or approvers_from_env())
        self.log = log or (lambda *a: None)
        self._cv = threading.Condition()
        self._inbox: dict[str, deque] = {}
        self.seen: list[dict] = []
        self.opened = 0
        self._t = transport
        if transport is not None:
            transport.on_message(board, self.handle)

    @property
    def t(self):
        if self._t is None:
            t = self.factory()
            t.on_message(self.board, self.handle)
            t.start()
            self._t, self.opened = t, self.opened + 1
        return self._t

    def release(self) -> None:
        if self.factory is not None and self._t is not None:
            t, self._t = self._t, None
            t.stop()

    def ask(self, payload: dict) -> str:
        return self.t.post(self.board, self.room, gate_text(payload, self.approvers, self.board))

    def handle(self, msg) -> None:
        d = asdict(msg) if is_dataclass(msg) else dict(msg)
        if not d.get("thread"):
            return
        with self._cv:
            self.seen = (self.seen + [d])[-self.keep:]
            self._inbox.setdefault(d["thread"], deque(maxlen=self.keep)).append(d)
            self._cv.notify_all()

    def wait(self, thread: str, deadline: float):
        self.t                                       # (re)connect: replies sent meanwhile are redelivered
        with self._cv:
            while True:
                q = self._inbox.get(thread)
                if q:
                    return q.popleft()
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 5.0))

    def announce(self, thread: str, text: str):
        try:
            self.t.post(self.board, self.room, text, thread=thread)
        except Exception as e:  # noqa: BLE001 - the record is already written
            self.log(f"gate: outcome post failed: {type(e).__name__}: {e}")


def switch_room_reader(transport, agent: str, rooms: dict[str, str] | None = None, *, room_prefix: str = "",
                       limit: int = 15) -> Callable:
    """read_rooms for reflect on Switch: read_context of every env room. transport: a SwitchTransport, or a
    () -> SwitchTransport (e.g. `lambda: gate.t`). rooms: env room -> Switch room id or name; default
    room_prefix + the env room's name, resolved by name (list_rooms)."""
    def read(env: Env) -> dict:
        out = {}
        t = transport() if callable(transport) and not hasattr(transport, "_link") else transport
        link = t._link(agent)
        for name in _rooms(env):
            try:
                rid = t.room_id((rooms or {}).get(name) or room_prefix + name, via=link)
                ctx = link.op("read_context", {"room_id": rid, "limit": limit}, room_id=rid)
            except Exception as e:  # noqa: BLE001
                out[name] = f"(read_context failed: {type(e).__name__})"
                continue
            msgs = ctx.get("messages") if isinstance(ctx, dict) else ctx
            lines = [f"{m.get('sender_name') or m.get('sender')}: {str(m.get('body', ''))[:160]}"
                     for m in (msgs or []) if isinstance(m, dict)]
            out[name] = " | ".join(lines[-limit:])
        return out
    return read


# ───────────────────────── the graph ─────────────────────────


class EvolveState(TypedDict, total=False):
    run: str
    runner: str
    seeds: list
    generations: int
    budget_usd: float | None
    timeout_s: float
    gen: int
    seed_env: str
    current: dict
    current_eval: dict
    spent_usd: float
    est_gen_usd: float
    summary: dict
    proposal: dict | None
    candidate: dict | None
    diff: str
    gate: dict | None
    trial: dict | None
    record: dict | None
    history: list
    stop_reason: str | None


def _strip_eval(ev: dict) -> dict:
    """What the lineage keeps from an evaluation (no full logs)."""
    return {"fitness": ev["fitness"], "metrics": ev["metrics"], "cost_usd": ev["cost_usd"],
            "per_seed": [{"seed": p["seed"], "fitness": p["fitness"], "metrics": p["metrics"]} for p in ev["per_seed"]]}


def build_graph(*, run_quarter, propose, gate, weights, approvers, lineage_path, envs_dir, read_rooms=None,
                roster=None, log=None):
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    say = log or (lambda *a: None)
    lineage_path = Path(lineage_path)
    envs_dir = Path(envs_dir)

    def _over_budget(s) -> str | None:
        b = s.get("budget_usd")
        if b is None:
            return None
        spent, est = s.get("spent_usd", 0.0), s.get("est_gen_usd", 0.0)
        if spent >= b:
            return f"budget: spent ${spent:.4f} of ${b:.2f}"
        if spent + est > b:
            return f"budget: spent ${spent:.4f}, the next generation (~${est:.4f}) would pass ${b:.2f}"
        return None

    def _write_record(s, rec):
        if any(r.get("run") == rec["run"] and r.get("gen") == rec["gen"] for r in read_lineage(lineage_path)):
            return                                   # already written before a crash: resume must not duplicate
        append_lineage(lineage_path, rec)

    def baseline(s):
        env = env_from_dict(s["current"])
        ev = evaluate(env, s["seeds"], run_quarter, weights, env_index(env.name))
        say(f"gen 0: {env.name} fitness {ev['fitness']:.3f} on seeds {s['seeds']} (quarters ${ev['cost_usd']:.4f})")
        envs_dir.mkdir(parents=True, exist_ok=True)
        if not (envs_dir / f"{env.name}.yaml").exists():
            save_env(env, envs_dir / f"{env.name}.yaml")
        rec = {"run": s["run"], "runner": s.get("runner"), "gen": 0, "env": env.name, "parent": env.parent,
               "mutation": None, "change": "seed",
               "diagnosis": None, "rationale": "seed design", "approver": None, "decision": None, "outcome": "seed",
               "reason": None, "fitness": ev["fitness"], "parent_fitness": None, "delta": None, "kept": True,
               "seeds": s["seeds"], "metrics": ev["metrics"], "eval": _strip_eval(ev),
               "cost_usd": {"quarters": ev["cost_usd"], "proposal": 0.0}, "ts": time.time()}
        _write_record(s, rec)
        spent = s.get("spent_usd", 0.0) + ev["cost_usd"]
        return {"current_eval": ev, "spent_usd": round(spent, 6), "est_gen_usd": ev["cost_usd"], "gen": 0,
                "record": rec, "history": []}

    def reflect(s):
        env = env_from_dict(s["current"])
        rooms = None
        if read_rooms is not None:
            try:
                rooms = read_rooms(env)
            except Exception as e:  # noqa: BLE001 - reflection works without the rooms
                say(f"read_rooms failed: {type(e).__name__}: {e}")
        gen = s.get("gen", 0) + 1
        say(f"gen {gen}: reflect on {env.name} (fitness {s['current_eval']['fitness']:.3f})")
        return {"gen": gen, "summary": summarize(env, s["current_eval"], rooms), "proposal": None, "candidate": None,
                "diff": "", "gate": None, "trial": None, "record": None}

    def propose_node(s):
        env = env_from_dict(s["current"])
        hist = s.get("history") or []
        attempts, err, cost, cand, prop = [], None, 0.0, None, None
        for _ in (1, 2):
            try:
                prop, c = propose(s["summary"], env, history=hist, error=err)
            except Exception as e:  # noqa: BLE001 - LLMError and friends: book the cost, re-ask once
                c, prop = float(getattr(e, "cost_usd", 0.0) or 0.0), None
                err = f"proposer failed: {type(e).__name__}: {str(e)[:200]}"
            else:
                cand, err = validate_mutation(env, (prop or {}).get("mutation"), roster=roster, tried=hist)
            cost += c
            attempts.append({"mutation": (prop or {}).get("mutation"), "error": err, "cost_usd": c})
            if err is None:
                break
        spent = round(s.get("spent_usd", 0.0) + cost, 6)
        prop = {**(prop or {}), "cost_usd": round(cost, 6), "attempts": attempts, "error": err}
        if err is not None:
            say(f"gen {s['gen']}: no valid proposal after {len(attempts)} attempts: {err}")
            return {"proposal": prop, "spent_usd": spent, "candidate": None}
        cand.name, cand.parent = f"env{env_index(s['seed_env']) + s['gen']}", env.name
        diff = env_diff(env, cand)
        say(f"gen {s['gen']}: propose {describe(prop['mutation'], env)}  (${cost:.4f})")
        return {"proposal": prop, "spent_usd": spent, "candidate": env_to_dict(cand), "diff": diff}

    def _payload(s, g):
        env = env_from_dict(s["current"])
        p = s["proposal"]
        return {"kind": GATE_KIND, "run": s["run"], "gen": s["gen"], "parent": env.name,
                "candidate": s["candidate"]["name"], "change": describe(p["mutation"], env),
                "mutation": p["mutation"], "diagnosis": p.get("diagnosis"), "rationale": p.get("rationale"),
                "diff": s["diff"], "parent_fitness": s["current_eval"]["fitness"], "seeds": s["seeds"],
                "timeout_s": s["timeout_s"], "thread": (g or {}).get("thread"),
                "deadline": (g or {}).get("deadline"), "ignored": len((g or {}).get("ignored", []))}

    def post_gate(s):
        payload = _payload(s, None)
        thread = gate.ask(payload)
        now = time.time()
        say(f"gen {s['gen']}: asked #board (thread {thread}), waiting up to {int(s['timeout_s'])}s")
        return {"gate": {"thread": thread, "posted_at": now, "deadline": now + float(s["timeout_s"]),
                         "ignored": [], "decision": None}}

    def await_gate(s):
        g = dict(s["gate"])
        v = interrupt(_payload(s, g))
        c = classify(v, thread=g["thread"], approvers=approvers, timeout_s=s["timeout_s"])
        if c["decision"] is None:
            say(f"gen {s['gen']}: gate ignored {c.get('sender')!r} (agent={c.get('sender_is_agent')}): {c['why']}")
            g["ignored"] = list(g.get("ignored", [])) + [c]
        else:
            say(f"gen {s['gen']}: gate {c['decision']} by {c.get('approver')} {c.get('reason') or ''}".rstrip())
            g["decision"] = c
        return {"gate": g}

    def _release_gate():
        if hasattr(gate, "release"):
            gate.release()

    def trial(s):
        _release_gate()                              # the quarter may open the board's own Switch identity
        cand = env_from_dict(s["candidate"])
        ev = evaluate(cand, s["seeds"], run_quarter, weights, env_index(cand.name))
        say(f"gen {s['gen']}: trial {cand.name} fitness {ev['fitness']:.3f} (quarters ${ev['cost_usd']:.4f})")
        return {"trial": ev, "spent_usd": round(s.get("spent_usd", 0.0) + ev["cost_usd"], 6)}

    def select(s):
        parent, ev = s["current_eval"], s["trial"]
        kept = ev["fitness"] > parent["fitness"]
        out = {}
        if kept:
            cand = env_from_dict(s["candidate"])
            envs_dir.mkdir(parents=True, exist_ok=True)
            save_env(cand, envs_dir / f"{cand.name}.yaml")
            out.update(current=s["candidate"], current_eval=ev)
        return {**out, "record": _gen_record(s, "kept" if kept else "reverted", parent, ev)}

    def _gen_record(s, outcome, parent_eval, ev):
        parent_env = env_from_dict(s["current"])         # still the parent: updates land after the node
        p = s.get("proposal") or {}
        g = s.get("gate") or {}
        dec = g.get("decision") or {}
        pf = parent_eval["fitness"]
        if outcome in ("kept", "reverted"):
            cmp = ">" if outcome == "kept" else "<="
            reason = (f"fitness {ev['fitness']:.3f} {cmp} parent {pf:.3f} on seeds {s['seeds']} "
                      f"(Δ {ev['fitness'] - pf:+.3f}): {why_moved(parent_eval, ev)}")
        elif outcome == "invalid":
            reason = f"no valid mutation after {len(p.get('attempts') or [])} attempts: {p.get('error')}"
        elif outcome == "timeout":
            reason = dec.get("reason")
        else:
            reason = f"rejected by {dec.get('approver')}: {dec.get('reason') or '(no reason given)'}"
        return {"run": s["run"], "runner": s.get("runner"), "gen": s["gen"],
                "env": (s.get("candidate") or {}).get("name") or f"env{env_index(s['seed_env']) + s['gen']}",
                "parent": parent_env.name, "mutation": p.get("mutation"),
                "change": describe(p.get("mutation"), parent_env),
                "diagnosis": p.get("diagnosis"), "rationale": p.get("rationale"),
                "approver": dec.get("approver"), "decision": dec.get("decision"), "outcome": outcome, "reason": reason,
                "fitness": ev["fitness"] if ev else None, "parent_fitness": pf,
                "delta": round(ev["fitness"] - pf, 4) if ev else None, "kept": outcome == "kept",
                "seeds": s["seeds"], "metrics": ev["metrics"] if ev else None, "eval": _strip_eval(ev) if ev else None,
                "gate": {"thread": g.get("thread"), "ignored": g.get("ignored", [])} if g else None,
                "cost_usd": {"quarters": ev["cost_usd"] if ev else 0.0, "proposal": p.get("cost_usd", 0.0)},
                "diff": s.get("diff") or None, "ts": time.time()}

    def not_trialed(s):
        g = s.get("gate") or {}
        outcome = "invalid" if s.get("candidate") is None else (
            "timeout" if (g.get("decision") or {}).get("decision") == "timeout" else "rejected")
        return {"record": _gen_record(s, outcome, s["current_eval"], None)}

    def record(s):
        rec = s["record"]
        _write_record(s, rec)
        say(f"gen {rec['gen']}: {rec['outcome']} — {rec['reason']}")
        if (s.get("gate") or {}).get("thread") and hasattr(gate, "announce"):
            gate.announce(s["gate"]["thread"], outcome_text(rec))
        hist = list(s.get("history") or []) + [{k: rec[k] for k in ("gen", "env", "change", "mutation", "outcome",
                                                                     "reason", "fitness", "parent_fitness")}]
        est = rec["cost_usd"]["quarters"] + rec["cost_usd"]["proposal"]
        return {"history": hist, "est_gen_usd": max(est, s.get("est_gen_usd", 0.0))}

    def after_record(s):
        if s.get("gen", 0) >= s["generations"]:
            return "done"
        return "stop" if _over_budget(s) else "reflect"

    def done(s):
        why = _over_budget(s) if s.get("gen", 0) < s["generations"] else f"{s['generations']} generations done"
        say(f"stop: {why}; best {s['current']['name']} fitness {s['current_eval']['fitness']:.3f}, "
            f"spent ${s.get('spent_usd', 0.0):.4f}")
        return {"stop_reason": why}

    b = StateGraph(EvolveState)
    for name, fn in (("baseline", baseline), ("reflect", reflect), ("propose", propose_node),
                     ("post_gate", post_gate), ("await_gate", await_gate), ("trial", trial), ("select", select),
                     ("not_trialed", not_trialed), ("record", record), ("done", done)):
        b.add_node(name, fn)
    b.add_edge(START, "baseline")
    b.add_conditional_edges("baseline", lambda s: "done" if s["generations"] <= 0 or _over_budget(s) else "reflect",
                            ["reflect", "done"])
    b.add_edge("reflect", "propose")
    b.add_conditional_edges("propose", lambda s: "post_gate" if s.get("candidate") else "not_trialed",
                            ["post_gate", "not_trialed"])
    b.add_edge("post_gate", "await_gate")

    def after_gate(s):
        d = ((s.get("gate") or {}).get("decision") or {}).get("decision")
        return "await_gate" if d is None else ("trial" if d == "approve" else "not_trialed")

    b.add_conditional_edges("await_gate", after_gate, ["await_gate", "trial", "not_trialed"])
    b.add_edge("trial", "select")
    b.add_edge("select", "record")
    b.add_edge("not_trialed", "record")
    b.add_conditional_edges("record", after_record, {"reflect": "reflect", "done": "done", "stop": "done"})
    b.add_edge("done", END)
    return b


# ───────────────────────── the driver ─────────────────────────


def _saver(checkpoint):
    """(checkpointer, closer). checkpoint: a sqlite path, an existing saver, or None (in memory)."""
    if checkpoint is None:
        from langgraph.checkpoint.memory import InMemorySaver
        return InMemorySaver(), (lambda: None)
    if isinstance(checkpoint, (str, Path)):
        from langgraph.checkpoint.sqlite import SqliteSaver
        Path(checkpoint).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(checkpoint), check_same_thread=False)
        return SqliteSaver(conn), conn.close
    return checkpoint, (lambda: None)


def thread_id_for(env: Env, seeds) -> str:
    return f"evolve:{env.name}:{'-'.join(str(int(s)) for s in seeds)}"


def load_quarter_runner(kind: str = "stub", **opts) -> Callable:
    """"stub" → company.board.stub_quarter.run_quarter; "quarter" → #10's company.board.quarter.run_quarter
    (imported only here, lazily), with `opts` (QuarterConfig fields: transport, agent_prefix, db, ...) bound."""
    import functools
    if kind == "stub":
        from company.board.stub_quarter import run_quarter
        fn = run_quarter
    elif kind == "quarter":
        try:
            from company.board import quarter as q   # #10, the LangGraph inner loop
        except ImportError as e:
            raise RuntimeError("company/board/quarter.py (#10) is not on this branch yet; use --quarter stub") from e
        fn = getattr(q, "run_quarter", None)
        if fn is None:
            raise RuntimeError("company.board.quarter has no run_quarter(env, *, seed, generation)")
    else:
        raise ValueError(f"unknown quarter runner {kind!r}")
    if not opts:
        return fn
    bound = functools.partial(fn, **opts)
    bound.__module__, bound.__name__ = fn.__module__, fn.__name__
    return bound


def evolve(env, *, generations: int = 3, seeds=DEFAULT_SEEDS, run_quarter: Callable | None = None,
           propose: Callable | None = None, gate=None, lineage_path="company/lineage.jsonl",
           envs_dir="company/envs", budget_usd: float | None = DEFAULT_BUDGET_USD, weights: dict | None = None,
           approvers=None, gate_timeout_s: float | None = None, checkpoint="company/state/evolve.sqlite",
           thread_id: str | None = None, read_rooms: Callable | None = None, roster: dict | None = None,
           fresh: bool = False, log: Callable | None = None) -> dict:
    """Run (or resume) the evolution. Returns {run, best, best_fitness, records, stop_reason, spent_usd}.

    Resumes the checkpointed thread when it exists (also paused at the gate); a finished thread asked for
    more generations continues; fresh=True drops the thread and its lineage rows first."""
    from langgraph.types import Command

    env = load_env(env) if isinstance(env, (str, Path)) else env
    seeds = [int(s) for s in seeds]
    say = log or (lambda *a: None)
    run_quarter = run_quarter or load_quarter_runner("stub")
    if roster is None:
        from company.depts.agent import load_personas
        roster = load_personas()
    propose = propose or StubProposer(roster)
    gate = gate if gate is not None else ScriptedGate()
    approvers = tuple(approvers or getattr(gate, "approvers", None) or approvers_from_env())
    weights = weights or load_weights()
    timeout_s = float(gate_timeout_s if gate_timeout_s is not None else DEFAULT_GATE_TIMEOUT_S)
    run = thread_id or thread_id_for(env, seeds)
    saver, close = _saver(checkpoint)
    try:
        graph = build_graph(run_quarter=run_quarter, propose=propose, gate=gate, weights=weights,
                            approvers=approvers, lineage_path=lineage_path, envs_dir=envs_dir,
                            read_rooms=read_rooms, roster=roster, log=say).compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": run}, "recursion_limit": 10_000}
        if fresh:
            saver.delete_thread(run)
            p = Path(lineage_path)
            if p.exists():
                keep = [r for r in read_lineage(p) if r.get("run") != run]
                p.write_text("".join(json.dumps(r, default=str) + "\n" for r in keep), encoding="utf-8")
        snap = graph.get_state(cfg)
        if snap.values and snap.next:
            say(f"resume {run} at {list(snap.next)}")
            inp = None
        elif snap.values:
            v = snap.values
            if v.get("gen", 0) < generations and not str(v.get("stop_reason") or "").startswith("budget"):
                say(f"continue {run}: {v.get('gen')} → {generations} generations")
                graph.update_state(cfg, {"generations": generations, "stop_reason": None}, as_node="record")
                inp = None
            else:
                return _result(run, snap.values, lineage_path)
        else:
            runner = f"{getattr(run_quarter, '__module__', '?')}.{getattr(run_quarter, '__name__', '?')}"
            inp = {"run": run, "runner": runner, "seeds": seeds, "generations": int(generations),
                   "budget_usd": budget_usd,
                   "timeout_s": timeout_s, "seed_env": env.name, "current": env_to_dict(env), "spent_usd": 0.0,
                   "est_gen_usd": 0.0, "history": []}
        while True:
            graph.invoke(inp, cfg)
            snap = graph.get_state(cfg)
            if not snap.next:
                return _result(run, snap.values, lineage_path)
            asks = [i.value for i in snap.interrupts if isinstance(i.value, dict) and i.value.get("kind") == GATE_KIND]
            if not asks:
                raise RuntimeError(f"{run} paused at {snap.next} without a gate interrupt")
            p = asks[0]
            msg = gate.wait(p["thread"], p["deadline"])
            inp = Command(resume=msg if msg is not None else {"timeout": True, "at": time.time()})
    finally:
        if hasattr(gate, "release"):
            gate.release()
        close()


def _result(run: str, v: dict, lineage_path) -> dict:
    recs = [r for r in read_lineage(lineage_path) if r.get("run") == run]
    return {"run": run, "best": (v.get("current") or {}).get("name"),
            "best_fitness": (v.get("current_eval") or {}).get("fitness"), "records": recs,
            "stop_reason": v.get("stop_reason"), "spent_usd": v.get("spent_usd"), "generations": v.get("gen")}


# ───────────────────────── CLI ─────────────────────────

# Offline demo approver: gen 1 an agent's approve (ignored), then a person's approve; gen 2 approve;
# gen 3 a reject with a reason.
DEFAULT_SCRIPT = (
    [{"sender": "ops-maint", "sender_is_agent": True, "text": "@board approve"}, "approve"],
    ["approve"],
    ["reject: that puts every weekly campaign on our desk in #board"],
)


def _parse_script(text: str | None):
    if not text:
        return DEFAULT_SCRIPT
    return tuple([x.strip()] for x in text.split("|"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m company.board.evolve", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default="company/envs/env1.yaml")
    ap.add_argument("--generations", type=int, default=3)
    ap.add_argument("--seeds", default="1,2")
    ap.add_argument("--offline", action="store_true", help="stub quarter + stub proposer + scripted approver")
    ap.add_argument("--quarter", choices=("stub", "quarter"),
                    help="quarter runner (default: stub offline, else #10's quarter)")
    ap.add_argument("--proposer", choices=("stub", "llm"), help="default: stub offline, else llm (Haiku)")
    ap.add_argument("--script", help='scripted approver, one entry per generation, "|"-separated '
                                     '(e.g. "approve|approve|reject: too risky")')
    ap.add_argument("--transport", choices=("script", "switch"), default=None,
                    help="where the gate asks: script (offline) or switch (SwitchTransport, the board agent)")
    ap.add_argument("--agent-prefix", default="", help="Switch registry name = prefix + env agent name")
    ap.add_argument("--room-prefix", default="", help="Switch room name = prefix + env room name")
    ap.add_argument("--state-dir", default="company/state")
    ap.add_argument("--approvers", help="comma list (default $COMPANY_APPROVERS or haseeb,aditya)")
    ap.add_argument("--gate-timeout", type=float, default=None, help="seconds; no decision = auto-reject")
    ap.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD)
    ap.add_argument("--out", help="write lineage.jsonl, lineage.md and envs/ here (default: company/)")
    ap.add_argument("--lineage")
    ap.add_argument("--envs-dir")
    ap.add_argument("--checkpoint", default="company/state/evolve.sqlite")
    ap.add_argument("--thread")
    ap.add_argument("--fresh", action="store_true", help="drop this thread's checkpoint + lineage rows first")
    a = ap.parse_args(argv)

    out = Path(a.out) if a.out else None
    lineage = Path(a.lineage or (out / "lineage.jsonl" if out else "company/lineage.jsonl"))
    envs_dir = Path(a.envs_dir or (out / "envs" if out else "company/envs"))
    quarter = a.quarter or ("stub" if a.offline else "quarter")
    proposer = a.proposer or ("stub" if a.offline else "llm")
    transport = a.transport or ("script" if a.offline else "switch")
    approvers = tuple(x.strip() for x in a.approvers.split(",")) if a.approvers else None
    board = a.agent_prefix + "board"
    qopts = {}
    if quarter == "quarter":                      # QuarterConfig fields; company/run.py passes the full live set
        qopts = {"transport": "fake" if transport == "script" else "switch", "state_dir": a.state_dir}
        if transport == "switch":
            qopts.update(agent_prefix=a.agent_prefix, room_prefix=a.room_prefix)
    lines: list[str] = []

    def say(*x):
        line = " ".join(map(str, x))
        lines.append(line)
        print(line, flush=True)

    from company.depts.agent import load_personas
    roster = load_personas()
    gate, read_rooms = None, None
    if transport == "script":
        gate = ScriptedGate(_parse_script(a.script))
        approvers = approvers or ("haseeb",)
    else:
        from company.transport import SwitchTransport

        def factory():
            return SwitchTransport(agents=[board], state_dir=a.state_dir, log=lambda *x: say("transport:", *x))
        gate = TransportGate(factory=factory, board=board, room=a.room_prefix + "board", approvers=approvers,
                             log=say)
        read_rooms = switch_room_reader(lambda: gate.t, board, room_prefix=a.room_prefix)
    res = evolve(a.env, generations=a.generations, seeds=[int(s) for s in a.seeds.split(",")],
                 run_quarter=load_quarter_runner(quarter, **qopts),
                 propose=StubProposer(roster) if proposer == "stub" else LLMProposer(roster=roster),
                 gate=gate, lineage_path=lineage, envs_dir=envs_dir, budget_usd=a.budget_usd, approvers=approvers,
                 gate_timeout_s=a.gate_timeout, checkpoint=a.checkpoint, thread_id=a.thread, read_rooms=read_rooms,
                 roster=roster, fresh=a.fresh, log=say)
    from company.lineage_report import render
    md = render(res["records"])
    if out:
        (out / "lineage.md").write_text(md, encoding="utf-8")
        (out / "run.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
