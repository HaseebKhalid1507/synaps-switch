"""A short department-agent run on FakeTransport: real Haiku calls (default) or a canned LLM.

    python -m company.depts.demo --out evidence/m4/dept-agents          # live, via company/llm.py
    python -m company.depts.demo --out /tmp/dept-demo --stub            # offline, no network

Scenes: a competitor price cut in #growth (price ACTION -> finance -> maybe marketing), a human
answering any escalation, a $1,500 campaign (booked, judged), a $7,000 one over the finance veto
(escalated, rejected), ops-maint over its token budget, an agent that will not stop pinging growth (loop guard), and an agent message that does
not address anyone (never delivered). Writes transcript.jsonl, quarterlog.json, summary.md.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from company.core.env import load_env
from company.depts.agent import CampaignJudge, Departments, parse_action, transport_rooms
from company.market.sim import Market, MarketEvent, QuarterLog
from company.transport import FakeTransport


def canned(system, prompt):
    if "judge marketing campaign" in system:
        return {"clarity": 2, "specificity": 2, "benefit": 1, "cta": 2, "honesty": 2, "note": "canned"}, 0.0
    if "Policy check (computed, binding)" in prompt:
        return {"say": "Reviewed against policy.", "decision": "approve"}, 0.0
    if "You are @growth" in system and "Competitor" in prompt:
        return {"say": "Propose $25.00. @finance please review.", "action": {"type": "price", "value": 25}}, 0.0
    if "You are @marketing" in system:
        return {"say": "Launching.", "action": {"type": "campaign", "spend": 7000, "cites_feature": True,
                "text": "One-click reorder is here: your usual order in one tap. Try it today."}}, 0.0
    return {"say": "Noted, @growth here: nothing to change yet.", "action": {"type": "none"}}, 0.0


def chat_lines(records):
    out = []
    for r in records:
        if r["kind"] != "post":
            continue
        text = " ".join(r["text"].split())
        thread = f" (thread {r['thread']})" if r["thread"] and r["thread"] != r["id"] else ""
        out.append(f"{r['id']:>8}  #{r['room']:<12} {r['sender']:<11}{thread} -> {r['to']}\n          {text}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="company/envs/env1.yaml")
    ap.add_argument("--out", default="evidence/m4/dept-agents")
    ap.add_argument("--stub", action="store_true", help="canned LLM, no network")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in ("transcript.jsonl", "quarterlog.json", "summary.md"):
        (out / f).unlink(missing_ok=True)

    env = load_env(args.env)
    env.company["token_budget"] = {"per_agent": {"ops-maint": 200, "default": 30000}, "per_quarter": 150000}
    log = QuarterLog(features=[{"issue": 20, "week_shipped": 1, "pr_url": None, "tests_passed": True}])
    t = FakeTransport(rooms=transport_rooms(env), transcript_path=out / "transcript.jsonl",
                      log=lambda *a: print("transport:", *a, file=sys.stderr))
    llm = canned if args.stub else None
    judge = CampaignJudge(llm=llm, cache_path=out / "judge-cache.json")
    deps = Departments(env, t, log, llm=llm, judge=judge)
    t.start()
    t0 = time.monotonic()

    def say(*a, **kw):
        mid = t.say(*a, **kw)
        print(f"[{time.monotonic() - t0:6.1f}s] {a[0]} -> #{a[1]}: {a[2][:70]!r}  (llm ${log.llm_cost_usd:.4f})")
        return mid

    def answer_escalations(who, verdict):
        for it in list(deps.pending.values()):
            if it["status"] == "escalated":
                say(who, "board", f"@finance {verdict}", thread=it["escalation"])

    # 1. week 2: competitor price cut in #growth
    deps.week = 2
    ev = MarketEvent(2, "growth", "competitor", "Competitor cut price by 18%. Consider response.",
                     {"price_cut_pct": 18})
    say("market", *deps.event_message(ev))
    answer_escalations("Haseeb", "approve")

    # 2. week 3: a campaign within policy is booked at once and scored by the judge
    deps.week = 3
    say("Haseeb", "marketing", "@marketing write a $1,500 campaign for One-click reorder (shipped in week 1) "
        "and book it.")

    # 3. a campaign over the finance veto; Aditya rejects it
    say("Aditya", "marketing", "@marketing we want a big launch push for One-click reorder this week: "
        "a $7,000 campaign. Write the copy and book it.")
    answer_escalations("Aditya", "reject")

    # 4. ops-maint is over its (tiny) token budget
    inc = MarketEvent(3, "maintenance", "incident", "30 customers report: Checkout 500s on empty coupon code "
                      "(sev1, issue #3)", {"issue": 3, "severity": 1})
    say("market", *deps.event_message(inc))

    # 5. an agent that keeps pinging growth in one thread: the loop guard stops growth at 3 hops
    root = say("synaps-dev", "board", "@growth ping 1: which feature should I build next, and why?", is_agent=True)
    for i in range(2, 5):
        say("synaps-dev", "board", f"@growth ping {i}: still need an answer, which feature next?",
            thread=root, is_agent=True)

    # 6. an agent message that addresses nobody is never delivered
    say("synaps-dev", "board", "growth: FYI the checkout fix is deployed", is_agent=True)

    metrics = Market(args.seed, env.company).settle(log)
    data = {"quarterlog": asdict(log), "metrics": asdict(metrics), "usage": deps.usage(),
            "routes": deps.routes, "pending": deps.pending, "transport_errors": t.errors,
            "mode": "stub" if args.stub else "live", "wall_s": round(time.monotonic() - t0, 1)}
    (out / "quarterlog.json").write_text(json.dumps(data, indent=1, default=str))

    acts = [(r["id"], r["sender"], parse_action(r["text"])) for r in t.records
            if r["kind"] == "post" and parse_action(r["text"])]
    summary = ["# Department agents: live FakeTransport run" + (" (stub)" if args.stub else " (Haiku via company/llm.py)"),
               "", f"env `{args.env}`, wall {data['wall_s']}s, LLM spend **${log.llm_cost_usd:.4f}** "
               f"({deps.usage()['total_tokens']:,} est. tokens), transport errors: {len(t.errors)}", "",
               "## Chat", "", "```", *chat_lines(t.records), "```", "",
               "## ACTION blocks parsed from the posts", "", "```",
               *[f"{mid:>8} {who:<10} {json.dumps(a)}" for mid, who, a in acts], "```", "",
               "## QuarterLog", "", "```json",
               json.dumps({k: v for k, v in asdict(log).items() if k != "decisions"}, indent=1), "```", "",
               "## Decisions (finance verdicts, budget refusals, loop guard)", "", "```",
               *[json.dumps(d) for d in log.decisions], "```", "",
               "## Usage", "", "```json", json.dumps(deps.usage(), indent=1), "```", "",
               f"Market.settle(seed={args.seed}): {metrics}", ""]
    (out / "summary.md").write_text("\n".join(summary))
    print(f"wrote {out}/summary.md; llm ${log.llm_cost_usd:.4f}; decisions "
          f"{[d['type'] for d in log.decisions]}; errors {len(t.errors)}")
    return 0 if not t.errors else 1


if __name__ == "__main__":
    sys.exit(main())
