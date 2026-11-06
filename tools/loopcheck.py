#!/usr/bin/env python3
"""Ask (3) from docs/DESIGN-company.md, live: two switch_client.Agent instances that always answer
each other (each reply @-targets the other, in the same thread) must not loop forever.

  python3 tools/loopcheck.py [--cap 4] [--wait 90]

Starts ss-ping and ss-pong (registered once, credentials under ~/.synaps-switch-dev/loopcheck, 0600),
puts both in a fresh room, a person mentions ss-ping once, then counts the agents' messages. Pass:
the exchange stops by itself, each agent handled at most --cap messages in the thread, and the guard
said so in its log. Costs nothing (no model). Writes scrubbed evidence to evidence/loopcheck-<ts>/.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "devstack"))
import dev  # noqa: E402
import switch_client as sc  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("--cap", type=int, default=4)
p.add_argument("--wait", type=int, default=90)
a = p.parse_args()

E = dev.env()
URL = f"http://127.0.0.1:{E.get('API_HOST_PORT', '18400')}"
DIR = dev.DEV_BASE / "loopcheck"
DIR.mkdir(parents=True, exist_ok=True)
os.chmod(DIR, 0o700)
OUT = ROOT / "evidence" / f"loopcheck-{time.strftime('%Y%m%d-%H%M%S')}"
lines = []


def log(*x):
    line = f"{time.strftime('%H:%M:%S')} " + " ".join(str(v) for v in x)
    print(line, flush=True)
    lines.append(line)


handled = {"ss-ping": 0, "ss-pong": 0}


def make(name, other):
    def handle(msg):
        handled[name] += 1
        log(f"{name} got {msg.message_id} from {msg.sender_name} (#{handled[name]}), answering @{other}")
        msg.agent.post(msg.room_id, f"your turn ({name} #{handled[name]})", thread_id=msg.thread,
                       targets=[other])
    d = DIR / name
    d.mkdir(exist_ok=True)
    os.chmod(d, 0o700)
    return sc.Agent(URL, name, on_message=handle, description=f"loop check agent {name}",
                    credentials_file=str(d / "credentials.json"),
                    registration_token_file=str(ROOT / "devstack" / "state" / "registration.token"),
                    state_file=str(d / "state.json"), max_thread_deliveries=a.cap,
                    log=lambda *x: log(f"[{name}]", *x))


ping, pong = make("ss-ping", "ss-pong"), make("ss-pong", "ss-ping")
for ag in (ping, pong):
    ag.start()
    assert ag.wait_connected(30), f"{ag.name} did not connect"
room_name = f"loop-{time.strftime('%H%M%S')}"
r = subprocess.run([sys.executable, "tools/mkroom.py", room_name, "--agent", "ss-ping", "--with", "ss-pong",
                    "--creds", str(DIR / "ss-ping" / "credentials.json")], cwd=ROOT, capture_output=True,
                   text=True, timeout=300)
if r.returncode:
    sys.exit(f"mkroom failed: {r.stderr[-400:]}")
room = json.loads(r.stdout.strip().splitlines()[-1])
cid = room["channel_id"]
log(f"room {room_name} ({room['room_id']}), members {room['members']}")
time.sleep(3)
root = dev.mm(E, "POST", "/posts", {"channel_id": cid, "message": "@ss-ping start a rally with @ss-pong"})
log(f"user posted {root['id']}")

last, quiet_since, t0 = -1, time.time(), time.time()
while time.time() - t0 < a.wait:
    posts = dev.mm(E, "GET", f"/channels/{cid}/posts?per_page=200")["posts"].values()
    n = sum(1 for x in posts if x.get("root_id") == root["id"])
    if n != last:
        last, quiet_since = n, time.time()
    elif time.time() - quiet_since > 20:
        break
    time.sleep(2)
for ag in (ping, pong):
    ag.stop()

posts = sorted(dev.mm(E, "GET", f"/channels/{cid}/posts?per_page=200")["posts"].values(), key=lambda x: x["create_at"])
users = {}
md = [f"# loop check ~{room_name}", ""]
for x in posts:
    users.setdefault(x["user_id"], dev.mm(E, "GET", f"/users/{x['user_id']}", who="admin").get("username"))
    md.append(f"- **{users[x['user_id']]}**{' ↳' if x.get('root_id') else ''} {x['message'][:160]}")
in_thread = [x for x in posts if x.get("root_id") == root["id"]]
guard_hits = [l for l in lines if "loop guard" in l]
checks = {
    "exchange stopped by itself (thread quiet for 20 s before the deadline)": time.time() - t0 < a.wait,
    f"each agent handled at most {a.cap} messages in the thread": max(handled.values()) <= a.cap,
    "the loop guard fired": bool(guard_hits),
}
for k, v in checks.items():
    log(("PASS " if v else "FAIL ") + k)
log(f"handled {handled}, {len(in_thread)} replies in the thread")
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "transcript.md").write_text(dev.scrub("\n".join(md) + "\n"))
(OUT / "run.log").write_text(dev.scrub("\n".join(lines) + "\n"))
sys.exit(0 if all(checks.values()) else 1)
