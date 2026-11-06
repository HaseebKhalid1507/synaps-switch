"""company/board/mechanics.py — how the org design (the env) changes the flow of work in a quarter (#25).

The real quarter (company/board/quarter.py) uses these rules on FakeTransport and on Switch alike. Each
one is a pure function of the env plus a draw keyed by (seed, item), never a shared random stream, so
env_k and env_k+1 on one seed face the same luck (common random numbers) and only the org change moves
the result. Every effect is written to QuarterLog.decisions with a human-readable "what", which is what
the board reads back when it reflects and what the demo shows.

  links     An urgent incident (sev1/sev2) goes straight to #dev when the room of the department that
            raised it is linked to #dev (`x-company.links: [[maintenance, dev]]`): the department agent
            itself posts the task in #dev and it is dispatched the week it was raised. Everything else (no
            link; sev3 and features, which the board triages) reaches synaps-dev only through the board,
            which relays it from #board at its next weekly sync: RELAY_WEEKS (1) later. On Switch the direct line is real: apply_env puts ops-maint into #dev (extra_members),
            ops-maint posts `@synaps-dev fix acme-shop#N` there and synaps-dev answers ops-maint.
  capacity  synaps-dev takes at most `x-company.dev_capacity.per_week` items a week (and `per_quarter` a
            quarter; QuarterConfig.dev_per_week / dev_per_quarter override, for a single quarter): incidents
            first by severity, then features by demand. The rest wait in the queue, and the wait is logged.
            company/run.py writes its --dev-per-* flags into the seed env's dev_capacity instead, so a
            `set_dev_capacity` mutation takes effect in its trial (#31).
  testers   Agents in #dev with an Agency `testing/*` persona review every PR. Each incident fix carries a
            latent regression with p REGRESSION_P (seeded per issue: the market decides whether customers
            hit it). One tester catches it before the merge with p 0.45, two or more with 0.9. Uncaught,
            the incident reopens after the merge and its re-fix lands REOPEN_WEEKS (2) later (past the
            quarter = still open at quarter end). Every tester beyond the first is another review round:
            its review runs (and is paid for), and with p EXTRA_REVIEW_SLIP_P the merge slips a week.
  policies  Budgets and the finance veto are enforced by the department agents (company/depts/agent.py);
            approved campaigns (spend, quality, cites_feature) and spend reach Market.settle unchanged.
"""
from __future__ import annotations

import random

MECHANICS_VERSION = 1          # part of the quarter's checkpoint fingerprint
RELAY_WEEKS = 1
DIRECT_MAX_SEVERITY = 2        # the direct line carries sev1/sev2 incidents
REGRESSION_P = 0.25
CATCH_P = {0: 0.0, 1: 0.45}
CATCH_P_MANY = 0.90
EXTRA_REVIEW_SLIP_P = 0.5
REOPEN_WEEKS = 2

# QuarterLog "mechanics" stats: key -> label (the board and the lineage compare these between envs)
STATS = {
    "relayed": "items relayed by the board",
    "relay_weeks": "weeks waited for the board's relay",
    "direct": "items handed to synaps-dev directly",
    "capacity_weeks": "weeks waited for dev capacity",
    "not_dispatched": "routes never dispatched",
    "caught": "regressions caught in review",
    "reopened": "fixes reopened after a regression",
    "review_rounds": "extra review rounds",
    "slipped": "merges slipped a week by review",
}


def draw(seed, *key) -> float:
    """A uniform [0, 1) draw for one item, independent of every other draw (common random numbers)."""
    return random.Random(f"{seed}:" + ":".join(map(str, key))).random()


def links(company: dict) -> list[list[str]]:
    return [list(p)[:2] for p in (company or {}).get("links") or []]


def linked(company: dict, a: str, b: str) -> bool:
    return any(sorted(map(str, p)) == sorted((a, b)) for p in links(company))


def room_of(env, agent: str) -> str | None:
    for r in env.rooms:
        if agent in (r.get("agents") or []):
            return r["name"]
    return None


def testers(env, room: str = "dev") -> list[str]:
    """Agents in #dev whose persona is one of the Agency's testing/* personas, in env order."""
    persona = {a["name"]: str(a.get("persona") or "") for a in env.agents}
    members = next((r.get("agents") or [] for r in env.rooms if r.get("name") == room), [])
    return [n for n in members if persona.get(n, "").startswith("testing/")]


def capacity(company: dict, per_week=None, per_quarter=None) -> tuple[int | None, int | None]:
    """(per_week, per_quarter) for synaps-dev: the explicit values, else x-company.dev_capacity, else None."""
    cap = (company or {}).get("dev_capacity") or {}
    pw = per_week if per_week is not None else cap.get("per_week")
    pq = per_quarter if per_quarter is not None else cap.get("per_quarter")
    return (None if pw is None else int(pw)), (None if pq is None else int(pq))


def cap_text(per_week, per_quarter) -> str:
    w = f"{per_week}/week" if per_week is not None else "unlimited/week"
    return w + (f", {per_quarter}/quarter" if per_quarter is not None else "")


def urgent(item: dict) -> bool:
    return item.get("kind") == "incident" and int(item.get("severity") or 9) <= DIRECT_MAX_SEVERITY


def route_plan(env, by: str, item: dict | None = None, *, dev_room: str = "dev", hub: str = "board",
               members=None) -> dict:
    """How a route raised by `by` reaches synaps-dev: {"path": "direct"|"board_relay", "asker", "room",
    "delay", "why"}. Direct when the item is urgent and `by` sits in #dev or in a room linked to #dev (and,
    when `members` = the room membership is given, is a member of #dev to post there); else the board
    relays it at its next weekly sync."""
    room = room_of(env, by) or hub
    line = by != hub and (room == dev_room or linked(env.company, room, dev_room))
    if line and members is not None and by not in (members.get(dev_room) or ()):
        line = False
    if line and (item is None or urgent(item)):
        return {"path": "direct", "asker": by, "room": room, "delay": 0,
                "why": f"#{room} ↔ #{dev_room} is linked, so {by} hands it to synaps-dev directly"}
    if by == hub:
        why = "the board dispatches it itself"
    elif line:
        why = f"not urgent (sev1/sev2 only take the #{room} ↔ #{dev_room} line), so it waits for the board's relay"
    else:
        why = f"no #{room} ↔ #{dev_room} link, so it waits for the board's relay"
    return {"path": "board_relay", "asker": hub, "room": room, "delay": 0 if by == hub else RELAY_WEEKS, "why": why}


def quality(env, seed, issue: int, kind: str) -> dict:
    """The review outcome for one PR: {"testers", "latent", "caught_by", "extra_rounds", "slip"}."""
    tst = testers(env)
    latent = kind == "incident" and draw(seed, "regress", issue) < REGRESSION_P
    p = CATCH_P.get(len(tst), CATCH_P_MANY)
    caught = latent and bool(tst) and draw(seed, "catch", issue) < p
    extra = max(0, len(tst) - 1)
    slip = 1 if extra and draw(seed, "review", issue) < EXTRA_REVIEW_SLIP_P else 0
    return {"testers": tst, "latent": latent, "caught_by": tst[0] if caught else None, "catch_p": p,
            "extra_rounds": extra, "slip": slip}


def org_text(env, per_week, per_quarter, hub: str = "board") -> str:
    ls = links(env.company)
    tst = testers(env)
    return ("org: direct lines " + (", ".join(f"#{a} ↔ #{b}" for a, b in ls) if ls else
                                    f"none (every hand-off to #dev goes through #{hub})")
            + f"; synaps-dev capacity {cap_text(per_week, per_quarter)}"
            + f"; PR reviewers in #dev: {', '.join(tst) or 'none'}")


def stats_text(stats: dict) -> str:
    return "; ".join(f"{STATS[k]}: {stats.get(k, 0)}" for k in STATS if stats.get(k))


def eval_stats(ev: dict | None) -> dict | None:
    """The quarter mechanics stats summed over an evaluation's seeds (None when its logs carry none)."""
    tot, seen = {}, False
    for p in (ev or {}).get("per_seed") or []:
        for d in reversed((p.get("log") or {}).get("decisions") or []):
            if d.get("type") == "mechanics":
                seen = True
                for k, v in (d.get("stats") or {}).items():
                    tot[k] = tot.get(k, 0) + v
                break
    return tot if seen else None


def _k(v):
    return f"{'−' if v < 0 else ''}${abs(v) / 1000:.1f}k"


def why_moved(parent: dict, ev: dict) -> str:
    """Why fitness moved between two evaluations on the same seeds: the metrics that changed, then the
    mechanics behind them (summed over the seeds), e.g. "fix time 2.25 → 1.50 wk, ...; board relays 6 → 3"."""
    pm, m = parent.get("metrics") or {}, ev.get("metrics") or {}
    parts = []
    for key, fmt in (("mean_fix_weeks", lambda v: f"{v:.2f} wk"), ("profit", _k), ("growth", lambda v: f"{v:+.1%}"),
                     ("open_incident_severity", lambda v: f"{v:.1f}"), ("llm_cost_usd", lambda v: f"${v:.3f}")):
        if key in pm and key in m and round(pm[key], 4) != round(m[key], 4):
            label = {"mean_fix_weeks": "fix time", "open_incident_severity": "open severity at quarter end",
                     "llm_cost_usd": "LLM"}.get(key, key)
            parts.append(f"{label} {fmt(pm[key])} → {fmt(m[key])}")
    a, b = eval_stats(parent), eval_stats(ev)
    mech = []
    if a is not None and b is not None:
        for k, label in STATS.items():
            if a.get(k, 0) != b.get(k, 0):
                mech.append(f"{label} {a.get(k, 0)} → {b.get(k, 0)}")
    out = ", ".join(parts) or "no metric moved"
    return out + (f"; across the seeds: {', '.join(mech)}" if mech else "")


__all__ = ["MECHANICS_VERSION", "RELAY_WEEKS", "DIRECT_MAX_SEVERITY", "urgent", "REGRESSION_P", "REOPEN_WEEKS", "STATS", "draw", "linked", "links",
           "testers", "capacity", "cap_text", "route_plan", "quality", "org_text", "stats_text", "room_of",
           "eval_stats", "why_moved"]
