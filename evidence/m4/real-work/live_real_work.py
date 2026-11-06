#!/usr/bin/env python3
"""Live: acme-shop#1 through #dev → synaps-dev → review → #board approval → merge, on the local Switch stack.

Setup (once): python3 tools/synaps_dev.py setup  (local stand-in origin with `baseline` + gen-1, the
synaps-dev agent entry), restart the private daemon, then the rooms:
  python3 tools/mkroom.py m12-dev   --agent synaps-dev --with m12-board --with m12-reviewer
  python3 tools/mkroom.py m12-board --agent synaps-dev --with m12-board
Run from the repo root:  python evidence/m4/real-work/live_real_work.py ISSUE [--gen 1]

  1. the market is built from the real acme-shop issues (read-only gh issue list)
  2. m12-board posts "@synaps-dev fix acme-shop#N (base gen-k)" in #m12-dev; synaps-dev (the extension's
     Sonnet room session, in its own clone) fixes it, runs the tests, commits, pushes its branch to the
     local origin and replies with the RESULT block
  3. testing-reality-checker (Haiku, company/llm.py) reviews the diff + a harness pytest run of the PR head
  4. m12-board asks @user in #m12-board; a non-approver, an agent and a question are ignored;
     user's "@m12-board approve" → devworker.merge_pr → pytest on the merged base → QuarterLog
  5. reset: gen-(k+1) is cut from `baseline` again and the new regression test fails there (same bug)
Nothing touches GitHub except read-only issue reads. Prints no secret.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "devstack"))
import dev  # noqa: E402
from company import acme, realwork as rw  # noqa: E402
from company.core.env import load_env  # noqa: E402
from company.market.sim import QuarterLog  # noqa: E402
from company.transport import SwitchTransport  # noqa: E402

OUT = Path(__file__).resolve().parent
PRIV = Path("~/.synaps-switch-dev/m12").expanduser()
REMOTE = "file://" + str(PRIV / "remote" / "acme-shop-origin.git")
BOARD, REVIEWER, DEV = "m12-board", "m12-reviewer", "synaps-dev"
DEV_ROOM, BOARD_ROOM = "m12-dev", "m12-board"
HUMAN = "user"                     # the dev stack's seeded Mattermost user
lines = []


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(map(str, a))
    lines.append(line)
    print(line, flush=True)


def mm(*args):
    out = subprocess.run([sys.executable, str(ROOT / "devstack" / "dev.py"), *args], capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout)


def wait(pred, timeout):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(1)
    return None


def mm_root_of(channel, needle, since):
    posts = mm("mm-thread", channel, "--since", str(since))
    return next((p["id"] for p in posts if needle in p["message"] and not p["root_id"]), None)


def transport_creds():
    """The m12 agents' Switch credentials (registered earlier, 0600) in the transport's layout."""
    for n in (BOARD, REVIEWER):
        dst = PRIV / "transport" / "agents" / f"{n}.json"
        if dst.exists():
            continue
        c = json.loads((PRIV / "agents" / n / "credentials.json").read_text())
        dst.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"name": n, "id": c["id"], "api_key": c["api_key"], "base_url": dev.api_url()}, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("issue", type=int)
    ap.add_argument("--gen", type=int, default=1)
    ap.add_argument("--dev-timeout", type=float, default=1500)
    a = ap.parse_args()
    checks, cost = {}, {"review_usd": 0.0}
    t_start = int(time.time() * 1000)

    # 1. market from the real issues
    issues = acme.load_issues(cache=OUT / "issues.json", refresh=True)
    env = load_env(ROOT / "company/envs/env1.yaml")
    market = acme.build_market(1, env.company, issues=issues)
    wk1 = market.events_for_week(1)
    log(f"market: {len(issues['incidents'])} incidents + {len(issues['features'])} features from {issues['repo']}; "
        f"week 1: {[(e.kind, e.payload.get('issue')) for e in wk1 if e.kind == 'incident']}")
    checks["market built from the 8 real issues"] = (len(issues["incidents"]), len(issues["features"])) == (4, 4)
    kind = acme.issue_kind(issues, a.issue)
    title = next(i["title"] for i in issues["incidents"] + issues["features"] if i["issue"] == a.issue)
    body = subprocess.run(["gh", "issue", "view", str(a.issue), "-R", issues["repo"], "--json", "body", "-q", ".body"],
                          capture_output=True, text=True, timeout=60).stdout

    cfg = acme.config(remote=REMOTE)
    base = acme.start_generation(a.gen, cfg)
    base_sha = acme.remote_refs(REMOTE)[f"refs/heads/{base}"]
    log(f"local origin {REMOTE}: {base} at {base_sha[:10]} (= baseline)")

    # transport: the board and the reviewer
    transport_creds()
    room_ids = json.loads((PRIV / "rooms.json").read_text())
    t = SwitchTransport(dev.api_url(), agents=[BOARD, REVIEWER], state_dir=PRIV / "transport",
                        transcript_path=OUT / "transcript.jsonl", log=lambda *x: log("transport:", *x))
    week = {"n": 1}
    waiter = rw.Waiter()
    gate = rw.MergeGate(t, board=BOARD, room=BOARD_ROOM, approvers=(HUMAN,), week=lambda: week["n"],
                        log=lambda *x: log(*x))
    got = {BOARD: [], REVIEWER: []}

    def board(m):
        got[BOARD].append(m)
        log(f"{BOARD} <- {m.sender} (agent={m.sender_is_agent}) in {m.room} thread={m.thread}: {m.text[:100]!r}")
        if m.room == BOARD_ROOM:
            gate.handle(m)
        else:
            waiter.offer(m)

    t.on_message(BOARD, board)
    t.on_message(REVIEWER, lambda m: got[REVIEWER].append(m))
    t.start()
    t.pin_rooms(room_ids)

    # 2. #dev: the task → synaps-dev → RESULT
    task = rw.DevTask(a.issue, kind, base=base, asker=BOARD, dev=DEV, room=DEV_ROOM, title=title)

    def no_fallback(issue, kind, base):
        return {"pr_url": None, "summary": "synaps-dev did not answer in time", "tests_passed": False}

    log(f"dispatch: {rw.dispatch_text(task)!r}")
    res = rw.dispatch(t, task, waiter, timeout=a.dev_timeout, fallback=no_fallback)
    log(f"RESULT via {res['via']} after {res['seconds']}s: {json.dumps({k: res[k] for k in ('status', 'summary', 'evidence', 'pr')})}")
    checks["synaps-dev replied with a valid RESULT block"] = res["via"] == DEV and res.get("valid", False)
    checks["RESULT status proposed with a PR ref"] = res["status"] == "proposed" and bool(res["pr"])
    if not res["pr"]:
        return finish(checks, cost, t, t_start, None)
    pr = res["pr"]
    p = acme.parse_pr_ref(pr)
    checks["PR ref is the local origin, base = the generation"] = p["local"] and p["remote"] == REMOTE and p["base"] == base

    # 3. review on the PR head
    tests = acme.run_tests_at(REMOTE, p["head"])
    log(f"harness pytest on {p['head']} ({tests['sha'][:10]}): {'green' if tests['passed'] else 'RED'}: {tests['summary']}")
    review = rw.review_pr(pr, issue=a.issue, issue_text=f"{title}\n\n{body}", tests=tests)
    cost["review_usd"] += review["cost_usd"]
    log(f"review: {review['verdict']} ({review['summary']}) concerns={review['concerns']} ${review['cost_usd']:.4f}")
    t.post(REVIEWER, DEV_ROOM, rw.review_text(review, pr, board=BOARD, dev=DEV), thread=res["thread"])
    checks["tests green on the PR head"] = bool(tests["passed"])
    checks["regression test in the diff"] = any(f.startswith("tests/") for f in review["files"])

    # 4. #board: ask the humans; only an approver's decision counts
    req = rw.MergeRequest(a.issue, kind, pr, res["summary"], review, res["evidence"])
    tid = gate.ask(req)
    log(f"{BOARD} asked #{BOARD_ROOM} (thread {tid})")
    since = t_start - 60_000
    mm_root = wait(lambda: mm_root_of(BOARD_ROOM, f"acme-shop#{a.issue} ({kind})", since), 60)
    week["n"] = 2
    t.post(REVIEWER, BOARD_ROOM, f"@{BOARD} approve", thread=tid)                       # an agent: ignored
    mm("mm-post", BOARD_ROOM, f"@{BOARD} what is the blast radius if this is wrong?", "--root", mm_root)
    time.sleep(8)
    checks["agent and a question did not merge"] = tid in gate.pending and \
        acme.remote_refs(REMOTE)[f"refs/heads/{base}"] == base_sha
    mm("mm-post", BOARD_ROOM, f"@{BOARD} approve", "--root", mm_root)
    rec = gate.wait(tid, timeout=180)
    log(f"decision: {json.dumps(rec)}")
    qlog = QuarterLog()
    rw.record_into(qlog, rec)
    checks["human approve merged into the generation branch"] = bool(rec and rec["merged"])
    checks["tests green on the merged base"] = bool(rec and rec["tests_passed"])
    checks["week_fixed = week of merge"] = bool(rec) and qlog.fixes and qlog.fixes[0]["week_fixed"] == 2
    refs = acme.remote_refs(REMOTE)
    checks["main and baseline untouched"] = refs["refs/heads/main"] == refs["refs/tags/baseline"] == base_sha
    log(f"QuarterLog.fixes = {qlog.fixes}")

    # 5. reset: the next generation faces the same bug
    nxt = acme.start_generation(a.gen + 1, cfg)
    refs = acme.remote_refs(REMOTE)
    checks[f"{nxt} starts at baseline"] = refs[f"refs/heads/{nxt}"] == base_sha
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        co = acme.clone(REMOTE, Path(tmp) / "co", nxt)
        head = acme.clone(REMOTE, Path(tmp) / "head", p["head"])
        new_tests = [f for f in review["files"] if f.startswith("tests/")]
        for f in new_tests:
            (co / f).write_text((head / f).read_text())
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *new_tests], cwd=co,
                           capture_output=True, text=True, timeout=120)
        tail = [x for x in r.stdout.splitlines() if re.search(r"\d+ (passed|failed)", x)]
        log(f"regression test on {nxt}: {tail[-1] if tail else r.stdout[-200:]}")
        checks[f"the regression test fails on {nxt} (same bug)"] = r.returncode != 0
    git_log = acme.git("log", "--format=%h %an <%ae> %s", "--graph", f"refs/heads/{base}", f"refs/heads/{p['head']}",
                       "-n", "8", cwd=acme.local_path(REMOTE)).stdout
    (OUT / "git-log.txt").write_text(f"# {REMOTE}\n"
                                     + "".join(f"{v[:10]} {k}\n" for k, v in sorted(refs.items())) + "\n" + git_log)
    (OUT / "pr.diff").write_text(acme.pr_diff(pr))
    (OUT / "quarter-log.json").write_text(json.dumps({"fixes": qlog.fixes, "features": qlog.features, "decision": rec,
                                                      "review": review, "result": res}, indent=1, default=str) + "\n")
    return finish(checks, cost, t, t_start, base)


def finish(checks, cost, t, t_start, base):
    time.sleep(3)
    t.stop()
    for ch in (DEV_ROOM, BOARD_ROOM):
        (OUT / f"mattermost-{ch}.json").write_text(
            json.dumps(mm("mm-thread", ch, "--since", str(t_start - 60_000)), indent=1, ensure_ascii=False) + "\n")
    for k, v in checks.items():
        log(f"{'PASS' if v else 'FAIL'}: {k}")
    log(f"{sum(map(bool, checks.values()))}/{len(checks)} PASS; review spend ${cost['review_usd']:.4f}")
    (OUT / "live.log").write_text("\n".join(lines) + "\n")
    for f in OUT.rglob("*"):                    # the shared evidence scrubber: secrets → [REDACTED], $HOME → ~
        if f.is_file() and f.suffix in (".log", ".json", ".jsonl", ".txt", ".diff", ".md"):
            f.write_text(dev.scrub(f.read_text()))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
