"""company/realwork.py — real engineering work inside a quarter: #dev → review → #board approval → merge.

    #dev    board       "@synaps-dev fix acme-shop#1 (base gen-1)"            dispatch_text() / dispatch()
            synaps-dev  "<summary> @board RESULT ```json {...}```"            parse_result()
            reviewer    testing-reality-checker verdict on the diff + pytest  review_pr() / review_text()
    #board  board       "@haseeb @aditya merge acme-shop#1? ..."              MergeGate.ask() / approval_text()
            a human     "@board approve"                                      MergeGate.handle() → decide():
                        merge_pr, then pytest on the merged base → a QuarterLog record (week_fixed = week of merge)

Everything talks through the Transport contract (company/transport.py: post / on_message / Msg). The
board's router hands every message addressed to the board agent to `Waiter.offer` (replies it waits on,
e.g. synaps-dev's RESULT) and to `MergeGate.handle` (approvals). Only a person on the approvers list can
approve; an agent never can.

For a LangGraph board (#10/#11) the gate is two calls around interrupt():
    text = approval_text(req, approvers, board)          # post it, then interrupt()
    rec  = apply_decision(req, msg.text, sender=msg.sender, sender_is_agent=msg.sender_is_agent,
                          approvers=approvers, week=current_week)       # None: not a decision, keep waiting
    record_into(quarter_log, rec)
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from company import acme

DEV_AGENT = "synaps-dev"
RESULT_STATUSES = ("proposed", "blocked", "done")
DEFAULT_APPROVERS = ("haseeb", "aditya")
PERSONA_DEFAULT = Path("~/.synaps-switch-dev/plugins/agency/skills/testing/agents/testing-reality-checker.md")
PERSONA_FALLBACK = ("You are TestingRealityChecker. You stop fantasy approvals: default to NEEDS WORK and "
                    "approve only on overwhelming, checkable evidence.")


def approvers_from_env() -> tuple[str, ...]:
    raw = os.environ.get("COMPANY_APPROVERS") or ",".join(DEFAULT_APPROVERS)
    return tuple(a.strip().lstrip("@").lower() for a in raw.split(",") if a.strip())


# ───────────────────────── waiting for replies ─────────────────────────


class _Slot:
    def __init__(self, match):
        self.match = match
        self.msg = None
        self.event = threading.Event()

    def wait(self, timeout=None):
        self.event.wait(timeout)
        return self.msg


class Waiter:
    """Replies the board is waiting for. The router calls offer(msg) for every message it gets.
    Unclaimed messages are kept for a while, so a reply that beats expect() (a fast or in-memory
    transport) is still found."""

    def __init__(self, keep=200):
        self._slots: list[_Slot] = []
        self._backlog: deque = deque(maxlen=keep)
        self._lock = threading.Lock()

    def expect(self, *, room=None, thread=None, sender=None, predicate=None) -> _Slot:
        def match(m):
            return ((room is None or m.room == room) and (thread is None or m.thread == thread)
                    and (sender is None or m.sender.lower() == sender.lower())
                    and (predicate is None or predicate(m)))
        slot = _Slot(match)
        with self._lock:
            for m in list(self._backlog):
                if match(m):
                    self._backlog.remove(m)
                    slot.msg = m
                    slot.event.set()
                    return slot
            self._slots.append(slot)
        return slot

    def offer(self, msg) -> bool:
        with self._lock:
            for s in self._slots:
                if s.match(msg):
                    self._slots.remove(s)
                    s.msg = msg
                    s.event.set()
                    return True
            self._backlog.append(msg)
        return False


# ───────────────────────── #dev: dispatch + RESULT ─────────────────────────


@dataclass
class DevTask:
    issue: int
    kind: str                       # incident | feature
    base: str = "main"              # the generation's branch, e.g. gen-1
    asker: str = "board"            # the agent that posts the task and gets the RESULT
    dev: str = DEV_AGENT
    room: str = "dev"
    title: str = ""


def dispatch_text(task: DevTask) -> str:
    what = f" ({task.title})" if task.title else ""
    return (f"@{task.dev} fix acme-shop#{task.issue} (base {task.base}){what}. Branch off {task.base}, "
            f"push your branch, open the PR into {task.base}, and reply here with the RESULT block.")


_FENCED = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.S)


def parse_result(text: str) -> dict | None:
    """The last RESULT block in a reply: {status, summary, evidence[], pr|None}, or None."""
    if not text:
        return None
    cands = [m.group(1) for m in _FENCED.finditer(text)]
    m = re.search(r"RESULT\s*:?\s*(\{.*\})", text, re.S)          # an unfenced RESULT {...}
    if m:
        cands.append(m.group(1))
    for raw in reversed(cands):
        raw = raw.strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            i, j = raw.find("{"), raw.rfind("}")
            try:
                obj = json.loads(raw[i:j + 1]) if 0 <= i < j else None
            except json.JSONDecodeError:
                obj = None
        if not isinstance(obj, dict) or "status" not in obj:
            continue
        status = str(obj.get("status") or "").strip().lower()
        pr = obj.get("pr")
        pr = None if pr in (None, "", "null", "none") else str(pr).strip()
        ev = obj.get("evidence") or []
        return {"status": status if status in RESULT_STATUSES else "blocked",
                "summary": str(obj.get("summary") or "").strip(),
                "evidence": [str(e) for e in (ev if isinstance(ev, list) else [ev])],
                "pr": pr, "valid": status in RESULT_STATUSES}
    return None


def dispatch(transport, task: DevTask, waiter: Waiter, *, timeout=900.0, fallback=None) -> dict:
    """Post the task in #dev and wait for synaps-dev's RESULT in its thread. When synaps-dev does not
    answer in time, fallback (default company.devworker.work_issue) does the work instead."""
    t0 = time.time()
    mid = transport.post(task.asker, task.room, dispatch_text(task))
    slot = waiter.expect(room=task.room, thread=mid, sender=task.dev, predicate=lambda m: parse_result(m.text))
    msg = slot.wait(timeout)
    if msg is not None:
        res = parse_result(msg.text)
        res.update(via=task.dev, message_id=msg.id, thread=mid, seconds=round(time.time() - t0, 1))
        return res
    if fallback is None:
        from company.devworker import work_issue as fallback
    r = fallback(task.issue, task.kind, base=task.base)
    return {"status": "proposed" if r.get("pr_url") else "blocked", "summary": r.get("summary", ""),
            "evidence": [f"tests_passed={r.get('tests_passed')}"], "pr": r.get("pr_url") or None,
            "valid": True, "via": "devworker", "thread": mid, "cost_usd": r.get("cost_usd", 0.0),
            "seconds": round(time.time() - t0, 1)}


# ───────────────────────── #dev: review (testing-reality-checker) ─────────────────────────


REVIEW_TASK = """\
You are reviewing a pull request on acme-shop (a small stdlib-only Python shop) for the #dev room.
Evidence you have: the GitHub issue, the PR's diff, and the output of the full pytest suite that the
review harness itself ran on a fresh clone of the PR head (not the author's claim).

Verdict PASS only if ALL hold: the change fixes the root cause the issue describes (not just the
symptom), a regression test for this issue was added and exercises the reported scenario, the full
suite passed on the PR head, and nothing unrelated changed. Otherwise NEEDS WORK, with concrete fixes.

Reply with JSON only: {"verdict": "PASS" or "NEEDS WORK", "summary": "<two sentences at most>",
"concerns": ["<short, specific>", ...]}"""


def persona_text(path=None) -> str:
    p = Path(os.path.expanduser(str(path or os.environ.get("COMPANY_REVIEWER_PERSONA") or PERSONA_DEFAULT)))
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return PERSONA_FALLBACK
    m = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.S)
    return (text[m.end():] if m else text).strip()


def review_pr(pr: str, *, issue: int, issue_text: str = "", tests: dict | None = None,
              complete_json: Callable | None = None, persona: str | None = None, max_diff=14000) -> dict:
    """testing-reality-checker on Haiku (company/llm.py). The harness's own facts override the model:
    failing tests or no test file in the diff is NEEDS WORK whatever the model says."""
    if complete_json is None:
        from company.llm import complete_json
    p = acme.parse_pr_ref(pr)
    diff = acme.pr_diff(pr)
    files = acme.pr_files(pr)
    if tests is None:
        tests = acme.run_tests_at(p["remote"], p["head"]) if p["local"] else {"passed": None, "summary": "not run"}
    prompt = (f"ISSUE acme-shop#{issue}\n{issue_text.strip()[:4000]}\n\n"
              f"FILES CHANGED: {', '.join(files) or '(none)'}\n\n"
              f"DIFF (base...head):\n{diff[:max_diff]}{' [truncated]' if len(diff) > max_diff else ''}\n\n"
              f"PYTEST ON THE PR HEAD ({tests.get('sha', '?')[:10]}): "
              f"{'PASSED' if tests.get('passed') else 'FAILED'}: {tests.get('summary')}\n"
              f"{(tests.get('output') or '')[-1500:]}")
    system = (persona if persona is not None else persona_text()) + "\n\n" + REVIEW_TASK
    obj, cost = complete_json(system, prompt)
    verdict = "PASS" if str(obj.get("verdict", "")).strip().upper() == "PASS" else "NEEDS WORK"
    concerns = [str(c) for c in (obj.get("concerns") or []) if str(c).strip()]
    if not tests.get("passed"):
        verdict = "NEEDS WORK"
        concerns.insert(0, f"pytest on the PR head did not pass: {tests.get('summary')}")
    if not any(f.startswith("tests/") for f in files):
        verdict = "NEEDS WORK"
        concerns.insert(0, "no test file in the diff: a regression test is required")
    return {"verdict": verdict, "summary": str(obj.get("summary") or "").strip(), "concerns": concerns,
            "tests": {k: tests.get(k) for k in ("passed", "summary", "sha")}, "files": files,
            "diff_lines": diff.count("\n"), "cost_usd": cost}


def review_text(review: dict, pr: str, *, board: str = "board", dev: str = DEV_AGENT) -> str:
    t = review["tests"]
    lines = [f"Review (testing-reality-checker): {review['verdict']}. {review['summary']}".strip(),
             f"Harness pytest on the PR head: {'green' if t.get('passed') else 'RED'} ({t.get('summary')}). "
             f"Files: {', '.join(review['files'])}."]
    if review["concerns"]:
        lines.append("Concerns: " + "; ".join(review["concerns"][:5]))
    if review["verdict"] != "PASS":
        lines.append(f"@{dev} please address the concerns above on the same branch.")
    lines.append(f"PR: {pr}")
    return "\n".join(lines)


# ───────────────────────── #board: the merge gate ─────────────────────────


@dataclass
class MergeRequest:
    issue: int
    kind: str                       # incident | feature
    pr: str
    summary: str = ""
    review: dict | None = None
    evidence: list = field(default_factory=list)


_APPROVE = re.compile(r"^(approve|approved|lgtm|ship it|merge it|yes)\b", re.I)
_REJECT = re.compile(r"^(reject|rejected|no|deny|denied|don'?t merge)\b", re.I)


def parse_decision(text: str) -> str | None:
    """'approve' | 'reject' | None. Leading @mentions and punctuation are ignored."""
    t = re.sub(r"^(\s*@[A-Za-z0-9._-]+[\s,:]*)+", "", text or "").strip().lstrip("*_`>-: ").strip()
    if _APPROVE.match(t):
        return "approve"
    if _REJECT.match(t):
        return "reject"
    return None


def approval_text(req: MergeRequest, approvers, board: str = "board") -> str:
    who = " ".join(f"@{a}" for a in approvers)
    rv = req.review or {}
    lines = [f"{who} merge request: acme-shop#{req.issue} ({req.kind}).",
             f"synaps-dev: {req.summary or '(no summary)'}"]
    if rv:
        lines.append(f"Review (testing-reality-checker): {rv.get('verdict')}. {rv.get('summary', '')}".rstrip())
        lines.append(f"Harness pytest on the PR head: {'green' if (rv.get('tests') or {}).get('passed') else 'RED'} "
                     f"({(rv.get('tests') or {}).get('summary')}).")
    lines.append(f"PR: {req.pr}")
    lines.append(f"Reply in this thread with `@{board} approve` to merge it, or `@{board} reject`.")
    return "\n".join(lines)


def decide(req: MergeRequest, decision: str, *, approver: str, week: int,
           merge: Callable | None = None, verify: Callable | None = None) -> dict:
    """Apply a human's decision. approve → merge_pr, then pytest on the merged base (tests_passed)."""
    rec = {"issue": req.issue, "kind": req.kind, "pr_url": req.pr, "decision": decision,
           "approver": approver, "week": week, "merged": False, "tests_passed": False}
    if decision != "approve":
        return rec
    if merge is None:
        from company.devworker import merge_pr as merge
    rec["merged"] = bool(merge(req.pr))
    if rec["merged"]:
        t = (verify or acme.tests_after_merge)(req.pr)
        rec.update(tests_passed=bool(t.get("passed")), tests=t.get("summary"), merged_sha=t.get("sha"))
        rec["week_fixed" if req.kind == "incident" else "week_shipped"] = week
    return rec


def apply_decision(req: MergeRequest, text: str, *, sender: str, sender_is_agent: bool, approvers,
                   week: int, merge: Callable | None = None, verify: Callable | None = None) -> dict | None:
    """The gate as one step: the message that resumed the board → a decision record, or None when the
    message is not a decision from an allowed person (an agent, someone else, or other words)."""
    if sender_is_agent or (sender or "").lstrip("@").lower() not in {a.lower() for a in approvers}:
        return None
    d = parse_decision(text)
    if d is None:
        return None
    return decide(req, d, approver=sender, week=week, merge=merge, verify=verify)


def record_into(log, rec: dict) -> None:
    """A merged decision → QuarterLog.fixes / .features in the market's shape."""
    if not rec or not rec.get("merged"):
        return
    if rec["kind"] == "incident":
        log.fixes.append({"issue": rec["issue"], "week_fixed": rec["week_fixed"], "pr_url": rec["pr_url"],
                          "tests_passed": rec["tests_passed"]})
    else:
        log.features.append({"issue": rec["issue"], "week_shipped": rec["week_shipped"], "pr_url": rec["pr_url"],
                             "tests_passed": rec["tests_passed"]})


def outcome_text(rec: dict) -> str:
    if rec["decision"] != "approve":
        return f"Not merged: {rec['approver']} rejected acme-shop#{rec['issue']}."
    if not rec["merged"]:
        return f"Approved by {rec['approver']}, but the merge of acme-shop#{rec['issue']} failed; nothing changed."
    wk = rec.get("week_fixed", rec.get("week_shipped"))
    return (f"Merged acme-shop#{rec['issue']} (approved by {rec['approver']}) as {str(rec.get('merged_sha'))[:10]}. "
            f"pytest on the merged base: {'green' if rec['tests_passed'] else 'RED'} ({rec.get('tests')}). "
            f"Booked as {'fixed' if rec['kind'] == 'incident' else 'shipped'} in week {wk}.")


class MergeGate:
    """Asks #board, takes the answer from an allowed human in that thread, merges, books the result.

        gate = MergeGate(transport, board="board", approvers=("haseeb", "aditya"), week=lambda: quarter.week)
        tid = gate.ask(MergeRequest(1, "incident", pr, summary, review))
        # router: for every Msg addressed to the board agent → gate.handle(msg)
        rec = gate.wait(tid, timeout=3600)
    """

    def __init__(self, transport, *, board="board", room="board", approvers=None, week: Callable[[], int] = lambda: 1,
                 merge: Callable | None = None, verify: Callable | None = None, log=None):
        self.t, self.board, self.room = transport, board, room
        self.approvers = tuple(approvers or approvers_from_env())
        self.week, self.merge, self.verify = week, merge, verify
        self.log = log or (lambda *a: None)
        self.pending: dict[str, MergeRequest] = {}
        self.decided: dict[str, dict] = {}
        self._cv = threading.Condition()

    def ask(self, req: MergeRequest) -> str:
        mid = self.t.post(self.board, self.room, approval_text(req, self.approvers, self.board))
        with self._cv:
            self.pending[mid] = req
        return mid

    def handle(self, msg) -> dict | None:
        with self._cv:
            req = self.pending.get(msg.thread or "")
        if req is None:
            return None
        rec = apply_decision(req, msg.text, sender=msg.sender, sender_is_agent=msg.sender_is_agent,
                             approvers=self.approvers, week=self.week(), merge=self.merge, verify=self.verify)
        if rec is None:
            self.log(f"gate: ignored {msg.id} from {msg.sender} (agent={msg.sender_is_agent}): not an approver's decision")
            return None
        with self._cv:
            if msg.thread not in self.pending:        # decided meanwhile
                return None
            self.pending.pop(msg.thread)
            self.decided[msg.thread] = rec
            self._cv.notify_all()
        self.t.post(self.board, self.room, outcome_text(rec), thread=msg.thread)
        return rec

    def wait(self, thread: str, timeout=None) -> dict | None:
        with self._cv:
            self._cv.wait_for(lambda: thread in self.decided, timeout)
            return self.decided.get(thread)
