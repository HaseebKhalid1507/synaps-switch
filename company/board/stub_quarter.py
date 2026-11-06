"""company/board/stub_quarter.py — a deterministic stand-in for one quarter (offline runs and tests).

    metrics, log = run_quarter(env, seed=1, generation=2)

Same contract as the real inner loop (company/board/quarter.py, #10): run one quarter of `env` on market
`seed` and return (Metrics, QuarterLog). No agents, no LLM, no Switch: the QuarterLog is derived from the
env's structure and the seed, then settled by the real `Market.settle`, so a mutation changes the score
through the same market model the live company is scored by. Every draw comes from a Random keyed by
(seed, item), never from a shared stream, so env_k and env_k+1 on one seed face the same luck
(common random numbers) and only the org change moves the result.

What the org changes (each effect is one line in QuarterLog.decisions, so the board can read it back):

  routing     incidents reach #dev the same week when #maintenance and #dev are linked (ops-maint talks to
              synaps-dev directly); otherwise they wait one week in #board. Same for features and
              #growth <-> #dev. No ops-maint: incidents wait two weeks.
  dev         synaps-dev does 2 work items a week, incidents first (by severity), then features (by
              demand when growth is staffed, else by issue number). Effort per item: 1-2 weeks (seeded).
  review      each fix risks a regression (25%). A tester in #dev (persona testing/*) catches it before
              the merge with 45%, two or more with 90%; an uncaught one reopens the incident for 2 weeks.
              Every tester beyond the first adds a review round: 50% of merges slip a week (seeded).
  marketing   the marketing budget is spent in equal weekly campaigns. Over policies.finance_veto_usd a
              campaign goes to the #board humans and runs a week late (the last week's is lost); with no
              finance agent it is denied. A campaign cites a shipped feature when the marketing
              instructions ask for it and something has shipped; #growth <-> #marketing adds quality.
  spend       maintenance and growth budgets are booked at 50% / 30% (tools, on-call, research).
  LLM cost    (simulated $, Haiku-priced) 2 calls a week per agent, +3 calls per item routed through
              #board, +1 per direct hand-off, 2 per review per tester, synaps-dev $0.15 per work item.

Not modelled: price elasticity (`Market.settle` has none), the token budget, competitor responses.
"""
from __future__ import annotations

import random
import re

from company.core.env import Env
from company.market.sim import Market, Metrics, QuarterLog, default_issues

CALL_USD = 0.01           # one Haiku-class call, prompt + reply
DEV_ITEM_USD = 0.15       # synaps-dev (Sonnet) per work item
DEV_CAPACITY = 2          # work items per week
REGRESSION_P = 0.25
CATCH_P = {0: 0.0, 1: 0.45}
CATCH_P_MANY = 0.90
EXTRA_REVIEW_SLIP_P = 0.5
REOPEN_WEEKS = 2


def _rng(seed: int, *key) -> random.Random:
    return random.Random(f"{seed}:" + ":".join(map(str, key)))


def _names(env: Env) -> dict[str, dict]:
    return {a["name"]: a for a in env.agents}


def _room_agents(env: Env, room: str) -> list[str]:
    for r in env.rooms:
        if r.get("name") == room:
            return list(r.get("agents") or [])
    return []


def linked(env: Env, a: str, b: str) -> bool:
    return any(sorted(map(str, pair)) == sorted((a, b)) for pair in (env.company.get("links") or []))


def testers(env: Env) -> list[str]:
    agents = _names(env)
    return [n for n in _room_agents(env, "dev") if str(agents.get(n, {}).get("persona", "")).startswith("testing/")]


def run_quarter(env: Env, *, seed: int, generation: int = 1, issues: dict | None = None) -> tuple[Metrics, QuarterLog]:
    issues = issues or default_issues()
    company = env.company
    weeks = int(company.get("weeks_per_quarter", 4))
    agents = _names(env)
    log = QuarterLog()
    note = lambda week, who, what: log.decisions.append({"week": week, "who": who, "what": what})  # noqa: E731
    calls = 2 * weeks * sum(1 for a in agents.values() if a.get("provider") != "synaps")
    dev_items = 0

    # ── routing: when does each work item reach #dev ──
    direct_maint = linked(env, "maintenance", "dev")
    direct_growth = linked(env, "growth", "dev")
    work = []
    for inc in sorted(issues["incidents"], key=lambda i: (i["severity"], i["issue"])):
        if "ops-maint" not in agents:
            arrive, how = 3, "nobody triages #maintenance: the board finds it in week 3"
        elif direct_maint:
            arrive, how = 1, "ops-maint -> synaps-dev directly (#maintenance <-> #dev)"
            calls += 1
        else:
            arrive, how = 2, "ops-maint -> #board -> #dev: waited a week in #board"
            calls += 3
        work.append({"kind": "incident", "issue": inc["issue"], "sev": inc["severity"], "arrive": arrive, "how": how})
    feats = issues["features"]
    if "growth" in agents:
        feats = sorted(feats, key=lambda f: (-f["demand"], f["issue"]))
    for f in feats:
        arrive = 2 if direct_growth else 3
        calls += 1 if direct_growth else 3
        how = "growth -> synaps-dev directly" if direct_growth else "growth -> #board -> #dev"
        work.append({"kind": "feature", "issue": f["issue"], "demand": f["demand"], "arrive": arrive, "how": how})

    # ── dev: 2 items a week, in order; review; regressions ──
    tst = testers(env)
    catch_p = CATCH_P.get(len(tst), CATCH_P_MANY)
    load: dict[int, int] = {}
    for item in work:
        effort = 1 if _rng(seed, "effort", item["issue"]).random() < 0.6 else 2
        start = item["arrive"]
        while load.get(start, 0) >= DEV_CAPACITY:
            start += 1
        for w in range(start, start + effort):
            load[w] = load.get(w, 0) + 1
        done = start + effort - 1
        dev_items += 1
        if len(tst) > 1 and _rng(seed, "review", item["issue"]).random() < EXTRA_REVIEW_SLIP_P:
            done += 1
            note(done, "qa", f"#{item['issue']}: extra review round, merge slipped a week")
        calls += 2 * len(tst)
        if item["kind"] == "incident" and _rng(seed, "regress", item["issue"]).random() < REGRESSION_P:
            if _rng(seed, "catch", item["issue"]).random() < catch_p:
                note(done, tst[0] if tst else "dev", f"#{item['issue']}: regression caught in review")
            else:
                done += REOPEN_WEEKS
                dev_items += 1
                note(done, "maintenance", f"#{item['issue']}: fix regressed after merge, reopened for "
                                          f"{REOPEN_WEEKS} weeks")
        if done > weeks:
            note(weeks, "dev", f"{item['kind']} #{item['issue']} not done this quarter ({item['how']})")
            continue
        if item["kind"] == "incident":
            log.fixes.append({"issue": item["issue"], "week_fixed": done, "pr_url": None, "tests_passed": True})
            note(done, "dev", f"fixed sev{item['sev']} #{item['issue']} in week {done} ({item['how']})")
        else:
            log.features.append({"issue": item["issue"], "week_shipped": done, "pr_url": None, "tests_passed": True})
            note(done, "dev", f"shipped feature #{item['issue']} in week {done} ({item['how']})")

    # ── marketing ──
    budgets = company.get("budgets") or {}
    veto = float((company.get("policies") or {}).get("finance_veto_usd", 5000))
    mk = agents.get("marketing")
    if mk:
        cite = bool(re.search(r"shipped", mk.get("instructions", ""), re.I))
        per_week = float(budgets.get("marketing", 0)) / weeks
        for w in range(1, weeks + 1):
            if per_week <= 0:
                break
            run_w = w
            if per_week > veto:
                if "finance" not in agents:
                    note(w, "finance", f"campaign ${per_week:,.0f} over the ${veto:,.0f} veto, no finance: denied")
                    log.spend.append({"week": w, "dept": "marketing", "usd": per_week, "approved": False})
                    continue
                run_w = w + 1
                calls += 3
                if run_w > weeks:
                    note(w, "finance", f"campaign ${per_week:,.0f} over the veto: escalated to #board, "
                                       "approved too late for this quarter")
                    continue
                note(run_w, "finance", f"campaign ${per_week:,.0f} over the ${veto:,.0f} veto: waited a week "
                                       "for the #board humans")
            shipped = any(f["week_shipped"] <= run_w for f in log.features)
            q = 0.45 + 0.2 * _rng(seed, "quality", w).random() + (0.1 if linked(env, "growth", "marketing") else 0.0)
            log.campaigns.append({"week": run_w, "text": f"week {w} campaign", "spend": per_week,
                                  "cites_feature": cite and shipped, "quality": round(min(q, 1.0), 3)})
            log.spend.append({"week": run_w, "dept": "marketing", "usd": per_week, "approved": True})
            calls += 2
    for dept, share in (("maintenance", 0.5), ("growth", 0.3)):
        if budgets.get(dept):
            log.spend.append({"week": 1, "dept": dept, "usd": float(budgets[dept]) * share, "approved": True})

    log.llm_cost_usd = round(calls * CALL_USD + dev_items * DEV_ITEM_USD, 4)
    market = Market(seed, company, issues=issues)
    return market.settle(log), log


__all__ = ["run_quarter", "linked", "testers"]
