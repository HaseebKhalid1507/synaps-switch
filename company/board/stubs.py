"""Deterministic stand-ins for an offline quarter (FakeTransport, tests): the LLM, synaps-dev,
the PR review, the merge, and the #board human.

Same input, same output: the stub LLM answers from the agent named in the system prompt and the
message it got, so a fixed seed gives the same quarter every time.
"""
from __future__ import annotations

import json
import re

STUB_CALL_USD = 0.0005      # what one stub LLM call "costs" (so the cost plumbing is exercised)
STUB_DEV_USD = 0.05         # one stub synaps-dev task
JUDGE_SCORES = {"clarity": 2, "specificity": 1, "benefit": 2, "cta": 2, "honesty": 2, "note": "stub judge"}

_WHO = re.compile(r"You are @([A-Za-z0-9._-]+), the")
_NEW = re.compile(r"New message for you in #(\S+) from (\S+) \([^)]*\):\n(.*?)\n\nReply as JSON", re.S)


def _price(prompt: str) -> float:
    m = re.search(r"Current price: \$([0-9.]+)", prompt)
    return float(m.group(1)) if m else 29.0


def stub_llm(system: str, prompt: str):
    """complete_json stand-in for every company LLM call: dept agents, finance reviews, the judge, the
    PR reviewer. Returns (obj, cost)."""
    if "judge marketing campaign" in system:
        return dict(JUDGE_SCORES), STUB_CALL_USD
    if "Policy check (computed, binding)" in prompt:
        return {"say": "Checked against the ledger and policy: approved.", "decision": "approve"}, STUB_CALL_USD
    if "reviewing a pull request on acme-shop" in system:
        return {"verdict": "PASS", "summary": "Stub review: root cause fixed, regression test added.",
                "concerns": []}, STUB_CALL_USD
    who = (_WHO.search(system) or [None, ""])[1]
    m = _NEW.search(prompt)
    text = m.group(3) if m else prompt
    none = {"say": "", "action": {"type": "none"}}
    if who == "ops-maint":
        inc = re.search(r"\(sev(\d), issue #(\d+)\)", text)
        if inc:
            sev, n = int(inc.group(1)), int(inc.group(2))
            return {"say": f"Sev{sev} triaged: issue #{n} is customer-facing, routing it for a fix. @board for visibility.",
                    "action": {"type": "route", "issue": n, "to": "synaps-dev"}}, STUB_CALL_USD
        return none, STUB_CALL_USD
    if who == "growth":
        if "Competitor cut price" in text:
            p = _price(prompt)
            return {"say": f"Holding at ${p:.2f}: we compete on fixes and features, not price.",
                    "action": {"type": "price", "value": p}}, STUB_CALL_USD
        feat = re.search(r"\(issue #(\d+), demand score ([0-9.]+)\)", text)
        if feat:
            n, demand = int(feat.group(1)), float(feat.group(2))
            if demand >= 0.8:
                return {"say": f"Build #{n} this quarter (demand {demand:.1f}). @marketing plan its launch campaign.",
                        "action": {"type": "route", "issue": n, "to": "synaps-dev"}}, STUB_CALL_USD
            return {"say": f"#{n} stays in the backlog this quarter (demand {demand:.1f}).",
                    "action": {"type": "none"}}, STUB_CALL_USD
        return none, STUB_CALL_USD
    if who == "marketing":
        n = re.search(r"#(\d+)", text)
        if n:
            copy = (f"Coming to acme-shop: the feature you asked for in issue #{n.group(1)}. "
                    "Faster checkout, fewer clicks. Try it today.")
            return {"say": "Launch campaign booked.",
                    "action": {"type": "campaign", "text": copy, "spend": 1500, "cites_feature": True}}, STUB_CALL_USD
        return none, STUB_CALL_USD
    return none, STUB_CALL_USD


class StubDev:
    """synaps-dev on FakeTransport: answers the board's dispatch in its #dev thread with a RESULT block."""

    def __init__(self, transport, *, name="synaps-dev", cost_usd=STUB_DEV_USD):
        self.t, self.name, self.cost_usd = transport, name, cost_usd
        self.spent = 0.0
        self.tasks: list[int] = []
        transport.on_message(name, self.handle)

    def handle(self, msg):
        m = re.search(r"fix acme-shop#(\d+) \(base ([^)\s]+)\)", msg.text)
        if not m or not msg.sender_is_agent:
            return
        n, base = int(m.group(1)), m.group(2)
        self.tasks.append(n)
        self.spent = round(self.spent + self.cost_usd, 6)
        res = {"status": "proposed", "summary": f"Fixed acme-shop#{n} (stub)",
               "evidence": [f"tests/test_issue_{n}.py fails before, passes after", "pytest -q: 36 passed"],
               "pr": f"stub://acme-shop?head={base}-fix-{n}&base={base}"}
        self.t.post(self.name, msg.room, f"Stub fix for acme-shop#{n}; full suite green.\n@{msg.sender}\nRESULT\n"
                    f"```json\n{json.dumps(res)}\n```", thread=msg.thread or msg.id)

    def cost(self) -> float:
        return self.spent


def stub_review(pr, *, issue, issue_text="", tests=None):
    n = re.search(r"fix-(\d+)", pr or "")
    return {"verdict": "PASS", "summary": "Stub review: root cause fixed, regression test added.", "concerns": [],
            "tests": {"passed": True, "summary": "36 passed", "sha": "stub"},
            "files": ["shop/core.py", f"tests/test_issue_{n.group(1) if n else issue}.py"], "diff_lines": 20,
            "cost_usd": STUB_CALL_USD}


def stub_merge(pr) -> bool:
    return True


def stub_verify(pr) -> dict:
    return {"passed": True, "summary": "36 passed", "sha": "stub-merged"}


def review_ok(review) -> bool:
    """The scripted human's rule: approve only a PASS review with green harness tests."""
    rv = review or {}
    return rv.get("verdict") == "PASS" and bool((rv.get("tests") or {}).get("passed"))


class StubApprover:
    """A #board person on FakeTransport: approves a merge whose review passed with green tests, else rejects."""

    def __init__(self, human="Haseeb"):
        self.human = human

    def __call__(self, rt, thread, req):
        ok = review_ok(req.review)
        rt.t.say(self.human, rt.hub, f"@{rt.board} {'approve' if ok else 'reject'}", thread=thread)
        return "approve" if ok else "reject"
