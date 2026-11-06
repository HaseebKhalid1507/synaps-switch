"""company/depts/agent.py on FakeTransport with a scripted LLM (no network)."""
import re

import pytest

from company.core.env import load_env
from company.depts.agent import (
    CampaignJudge, Departments, TokenBudget, load_personas, parse_action, render, transport_rooms,
    validate_action)
from company.llm import LLMError
from company.market.sim import MarketEvent, QuarterLog
from company.transport import FakeTransport, Msg

ENV1 = "company/envs/env1.yaml"
JUDGE_OK = {"clarity": 2, "specificity": 2, "benefit": 1, "cta": 2, "honesty": 2, "note": "solid"}
COPY = "Now $26/month, and reorder in one click with One-click reorder. Try it today."


class Stub:
    """complete_json stand-in: script[agent](prompt) -> reply dict; 'judge' for the campaign judge."""

    def __init__(self, script, cost=0.0015):
        self.script, self.cost, self.calls = script, cost, []

    def __call__(self, system, prompt):
        m = re.search(r"You are @([A-Za-z0-9_-]+)", system)
        who = m.group(1) if m else ("judge" if "judge marketing campaign" in system else "?")
        self.calls.append((who, prompt))
        out = self.script.get(who, lambda p: {"say": "", "action": {"type": "none"}})(prompt)
        if isinstance(out, Exception):
            raise out
        return out, self.cost

    def n(self, who):
        return sum(1 for w, _ in self.calls if w == who)


def review(p):
    return "Policy check (computed, binding)" in p


def company(*, script, env=None, features=(20,), rooms=None, **kw):
    env = env or load_env(ENV1)
    log = QuarterLog(features=[{"issue": i, "week_shipped": 1, "pr_url": None, "tests_passed": True}
                               for i in features])
    t = FakeTransport(rooms=rooms if rooms is not None else transport_rooms(env))
    stub = Stub(script)
    deps = Departments(env, t, log, llm=stub, roster={}, **kw)
    t.start()
    return env, t, log, stub, deps


def posts(t, sender=None):
    return [r for r in t.records if r["kind"] == "post" and (sender is None or r["sender"] == sender)]


def competitor(deps, t, pct=15, week=2):
    deps.week = week
    ev = MarketEvent(week, "growth", "competitor", f"Competitor cut price by {pct}%. Consider response.",
                     {"price_cut_pct": pct})
    room, text = deps.event_message(ev)
    return t.say("market", room, text)


# ───────────────────────── ACTION blocks ─────────────────────────


def test_render_and_parse_round_trip():
    a = {"type": "price", "value": 26.0}
    text = render("Proposing $26. @finance please review.", a)
    assert text.endswith("```") and parse_action(text) == a
    assert parse_action(render("", {"type": "none"})) == {"type": "none"}
    assert parse_action("no block here") is None
    assert parse_action(text + "\ntrailing words") is None                       # must END the reply
    assert parse_action('```json\n{"status":"done","pr":null}\n```') is None      # synaps-dev RESULT
    assert parse_action('```json\n{"type": "none"\n```') is None                  # broken JSON
    two = render("x", {"type": "none"}) + "\n" + render("y", {"type": "price", "value": 20})
    assert parse_action(two) == {"type": "price", "value": 20}                   # the last block


def test_validate_action_normalizes_and_rejects():
    assert validate_action({"type": "campaign", "text": " Hi ", "spend": "$1,200", "cites_feature": "true"}) == \
        {"type": "campaign", "text": "Hi", "spend": 1200.0, "cites_feature": True}
    assert validate_action({"type": "spend_request", "dept": "#maintenance", "usd": 900}) == \
        {"type": "spend_request", "dept": "maintenance", "usd": 900.0}
    assert validate_action({"type": "price", "value": "26.5"}) == {"type": "price", "value": 26.5}
    assert validate_action({"type": "route", "issue": "#3", "to": "@synaps-dev"}, ["synaps-dev"]) == \
        {"type": "route", "issue": 3, "to": "synaps-dev"}
    assert validate_action({"type": "price", "value": 26, "decision": "approve"})["decision"] == "approve"
    for bad in ({"type": "campaign", "text": ""}, {"type": "price", "value": -1}, {"type": "price", "value": "x"},
                {"type": "spend_request", "usd": 0}, {"type": "route", "issue": 3, "to": "ceo"},
                {"type": "hire"}, "price", None):
        with pytest.raises((ValueError, TypeError)):
            validate_action(bad, ["synaps-dev"])


def test_transport_rooms_hub_board_everywhere_and_links():
    env = load_env(ENV1)
    r = transport_rooms(env)
    assert {"board", "growth", "finance", "marketing", "ops-maint", "synaps-dev", "Aditya", "Haseeb"} <= set(r["board"])
    assert set(r["growth"]) == {"growth", "board", "Aditya", "Haseeb"}
    env.company["links"] = [["maintenance", "dev"]]
    r = transport_rooms(env)
    assert "synaps-dev" in r["maintenance"] and "ops-maint" in r["dev"]


# ───────────────────────── the "Done when" scenario ─────────────────────────


def test_price_cut_price_action_finance_approves_marketing_campaign_no_loop():
    script = {
        "growth": lambda p: {"say": "Competitor cut 15%. Proposing $26.00 to hold conversion. @finance please review.",
                             "action": {"type": "price", "value": 26}},
        "finance": lambda p: ({"say": "Margin holds at $26. @marketing announce it with One-click reorder.",
                               "decision": "approve"} if review(p) else {"say": "", "action": {"type": "none"}}),
        "marketing": lambda p: {"say": "Campaign is live.",
                                "action": {"type": "campaign", "text": COPY, "spend": 800, "cites_feature": True}},
        "judge": lambda p: JUDGE_OK,
    }
    env, t, log, stub, deps = company(script=script)
    competitor(deps, t, 15)

    assert t.errors == []
    assert log.price_changes == [{"week": 2, "price": 26.0}]
    assert len(log.campaigns) == 1
    c = log.campaigns[0]
    assert (c["week"], c["spend"], c["cites_feature"], c["quality"]) == (2, 800.0, True, 0.9)
    assert log.spend == [{"week": 2, "dept": "marketing", "usd": 800.0, "approved": True}]
    assert log.llm_cost_usd == pytest.approx(0.0015 * 4)                       # growth, finance, marketing, judge
    assert [stub.n(a) for a in ("growth", "finance", "marketing", "judge")] == [1, 1, 1, 1]

    g, f, m = posts(t, "growth")[0], posts(t, "finance")[0], posts(t, "marketing")[0]
    # env1 has no links: #growth reaches finance through #board, same conversation
    assert g["room"] == "board" and g["text"].startswith("(re #growth)") and g["to"] == ["finance"]
    assert parse_action(g["text"]) == {"type": "price", "value": 26.0}
    assert parse_action(f["text"]) == {"type": "price", "value": 26.0, "decision": "approve"}
    assert f["thread"] == g["id"] and f["to"] == ["marketing"]
    assert parse_action(m["text"])["type"] == "campaign" and m["to"] == []
    assert max(deps.hops.values()) <= 3 and len(posts(t)) == 4                 # market + 3 replies, then quiet
    kinds = [d["type"] for d in log.decisions]
    assert kinds.count("verdict") == 2 and "loop_guard" not in kinds


def test_big_price_cut_escalates_to_board_humans_and_only_a_person_approves():
    script = {
        "growth": lambda p: {"say": "Match hard: $20. @finance", "action": {"type": "price", "value": 20}},
        "finance": lambda p: ({"say": "This needs the board; @growth wants a deep cut.", "decision": "approve"}
                              if review(p) else {"say": "", "action": {"type": "none"}}),
    }
    env, t, log, stub, deps = company(script=script)
    competitor(deps, t, 20)
    esc = posts(t, "finance")[0]
    assert esc["room"] == "board" and esc["to"] == ["Aditya", "Haseeb"]       # people only, no agent
    assert "@Aditya" in esc["text"] and "@Haseeb" in esc["text"] and "@growth" not in esc["text"]
    assert parse_action(esc["text"])["decision"] == "escalate"                 # the LLM said approve: code wins
    assert log.price_changes == []

    # an agent "approving" books nothing
    t.say("synaps-dev", "board", "@finance approve", thread=esc["id"], is_agent=True)
    assert log.price_changes == []
    # a person does
    t.say("Haseeb", "board", "@finance approve", thread=esc["id"])
    assert log.price_changes == [{"week": 2, "price": 20.0}]
    ok = posts(t, "finance")[-1]
    assert parse_action(ok["text"])["decision"] == "approve" and ok["thread"] == (esc["thread"] or esc["id"])
    assert deps.pending[posts(t, "growth")[0]["id"]]["status"] == "approved"
    assert t.errors == []


def test_campaign_over_veto_is_held_escalated_and_rejected():
    script = {
        "marketing": lambda p: {"say": "Big push.", "action": {"type": "campaign", "text": COPY, "spend": 8000,
                                                                "cites_feature": True}},
        "finance": lambda p: {"say": "@Aditya @Haseeb: over the limit, please decide.", "decision": "escalate"},
        "judge": lambda p: JUDGE_OK,
    }
    env, t, log, stub, deps = company(script=script)
    deps.week = 3
    t.say("Aditya", "marketing", "@marketing run a big campaign for one-click reorder")
    m = posts(t, "marketing")[0]
    assert "cc @finance" in m["text"] and m["to"] == ["finance"]
    assert log.campaigns == []
    esc = posts(t, "finance")[0]
    assert esc["text"].count("@Aditya") == 1 and esc["text"].startswith("@Aditya @Haseeb over the limit")
    t.say("Aditya", "board", "@finance reject, too much", thread=esc["id"])
    assert log.campaigns == [] and stub.n("judge") == 0
    assert log.spend == [{"week": 3, "dept": "marketing", "usd": 8000.0, "approved": False}]
    assert deps.pending[m["id"]]["status"] == "rejected"


def test_spend_over_dept_budget_is_denied_by_policy():
    script = {
        "growth": lambda p: {"say": "Need ads money. @finance", "action": {"type": "spend_request", "dept": "growth",
                                                                           "usd": 3500}},
        "finance": lambda p: {"say": "Looks fine to me.", "decision": "approve"},
    }
    env, t, log, stub, deps = company(script=script)
    t.say("Haseeb", "growth", "@growth what do you need this quarter?")
    f = posts(t, "finance")[0]
    assert parse_action(f["text"])["decision"] == "deny"                       # $3500 > growth's $3000
    assert log.spend == [{"week": 1, "dept": "growth", "usd": 3500.0, "approved": False}]


def test_campaign_within_limits_books_at_once_and_cites_feature_is_verified():
    script = {"marketing": lambda p: {"say": "Spring sale!", "action": {"type": "campaign", "spend": 500,
                                      "text": "Spring sale: everything is great. Buy now.", "cites_feature": True}},
              "judge": lambda p: {"clarity": 1, "specificity": 0, "benefit": 1, "cta": 2, "honesty": 2}}
    env, t, log, stub, deps = company(script=script)
    t.say("Haseeb", "marketing", "@marketing do something")
    assert stub.n("finance") == 0                                             # no review needed
    assert log.campaigns[0]["cites_feature"] is False                          # claimed, but names no feature
    assert log.campaigns[0]["quality"] == 0.6
    assert deps.cites_shipped("Try One-click reorder today") and deps.cites_shipped("see #20")
    assert not deps.cites_shipped("Guest checkout is here")                   # #23 not shipped


def test_finance_never_books_an_auto_booked_campaign_twice():
    script = {"marketing": lambda p: {"say": "Live. @finance book the $1,500 spend.",
                                      "action": {"type": "campaign", "text": COPY, "spend": 1500, "cites_feature": True}},
              "finance": lambda p: {"say": "Booked.", "action": {"type": "spend_request", "dept": "marketing", "usd": 1500}},
              "judge": lambda p: JUDGE_OK}
    env, t, log, stub, deps = company(script=script)
    t.say("Haseeb", "marketing", "@marketing run a $1,500 campaign")
    assert stub.n("finance") == 1 and "Ledger, approved spend vs budget: marketing $1,500 of $10,000" in \
        [p for w, p in stub.calls if w == "finance"][0]
    assert log.spend == [{"week": 1, "dept": "marketing", "usd": 1500.0, "approved": True}]
    assert [d["type"] for d in log.decisions] == ["verdict", "ignored_action"]
    assert "Never raise a price or spend_request ACTION yourself" in deps.agents["finance"].system


def test_finance_reviews_action_blocks_from_agents_outside_the_runtime():
    script = {"finance": lambda p: ({"say": "Fine.", "decision": "approve"} if review(p)
                                    else {"say": "", "action": {"type": "none"}})}
    env, t, log, stub, deps = company(script=script)
    t.say("synaps-dev", "board", render("@finance need $900 for CI runners", {"type": "spend_request",
          "dept": "maintenance", "usd": 900}), is_agent=True)
    t.say("synaps-dev", "board", render("@finance FYI", {"type": "route", "issue": 3, "to": "synaps-dev"}),
          is_agent=True)
    assert log.spend == [{"week": 1, "dept": "maintenance", "usd": 900.0, "approved": True}]
    assert t.errors == [] and stub.n("finance") == 2


def test_route_action_is_recorded_and_reaches_the_target():
    script = {"ops-maint": lambda p: {"say": "Sev1 checkout outage, needs a fix now.",
                                      "action": {"type": "route", "issue": 3, "to": "synaps-dev"}}}
    env, t, log, stub, deps = company(script=script)
    ev = MarketEvent(1, "maintenance", "incident", "30 customers report: Checkout 500s (sev1, issue #3)",
                     {"issue": 3, "severity": 1})
    room, text = deps.event_message(ev)
    assert room == "maintenance" and text.startswith("@ops-maint ")
    t.say("market", room, text)
    p = posts(t, "ops-maint")[0]
    assert "@synaps-dev" in p["text"] and p["to"] == ["synaps-dev"] and p["room"] == "board"
    assert deps.routes == [{"week": 1, "issue": 3, "to": "synaps-dev", "by": "ops-maint", "msg": p["id"]}]
    assert any(d["type"] == "route" for d in log.decisions)
    prompt = stub.calls[0][1]                     # stateless calls carry the company facts
    assert "Week 1 of 4" in prompt and "Open incidents: #3 Checkout 500s on empty coupon code (sev1)" in prompt
    assert "Shipped this quarter: #20 One-click reorder (week 1)" in prompt and "#20" not in prompt.split("Feature backlog:")[1]


# ───────────────────────── budgets ─────────────────────────


def test_agent_over_token_budget_refuses_and_says_so_once():
    env = load_env(ENV1)
    env.company["token_budget"] = {"per_agent": {"ops-maint": 50, "default": 30000}, "per_quarter": 150000}
    env_, t, log, stub, deps = company(script={}, env=env)
    root = t.say("market", "maintenance", "@ops-maint checkout is down (sev1, issue #3)")
    t.say("Haseeb", "maintenance", "@ops-maint status?", thread=root)
    assert stub.n("ops-maint") == 0
    refusals = posts(t, "ops-maint")
    assert len(refusals) == 1 and "over my LLM budget" in refusals[0]["text"] and "@" not in refusals[0]["text"]
    assert [d["type"] for d in log.decisions] == ["budget_refusal"]


def test_quarter_budget_stops_everyone_and_real_usage_is_counted():
    b = TokenBudget({"per_agent": 1000, "per_quarter": 1500})
    assert b.over("growth", 900) is None
    b.charge("growth", 900)
    assert b.over("growth", 200) == "growth has 100 of its 1,000 tokens left this quarter; a reply needs about 200"
    assert "the company has 600 of its 1,500 tokens left" in b.over("finance", 700)
    env = load_env(ENV1)
    env.company["token_budget"] = {"per_agent": 30000, "per_quarter": 10}
    env_, t, log, stub, deps = company(script={}, env=env)
    competitor(deps, t)
    assert stub.calls == [] and "the company has 10 of its 10 tokens left" in log.decisions[0]["why"]


def test_llm_failure_books_its_cost_and_posts_nothing():
    env, t, log, stub, deps = company(script={"growth": lambda p: LLMError("no JSON object", cost_usd=0.003)})
    competitor(deps, t)
    assert posts(t, "growth") == [] and log.llm_cost_usd == pytest.approx(0.003)
    assert log.decisions[0]["type"] == "llm_error" and deps.budget.used["growth"] > 0


# ───────────────────────── loop guard ─────────────────────────


def test_agents_that_keep_mentioning_each_other_stop_after_three_hops():
    script = {"growth": lambda p: {"say": "@marketing what do you think?", "action": {"type": "none"}},
              "marketing": lambda p: {"say": "@growth and you?", "action": {"type": "none"}}}
    env = load_env(ENV1)
    env.company["links"] = [["growth", "marketing"]]
    env_, t, log, stub, deps = company(script=script, env=env)
    root = t.say("Haseeb", "growth", "@growth talk to marketing")
    agent_replies = [p for p in posts(t) if p["sender"] in ("growth", "marketing")]
    assert len(agent_replies) == 4                     # 1 answer to the person + 3 agent<->agent hops
    assert all(p["room"] == "growth" for p in agent_replies)      # linked: no detour through #board
    guards = [d for d in log.decisions if d["type"] == "loop_guard"]
    assert len(guards) == 1 and guards[0]["hops"] == 3 and t.errors == []
    # a person speaking in the thread resets the count
    t.say("Haseeb", "growth", "@growth one more round", thread=root)
    assert len([p for p in posts(t) if p["sender"] in ("growth", "marketing")]) == 8


def test_never_handles_own_unaddressed_or_duplicate_messages():
    env, t, log, stub, deps = company(script={"growth": lambda p: {"say": "ok", "action": {"type": "none"}}})
    g = deps.agents["growth"]
    g.handle(Msg("x1", "growth", None, "marketing", True, "growth, FYI the deploy is done", []))
    g.handle(Msg("x2", "growth", None, "growth", True, "@growth talking to myself", ["growth"]))
    assert stub.calls == []
    m = Msg("x3", "growth", None, "Haseeb", False, "@growth hi", ["growth"])
    g.handle(m)
    g.handle(m)
    assert stub.n("growth") == 1
    # through the transport, an agent message without @growth never arrives at all
    t.say("synaps-dev", "board", "growth: deploy done", is_agent=True)
    assert stub.n("growth") == 1


def test_mentions_are_cleaned_for_switch_addressing():
    env, t, log, stub, deps = company(script={})
    g = deps.agents["growth"]
    assert g.clean("Ask @finance. Then @Marketing, and @growth!") == "Ask @finance Then @marketing, and growth!"
    assert g.clean("ping @finance", agents=False) == "ping finance"
    assert g.clean("x ```json\n{}\n``` y") == "x  y"
    assert g.clean("@unknown. stays") == "@unknown. stays"


# ───────────────────────── judge, personas ─────────────────────────


def test_judge_rubric_cache_by_text_hash(tmp_path):
    stub = Stub({"judge": lambda p: {"clarity": 2, "specificity": 1, "benefit": 2, "cta": 0, "honesty": "2"}})
    j = CampaignJudge(llm=stub, cache_path=tmp_path / "judge.json")
    r1, c1 = j.score("Buy One-click reorder")
    r2, c2 = j.score("  Buy One-click reorder ")
    assert r1["quality"] == 0.7 and r1["scores"]["honesty"] == 2 and c1 == 0.0015
    assert r2["cached"] and c2 == 0.0 and stub.n("judge") == 1
    j2 = CampaignJudge(llm=stub, cache_path=tmp_path / "judge.json")       # persisted
    assert j2.score("Buy One-click reorder")[0]["cached"] and stub.n("judge") == 1


def test_one_dept_agent_per_env_agent_except_synaps_dev_and_board():
    env, t, log, stub, deps = company(script={})
    assert set(deps.agents) == {"ops-maint", "growth", "marketing", "finance", "reviewer"}
    assert deps.finance == "finance"
    assert set(t.handlers) == set(deps.agents)
    env2 = load_env(ENV1)
    d2 = Departments(env2, FakeTransport(rooms=transport_rooms(env2)), QuarterLog(), llm=Stub({}), roster={}, skip=())
    assert "board" in d2.agents and "synaps-dev" not in d2.agents


def test_instructions_are_the_agency_persona_plus_role_note():
    roster = load_personas()
    if "marketing/marketing-growth-hacker" not in roster:
        pytest.skip("Agency roster not available")
    env = load_env(ENV1)
    deps = Departments(env, FakeTransport(rooms=transport_rooms(env)), QuarterLog(), llm=Stub({}), roster=roster)
    m = deps.agents["marketing"]
    note = next(a for a in env.agents if a["name"] == "marketing")["instructions"].strip()
    assert m.instructions.startswith(note.split("\n")[0])
    assert roster["marketing/marketing-growth-hacker"]["body"][:80] in m.instructions
    assert "You are @marketing" in m.system and '{"type":"price"' in m.system
