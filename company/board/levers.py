"""company/board/levers.py — the org levers the quarter actually reads (#31), in one registry.

A mutation deserves a #board decision only if it moves something the quarter reads. `read(env)` projects an env
onto exactly those levers; a mutation that leaves the projection unchanged does nothing to the quarter, and
`refusal()` says why. evolve refuses it before the human gate (re-asks once, then skips the generation), and the
board's proposer prompt lists LEVERS, one line each, and nothing else.

  direct line   x-company.links, only #maintenance ↔ #dev (mechanics.route_plan: sev1/sev2 incidents, which the
                market raises in #maintenance, go straight to #dev; every other item waits for the board's relay)
  dev capacity  x-company.dev_capacity {per_week, per_quarter} (mechanics.capacity)
  reviewers     the testing/* agents in #dev (mechanics.testers, mechanics.quality)
  finance veto  x-company.policies.finance_veto_usd (depts/agent.py Departments.check)
  budgets       x-company.budgets.<dept> (Departments.check)
  price         x-company.price (market/sim.py Market.settle, Departments.price)

Not levers (nothing in the quarter reads them): links between other rooms (they only change who can chat with
whom), hiring or swapping in a non-testing persona or anyone outside #dev, firing anyone but a #dev reviewer,
rewriting instructions (wording only), and any policy key but finance_veto_usd.
"""
from __future__ import annotations

from company.board import mechanics as mech

DEV = "dev"
INCIDENT_ROOMS = ("maintenance",)        # where the market raises incidents (market/sim.py events_for_week)
POLICY_KEYS = ("finance_veto_usd",)
NO_LEVER = "no lever:"                   # prefix of every refusal (evolve records those as "skipped")

# (name, how to propose it, what it does in the quarter): the proposer prompt shows exactly these lines
LEVERS = (
    ("direct line", '{"op":"link","rooms":["maintenance","dev"]} (or "unlink")',
     "sev1/sev2 incidents raised in #maintenance reach synaps-dev in #dev the same week, instead of a week later "
     "through the board's relay"),
    ("dev capacity", '{"op":"set_dev_capacity","per_week":<n>,"per_quarter":<n>} (either key)',
     "synaps-dev takes at most per_week items a week and per_quarter a quarter (incidents by severity, then "
     "features); the rest wait or are never dispatched"),
    ("reviewers", '{"op":"hire","persona":"testing/<roster id>","name":"<new-name>","room":"dev","role_note":"…"}, '
     '{"op":"fire","agent":"<a #dev tester>"} or swap_persona of a #dev agent into/out of testing/*',
     "testing/* agents in #dev review every PR: one catches a fix's latent regression 45% of the time, two or more "
     "90%; each beyond the first is another paid review round that slips the merge a week half the time"),
    ("finance veto", '{"op":"set_policy","key":"finance_veto_usd","value":<usd>}',
     "a spend or a price change worth more than this goes to the #board humans; under it, finance decides"),
    ("budgets", '{"op":"set_budget","dept":"<dept>","value":<usd>}',
     "caps a department's approved spend this quarter; marketing's campaigns buy leads, and all spend is a cost"),
    ("price", '{"op":"set_price","value":<usd>}', "the list price: revenue per customer purchase"),
)


def prompt_lines() -> str:
    return "\n".join(f"- {name}: {how}. Effect: {effect}." for name, how, effect in LEVERS)


def read(env) -> dict:
    """The env as the quarter reads it: two envs with the same projection run the same quarter on a seed."""
    from company.depts.agent import DEFAULT_VETO_USD
    c = env.company or {}
    return {"direct_lines": sorted(r for r in INCIDENT_ROOMS if mech.linked(c, r, DEV)),
            "dev_capacity": list(mech.capacity(c)),
            "reviewers": mech.testers(env, DEV),
            "finance_veto_usd": float((c.get("policies") or {}).get("finance_veto_usd", DEFAULT_VETO_USD)),
            "budgets": {str(k): float(v) for k, v in (c.get("budgets") or {}).items()},
            "price": float(c.get("price", 29.0))}


def moved(env, new) -> list[str]:
    a, b = read(env), read(new)
    return [k for k in a if a[k] != b[k]]


def _why(env, m: dict) -> str:
    op = m.get("op")
    if op in ("link", "unlink"):
        a, b = (list(m.get("rooms") or []) + ["?", "?"])[:2]
        return (f"#{a} ↔ #{b} has no mechanic: only #maintenance ↔ #dev does (the direct line for sev1/sev2 "
                "incidents); other links only change who can chat with whom")
    if op in ("hire", "add_agent"):
        persona = m.get("persona") if op == "hire" else (m.get("agent") or {}).get("persona")
        return (f"{persona or 'an agent without a persona'} in #{m.get('room')} has no mechanic: only a testing/* "
                "persona hired into #dev does (it reviews every PR)")
    if op in ("fire", "remove_agent"):
        who = m.get("agent")
        room = mech.room_of(env, who)
        alone = room and [a for a in (next((r.get("agents") or [] for r in env.rooms if r["name"] == room), []))
                          if a not in ("board", "synaps-dev")] == [who]
        return (f"only firing a testing/* reviewer in #dev is a lever; firing {who} "
                + (f"would leave #{room} with nobody to answer its market events" if alone else "has no mechanic"))
    if op == "swap_persona":
        return (f"{m.get('agent')}: {m.get('persona')} changes no mechanic: only a swap that adds or removes a "
                "testing/* reviewer in #dev does")
    if op == "set_policy":
        key = str(m.get("key"))
        hint = (' (synaps-dev\'s capacity is x-company.dev_capacity: {"op":"set_dev_capacity","per_week":<n>,'
                '"per_quarter":<n>})') if "capacity" in key or "dev" in key else ""
        return f"policy {key!r} is read by nothing: the quarter reads only {', '.join(POLICY_KEYS)}{hint}"
    if op == "set_instructions":
        return f"rewriting {m.get('agent')}'s instructions changes only an LLM's wording; no mechanic reads them"
    return f"{op} moves none of the levers"


def refusal(env, new, m: dict) -> str | None:
    """None when `new` (= apply_mutation(env, m)) moves a lever; else why not, prefixed NO_LEVER."""
    if moved(env, new):
        return None
    return (f"{NO_LEVER} {_why(env, m)}. The quarter reads only these levers: "
            + ", ".join(name for name, _, _ in LEVERS))


__all__ = ["DEV", "INCIDENT_ROOMS", "POLICY_KEYS", "NO_LEVER", "LEVERS", "prompt_lines", "read", "moved", "refusal"]
