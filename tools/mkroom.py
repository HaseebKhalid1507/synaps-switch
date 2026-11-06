#!/usr/bin/env python3
"""Create a Mattermost-bridged Switch room for a synaps-switch agent and add the seeded
Mattermost user to its channel. Uses the agent's own credentials from the state dir.

  python3 tools/mkroom.py ROOM_NAME [--agent synaps] [--state ~/.synaps-switch-dev/synaps-switch]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "devstack"))
import switch_client as sc  # noqa: E402
import dev  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("room")
p.add_argument("--agent", default="synaps")
p.add_argument("--state", default=os.path.expanduser("~/.synaps-switch-dev/synaps-switch"))
p.add_argument("--description", default="synaps-switch dev room")
a = p.parse_args()

creds = json.loads((Path(a.state) / "agents" / a.agent / "credentials.json").read_text())
e = dev.env()
b = sc.Bridge(dev.api_url(e), creds["id"], creds["api_key"])
rooms = b.op("list_rooms", {})
rooms = rooms if isinstance(rooms, list) else rooms.get("rooms", [])
room = next((r for r in rooms if r.get("name") == a.room and not r.get("archived")), None)
if room is None:
    bl = b.op("list_bridges", {})
    bl = bl if isinstance(bl, list) else bl.get("bridges", [])
    bridge_id = next(x["id"] for x in bl if x["type"] == "mattermost")
    room = b.op("create_room", {"name": a.room, "description": a.description, "agent_names": [a.agent],
                                "bridge_id": bridge_id, "channel_type": "channel_public"})
    print(f"created room {a.room}: {room.get('id')}")
rid = room.get("id") or room.get("room_id")
for _ in range(30):
    try:
        cid = dev.channel_id(e, a.room, "admin")
        break
    except SystemExit:
        time.sleep(1)
me = dev.mm(e, "GET", f"/users/username/{e['MATTERMOST_USER']}", who="admin")
dev.mm(e, "POST", f"/channels/{cid}/members", {"user_id": me["id"]}, who="admin")
members = sorted(dev.mm(e, "GET", f"/users/{m['user_id']}", who="admin")["username"]
                 for m in dev.mm(e, "GET", f"/channels/{cid}/members?per_page=100", who="admin"))
print(json.dumps({"room_id": rid, "channel": a.room, "members": members}))
if a.agent not in members:
    sys.exit(f"the agent's bot is not in ~{a.room}")
