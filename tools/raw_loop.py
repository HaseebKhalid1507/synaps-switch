#!/usr/bin/env python3
"""The core loop, raw: register, make a room, stream, beat, reply in Mattermost.

No Synaps involved. Proves the protocol path end to end before the extension
wraps it:

  python3 tools/raw_loop.py setup              register agent `synaps-raw`, create room `synaps-raw`
  python3 tools/raw_loop.py serve [--secs N]   hold the connection; echo-reply to addressed messages
  python3 tools/raw_loop.py prove              setup + serve in the background + post as the
                                               Mattermost user + wait for the threaded reply

Credentials live in devstack/state/raw-agent.json (0600) and are never printed.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "devstack"))

import switch_client as sc  # noqa: E402
import dev  # noqa: E402

STATE = ROOT / "devstack" / "state"
CREDS = STATE / "raw-agent.json"
NAME = "synaps-raw"
ROOM = "raw-loop"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def save(path, obj):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, indent=1)


def bridge_url():
    return dev.api_url(dev.env())


def setup(_a=None):
    e = dev.env()
    url = dev.api_url(e)
    st = json.loads(CREDS.read_text()) if CREDS.exists() else {}
    if not st.get("id"):
        token = dev.TOKEN.read_text().strip()
        reg = sc.register(url, token, NAME, "synaps-switch raw protocol probe (echo)", display_name="Synaps Raw")
        st = {"id": reg["id"], "api_key": reg["api_key"]}
        save(CREDS, st)
        log(f"registered {NAME} id={st['id']}")
    b = sc.Bridge(url, st["id"], st["api_key"])
    if not st.get("room_id"):
        bridges = b.op("list_bridges", {})
        blist = bridges if isinstance(bridges, list) else bridges.get("bridges", [])
        bridge_id = next(x["id"] for x in blist if x["type"] == "mattermost")
        room = b.op("create_room", {"name": ROOM, "description": "raw protocol probe",
                                    "agent_names": [NAME], "bridge_id": bridge_id,
                                    "channel_type": "channel_public"})
        st["room_id"] = room["id"]
        save(CREDS, st)
        log(f"created room {ROOM} id={room['id']}")
    for _ in range(30):
        try:
            dev.channel_id(e, ROOM, "admin")
            break
        except SystemExit:
            time.sleep(1)
    cid = dev.channel_id(e, ROOM, "admin")
    me = dev.mm(e, "GET", f"/users/username/{e['MATTERMOST_USER']}", who="admin")
    dev.mm(e, "POST", f"/channels/{cid}/members", {"user_id": me["id"]}, who="admin")
    members = {dev.mm(e, "GET", f"/users/{m['user_id']}", who="admin")["username"]
               for m in dev.mm(e, "GET", f"/channels/{cid}/members?per_page=100", who="admin")}
    if NAME not in members:
        sys.exit(f"the agent's Mattermost bot is not in ~{ROOM}: {sorted(members)}")
    log(f"mattermost channel ~{ROOM} ready, members {sorted(members)}")
    return st


def serve(a):
    st = json.loads(CREDS.read_text())
    b = sc.Bridge(bridge_url(), st["id"], st["api_key"])
    conn_holder = {}
    seen = set()

    def on_event(ev):
        p = ev.get("payload") or {}
        log(f"event {ev.get('type')} seq={ev.get('sequence')} room={ev.get('room_id')} "
            f"from={p.get('sender_name')} addressed={p.get('addressed')} thread={p.get('thread_id')} "
            f"body={str(p.get('body'))[:80]!r}")
        if ev.get("type") != "message" or not p.get("addressed") or p.get("message_id") in seen:
            return
        seen.add(p.get("message_id"))
        c = conn_holder["c"]
        b.op("connect_to_room", {"room_id": ev["room_id"], "include_general_instructions": False}, c.connection_id)
        thread = p.get("thread_id") or p.get("message_id")
        out = b.op("post_message", {"body": f"raw loop heard {p.get('sender_name')}: {str(p.get('body'))[:120]}",
                                    "thread_id": thread}, c.connection_id)
        log(f"replied in thread {thread}: {out}")

    def on_control(f):
        keep = {k: f.get(k) for k in ("type", "rooms", "cursor", "generation", "heartbeat_interval_seconds",
                                      "code", "reason", "protocol") if k in f}
        log("control", json.dumps(keep))

    c = sc.Connection(b, st.get("connection_id"), scope="all", filter="addressed",
                      cursor=st.get("cursor"), on_event=on_event, on_control=on_control, log=log)
    conn_holder["c"] = c
    st["connection_id"] = c.connection_id
    save(CREDS, st)
    c.start()
    deadline = time.monotonic() + (a.secs if a and a.secs else 1e9)
    try:
        while time.monotonic() < deadline and not c.stopped.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    log(f"stopping: opens={c.opens} beats_ok={c.beats_ok} cursor={c.cursor} generation={c.generation}")
    st["cursor"] = c.cursor
    save(CREDS, st)
    c.stop()


def prove(_a):
    setup()
    t = threading.Thread(target=serve, args=(argparse.Namespace(secs=60),), daemon=True)
    t.start()
    time.sleep(4)
    e = dev.env()
    cid = dev.channel_id(e, ROOM)
    post = dev.mm(e, "POST", "/posts", {"channel_id": cid, "message": f"@{NAME} ping from the raw loop proof"})
    log(f"posted as {e['MATTERMOST_USER']}: {post['id']}")
    for _ in range(60):
        thread = dev.mm(e, "GET", f"/posts/{post['id']}/thread")
        replies = [p for p in thread["posts"].values() if p["id"] != post["id"]]
        if replies:
            for r in replies:
                u = dev.mm(e, "GET", f"/users/{r['user_id']}")
                log(f"REPLY in thread {r.get('root_id')} by @{u['username']}: {r['message']!r}")
            break
        time.sleep(1)
    else:
        log("no reply within 60 s")
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(fn=setup)
    s = sub.add_parser("serve")
    s.add_argument("--secs", type=float)
    s.set_defaults(fn=serve)
    sub.add_parser("prove").set_defaults(fn=prove)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
