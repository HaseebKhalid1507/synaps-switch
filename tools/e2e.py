#!/usr/bin/env python3
"""M3 end-to-end check against the devstack and the private daemon.

  python3 tools/e2e.py [--agent synaps] [--room NAME]

1. Creates a fresh Mattermost-bridged room for the agent (tools/mkroom.py).
2. A person @-mentions the agent: a room session is created and pinned, the reply lands in the thread.
3. A second, separate message: same session, and it remembers the first.
4. The private daemon is stopped, a third message is posted while it is down, the daemon is started:
   the same session answers it (cursor resumed), and nothing is answered twice.
Writes scrubbed evidence to evidence/m3-e2e-<timestamp>/ and exits non-zero on any failed check.
Only ever touches the private base dir's daemon (devstack/synapsdev.py).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "devstack"))
import dev  # noqa: E402

BASE = Path(os.environ.get("SYNAPS_SWITCH_DEV_BASE", Path.home() / ".synaps-switch-dev"))
STATE = BASE / "synaps-switch"
WORD = "tangerine"

p = argparse.ArgumentParser()
p.add_argument("--agent", default="synaps")
p.add_argument("--room", default=f"e2e-{time.strftime('%H%M%S')}")
p.add_argument("--timeout", type=int, default=150)
a = p.parse_args()

E = dev.env()
T0 = time.time()
OUT = ROOT / "evidence" / f"m3-e2e-{time.strftime('%Y%m%d-%H%M%S')}"
checks = []
lines = []


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    lines.append(line)


def check(name, ok, detail=""):
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    log(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
    return ok


def sh(*argv, timeout=120):
    r = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise SystemExit(f"{' '.join(argv)} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r.stdout


def agent_state():
    return json.loads((STATE / "agents" / a.agent / "state.json").read_text())


def post(cid, text, root=""):
    p = dev.mm(E, "POST", "/posts", {"channel_id": cid, "message": text, "root_id": root})
    log(f"user posted {p['id']}{' in thread ' + root if root else ''}: {text!r}")
    return p


def channel_posts(cid):
    data = dev.mm(E, "GET", f"/channels/{cid}/posts?per_page=200")
    return sorted(data.get("posts", {}).values(), key=lambda p: p["create_at"])


def wait_reply(cid, bot_id, root_id, timeout):
    t = time.time()
    while time.time() - t < timeout:
        for p in channel_posts(cid):
            if p["user_id"] == bot_id and p.get("root_id") == root_id:
                log(f"reply in thread {root_id} after {time.time() - t:.1f}s: {p['message'][:200]!r}")
                return p
        time.sleep(2)
    return None


def write(name, text):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(dev.scrub(text))


def main():
    st = sh("devstack/synapsdev.py", "daemon", "status")
    if "running" not in st or "not running" in st:
        raise SystemExit("start the private daemon first: python3 devstack/synapsdev.py daemon start")
    room = json.loads(sh("tools/mkroom.py", a.room, "--agent", a.agent).strip().splitlines()[-1])
    rid, cid = room["room_id"], room["channel_id"]
    log(f"room {a.room}: {rid}, channel {room['channel']} ({cid}), members {room['members']}")
    bot = dev.mm(E, "GET", f"/users/username/{a.agent}", who="admin")["id"]

    # 1. first mention
    p1 = post(cid, f"@{a.agent} e2e step 1: please remember the word '{WORD}'. Reply with just: noted.")
    r1 = wait_reply(cid, bot, p1["id"], a.timeout)
    check("1 reply to the mention, threaded under it", r1 is not None)
    room_state = agent_state()["rooms"].get(rid) or {}
    sid = room_state.get("session_id")
    check("1 room session created", bool(sid), sid or "")
    sessions = sh("devstack/synapsdev.py", "sessions")
    row = next((l for l in sessions.splitlines() if sid and l.startswith(sid)), "")
    check("1 room session is live and pinned in the private daemon", " pinned " in f" {row} ", row[:160])

    # 2. a second, separate message: same session, remembers
    p2 = post(cid, f"@{a.agent} e2e step 2: what word did I ask you to remember? One word.")
    r2 = wait_reply(cid, bot, p2["id"], a.timeout)
    check("2 reply threaded under the second message", r2 is not None)
    check("2 remembers the first message", r2 is not None and WORD in r2["message"].lower(), (r2 or {}).get("message", "")[:120])
    check("2 same session", (agent_state()["rooms"].get(rid) or {}).get("session_id") == sid)

    # 3. daemon restart with a message in between
    cursor0 = agent_state().get("cursor")
    log(sh("devstack/synapsdev.py", "daemon", "stop").strip().splitlines()[-1])
    p3 = post(cid, f"@{a.agent} e2e step 3 (sent while your daemon was down): the word again? One word.")
    time.sleep(10)  # past the connection TTL: the server closes the old stream
    log(sh("devstack/synapsdev.py", "daemon", "start", timeout=90).strip().splitlines()[-1])
    r3 = wait_reply(cid, bot, p3["id"], a.timeout)
    check("3 message sent while down is answered after restart", r3 is not None)
    check("3 remembers across the restart", r3 is not None and WORD in r3["message"].lower(), (r3 or {}).get("message", "")[:120])
    st3 = agent_state()
    check("3 same session after restart", (st3["rooms"].get(rid) or {}).get("session_id") == sid)
    check("3 cursor resumed forward", (st3.get("cursor") or 0) > (cursor0 or 0), f"{cursor0} -> {st3.get('cursor')}")

    # no duplicates
    time.sleep(15)
    posts = channel_posts(cid)
    per_root = {r: sum(1 for p in posts if p["user_id"] == bot and p.get("root_id") == r) for r in (p1["id"], p2["id"], p3["id"])}
    check("no duplicate replies (exactly one per message)", all(n == 1 for n in per_root.values()), json.dumps(per_root))
    elog = (STATE / "extension.log").read_text(errors="replace").splitlines()
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(T0))
    mine = [l for l in elog if l[:19] >= since]
    delivered = [l for l in mine if " delivered " in l and sid in l]
    check("each message delivered to the session once", len(delivered) == 3, f"{len(delivered)} deliveries")

    # evidence
    users = {}
    md = [f"# Mattermost ~{room['channel']} (Switch room {rid})", "", "Read back with the Mattermost API.", ""]
    for p in posts:
        u = users.get(p["user_id"]) or dev.mm(E, "GET", f"/users/{p['user_id']}").get("username")
        users[p["user_id"]] = u
        when = time.strftime("%H:%M:%S", time.localtime(p["create_at"] / 1000))
        where = f"  ↳ in thread {p['root_id']}" if p.get("root_id") else ""
        md.append(f"- `{when}` **{u}** `{p['id']}`{where}\n\n  > " + p["message"].replace("\n", "\n  > "))
    write("mattermost-transcript.md", "\n".join(md) + "\n")
    write("mattermost-posts.json", json.dumps([{k: p.get(k) for k in ("id", "root_id", "user_id", "message", "create_at", "type")}
                                               for p in posts], indent=1, ensure_ascii=False))
    write("extension.log", "\n".join(mine) + "\n")
    write("sessions.txt", sh("devstack/synapsdev.py", "sessions"))
    st = agent_state()
    write("agent-state.json", json.dumps({"cursor": st.get("cursor"), "connection_id": st.get("connection_id"),
                                          "room": st["rooms"].get(rid)}, indent=1))
    write("checks.json", json.dumps(checks, indent=1))
    write("run.log", "\n".join(lines) + "\n")
    ok = all(c["ok"] for c in checks)
    log(f"{'ALL PASS' if ok else 'FAILED'}: {sum(c['ok'] for c in checks)}/{len(checks)}; evidence in {OUT.relative_to(ROOT)}")
    write("run.log", "\n".join(lines) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
