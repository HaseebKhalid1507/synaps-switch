#!/usr/bin/env python3
"""Live check of company/switch_env.py against the local Switch dev stack (HTTP only).

  1. op registry: every op apply/teardown use, checked against GET /agents/{id}/ops
  2. env1 as generation G1 (template path; G1 = $M4E_GEN, default 91): `python -m company.switch_env apply ...`, then again (idempotent)
  3. what Switch and Mattermost now hold: rooms, agents, users, links, channels + members, the #board documents
  4. update_ledger with one real row (the seeded market settled with no company actions), read back
  5. teardown: the six rooms archived
  6. env2 = env1 + link maintenance<->dev as generation G1 + 1 through the per-room path (--mode rooms), then teardown

Everything is named m4e-*: agents m4e-<env agent>, rooms m4e-<room>-g<k>. Nothing else is touched.
Run from the repo root:  python evidence/m4/apply-env/live_apply_env.py
Outputs are scrubbed (devstack scrubber + the m4e agents' API keys). Prints no secret.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
DEVSTACK = Path(os.environ.get("SWITCH_DEVSTACK", Path.home() / "Projects/synaps-switch/devstack"))
sys.path.insert(0, str(ROOT))

import switch_client as sc  # noqa: E402
from company.core.env import apply_mutation, load_env, save_env  # noqa: E402
from company.core.fitness import fitness  # noqa: E402
from company.market.sim import Market, QuarterLog  # noqa: E402
from company.switch_env import (Ops, check_ops, load_devstack, load_state,  # noqa: E402
                                update_ledger)

PY = sys.executable
STATE = ROOT / "company/state"
PREFIX = "m4e-"
G1 = int(os.environ.get("M4E_GEN", "91"))     # env1 as generation G1, env2 as G1 + 1
G2 = G1 + 1
dev = load_devstack(DEVSTACK)
E = dev.env()
lines = []


def scrub(text):
    text = dev.scrub(text)
    for f in (STATE / "agents").glob("*.json"):
        key = json.loads(f.read_text()).get("api_key")
        if key:
            text = text.replace(key, "[REDACTED]")
    return text.replace(str(ROOT), "<repo>")


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(map(str, a))
    lines.append(line)
    print(scrub(line), flush=True)


def write(name, obj):
    text = obj if isinstance(obj, str) else json.dumps(obj, indent=1, ensure_ascii=False) + "\n"
    (OUT / name).write_text(scrub(text))


def cli(*args, name):
    trace = OUT / f"{name}.trace.jsonl"
    trace.unlink(missing_ok=True)
    cmd = [PY, "-m", "company.switch_env", *args, "--agent-prefix", PREFIX, "--room-prefix", PREFIX,
           "--trace", str(trace)]
    log("$ python -m company.switch_env", " ".join(args), f"--agent-prefix {PREFIX} --room-prefix {PREFIX}")
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    write(f"{name}.log", f"$ python -m company.switch_env {' '.join(args)} --agent-prefix {PREFIX} "
                         f"--room-prefix {PREFIX} --trace {trace.name}\n\n# stderr\n{p.stderr}\n# stdout\n{p.stdout}"
                         f"# exit {p.returncode}\n")
    trace.write_text(scrub(trace.read_text()))
    if p.returncode:
        raise SystemExit(f"{name} failed:\n{scrub(p.stderr)[-2000:]}")
    ops = [json.loads(l)["op"] for l in trace.read_text().splitlines()]
    log(f"  -> {len(ops)} Switch calls: " + ", ".join(f"{o} x{ops.count(o)}" for o in dict.fromkeys(ops)))
    return json.loads(p.stdout)


def operator():
    c = json.loads((STATE / "agents" / f"{PREFIX}board.json").read_text())
    return Ops(sc.Bridge(dev.api_url(E), c["id"], c["api_key"]), f"{PREFIX}board", log=log)


def snapshot(ops, group, name):
    """Switch's and Mattermost's view of one applied generation."""
    st = load_state(STATE, group)
    out = {"group": group, "rooms": []}
    for room, r in st["rooms"].items():
        d = ops.op("get_room_detail", {"room_id": r["id"]})
        links = ops.op("list_linked_rooms", {}, room_id=r["id"])
        cid = d.get("external_channel_id")
        ch = dev.mm(E, "GET", f"/channels/{cid}", who="admin")
        members = sorted(dev.mm(E, "GET", f"/users/{m['user_id']}", who="admin")["username"]
                         for m in dev.mm(E, "GET", f"/channels/{cid}/members?per_page=200", who="admin"))
        out["rooms"].append({
            "env_room": room, "switch_room": d["name"], "room_id": d["id"], "group": d.get("group_name"),
            "archived": d.get("archived"), "agents": d.get("agent_names"), "users": d.get("connected_user_names"),
            "links": [f"-> {x['target_room_name']} ({x['label']}, {x['access']})" for x in links],
            "mattermost": {"channel": ch.get("name"), "display_name": ch.get("display_name"),
                           "type": ch.get("type"), "members": members}})
    board = st["rooms"][st["board_room"]]["id"]
    ctx = ops.connect(board)
    docs = {x["name"]: x["id"] for x in ctx.get("documents") or []}
    loaded = ops.op("load_internal_documents", {"ids": list(docs.values())}, room_id=board)
    out["documents"] = [{"name": n, "id": i} for n, i in docs.items()]
    write(f"{name}.switch-and-mattermost.json", out)
    for n, i in docs.items():
        body = next(x["content"] for x in loaded if x["id"] == i)
        write(f"{name}.{n.lower()}.md", body)
    return out, docs


def table(snap):
    rows = ["| env room | Switch room | agents | users | links | Mattermost channel members |", "|---|---|---|---|---|---|"]
    for r in snap["rooms"]:
        rows.append(f"| {r['env_room']} | {r['switch_room']} | {', '.join(r['agents'])} | {', '.join(r['users'])} | "
                    f"{'<br>'.join(r['links']) or '-'} | {', '.join(r['mattermost']['members'])} |")
    return "\n".join(rows)


def main():
    env1 = load_env(ROOT / "company/envs/env1.yaml")
    ops = operator()
    # 1. op registry
    check = check_ops(ops, log)
    write("ops-check.json", check)

    # 2. env1 as generation 91, twice
    rooms = cli("apply", "company/envs/env1.yaml", "--gen", str(G1), "--stand-in-synaps", name=f"apply-env1-g{G1}")
    log("  rooms:", json.dumps(rooms))
    again = cli("apply", "company/envs/env1.yaml", "--gen", str(G1), "--stand-in-synaps", name=f"reapply-env1-g{G1}")
    assert again == rooms, "re-apply changed the rooms"
    reapply_ops = {json.loads(l)["op"] for l in (OUT / f"reapply-env1-g{G1}.trace.jsonl").read_text().splitlines()}
    assert not reapply_ops & {"create_room_from_yaml", "create_room", "create_room_document", "link_rooms"}

    # 3. what is there
    snap, docs = snapshot(ops, f"{PREFIX}env1-g{G1}", f"env1-g{G1}")
    log("\n" + table(snap))
    for r in snap["rooms"]:
        want = set(load_state(STATE, f"{PREFIX}env1-g{G1}")["rooms"][r["env_room"]]["agents"])
        assert set(r["agents"]) == want, (r["env_room"], r["agents"])
        assert want <= set(r["mattermost"]["members"]) and E["MATTERMOST_USER"] in r["mattermost"]["members"]
    assert set(docs) == {"Handbook", "Ledger"}

    # 4. the Ledger gets a real row: the seeded market settled with no company actions
    weights = __import__("yaml").safe_load((ROOT / "company/fitness.yaml").read_text())["weights"]
    m = Market(7, env1.company).settle(QuarterLog())
    row = {**vars(m), "quarter": "smoke (no actions)", "seed": 7, "fitness": round(fitness(m, weights), 3)}
    update_ledger(env1, ops, [row], generation=G1, room_prefix=PREFIX, state_dir=STATE)
    board = load_state(STATE, f"{PREFIX}env1-g{G1}")["rooms"]["board"]["id"]
    ledger = ops.op("load_internal_documents", {"ids": [docs["Ledger"]]}, room_id=board)[0]["content"]
    write(f"env1-g{G1}.ledger-after-update.md", ledger)
    assert "smoke (no actions)" in ledger
    log("ledger after update_ledger:\n" + ledger)

    # 5. teardown
    cli("teardown", "company/envs/env1.yaml", "--gen", str(G1), name=f"teardown-env1-g{G1}")
    listed = ops.op("list_rooms", {"include_archived": True})
    archived = {r["name"]: r.get("archived") for r in listed if r["name"].endswith(f"-g{G1}")}
    write(f"teardown-env1-g{G1}.list_rooms.json", archived)
    assert len(archived) == 6 and all(archived.values()), archived
    log("archived:", json.dumps(archived))

    # 6. env2 = env1 + one link, through the per-room path
    env2 = apply_mutation(env1, {"op": "link", "rooms": ["maintenance", "dev"]})
    env2.name, env2.parent = "env2", "env1"
    save_env(env2, OUT / "env2-link.yaml")
    rooms2 = cli("apply", str((OUT / "env2-link.yaml").relative_to(ROOT)), "--gen", str(G2), "--mode", "rooms",
                 "--stand-in-synaps", name=f"apply-env2-g{G2}-rooms")
    log("  rooms:", json.dumps(rooms2))
    snap2, _ = snapshot(ops, f"{PREFIX}env2-g{G2}", f"env2-g{G2}")
    log("\n" + table(snap2))
    maint = next(r for r in snap2["rooms"] if r["env_room"] == "maintenance")
    assert any(f"m4e-dev-g{G2} (direct line" in x for x in maint["links"]), maint["links"]
    cli("teardown", str((OUT / "env2-link.yaml").relative_to(ROOT)), "--gen", str(G2), name=f"teardown-env2-g{G2}")
    ops.close()
    write("run.log", "\n".join(lines) + "\n")
    log("PASS")


if __name__ == "__main__":
    main()
