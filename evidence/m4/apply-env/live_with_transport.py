#!/usr/bin/env python3
"""Live: apply an env through a running SwitchTransport (what the inner loop does), talk, tear down.

  t = SwitchTransport(...m4e agents...); t.start()
  rooms = apply_env(env1, t._link("m4e-board"), generation=GEN, ...)   # the board's own connection
  t.pin_rooms(rooms)                                                  # env room names -> this generation
  ops-maint posts in "maintenance" to @board; board answers in the thread; both arrive by env room name
  update_ledger(...) through the same link; teardown(...)

Run from the repo root:  python evidence/m4/apply-env/live_with_transport.py   (no model calls)
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
DEVSTACK = Path(os.environ.get("SWITCH_DEVSTACK", Path.home() / "Projects/synaps-switch/devstack"))
sys.path.insert(0, str(ROOT))

from company.core.env import load_env  # noqa: E402
from company.switch_env import apply_env, load_devstack, teardown, update_ledger  # noqa: E402
from company.transport import SwitchTransport  # noqa: E402

P, GEN = "m4e-", int(os.environ.get("M4E_TGEN", "93"))
dev = load_devstack(DEVSTACK)
lines = []


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(map(str, a))
    lines.append(line)
    print(line, flush=True)


def wait(pred, timeout=30):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


def main():
    env = load_env(ROOT / "company/envs/env1.yaml")
    names = {a["name"]: P + a["name"] for a in env.agents}
    state = ROOT / "company/state"
    t = SwitchTransport(dev.api_url(), agents=list(names.values()), state_dir=state,
                        registration_token_file=DEVSTACK / "state/registration.token",
                        transcript_path=OUT / f"transport-g{GEN}.transcript.jsonl", log=lambda *a: log("transport:", *a))
    (OUT / f"transport-g{GEN}.transcript.jsonl").unlink(missing_ok=True)
    got = {"board": [], "ops": []}
    t.on_message(names["board"], got["board"].append)
    t.on_message(names["ops-maint"], got["ops"].append)
    t.start()
    board = t._link(names["board"])
    rooms = apply_env(env, board, generation=GEN, agent_names=names, room_prefix=P, state_dir=state, log=log)
    t.pin_rooms(rooms)
    log("pinned:", json.dumps(rooms))

    root = t.post(names["ops-maint"], "maintenance", f"@{names['board']} sev1: checkout returns 500 (g{GEN} smoke)")
    ok1 = wait(lambda: got["board"])
    m = got["board"][0] if ok1 else None
    if m:
        log(f"board got: room={m.room!r} sender={m.sender!r} agent={m.sender_is_agent} text={m.text!r}")
        t.post(names["board"], m.room, f"@{names['ops-maint']} ack, routing to dev", thread=m.thread or m.id)
    ok2 = wait(lambda: got["ops"])
    r = got["ops"][0] if ok2 else None
    if r:
        log(f"ops-maint got: room={r.room!r} thread={r.thread!r} sender={r.sender!r} text={r.text!r}")
    update_ledger(env, board, [{"quarter": "smoke", "seed": None}], generation=GEN, room_prefix=P, state_dir=state)
    log("update_ledger through the board's transport link: ok")
    done = teardown(env, board, generation=GEN, room_prefix=P, state_dir=state, log=log)
    t.stop()
    checks = {
        "apply through the board's transport link returns all six env rooms": list(rooms) == [x["name"] for x in env.rooms],
        "a post to env room 'maintenance' reaches @board, room reported as 'maintenance'": bool(m and m.room == "maintenance"),
        "sender is ops-maint, flagged as an agent": bool(m and m.sender == names["ops-maint"] and m.sender_is_agent),
        "board's reply lands in the same thread": bool(r and r.room == "maintenance" and r.thread == root),
        "teardown archived the six rooms": len(done) == 6,
    }
    for k, v in checks.items():
        log(("PASS " if v else "FAIL ") + k)
    (OUT / f"transport-g{GEN}.log").write_text(dev.scrub("\n".join(lines) + "\n"))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
