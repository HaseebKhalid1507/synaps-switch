"""company/realwork.py: RESULT parsing, dispatch, review, the #board merge gate (FakeTransport, no network)."""
from __future__ import annotations

import pytest

from company import realwork as rw
from company.market.sim import QuarterLog
from company.transport import FakeTransport

PR = "file:///tmp/o.git?head=gen-1-fix-1&base=gen-1"

REPLY = """Coupon lookup crashed on an empty code; it now raises a ValueError. Full suite green.
@board
RESULT
```json
{"status": "proposed", "summary": "Empty coupon no longer crashes", "evidence": ["pytest -q: 33 passed"], "pr": "%s"}
```""" % PR


# ───────── RESULT ─────────

def test_parse_result_fenced():
    r = rw.parse_result(REPLY)
    assert r == {"status": "proposed", "summary": "Empty coupon no longer crashes",
                 "evidence": ["pytest -q: 33 passed"], "pr": PR, "valid": True}


def test_parse_result_variants():
    assert rw.parse_result('RESULT {"status":"blocked","summary":"no access","evidence":[],"pr":null}')["pr"] is None
    assert rw.parse_result('```\n{"status":"done","summary":"s","evidence":"one","pr":"null"}\n```')["evidence"] == ["one"]
    last = rw.parse_result('```json\n{"status":"blocked"}\n```\nlater:\n```json\n{"status":"proposed","pr":"x"}\n```')
    assert last["status"] == "proposed"
    odd = rw.parse_result('```json\n{"status":"shipped-it"}\n```')
    assert odd["status"] == "blocked" and odd["valid"] is False
    assert rw.parse_result("no block here") is None
    assert rw.parse_result('```json\n{"no": "status"}\n```') is None
    assert rw.parse_result("```json\n{broken\n```") is None


def test_dispatch_text_addresses_synaps_dev():
    t = rw.DevTask(issue=1, kind="incident", base="gen-1", asker="board")
    text = rw.dispatch_text(t)
    assert text.startswith("@synaps-dev fix acme-shop#1 (base gen-1)")
    from company.transport import parse_mentions
    assert parse_mentions(text) == ["synaps-dev"]


# ───────── dispatch through a transport ─────────

def rooms():
    return {"dev": ["board", "synaps-dev", "reviewer", "haseeb"], "board": ["board", "haseeb", "aditya"]}


def test_dispatch_gets_the_result_from_synaps_dev():
    t = FakeTransport(rooms=rooms())
    waiter = rw.Waiter()
    t.on_message("board", waiter.offer)

    def synaps_dev(msg):              # a fake synaps-dev: heads-up, then the RESULT addressed to the asker
        t.post("synaps-dev", msg.room, "On it.", thread=msg.thread or msg.id)
        t.post("synaps-dev", msg.room, REPLY, thread=msg.thread or msg.id)
    t.on_message("synaps-dev", synaps_dev)
    t.start()
    res = rw.dispatch(t, rw.DevTask(1, "incident", base="gen-1"), waiter, timeout=1)
    assert res["status"] == "proposed" and res["pr"] == PR and res["via"] == "synaps-dev"


def test_dispatch_falls_back_to_devworker_when_synaps_dev_is_silent():
    t = FakeTransport(rooms=rooms())
    waiter = rw.Waiter()
    t.on_message("board", waiter.offer)
    t.start()
    seen = {}

    def fallback(issue, kind, base):
        seen.update(issue=issue, kind=kind, base=base)
        return {"issue": issue, "pr_url": PR, "tests_passed": True, "summary": "fixed", "cost_usd": 0.2}
    res = rw.dispatch(t, rw.DevTask(1, "incident", base="gen-1"), waiter, timeout=0.05, fallback=fallback)
    assert seen == {"issue": 1, "kind": "incident", "base": "gen-1"}
    assert res["via"] == "devworker" and res["pr"] == PR and res["cost_usd"] == 0.2


def test_waiter_ignores_other_threads_and_senders():
    t = FakeTransport(rooms=rooms())
    waiter = rw.Waiter()
    t.on_message("board", waiter.offer)
    t.start()
    root = t.post("board", "dev", "@synaps-dev fix acme-shop#1 (base gen-1)")
    slot = waiter.expect(room="dev", thread=root, sender="synaps-dev", predicate=lambda m: rw.parse_result(m.text))
    t.post("reviewer", "dev", "@board " + REPLY, thread=root)          # right thread, wrong sender
    t.post("synaps-dev", "dev", REPLY)                                  # right sender, wrong thread
    assert slot.wait(0.05) is None
    t.post("synaps-dev", "dev", REPLY, thread=root)
    assert slot.wait(0.05).sender == "synaps-dev"


# ───────── review ─────────

def fake_complete(verdict="PASS"):
    seen = {}

    def complete_json(system, prompt):
        seen.update(system=system, prompt=prompt)
        return {"verdict": verdict, "summary": "Root cause fixed; test covers it.", "concerns": []}, 0.004
    return complete_json, seen


@pytest.fixture
def fake_pr(monkeypatch):
    monkeypatch.setattr(rw.acme, "pr_diff", lambda pr: "diff --git a/shop/pricing.py b/shop/pricing.py\n+guard\n")
    monkeypatch.setattr(rw.acme, "pr_files", lambda pr: ["shop/pricing.py", "tests/test_issue_1.py"])


def test_review_pass(fake_pr):
    cj, seen = fake_complete("PASS")
    tests = {"passed": True, "summary": "33 passed", "sha": "abc123", "output": "33 passed"}
    r = rw.review_pr(PR, issue=1, issue_text="empty coupon crashes", tests=tests, complete_json=cj, persona="P")
    assert r["verdict"] == "PASS" and r["cost_usd"] == 0.004 and r["tests"]["passed"]
    assert seen["system"].startswith("P") and "PASSED: 33 passed" in seen["prompt"] and "empty coupon" in seen["prompt"]
    assert "@synaps-dev" not in rw.review_text(r, PR)         # a pass does not wake synaps-dev


def test_review_harness_facts_override_the_model(fake_pr, monkeypatch):
    cj, _ = fake_complete("PASS")
    red = {"passed": False, "summary": "1 failed, 32 passed", "sha": "abc"}
    r = rw.review_pr(PR, issue=1, tests=red, complete_json=cj, persona="P")
    assert r["verdict"] == "NEEDS WORK" and "did not pass" in r["concerns"][0]
    monkeypatch.setattr(rw.acme, "pr_files", lambda pr: ["shop/pricing.py"])
    r = rw.review_pr(PR, issue=1, tests={"passed": True, "summary": "ok", "sha": "a"}, complete_json=cj, persona="P")
    assert r["verdict"] == "NEEDS WORK" and "regression test" in r["concerns"][0]
    assert "@synaps-dev" in rw.review_text(r, PR)


def test_persona_strips_frontmatter(tmp_path):
    p = tmp_path / "p.md"
    p.write_text("---\nname: Reality Checker\n---\n\n# Body\nDefault to NEEDS WORK")
    assert rw.persona_text(p).startswith("# Body")
    assert "NEEDS WORK" in rw.persona_text(tmp_path / "missing.md")


# ───────── the merge gate ─────────

def test_parse_decision():
    assert rw.parse_decision("@board approve") == "approve"
    assert rw.parse_decision("Approved, ship it") == "approve"
    assert rw.parse_decision("**LGTM**") == "approve"
    assert rw.parse_decision("@board reject: tests are thin") == "reject"
    assert rw.parse_decision("@board what does this change?") is None
    assert rw.parse_decision("I don't approve") is None              # only a leading decision counts


def gate_setup(merged=True, passed=True):
    t = FakeTransport(rooms=rooms())
    calls = {"merge": [], "verify": []}

    def merge(pr):
        calls["merge"].append(pr)
        return merged

    def verify(pr):
        calls["verify"].append(pr)
        return {"passed": passed, "summary": "34 passed" if passed else "1 failed", "sha": "deadbeef00"}
    week = {"n": 1}
    gate = rw.MergeGate(t, board="board", room="board", approvers=("haseeb", "aditya"),
                        week=lambda: week["n"], merge=merge, verify=verify)
    t.on_message("board", gate.handle)
    t.start()
    return t, gate, calls, week


def req(kind="incident"):
    return rw.MergeRequest(1, kind, PR, "fixed the empty coupon", {"verdict": "PASS", "summary": "ok",
                                                                   "tests": {"passed": True, "summary": "34 passed"}})


def test_gate_human_approve_merges_and_books_the_week():
    t, gate, calls, week = gate_setup()
    tid = gate.ask(req())
    ask = t.records[-1]
    assert ask["mentions"][:2] == ["haseeb", "aditya"] and "approve" in ask["text"] and PR in ask["text"]
    week["n"] = 2
    t.say("haseeb", "board", "@board approve", thread=tid)
    rec = gate.wait(tid, timeout=1)
    assert calls["merge"] == [PR] and calls["verify"] == [PR]
    assert rec["merged"] and rec["tests_passed"] and rec["week_fixed"] == 2 and rec["approver"] == "haseeb"
    assert "Merged acme-shop#1" in t.records[-1]["text"] and t.records[-1]["thread"] == tid
    log = QuarterLog()
    rw.record_into(log, rec)
    assert log.fixes == [{"issue": 1, "week_fixed": 2, "pr_url": PR, "tests_passed": True}]


def test_gate_agents_and_strangers_cannot_approve():
    t, gate, calls, _ = gate_setup()
    tid = gate.ask(req())
    t.post("board", "board", "@board approve", thread=tid)                    # an agent
    t.say("aditya", "board", "@board approve", thread=tid, is_agent=True)     # an agent using a person's name
    t.say("mallory", "board", "@board approve", thread=tid)                   # not an approver
    t.say("haseeb", "board", "@board approve")                                # not in the gate's thread
    t.say("haseeb", "board", "@board what's the risk?", thread=tid)           # not a decision
    assert calls["merge"] == [] and gate.wait(tid, timeout=0.01) is None
    t.say("aditya", "board", "@board reject", thread=tid)
    rec = gate.wait(tid, timeout=1)
    assert rec["decision"] == "reject" and not rec["merged"] and calls["merge"] == []
    t.say("haseeb", "board", "@board approve", thread=tid)                    # already decided
    assert calls["merge"] == []


def test_gate_failed_merge_books_nothing():
    t, gate, calls, _ = gate_setup(merged=False)
    tid = gate.ask(req())
    t.say("haseeb", "board", "@board approve", thread=tid)
    rec = gate.wait(tid, timeout=1)
    assert not rec["merged"] and calls["verify"] == [] and "week_fixed" not in rec
    log = QuarterLog()
    rw.record_into(log, rec)
    assert log.fixes == []


def test_gate_feature_goes_to_features_with_red_tests_recorded():
    t, gate, _, _ = gate_setup(passed=False)
    tid = gate.ask(req("feature"))
    t.say("haseeb", "board", "@board approve", thread=tid)
    rec = gate.wait(tid, timeout=1)
    log = QuarterLog()
    rw.record_into(log, rec)
    assert log.features == [{"issue": 1, "week_shipped": 1, "pr_url": PR, "tests_passed": False}]


def test_apply_decision_for_a_langgraph_board():
    r = req()
    kw = dict(approvers=("haseeb",), week=3, merge=lambda pr: True,
              verify=lambda pr: {"passed": True, "summary": "ok", "sha": "s"})
    assert rw.apply_decision(r, "@board approve", sender="board", sender_is_agent=True, **kw) is None
    assert rw.apply_decision(r, "hmm", sender="haseeb", sender_is_agent=False, **kw) is None
    rec = rw.apply_decision(r, "approve", sender="Haseeb", sender_is_agent=False, **kw)
    assert rec["week_fixed"] == 3 and rec["tests_passed"]


def test_approvers_from_env(monkeypatch):
    monkeypatch.setenv("COMPANY_APPROVERS", "@Haseeb, aditya ,")
    assert rw.approvers_from_env() == ("haseeb", "aditya")
