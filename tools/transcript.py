#!/usr/bin/env python3
"""Write scrubbed evidence for a Mattermost channel: its transcript (Mattermost API) and the
synaps-switch extension log lines for its room.

  python3 tools/transcript.py CHANNEL_NAME OUT_DIR [--agent synaps]
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "devstack"))
import dev  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("channel")
p.add_argument("out")
p.add_argument("--agent", default="synaps")
a = p.parse_args()

e = dev.env()
cid = dev.channel_id(e, a.channel, "admin")
posts = sorted(dev.mm(e, "GET", f"/channels/{cid}/posts?per_page=200", who="admin").get("posts", {}).values(),
               key=lambda p: p["create_at"])
users, md = {}, [f"# Mattermost ~{a.channel}", "", "Read back with the Mattermost API.", ""]
for p in posts:
    users.setdefault(p["user_id"], dev.mm(e, "GET", f"/users/{p['user_id']}", who="admin").get("username"))
    when = time.strftime("%H:%M:%S", time.localtime(p["create_at"] / 1000))
    where = f"  ↳ in thread {p['root_id']}" if p.get("root_id") else ""
    md.append(f"- `{when}` **{users[p['user_id']]}** `{p['id']}`{where}\n\n  > " + p["message"].replace("\n", "\n  > "))
state = json.loads((dev.DEV_BASE / "synaps-switch" / "agents" / a.agent / "state.json").read_text())
rooms = [rid for rid, r in state["rooms"].items() if r.get("room_name", "").endswith(a.channel)]
sids = [state["rooms"][r]["session_id"] for r in rooms]
log = (dev.DEV_BASE / "synaps-switch" / "extension.log").read_text(errors="replace").splitlines()
mine = [l for l in log if any(x in l for x in rooms + sids)]
out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
(out / "mattermost-transcript.md").write_text(dev.scrub("\n".join(md) + "\n"))
(out / "extension.log").write_text(dev.scrub("\n".join(mine) + "\n"))
print(f"{len(posts)} posts, {len(mine)} log lines -> {out}")
