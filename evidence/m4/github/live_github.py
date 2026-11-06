#!/usr/bin/env python3
"""Live (#26): an acme-shop incident fixed by synaps-dev becomes a REAL PR on the fork, merged after a #board human.

Setup (once, see README.md): `tools/synaps_dev.py setup --github --gen 1 --prefix m26-` (baseline tag + m26-gen-1
on the fork, deploy-key synaps-dev entry, ~/.synaps-switch-dev/acme-github.env), private daemon restarted, then
  python evidence/m4/github/live_github.py register
  python3 tools/mkroom.py m26-dev   --agent synaps-dev --with m26-board --with m26-reviewer
  python3 tools/mkroom.py m26-board --agent synaps-dev --with m26-board --with m26-reviewer
Run from the repo root, with the company venv:
  set -a; . ~/.synaps-switch-dev/acme-github.env; set +a
  python evidence/m4/github/live_github.py run 3

  1. market: the fork's issues, only allowlisted authors (ACME_ISSUE_AUTHORS); live check that the filter applies
  2. m26-board posts "@synaps-dev fix acme-shop#N (base m26-gen-1)" in #m26-dev. synaps-dev (the extension's Sonnet
     room session: gh logged out, no ssh agent, deploy key only) fixes it, pushes m26-gen-1-fix-N, and reports
     the BRANCH in its RESULT block
  3. this process (Haseeb's normal gh) opens the PR: gh pr create -R <fork> --base m26-gen-1 --head m26-gen-1-fix-N
  4. testing-reality-checker (Haiku) reviews `gh pr diff` + a harness pytest run of the head
  5. m26-board asks @user in #m26-board; an agent's "approve" and a question are ignored; a SCRIPTED human approval
     (devstack/dev.py mm-post --as user) → gh pr merge --squash --match-head-commit <reviewed sha> into m26-gen-1
  6. the session's own environment: synaps-dev runs session_probe.sh in its room (gh auth status fails, a push to
     another repo is denied); the same probe is run here with the env the extension passes (no LLM)
Prints no secret; every output file goes through the repo scrubber.
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
BASE = Path("~/.synaps-switch-dev").expanduser()
PRIV = BASE / "m26"
BOARD, REVIEWER, DEV = "m26-board", "m26-reviewer", "synaps-dev"
DEV_ROOM, BOARD_ROOM = "m26-dev", "m26-board"
HUMAN = "user"                     # the dev stack's seeded Mattermost user; its approval below is SCRIPTED
PREFIX, GEN = "m26-", 1
OTHER_REPO = "git@github.com:HaseebKhalid1507/synaps-switch.git"     # the probe's "another repo" (dry-run only)
lines = []


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(map(str, a))
    lines.append(line)
    print(line, flush=True)


def mm(*args):
    out = subprocess.run([sys.executable, str(ROOT / "devstack" / "dev.py"), *args], capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout)


def wait(pred, timeout, every=1.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(every)
    return None


def transport(**kw):
    return SwitchTransport(dev.api_url(), agents=[BOARD, REVIEWER], state_dir=PRIV / "transport",
                           registration_token_file=str(Path("~/Projects/synaps-switch/devstack/state/registration.token")
                                                       .expanduser()), **kw)


def register():
    """Register m26-board and m26-reviewer (credentials 0600 under ~/.synaps-switch-dev/m26/transport)."""
    t = transport(log=lambda *x: print(*x))
    t.start()
    for n in (BOARD, REVIEWER):
        t._link(n)
        creds = PRIV / "transport" / "agents" / f"{n}.json"
        print(f"{n}: registered, credentials {creds} ({oct(creds.stat().st_mode & 0o777)})")
    t.stop()


def dev_entry():
    cfg = json.loads((BASE / "synaps-switch.json").read_text())
    return next(a for a in cfg["agents"] if a["name"] == DEV)


def session_env_of_entry():
    """Exactly what the extension passes to synaps-dev's room session (main.session_env of the live config)."""
    sys.path.insert(0, str(ROOT))
    import main
    e = dev_entry()
    return dict(main.session_env(e.get("session_env"), e.get("session_path")))


def dev_session_id(room):
    st = json.loads((BASE / "synaps-switch" / "agents" / DEV / "state.json").read_text())
    rid = json.loads((PRIV / "rooms.json").read_text())[room]
    r = st.get("rooms", {}).get(rid) or {}
    return r.get("session_id"), r.get("cwd")


def session_cost(sid):
    f = BASE / "sessions" / f"{sid}.json"
    if not sid or not f.exists():
        return None
    s = json.loads(f.read_text())
    for k in ("session_cost", "total_cost", "cost_usd"):
        if isinstance(s.get(k), (int, float)):
            return float(s[k])
    return None


def probe_here(cwd):
    """The probe with the env the extension gives the session (rebuilt from the live config), in its clone."""
    env = session_env_of_entry()
    r = subprocess.run(["bash", str(OUT / "session_probe.sh")], cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=120)
    return r.stdout + r.stderr


def probe_checks(text, where):
    return {
        f"{where}: gh auth status fails (gh logged out)": bool(re.search(r"gh auth status exit=[1-9]", text)),
        f"{where}: gh api user fails": bool(re.search(r"gh api user exit=[1-9]", text)),
        f"{where}: no ssh agent": "SSH_AUTH_SOCK=<unset>" in text and "ssh-add -l exit=2" in text,
        f"{where}: push to another repo denied": bool(re.search(r"push other repo exit=[1-9]", text))
                                                    and "denied to deploy key" in text,
        f"{where}: the deploy key reads the fork": bool(re.search(r"ls-remote origin exit=0", text)),
    }


def result_from_mm(issue, base, since):
    """Resume: synaps-dev's last parseable RESULT already in the dispatch thread (read from Mattermost)."""
    posts = mm("mm-thread", DEV_ROOM, "--since", str(since - 24 * 3600_000))
    root = next((p["id"] for p in posts if not p["root_id"]
                 and p["message"].startswith(f"@{DEV} fix acme-shop#{issue} (base {base})")), None)
    found = [p for p in posts if p["root_id"] == root and p.get("user") == DEV and rw.parse_result(p["message"])]
    return (found[-1]["message"], f"mm:{found[-1]['id']}") if found else (None, None)


def run(a):
    checks, cost = {}, {"review_usd": 0.0}
    t_start = int(time.time() * 1000)
    cfg = acme.config()
    assert cfg.repo == acme.FORK_DEFAULT and not cfg.local and cfg.allow_github_writes and cfg.deploy_key, \
        "load ~/.synaps-switch-dev/acme-github.env first"
    log(f"config: ACME_REPO={cfg.repo} ACME_REMOTE={cfg.remote_url} writes={cfg.allow_github_writes} "
        f"deploy key {cfg.deploy_key} authors={list(acme.issue_authors())}")

    # 1. the market: the fork's issues, allowlisted authors only
    issues = acme.load_issues(cfg.repo, cache=OUT / "issues.json", refresh=True)
    log(f"market: {len(issues['incidents'])} incidents + {len(issues['features'])} features from {issues['repo']} "
        f"(authors {issues['authors']})")
    checks["market built from the fork's 8 issues"] = (len(issues["incidents"]), len(issues["features"])) == (4, 4)
    os.environ["ACME_ISSUE_AUTHORS"] = "adityapat958"       # the fork's issues are by HaseebKhalid1507: all dropped
    only_aditya = acme.load_issues(cfg.repo)
    os.environ["ACME_ISSUE_AUTHORS"] = ",".join(issues["authors"])
    log(f"same fork with ACME_ISSUE_AUTHORS=adityapat958: {len(only_aditya['incidents'])} incidents, "
        f"{len(only_aditya['features'])} features")
    checks["the author allowlist filters live issues"] = only_aditya["incidents"] == only_aditya["features"] == []
    kind = acme.issue_kind(issues, a.issue)
    issue = acme.fetch_issue(cfg.repo, a.issue)
    title = issue["title"]

    base = acme.gen_branch(GEN, PREFIX)
    refs0 = acme.remote_refs(cfg.remote_url)
    base_sha = refs0[f"refs/heads/{base}"]
    main_sha = refs0["refs/heads/main"]
    log(f"fork: {base} at {base_sha[:10]}, baseline {refs0['refs/tags/baseline'][:10]}, main {main_sha[:10]}")
    checks[f"{base} starts at baseline"] = base_sha == refs0["refs/tags/baseline"]
    head = acme.work_branch(a.issue, base)
    if f"refs/heads/{head}" in refs0 and not a.thread:
        sys.exit(f"{head} already exists on the fork; use another issue or generation")

    room_ids = json.loads((PRIV / "rooms.json").read_text())
    t = transport(transcript_path=OUT / "transcript.jsonl", log=lambda *x: log("transport:", *x))
    week = {"n": 1}
    waiter = rw.Waiter()
    gate = rw.MergeGate(t, board=BOARD, room=BOARD_ROOM, approvers=(HUMAN,), week=lambda: week["n"],
                        log=lambda *x: log(*x))

    def board(m):
        log(f"{BOARD} <- {m.sender} (agent={m.sender_is_agent}) in {m.room} thread={m.thread}: {m.text[:100]!r}")
        if m.room == BOARD_ROOM:
            gate.handle(m)
        else:
            waiter.offer(m)

    t.on_message(BOARD, board)
    t.on_message(REVIEWER, lambda m: None)
    t.start()
    t.pin_rooms(room_ids)

    # 2-3. #dev: the task → synaps-dev pushes a branch → this process opens the PR
    task = rw.DevTask(a.issue, kind, base=base, asker=BOARD, dev=DEV, room=DEV_ROOM, title=title)

    def no_fallback(issue, kind, base):
        return {"pr_url": None, "summary": "synaps-dev did not answer in time", "tests_passed": False}

    if a.thread:            # resume: the dispatch was already posted (an earlier run of this script died waiting)
        log(f"resume: waiting on the dispatch already in #{DEV_ROOM}, thread {a.thread}")
        slot = waiter.expect(room=DEV_ROOM, thread=a.thread, sender=DEV, predicate=lambda m: rw.parse_result(m.text))
        if a.nudge:
            log(f"nudge in the thread: {a.nudge!r}")
            t.post(BOARD, DEV_ROOM, a.nudge, thread=a.thread)
        msg = None if a.result_from_mm else slot.wait(a.dev_timeout)
        text, mid = (msg.text, msg.id) if msg is not None else result_from_mm(a.issue, base, t_start)
        res = {"status": "blocked", "pr": None, "via": "none", "summary": "no RESULT", "evidence": [], "seconds": None}
        if text:
            res = rw.parse_result(text)
            res.update(via=DEV, message_id=mid, thread=a.thread, seconds=None)
            log(f"RESULT as reported: status={res['status']} pr={res['pr']}")
            res = rw.land_pr(res, task)
    else:
        log(f"dispatch: {rw.dispatch_text(task)!r}")
        res = rw.dispatch(t, task, waiter, timeout=a.dev_timeout, fallback=no_fallback)
    log(f"RESULT via {res['via']} after {res['seconds']}s: "
        + json.dumps({k: res.get(k) for k in ('status', 'summary', 'evidence', 'reported_pr', 'pr', 'pr_opened_by')}))
    checks["synaps-dev replied with a valid RESULT block"] = res["via"] == DEV and res.get("valid", False)
    checks["RESULT pr = the pushed branch (the session opened no PR)"] = res.get("reported_pr") == head
    pr = res.get("pr")
    if not pr or not pr.startswith("https://"):
        return finish(checks, cost, t, t_start)
    info = acme.pr_info(pr)
    view = json.loads(subprocess.run(["gh", "pr", "view", pr, "--json", "author,title,body,commits,url"],
                                     capture_output=True, text=True, timeout=60).stdout)
    log(f"PR {pr}: {info['head']} -> {info['base']} by {view['author']['login']}, head {info['head_sha'][:10]}")
    checks["a real PR on the fork into the generation branch"] = \
        info["repo"] == cfg.repo and info["base"] == base and info["head"] == head and not info["cross_repo"]
    checks["the PR was opened by the company process (operator's gh)"] = \
        res.get("pr_opened_by") == "company" and view["author"]["login"] == "HaseebKhalid1507"

    # 4. review: gh pr diff + harness pytest of the head
    tests = acme.run_tests_at(acme.read_url(cfg.repo), head)
    log(f"harness pytest on {head} ({tests['sha'][:10]}): {'green' if tests['passed'] else 'RED'}: {tests['summary']}")
    checks["the reviewed commit is the PR head"] = tests["sha"] == info["head_sha"]
    review = rw.review_pr(pr, issue=a.issue, issue_text=f"{title}\n\n{issue['body']}", tests=tests)
    cost["review_usd"] += review["cost_usd"]
    log(f"review: {review['verdict']} ({review['summary']}) concerns={review['concerns']} ${review['cost_usd']:.4f}")
    t.post(REVIEWER, DEV_ROOM, rw.review_text(review, pr, board=BOARD, dev=DEV), thread=res["thread"])
    checks["tests green on the PR head"] = bool(tests["passed"])
    checks["review read the diff: regression test in it"] = any(f.startswith("tests/") for f in review["files"])

    # 5. #board: only a human approver merges
    req = rw.MergeRequest(a.issue, kind, pr, res["summary"], review, res["evidence"])
    tid = gate.ask(req)
    log(f"{BOARD} asked #{BOARD_ROOM} (thread {tid})")
    mm_root = wait(lambda: next((p["id"] for p in mm("mm-thread", BOARD_ROOM, "--since", str(t_start - 60_000))
                                 if f"acme-shop#{a.issue} ({kind})" in p["message"] and not p["root_id"]), None), 60)
    week["n"] = 2
    t.post(REVIEWER, BOARD_ROOM, f"@{BOARD} approve", thread=tid)                       # an agent: ignored
    mm("mm-post", BOARD_ROOM, f"@{BOARD} what is the blast radius if this is wrong?", "--root", mm_root, "--as", HUMAN)
    time.sleep(8)
    checks["an agent's approve and a question did not merge"] = tid in gate.pending and \
        acme.pr_info(pr)["state"] == "OPEN"
    log(f"SCRIPTED human approval: devstack/dev.py mm-post {BOARD_ROOM} '@{BOARD} approve' --root <thread> --as {HUMAN}")
    mm("mm-post", BOARD_ROOM, f"@{BOARD} approve", "--root", mm_root, "--as", HUMAN)
    rec = gate.wait(tid, timeout=240)
    log(f"decision: {json.dumps(rec)}")
    qlog = QuarterLog()
    rw.record_into(qlog, rec)
    after = acme.pr_info(pr)
    merged = json.loads(subprocess.run(["gh", "pr", "view", pr, "--json", "state,mergedBy,mergeCommit,baseRefName"],
                                       capture_output=True, text=True, timeout=60).stdout)
    log(f"PR after: {json.dumps(merged)}")
    refs = acme.remote_refs(cfg.remote_url)
    checks["human approve → gh pr merge --squash into the generation"] = bool(rec and rec["merged"]) and \
        after["state"] == "MERGED" and merged["baseRefName"] == base
    checks["tests green on the merged base"] = bool(rec and rec["tests_passed"])
    checks["week_fixed = week of merge"] = bool(rec) and bool(qlog.fixes) and qlog.fixes[0]["week_fixed"] == 2
    checks["fork main and baseline untouched"] = refs["refs/heads/main"] == main_sha == refs["refs/tags/baseline"]
    log(f"QuarterLog.fixes = {qlog.fixes}")
    (OUT / "pr.diff").write_text(acme.pr_diff(pr))
    (OUT / "pr.json").write_text(json.dumps({"url": pr, "info": info, "after": merged, "opened_as": view["author"],
                                             "title": view["title"], "body": view["body"],
                                             "commits": view["commits"]}, indent=1) + "\n")
    (OUT / "fork-refs.txt").write_text(f"# git ls-remote {cfg.remote_url} (after the merge)\n"
                                       + "".join(f"{v} {k}\n" for k, v in sorted(refs.items())))

    # 6. the session's environment: no gh, no agent, deploy key only
    sid, cwd = dev_session_id(DEV_ROOM)
    here = probe_here(cwd)
    (OUT / "probe-company-side.txt").write_text(here)
    checks.update(probe_checks(here, "probe with the extension's session env"))
    ask = (f"@{DEV} housekeeping, not a code task: run `bash {OUT / 'session_probe.sh'}` exactly once in your "
           "clone and paste its full output in this thread inside a ``` block. Change nothing, push nothing.")
    root = t.post(BOARD, DEV_ROOM, ask)
    slot = waiter.expect(room=DEV_ROOM, thread=root, sender=DEV, predicate=lambda m: "push other repo exit=" in m.text)
    got = slot.wait(a.probe_timeout)
    in_session = got.text if got else ""
    (OUT / "probe-in-session.txt").write_text(in_session or "(no reply)\n")
    checks.update(probe_checks(in_session, "probe run by synaps-dev in its room session"))
    sid, _ = dev_session_id(DEV_ROOM)
    cost["synaps_dev_session_usd"] = session_cost(sid)
    cost["session"] = sid
    (OUT / "quarter-log.json").write_text(json.dumps({"fixes": qlog.fixes, "features": qlog.features, "decision": rec,
                                                      "review": review, "result": res}, indent=1, default=str) + "\n")
    return finish(checks, cost, t, t_start)


def finish(checks, cost, t, t_start):
    time.sleep(3)
    t.stop()
    for ch in (DEV_ROOM, BOARD_ROOM):
        (OUT / f"mattermost-{ch}.json").write_text(
            json.dumps(mm("mm-thread", ch, "--since", str(t_start - 60_000)), indent=1, ensure_ascii=False) + "\n")
    for k, v in checks.items():
        log(f"{'PASS' if v else 'FAIL'}: {k}")
    log(f"{sum(map(bool, checks.values()))}/{len(checks)} PASS; review spend ${cost['review_usd']:.4f}; "
        f"synaps-dev session {cost.get('session')} ${cost.get('synaps_dev_session_usd')}")
    (OUT / "live.log").write_text("\n".join(lines) + "\n")
    scrub()
    return 0 if all(checks.values()) else 1


def scrub():
    for f in OUT.rglob("*"):                    # the shared evidence scrubber: secrets → [REDACTED], $HOME → ~
        if f.is_file() and f.suffix in (".log", ".json", ".jsonl", ".txt", ".diff", ".md"):
            f.write_text(dev.scrub(f.read_text()))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("register")
    r = sub.add_parser("run")
    r.add_argument("issue", type=int)
    r.add_argument("--dev-timeout", type=float, default=1500)
    r.add_argument("--probe-timeout", type=float, default=300)
    r.add_argument("--thread", help="resume: the Switch id of a dispatch already posted in #m26-dev")
    r.add_argument("--nudge", help="resume: the board posts this in the thread first")
    r.add_argument("--result-from-mm", action="store_true", help="resume: take the RESULT already in the thread")
    a = ap.parse_args()
    sys.exit(register() if a.cmd == "register" else run(a))
