#!/usr/bin/env python3
"""The scripted #board person for #13's org-change gate. Posts as the devstack's Mattermost user `user`
(`devstack/dev.py mm-post --as user`, a real human account; the gate accepts it because Switch reports it as a
person on the approvers list). It never decides by content: the policy below was fixed before the run.

POLICY (fixed before the run, 2026-11-06):
  org changes   generation 1: approve.  generation 2: approve.
                generation 3: reject, "change freeze: no more org changes before the demo; propose it next quarter".
  merges        (not this script) company.board.live.MattermostApprover via `company.run --approver mattermost`:
                approve iff the reality-checker review is PASS and the harness pytest on the PR head is green.

    python evidence/m13/gate_person.py --channel m13-board-g1 --board m13-board --since-ms <ms> --log FILE

Polls the channel for the board's root post "Generation <g>: the board proposes", waits a human-ish
`--read` seconds, replies in its thread, logs every decision (jsonl), and exits after generation 3.
"""
import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

DEV = Path("~/Projects/synaps-switch/devstack/dev.py").expanduser()
POLICY = {1: "approve", 2: "approve",
          3: "reject change freeze: no more org changes before the demo; propose it next quarter"}


def dev(*args):
    r = subprocess.run([sys.executable, str(DEV), *args], capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:])
    return json.loads(r.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--board", required=True)
    ap.add_argument("--since-ms", type=int, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--read", type=float, default=20.0, help="seconds between seeing a proposal and answering")
    ap.add_argument("--last", type=int, default=max(POLICY))
    a = ap.parse_args()
    done = set()
    logf = Path(a.log)
    if logf.exists():
        done = {json.loads(line)["gen"] for line in logf.read_text().splitlines() if line.strip()}
    print(f"gate person: watching ~{a.channel} for {a.board}'s proposals; policy {POLICY}", flush=True)
    while len([g for g in done if g <= a.last]) < a.last:
        try:
            posts = dev("mm-thread", a.channel, "--since", str(a.since_ms))
        except Exception as e:  # noqa: BLE001 - Mattermost hiccup: try again
            print(f"mm-thread failed: {e}", flush=True)
            time.sleep(15)
            continue
        for p in posts:
            m = re.search(r"Generation (\d+): the board proposes (\S+) → (\S+)\.", p["message"])
            if p["root_id"] or p["user"] != a.board or not m:
                continue
            g = int(m.group(1))
            if g in done or g not in POLICY:
                continue
            time.sleep(a.read)
            decision = POLICY[g]
            text = f"@{a.board} {decision}"
            out = dev("mm-post", a.channel, text, "--root", p["id"], "--as", "user")
            rec = {"gen": g, "proposal": f"{m.group(2)} → {m.group(3)}", "mm_root": p["id"], "mm_post": out.get("id"),
                   "as": "user", "text": text, "scripted": True, "policy": "fixed before the run: approve gens 1-2, "
                   "reject gen 3", "ts": time.time()}
            with logf.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            done.add(g)
            print(f"gate person: gen {g} {rec['proposal']}: posted {text!r} as user", flush=True)
        time.sleep(10)
    print("gate person: all generations answered", flush=True)


if __name__ == "__main__":
    main()
