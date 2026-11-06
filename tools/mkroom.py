#!/usr/bin/env python3
"""Create a Mattermost-bridged Switch room for a synaps-switch agent and add the seeded
Mattermost user to its channel. Uses the agent's own credentials from the state dir.

  python3 tools/mkroom.py ROOM_NAME [--agent synaps] [--state ~/.synaps-switch-dev/synaps-switch] [--creds FILE]
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
p.add_argument("--with", dest="also", action="append", default=[], metavar="AGENT",
               help="another registered agent to put in the room (repeatable)")
p.add_argument("--creds", help="the agent's credentials.json, when it is not an extension agent (e.g. a library agent)")
p.add_argument("--state", default=os.path.expanduser("~/.synaps-switch-dev/synaps-switch"))
p.add_argument("--description", default="synaps-switch dev room")
a = p.parse_args()

creds = json.loads(Path(a.creds or Path(a.state) / "agents" / a.agent / "credentials.json").read_text())
e = dev.env()
b = sc.Bridge(dev.api_url(e), creds["id"], creds["api_key"])


def find_room():
    rooms = b.op("list_rooms", {})
    rooms = rooms if isinstance(rooms, list) else rooms.get("rooms", [])
    return next((r for r in rooms if r.get("name") == a.room and not r.get("archived")), None)


room = find_room()
if room is None:
    bl = b.op("list_bridges", {})
    bl = bl if isinstance(bl, list) else bl.get("bridges", [])
    bridge_id = next(x["id"] for x in bl if x["type"] == "mattermost")
    # Switch calls Mattermost with a 10 s timeout; on a loaded machine that can fail and drop its
    # Mattermost client for a moment (HTTP 500). Retry a few times; the room may exist after all.
    for attempt in range(1, 6):
        try:
            room = b.op("create_room", {"name": a.room, "description": a.description, "agent_names": [a.agent, *a.also],
                                        "bridge_id": bridge_id, "channel_type": "channel_public"})
            print(f"created room {a.room}: {room.get('id') or room.get('room_id')}", file=sys.stderr)
            break
        except sc.SwitchError as ex:
            transient = ex.status == 500 or "not running" in str(ex.detail)
            if not transient or attempt == 5:
                raise
            print(f"create_room attempt {attempt}: HTTP {ex.status}, retrying", file=sys.stderr)
            time.sleep(10 * attempt)
            room = find_room()
            if room is not None:
                break
rid = room.get("id") or room.get("room_id")
# The room's own channel, by id: a name lookup can find a channel left by an earlier failed create.
cid = b.op("get_room_detail", {"room_id": rid}).get("external_channel_id")
if not cid:
    sys.exit(f"room {rid} has no Mattermost channel")
# The seeded user, and the agent's bot: Switch adds the bot when it creates the channel, but
# when its Mattermost adapter is restarting (slow DB under load) that add is skipped with a warning.
for uname in (e["MATTERMOST_USER"], a.agent, *a.also):
    u = dev.mm(e, "GET", f"/users/username/{uname}", who="admin")
    dev.mm(e, "POST", f"/channels/{cid}/members", {"user_id": u["id"]}, who="admin")
members = sorted(dev.mm(e, "GET", f"/users/{m['user_id']}", who="admin")["username"]
                 for m in dev.mm(e, "GET", f"/channels/{cid}/members?per_page=100", who="admin"))
ch = dev.mm(e, "GET", f"/channels/{cid}", who="admin")
print(json.dumps({"room_id": rid, "channel": ch.get("name"), "channel_id": cid, "members": members}))
if a.agent not in members:
    sys.exit(f"the agent's bot is not in ~{a.room}")
