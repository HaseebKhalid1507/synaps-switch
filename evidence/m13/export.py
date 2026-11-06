#!/usr/bin/env python3
"""Collect #13's live run into evidence/m13/ (everything through the repo's scrubber).

    python evidence/m13/export.py --raw company/state/m13-out --db company/state/m13.db --prefix m13- --out evidence/m13/run1-seed42

Reads the run's own output (run.log, console.log, transcript.jsonl, lineage.jsonl/md, envs/, gate-person.jsonl),
the final state of every quarter thread in the board's checkpoint db (Metrics, fitness, the KPI post, dev tasks,
costs), the Mattermost channels of every generation that ran (dev.py mm-thread), the fork's PRs into the run's
`m13-gen-<k>` branches (gh, read-only) and the synaps-dev room sessions' cost. Writes no secret: every file
goes through company.board.live.scrubber (the devstack secrets, every agent key, the broker token, $HOME → ~).
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = Path(__file__).resolve().parent
REPO = "HaseebKhalid1507/acme-shop"


def quarter_states(db):
    from langgraph.checkpoint.sqlite import SqliteSaver
    conn = sqlite3.connect(str(db), check_same_thread=False)
    saver = SqliteSaver(conn)
    tids = [r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")]
    out = {}
    for tid in sorted(tids):
        t = saver.get_tuple({"configurable": {"thread_id": tid}})
        if t is not None:
            out[tid] = t.checkpoint["channel_values"]
    conn.close()
    return out


def prs(prefix):
    r = subprocess.run(["gh", "pr", "list", "-R", REPO, "--state", "all", "--limit", "100", "--json",
                        "number,title,url,state,headRefName,baseRefName,author,createdAt,mergedAt,mergeCommit,headRefOid"],
                       capture_output=True, text=True, timeout=60, check=True)
    return [p for p in json.loads(r.stdout) if p["baseRefName"].startswith(prefix)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="company/state/m13-out")
    ap.add_argument("--db", default="company/state/m13.db")
    ap.add_argument("--state-dir", default="company/state")
    ap.add_argument("--prefix", default="m13-")
    ap.add_argument("--out", default=str(OUT), help="evidence dir (default evidence/m13)")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    from company.board import live

    raw = ROOT / a.raw
    scrub = live.scrubber(ROOT / a.state_dir)
    w = lambda name, text: (out / name).write_text(scrub(text), encoding="utf-8")   # noqa: E731

    start = int((raw / "start.epoch").read_text().strip())
    end_f = raw / "end.epoch"
    end = int(end_f.read_text().strip()) if end_f.exists() else int(time.time())
    for f in ("run.log", "console.log", "transcript.jsonl", "lineage.jsonl", "lineage.md", "gate-person.jsonl",
              "gate-person.log"):
        if (raw / f).exists():
            w(f, (raw / f).read_text(encoding="utf-8"))
    if (raw / "envs").is_dir():
        (out / "envs").mkdir(exist_ok=True)
        for f in sorted((raw / "envs").glob("*.yaml")):
            w(f"envs/{f.name}", f.read_text(encoding="utf-8"))

    states = quarter_states(ROOT / a.db)
    dev = live.devstack_dir()
    quarters, channels_done, cost_rows = {}, {}, []
    for tid, st in states.items():
        setup, work, log = st.get("setup") or {}, st.get("work") or {}, st.get("log") or {}
        env, gen = (st.get("env") or {}).get("name"), st.get("generation")
        total = float(log.get("llm_cost_usd") or 0)
        dev_usd, rev_usd = float(work.get("dev_usd") or 0), float(work.get("review_usd") or 0)
        room = setup.get("dev_room_name") or f"{a.prefix}dev-g{gen}"
        sessions = live.SessionCost(room).sessions()
        cost = {"thread": tid, "env": env, "generation": gen, "total_usd": round(total, 6),
                "synaps_dev_usd": round(dev_usd, 6), "reviews_usd": round(rev_usd, 6),
                "dept_agents_and_judge_usd": round(total - dev_usd - rev_usd, 6),
                "synaps_dev_sessions": [{k: s[k] for k in ("name", "cost", "messages")} for s in sessions]}
        cost_rows.append(cost)
        quarters[tid] = {"env": env, "generation": gen, "seed": st.get("seed"), "weeks": st.get("weeks"),
                         "finished": "metrics" in st, "metrics": st.get("metrics"), "fitness": st.get("fitness"),
                         "setup": setup, "dev_tasks": work.get("tasks"), "mechanics": work.get("mech"),
                         "skipped_routes": work.get("skipped"), "pending_at_end": st.get("pending_at_end"),
                         "weeks_done": st.get("weeks_done"), "quarter_log": log, "board_inbox": st.get("board_inbox"),
                         "cost": cost}
        if st.get("report"):
            w(f"kpi-{env}-g{gen}.md", st["report"] + "\n")
        for r, ch in (setup.get("channels") or {}).items():
            channels_done[f"{r}-g{gen}"] = ch
    w("quarters.json", json.dumps(quarters, indent=1, ensure_ascii=False, default=str) + "\n")

    since = start * 1000 - 60_000
    tmp = raw / "mm"
    counts = live.export_channels(dev, channels_done, since, tmp) if dev else {}
    (out / "mattermost").mkdir(exist_ok=True)
    for f in sorted(tmp.glob("mattermost-*.json")):
        w(f"mattermost/{f.name[len('mattermost-'):]}", f.read_text(encoding="utf-8"))

    pr_list = prs(a.prefix)
    w("prs.json", json.dumps(pr_list, indent=1) + "\n")

    lineage = [json.loads(x) for x in (raw / "lineage.jsonl").read_text().splitlines() if x.strip()] \
        if (raw / "lineage.jsonl").exists() else []
    proposals = round(sum(float((r.get("cost_usd") or {}).get("proposal") or 0) for r in lineage), 6)
    quarters_usd = round(sum(c["total_usd"] for c in cost_rows), 6)
    summary = {"wall_seconds": end - start, "start": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(start)),
               "end": time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(end)),
               "quarters": cost_rows, "proposals_usd": proposals, "quarters_usd": quarters_usd,
               "total_usd": round(quarters_usd + proposals, 6), "mattermost_posts": counts,
               "prs": [{k: p[k] for k in ("number", "url", "state", "headRefName", "baseRefName", "mergedAt")} for p in pr_list]}
    w("cost.json", json.dumps(summary, indent=1) + "\n")
    print(json.dumps({k: summary[k] for k in ("wall_seconds", "quarters_usd", "proposals_usd", "total_usd")}))
    for c in cost_rows:
        print(c["thread"], c["total_usd"], "dev", c["synaps_dev_usd"], "reviews", c["reviews_usd"],
              "depts", c["dept_agents_and_judge_usd"])
    for p in pr_list:
        print(p["url"], p["state"], p["headRefName"], "->", p["baseRefName"])
    if tmp.exists():
        shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
