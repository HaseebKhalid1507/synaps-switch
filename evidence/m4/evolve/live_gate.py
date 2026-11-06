#!/usr/bin/env python3
"""Live: the evolution gate on Switch. A real env_diff goes to a #board room, an agent's "approve" is ignored,
the process dies while paused at the gate, a new process resumes from the checkpoint, and a person's
Mattermost reply decides.

Run from the repo root, with the company venv:
  export COMPANY_SYNAPS_BASE_DIR=~/.synaps-switch-dev/company-llm
  export COMPANY_BROKER_TOKEN_FILE=~/.synaps-switch-dev/broker/machine.token     # path only; never printed
  export COMPANY_BROKER_URL=http://127.0.0.1:18191
  python evidence/m4/evolve/live_gate.py all          # setup, phase1, phase2, export (each phase its own process)

  setup   register m4v-board + m4v-peer (registration token read in place), room m4v-board on Mattermost with
          both agents, the seeded Mattermost user joined
  phase1  evolve gen 1 on env1: stub quarter (seeds 1, 2), the board persona proposes on Haiku
          (company.llm), the gate posts env_diff + rationale in #m4v-board as m4v-board. m4v-peer (an agent)
          replies "@m4v-board approve" in the thread: ignored, the run pauses again. Then the process exits
          while paused (SqliteSaver checkpoint, thread evolve:env1:1-2:m4v).
  phase2  a new process resumes the same thread at the gate (nothing re-posted). `user` replies
          "@m4v-board approve" in Mattermost (devstack dev.py mm-post --as user) → trial on the same seeds →
          keep/revert → lineage → the outcome in the thread.
  export  the Mattermost channel, SwitchTransport transcript, lineage table; all scrubbed (devstack dev.py scrub)

Private state (credentials 0600, checkpoint): ~/.synaps-switch-dev/m4v/. Nothing else on the stack is touched.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
DEVSTACK = Path(os.environ.get("SS_DEVSTACK", "~/Projects/synaps-switch/devstack")).expanduser()
sys.path.insert(0, str(DEVSTACK))
import dev  # noqa: E402  (the main checkout's devstack: its .env + state, read in place)

OUT = Path(__file__).resolve().parent
PRIV = Path("~/.synaps-switch-dev/m4v").expanduser()
TOKEN = DEVSTACK / "state" / "registration.token"
BOARD, PEER, ROOM, HUMAN = "m4v-board", "m4v-peer", "m4v-board", "user"
THREAD = "evolve:env1:1-2:m4v"
CP = PRIV / "evolve.sqlite"
STATE = OUT / "live-state.json"           # non-secret: room id, t0, the gate thread
LOG = OUT / "live.log"


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(map(str, a))
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(dev.scrub(line) + "\n")


def state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(**kw):
    STATE.write_text(json.dumps({**state(), **kw}, indent=1) + "\n")


def mm(*args):
    out = subprocess.run([sys.executable, str(DEVSTACK / "dev.py"), *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def transport(agents, transcript=True):
    from company.transport import SwitchTransport
    t = SwitchTransport(dev.api_url(), agents=agents, state_dir=PRIV / "transport",
                        registration_token_file=str(TOKEN),
                        transcript_path=(PRIV / "transcript.jsonl") if transcript else None,
                        log=lambda *x: log("transport:", *x))
    return t


def setup():
    PRIV.mkdir(parents=True, exist_ok=True)
    t = transport([BOARD, PEER], transcript=False)
    t.start()
    link = t._link(BOARD)
    rooms = link.op("list_rooms", {})
    rooms = rooms if isinstance(rooms, list) else rooms.get("rooms", [])
    room = next((r for r in rooms if r.get("name") == ROOM and not r.get("archived")), None)
    if room is None:
        bl = link.op("list_bridges", {})
        bl = bl if isinstance(bl, list) else bl.get("bridges", [])
        bridge_id = next(x["id"] for x in bl if x["type"] == "mattermost")
        room = link.op("create_room", {"name": ROOM, "description": "m4v: evolve gate live check (#11)",
                                       "agent_names": [BOARD, PEER], "bridge_id": bridge_id,
                                       "channel_type": "channel_public"})
        log(f"created room {ROOM}")
    rid = room.get("id") or room.get("room_id")
    cid = link.op("get_room_detail", {"room_id": rid}).get("external_channel_id")
    e = dev.env()
    for uname in (e["MATTERMOST_USER"], BOARD, PEER):
        u = dev.mm(e, "GET", f"/users/username/{uname}", who="admin")
        dev.mm(e, "POST", f"/channels/{cid}/members", {"user_id": u["id"]}, who="admin")
    members = sorted(dev.mm(e, "GET", f"/users/{m['user_id']}", who="admin")["username"]
                     for m in dev.mm(e, "GET", f"/channels/{cid}/members?per_page=100", who="admin"))
    save_state(room_id=rid, channel=dev.mm(e, "GET", f"/channels/{cid}", who="admin")["name"], members=members,
               t0_ms=int(time.time() * 1000))
    log(f"room {ROOM}: Mattermost members {members}")
    t.stop()


def env_vars_ok():
    need = ("COMPANY_SYNAPS_BASE_DIR", "COMPANY_BROKER_TOKEN_FILE", "COMPANY_BROKER_URL")
    miss = [k for k in need if not os.environ.get(k)]
    if miss:
        sys.exit(f"set {', '.join(miss)} (see the docstring)")


class LiveGate:
    """TransportGate plus the scene: phase1 has m4v-peer approve then 'dies'; phase2 has the human answer."""

    def __init__(self, inner, phase, peer_t=None):
        self.inner, self.phase, self.peer_t = inner, phase, peer_t
        self.approvers = inner.approvers
        self.waits = 0
        self.asked = []

    def ask(self, payload):
        tid = self.inner.ask(payload)
        self.asked.append(tid)
        save_state(gate_thread=tid, gate_payload={k: payload[k] for k in ("gen", "parent", "candidate", "change")})
        log(f"gate: posted gen {payload['gen']} proposal ({payload['change']}) in #{ROOM}, thread {tid}")
        return tid

    def wait(self, thread, deadline):
        self.waits += 1
        if self.phase == "phase1":
            if self.waits == 1:
                time.sleep(2)
                mid = self.peer_t.post(PEER, ROOM, f"@{BOARD} approve", thread=thread)
                log(f"{PEER} (an agent) posted '@{BOARD} approve' in the gate thread: {mid}")
            elif self.waits == 2:
                from company.board.evolve import Paused
                raise Paused("phase1: exiting while paused at the gate")
        elif self.phase == "phase2" and self.waits == 1:
            time.sleep(2)
            st = state()
            posts = mm("mm-thread", st["channel"], "--since", str(st["t0_ms"]))
            root = [p for p in posts if not p["root_id"] and "Generation 1: the board proposes" in p["message"]]
            log(f"Mattermost: {len(root)} gate post(s) in ~{st['channel']} since setup")
            r = mm("mm-post", st["channel"], f"@{BOARD} approve", "--root", root[-1]["id"], "--as", HUMAN)
            log(f"{HUMAN} (a person) replied '@{BOARD} approve' in Mattermost: post {r['id']}")
            save_state(mm_gate_root=root[-1]["id"], mm_gate_posts=len(root), mm_human_post=r["id"])
        msg = self.inner.wait(thread, min(deadline, time.time() + 180))
        if msg is None:                       # keep the run paused rather than let the scene time it out
            from company.board.evolve import Paused
            raise Paused(f"{self.phase}: no reply reached {BOARD} in 180 s")
        log(f"gate: got {msg['sender']!r} sender_is_agent={msg['sender_is_agent']} text={msg['text']!r}")
        return msg

    def announce(self, thread, text):
        self.inner.announce(thread, text)
        log(f"gate: outcome posted in the thread: {text}")


def run_phase(phase):
    env_vars_ok()
    from company.board.evolve import LLMProposer, Paused, TransportGate, evolve, switch_room_reader
    from company.board.stub_quarter import run_quarter
    from company.depts.agent import load_personas
    st = state()
    t = transport([BOARD])
    inner = TransportGate(t, board=BOARD, room=ROOM, approvers=(HUMAN,), log=log)
    peer_t = None
    t.start()
    t.pin_rooms({ROOM: st["room_id"]})
    if phase == "phase1":
        peer_t = transport([PEER], transcript=False)
        peer_t.start()
        peer_t.pin_rooms({ROOM: st["room_id"]})
    gate = LiveGate(inner, phase, peer_t)
    roster = load_personas()
    prop = LLMProposer(roster=roster)
    cost = {"usd": 0.0}

    def counted(system, prompt):
        from company.llm import complete_json
        obj, c = complete_json(system, prompt)
        cost["usd"] += c
        log(f"Haiku proposal call: ${c:.4f}")
        return obj, c

    prop.llm = counted
    try:
        res = evolve(ROOT / "company/envs/env1.yaml", generations=1, seeds=(1, 2), run_quarter=run_quarter,
                     propose=prop, gate=gate, lineage_path=OUT / "lineage.jsonl", envs_dir=OUT / "envs",
                     budget_usd=25.0, approvers=(HUMAN,), gate_timeout_s=900, checkpoint=CP, thread_id=THREAD,
                     read_rooms=switch_room_reader(t, BOARD, {"board": st["room_id"]}), roster=roster,
                     log=lambda *x: log("evolve:", *x))
        log(f"{phase}: done: best {res['best']} ({res['best_fitness']}), stop: {res['stop_reason']}")
        save_state(**{f"{phase}_llm_usd": cost["usd"]}, result={k: res[k] for k in ("best", "best_fitness",
                                                                                      "stop_reason")})
        code = 0
    except Paused as e:
        log(f"{phase}: {e}")
        save_state(**{f"{phase}_llm_usd": cost["usd"]})
        code = 3
    finally:
        t.stop()
        if peer_t:
            peer_t.stop()
    return code


def checkpoint_view():
    """The paused/finished thread as the checkpoint has it (no secrets in there)."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    from company.board.evolve import build_graph
    conn = sqlite3.connect(str(CP), check_same_thread=False)
    try:
        g = build_graph(run_quarter=None, propose=None, gate=None, weights={}, approvers=(), lineage_path=OUT / "x",
                        envs_dir=OUT / "x").compile(checkpointer=SqliteSaver(conn))
        snap = g.get_state({"configurable": {"thread_id": THREAD}})
        v = snap.values or {}
        return {"next": list(snap.next), "gen": v.get("gen"), "gate": v.get("gate"),
                "interrupts": [i.value.get("kind") for i in snap.interrupts]}
    finally:
        conn.close()


def export():
    st = state()
    posts = mm("mm-thread", st["channel"], "--since", str(st["t0_ms"]))
    (OUT / "mattermost-m4v-board.json").write_text(dev.scrub(json.dumps(posts, indent=1, ensure_ascii=False)) + "\n")
    tr = PRIV / "transcript.jsonl"
    if tr.exists():
        (OUT / "transcript.jsonl").write_text(dev.scrub(tr.read_text()))
    from company.lineage_report import render
    from company.core.lineage import read_lineage
    (OUT / "lineage.md").write_text(render(read_lineage(OUT / "lineage.jsonl"), run=THREAD))
    for p in [OUT / "lineage.jsonl", STATE, LOG]:
        if p.exists():
            p.write_text(dev.scrub(p.read_text()))
    log(f"export: {len(posts)} Mattermost posts")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd == "setup":
        setup()
    elif cmd in ("phase1", "phase2"):
        sys.exit(run_phase(cmd))
    elif cmd == "view":
        print(json.dumps(checkpoint_view(), indent=1, default=str))
    elif cmd == "export":
        export()
    elif cmd == "all":
        me = [sys.executable, str(Path(__file__).resolve())]
        subprocess.run(me + ["setup"], check=True)
        r1 = subprocess.run(me + ["phase1"])
        log(f"phase1 process exited {r1.returncode} (3 = paused at the gate)")
        v = checkpoint_view()
        log(f"checkpoint after phase1: next={v['next']} gen={v['gen']} interrupts={v['interrupts']} "
            f"ignored={[(i.get('sender'), i.get('why')) for i in (v['gate'] or {}).get('ignored', [])]}")
        r2 = subprocess.run(me + ["phase2"])
        log(f"phase2 process exited {r2.returncode}")
        v = checkpoint_view()
        log(f"checkpoint after phase2: next={v['next']} decision={(v['gate'] or {}).get('decision')}")
        subprocess.run(me + ["export"], check=True)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
