#!/usr/bin/env python3
"""Live check of company/transport.py SwitchTransport against the local Switch dev stack.

Two test agents (m4t-alpha, m4t-beta) in one test room (m4t-room, Mattermost-bridged):
  1. alpha @-addresses beta; beta replies in the thread; both see sender_is_agent=True
  2. a person (Mattermost user) @-addresses alpha, top level and in alpha's thread: sender_is_agent=False
  3. the transport restarts: nothing already handled is delivered again; a message sent while
     it was down arrives once
Run from the repo root:  python evidence/m4/transport/live_transport.py
Credentials: company/state/agents/<name>.json (0600, gitignored). Prints no secret.
"""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEVSTACK = Path.home() / "Projects/synaps-switch/devstack"   # the running stack's .env + registration token
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEVSTACK))
import dev  # noqa: E402
from company.transport import SwitchTransport  # noqa: E402

OUT = Path(__file__).resolve().parent
ROOM = "m4t-room"
A, B = "m4t-alpha", "m4t-beta"
nonce = uuid.uuid4().hex[:6]
log_lines = []


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(map(str, a))
    log_lines.append(line)
    print(line, flush=True)


def wait(pred, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


def mm(*args):
    out = subprocess.run([sys.executable, str(DEVSTACK / "dev.py"), *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def transport():
    t = SwitchTransport(dev.api_url(), state_dir=ROOT / "company/state",
                        registration_token_file=DEVSTACK / "state/registration.token",
                        transcript_path=OUT / "transcript.jsonl", log=lambda *a: log("transport:", *a))
    got = {A: [], B: []}

    def beta(m):
        got[B].append(m)
        if m.sender_is_agent:
            t.post(B, m.room, f"@{m.sender} ack from {B} [{nonce}]", thread=m.thread or m.id)

    t.on_message(A, got[A].append)
    t.on_message(B, beta)
    t.start()
    return t, got


def show(who, m):
    log(f"  {who} <- id={m.id} room={m.room} thread={m.thread} sender={m.sender} "
        f"sender_is_agent={m.sender_is_agent} mentions={m.mentions} text={m.text!r}")


checks = {}
t, got = transport()
log(f"started: {A}, {B}; room {ROOM} = {t.room_id(ROOM)}")

# 1. agent -> agent, threaded reply
root = t.post(A, ROOM, f"@{B} live transport test [{nonce}]: please ack in this thread")
log(f"{A} posted root {root}")
ok = wait(lambda: any(m.thread == root for m in got[A]))
for m in got[B]:
    show(B, m)
for m in got[A]:
    show(A, m)
mb = next((m for m in got[B] if m.id == root), None)
ma = next((m for m in got[A] if m.thread == root), None)
checks["agent->agent addressed delivery"] = bool(mb and mb.sender == A and mb.sender_is_agent)
checks["threaded reply delivered in root's thread"] = bool(ok and ma and ma.sender == B and ma.sender_is_agent)

# 2. a person, top level and inside alpha's thread
human = mm("mm-post", ROOM, f"@{A} approve [{nonce}] (a human, top level)")
log(f"mattermost user posted {human['id']}")
ok1 = wait(lambda: any(not m.sender_is_agent and "top level" in m.text for m in got[A]))
posts = mm("mm-thread", ROOM, "--since", str(human["create_at"] - 60_000))
mm_root = next(p["id"] for p in posts if nonce in p["message"] and "please ack" in p["message"])
mm("mm-post", ROOM, f"@{A} approve [{nonce}] (a human, in the thread)", "--root", mm_root)
ok2 = wait(lambda: any(not m.sender_is_agent and "in the thread" in m.text for m in got[A]))
for m in got[A]:
    if not m.sender_is_agent:
        show(A, m)
hm = [m for m in got[A] if not m.sender_is_agent]
checks["human message flagged human"] = bool(ok1 and ok2 and len(hm) == 2)
checks["human thread reply lands in the root's thread"] = any(m.thread == root for m in hm)
seen = {n: [m.id for m in got[n]] for n in got}
t.stop()

# 3. restart: nothing redelivered; a message sent while down arrives once
down_id = mm("mm-post", ROOM, f"@{B} sent while the transport was down [{nonce}]")["id"]
log(f"transport stopped; mattermost user posted {down_id} to {B} meanwhile; restarting")
t, got = transport()
ok = wait(lambda: any("while the transport was down" in m.text for m in got[B]))
time.sleep(5)
for n in got:
    for m in got[n]:
        show(n, m)
redelivered = [m.id for n in got for m in got[n] if m.id in seen[n]]
checks["restart: no redelivery"] = not redelivered
checks["restart: message sent while down delivered once"] = \
    ok and sum("while the transport was down" in m.text for m in got[B]) == 1
t.stop()

(OUT / "mattermost-thread.json").write_text(json.dumps(
    mm("mm-thread", ROOM, "--since", str(human["create_at"] - 60_000)), indent=1, ensure_ascii=False) + "\n")
for k, v in checks.items():
    log(f"{'PASS' if v else 'FAIL'}  {k}")
(OUT / "live.log").write_text("\n".join(log_lines) + "\n")
sys.exit(0 if all(checks.values()) else 1)
