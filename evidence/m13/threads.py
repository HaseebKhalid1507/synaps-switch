#!/usr/bin/env python3
"""Render the Mattermost exports of one #13 run (evidence/m13/<run>/mattermost/*.json) into readable threads.

    python evidence/m13/threads.py evidence/m13/run1-seed42 --issue 1 --gens 1,2

Writes <run>/threads.md: for each generation, incident #N end to end (the market's post in #maintenance and
ops-maint's triage, the board's relay or ops-maint's direct hand-off, synaps-dev's thread in #dev with the
RESULT and the review, the merge request in #board with the person's approve and the merge), the quarter's
KPI post, and every org-change gate thread (proposal → decision → outcome). Text is the exported text, cut
only for length; the exports went through the scrubber already.
"""
import argparse
import json
import re
import time
from pathlib import Path

SKIP_USERS = {"admin"}


def load(run, room, g):
    p = Path(run) / "mattermost" / f"{room}-g{g}.json"
    return json.loads(p.read_text()) if p.exists() else []


def ts(p):
    return time.strftime("%H:%M:%S", time.localtime(p["create_at"] / 1000))


def fmt(p, cut=900, indent=""):
    msg = p["message"].strip()
    if len(msg) > cut:
        msg = msg[:cut].rstrip() + " …"
    msg = msg.replace("\n", "\n" + indent + "> ")
    return f"{indent}> **{p['user']}** ({ts(p)}{', reply' if p['root_id'] else ''}): {msg}\n"


def thread(posts, root_id):
    return [p for p in posts if p["id"] == root_id or p["root_id"] == root_id]


def roots(posts, pred):
    return [p for p in posts if not p["root_id"] and p["user"] not in SKIP_USERS and pred(p["message"])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--issue", type=int, default=1)
    ap.add_argument("--gens", default="1,2")
    ap.add_argument("--gate-room", default="board-g1")
    a = ap.parse_args()
    n = a.issue
    out = [f"# Mattermost threads: {Path(a.run).name}\n",
           "Rendered from `mattermost/*.json` (`devstack/dev.py mm-thread` exports, scrubbed). Switch's own "
           "*\"synaps-dev isn't in this room\"* notices are left out of the incident chains (by design synaps-dev "
           "is only in #dev); they are in the JSON.\n"]
    issue_pat = re.compile(rf"(issue #{n}\b|acme-shop#{n}\b|Issue #{n}\b|#{n}\b)")
    for g in [int(x) for x in a.gens.split(",")]:
        maint, board, dev = load(a.run, "maintenance", g), load(a.run, "board", g), load(a.run, "dev", g)
        out.append(f"\n## Generation {g}: incident acme-shop#{n}, end to end\n")
        out.append(f"### #maintenance-g{g}\n")
        for r in roots(maint, lambda m: f"issue #{n})" in m):
            for p in thread(maint, r["id"]):
                if p["user"] != "switch-admin":
                    out.append(fmt(p))
        out.append(f"\n### #board-g{g}: triage and hand-off\n")
        hub = roots(board, lambda m: m.startswith("(re #maintenance)") and f'"issue": {n},' in m
                    or m.startswith(f"Relaying acme-shop#{n} "))
        for r in hub:
            out.append(fmt(r))
        if not hub:
            out.append("(nothing in #board: #maintenance ↔ #dev is linked, so ops-maint triaged it in #maintenance and "
                       "handed it to synaps-dev in #dev itself, the same week; no relay)\n")
        out.append(f"\n### #dev-g{g}: the fix\n")
        for r in roots(dev, lambda m: f"fix acme-shop#{n} " in m):
            for p in thread(dev, r["id"]):
                out.append(fmt(p, cut=1600))
        out.append(f"\n### #board-g{g}: the human gate and the merge\n")
        for r in roots(board, lambda m: f"merge request: acme-shop#{n} " in m):
            for p in thread(board, r["id"]):
                out.append(fmt(p, cut=1600))
        out.append(f"\n### #board-g{g}: the quarter's KPI post\n")
        for r in roots(board, lambda m: ": KPI**" in m):
            out.append("```\n" + r["message"].strip() + "\n```\n")
    gate = load(a.run, *a.gate_room.rsplit("-g", 1))
    out.append(f"\n## The board's org-change gate (#{a.gate_room}): reflect → proposal → decision → outcome\n")
    for r in roots(gate, lambda m: "Generation " in m and "the board proposes" in m):
        for p in thread(gate, r["id"]):
            if p["user"] == "switch-admin":
                continue
            out.append(fmt(p, cut=4000))
        out.append("\n")
    (Path(a.run) / "threads.md").write_text("".join(out), encoding="utf-8")
    print(f"wrote {Path(a.run) / 'threads.md'}")


if __name__ == "__main__":
    main()
