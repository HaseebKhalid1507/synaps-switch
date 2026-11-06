#!/usr/bin/env python3
"""Run session_probe.sh with EXACTLY the environment the private daemon gives a room session's tools.

The daemon records, per session, the env its tools run with (`env` in <base>/sessions/<id>.json: the
extension's hello env; bash/grep/find/ls/shell call env_clear() and then set exactly this). The probe runs
with `env -i` + that env, in the session's own clone, so what it shows is what the session's bash sees.

  python3 evidence/m4/github/probe_session_env.py SESSION_ID [--room m26-dev] [--out probe-session-env.txt]
Prints env NAMES only (plus GH_CONFIG_DIR and GIT_SSH_COMMAND, which hold paths, no secret).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "devstack"))
import dev  # noqa: E402

BASE = Path("~/.synaps-switch-dev").expanduser()

ap = argparse.ArgumentParser()
ap.add_argument("session")
ap.add_argument("--room", default="m26-dev")
ap.add_argument("--out", default=str(HERE / "probe-session-env.txt"))
a = ap.parse_args()

s = json.loads((BASE / "sessions" / f"{a.session}.json").read_text())
env = {k: v for k, v in (s.get("env") or [])}
st = json.loads((BASE / "synaps-switch" / "agents" / "synaps-dev" / "state.json").read_text())
room = next(r for r in st["rooms"].values() if r.get("room_name") == a.room and r.get("session_id") == a.session)
cwd = room["cwd"]
r = subprocess.run(["env", "-i", *[f"{k}={v}" for k, v in env.items()], "bash", str(HERE / "session_probe.sh")],
                   cwd=cwd, capture_output=True, text=True, timeout=120)
text = (f"# session {a.session} ({s.get('name')}), room {a.room}, cwd {cwd}\n"
        f"# env the daemon gives this session's tools (names): {' '.join(sorted(env))}\n"
        f"# SSH_AUTH_SOCK in it: {'SSH_AUTH_SOCK' in env}\n\n" + r.stdout + r.stderr)
Path(a.out).write_text(dev.scrub(text))
print(dev.scrub(text))
