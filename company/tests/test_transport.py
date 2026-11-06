"""company/transport.py: FakeTransport, and SwitchTransport against tests/fake_bridge.py."""
import json
import os
import stat
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import switch_client as sc  # noqa: E402
from fake_bridge import REG_TOKEN, WAIT, FakeBridge  # noqa: E402

from company.transport import (  # noqa: E402
    FakeTransport, Msg, SwitchTransport, Transport, addresses, parse_mentions)


def wait(pred, timeout=WAIT):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ───────────────────────── mentions ─────────────────────────


def test_parse_mentions_follows_switch_rules():
    assert parse_mentions("@ops-maint and @synaps-dev, also @OPS-MAINT") == ["ops-maint", "synaps-dev"]
    assert parse_mentions("ping @*board* please") == ["board"]          # emphasis stripped
    assert parse_mentions("no mentions here") == []
    # a trailing name character makes it a different name, exactly as Switch reads it
    assert parse_mentions("ask @board.") == ["board."]
    assert not addresses(parse_mentions("ask @board."), "board")
    assert addresses(parse_mentions("ask @Board now"), "board")


def test_msg_matches_the_contract():
    m = Msg("m1", "board", None, "Haseeb", False, "@board approve", ["board"])
    assert (m.id, m.room, m.thread, m.sender, m.sender_is_agent, m.text, m.mentions) == \
        ("m1", "board", None, "Haseeb", False, "@board approve", ["board"])
    for t in (FakeTransport, SwitchTransport):
        assert all(callable(getattr(t, n, None)) for n in ("post", "on_message", "start"))
    assert Transport  # the Protocol is importable


# ───────────────────────── FakeTransport ─────────────────────────


def test_fake_two_agents_exchange_mentions_in_a_thread():
    t = FakeTransport(rooms={"maintenance": ["ops-maint", "board", "Haseeb"]})
    got = {"board": [], "ops-maint": []}

    def ops(m):
        got["ops-maint"].append(m)
        t.post("ops-maint", m.room, f"@{m.sender} on it", thread=m.thread or m.id)

    t.on_message("ops-maint", ops)
    t.on_message("board", got["board"].append)
    t.start()
    root = t.post("board", "maintenance", "@ops-maint sev1: checkout 500s")
    [m1] = got["ops-maint"]
    assert (m1.id, m1.room, m1.thread, m1.sender, m1.sender_is_agent, m1.mentions) == \
        (root, "maintenance", None, "board", True, ["ops-maint"])
    [m2] = got["board"]
    assert (m2.thread, m2.sender, m2.sender_is_agent, m2.text) == (root, "ops-maint", True, "@board on it")
    # a reply to a reply stays in the root's thread
    t.post("board", "maintenance", "@ops-maint thanks", thread=m2.id)
    assert got["ops-maint"][-1].thread == root


def test_fake_human_is_flagged_human_and_agents_never_hear_themselves():
    t = FakeTransport(rooms={"board": ["board", "Haseeb"]})
    got = []
    t.on_message("board", got.append)
    t.start()
    t.say("Haseeb", "board", "@board approve")
    t.post("board", "board", "@board note to self")
    t.say("market", "board", "no mention, nobody hears this")
    [m] = got
    assert (m.sender, m.sender_is_agent, m.text) == ("Haseeb", False, "@board approve")
    t.say("synaps-dev", "board", "@board PR #12 is green", is_agent=True)
    assert got[-1].sender_is_agent is True


def test_fake_rooms_membership_and_env_room_list():
    env_rooms = [{"name": "dev", "agents": ["synaps-dev"], "users": ["{$creator}"]},
                 {"name": "board", "agents": ["board"], "users": []}]
    t = FakeTransport(rooms=env_rooms)
    got = []
    t.on_message("board", got.append)
    t.start()
    with pytest.raises(ValueError):
        t.post("board", "dev", "@synaps-dev hi")          # board is not in #dev
    with pytest.raises(KeyError):
        t.say("Haseeb", "nowhere", "@board hi")
    t.say("synaps-dev", "dev", "@board done", is_agent=True)
    assert got == []                                        # board is not a member of #dev


def test_fake_queues_until_start_and_writes_a_jsonl_transcript(tmp_path):
    path = tmp_path / "t.jsonl"
    t = FakeTransport(transcript_path=path)
    got = []
    t.on_message("growth", got.append)
    mid = t.post("marketing", "growth", "@growth spring campaign draft")
    assert got == []
    t.start()
    assert [m.id for m in got] == [mid]
    recs = [json.loads(line) for line in path.read_text().splitlines()]
    assert recs == t.records
    assert recs[0]["kind"] == "post" and recs[0]["to"] == ["growth"] and recs[0]["sender_is_agent"] is True


def test_fake_handler_errors_are_recorded_and_storms_stop():
    t = FakeTransport(max_deliveries=50)
    t.on_message("a", lambda m: 1 / 0)
    t.on_message("b", lambda m: t.post("b", m.room, "@c ping"))
    t.on_message("c", lambda m: t.post("c", m.room, "@b pong"))
    t.start()
    t.post("x", "r", "@a hi")
    assert "ZeroDivisionError" in t.errors[0]["error"]
    t.post("x", "r", "@b go")                                # b and c ping-pong forever
    assert "delivery storm" in t.errors[-1]["error"]
    t.post("x", "r", "@a still delivered after a storm")
    assert len([e for e in t.errors if e["agent"] == "a"]) == 2


# ───────────────────────── SwitchTransport over fake bridges ─────────────────────────


class Fabric:
    """Switch rooms spread over FakeBridges, one per hosted agent (a FakeBridge serves one
    stream). It routes post_message to the stream of every member the body @-addresses."""

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.bridges = {}       # agent name -> FakeBridge
        self.pids = {}          # agent name -> participant id (opaque)
        self.rooms = {}         # room id -> {name, agents, humans}
        self.roots = {}
        self.posts = []         # (agent, args) of every post_message

    def host(self, name):
        fb = FakeBridge().start()
        aid, key = fb.add_agent(name)
        self.bridges[name] = fb
        self.pids[name] = f"@switch-agent-{uuid.uuid4().hex[:8]}:localhost"
        creds = self.state_dir / "agents" / f"{name}.json"
        creds.parent.mkdir(parents=True, exist_ok=True)
        creds.write_text(json.dumps({"name": name, "id": aid, "api_key": key, "base_url": fb.url}))
        fb.ops_handlers.update({
            "list_rooms": lambda a, c: [{"room_id": rid, "name": r["name"], "archived": False}
                                        for rid, r in self.rooms.items() if name in r["agents"]],
            "connect_to_room": lambda a, c: self.context(a["room_id"]),
            "post_message": lambda a, c: self.post_message(name, fb, a, c),
        })
        return fb

    def room(self, name, agents, humans=()):
        rid = str(uuid.uuid4())
        self.rooms[rid] = {"name": name, "agents": list(agents), "humans": list(humans)}
        return rid

    def context(self, rid):
        r = self.rooms[rid]
        return {"room_id": rid, "name": r["name"], "warning": None,
                "participants": [{"id": str(uuid.uuid4()), "name": a, "type": "agent"} for a in r["agents"]]
                + [{"id": str(uuid.uuid4()), "name": h, "type": "user"} for h in r["humans"]]}

    def post_message(self, name, fb, args, conn):
        rid = fb.connection_rooms.get(conn)
        if rid is None:
            raise ValueError("not connected to a room")
        self.posts.append((name, args))
        return {"event_id": self.say(rid, self.pids[name], name, args["body"], args.get("thread_id"))}

    def say(self, rid, sender, sender_name, body, thread=None, role=None, mid=None):
        mid = mid or "sw_" + uuid.uuid4().hex
        root = self.roots.get(thread, thread) if thread else None
        self.roots[mid] = root or mid
        payload = {"addressed": True, "sender": sender, "sender_name": sender_name, "sender_kind": None,
                   "on_behalf_of": None, "message_id": mid, "body": body, "timestamp": int(time.time() * 1000),
                   "thread_id": root, "attachments": [], "sender_role": role}
        for a in self.rooms[rid]["agents"]:
            if a != sender_name and a in self.bridges and addresses(parse_mentions(body), a):
                self.bridges[a].push("message", rid, dict(payload))
        return mid

    def stop(self):
        for fb in self.bridges.values():
            fb.stop()


@pytest.fixture
def fabric(tmp_path):
    f = Fabric(tmp_path / "state")
    yield f
    f.stop()


def switch(fabric, **kw):
    logs = []
    t = SwitchTransport(state_dir=fabric.state_dir, connect_timeout=WAIT,
                        log=lambda *a: logs.append(" ".join(map(str, a))), **kw)
    t.logs = logs
    return t


def test_switch_two_agents_exchange_a_mention_and_a_threaded_reply(fabric):
    fabric.host("board"), fabric.host("ops-maint")
    rid = fabric.room("maintenance", ["board", "ops-maint"], ["Haseeb"])
    t = switch(fabric)
    got = {"board": [], "ops-maint": []}

    def ops(m):
        got["ops-maint"].append(m)
        t.post("ops-maint", m.room, f"@{m.sender} on it", thread=m.thread or m.id)

    t.on_message("ops-maint", ops)
    t.on_message("board", got["board"].append)
    t.start()
    try:
        root = t.post("board", "maintenance", "@ops-maint sev1: checkout 500s")
        assert wait(lambda: got["board"])
        [m1], [m2] = got["ops-maint"], got["board"]
        assert (m1.id, m1.room, m1.thread, m1.sender, m1.sender_is_agent, m1.mentions) == \
            (root, "maintenance", None, "board", True, ["ops-maint"])
        assert (m2.room, m2.thread, m2.sender, m2.sender_is_agent, m2.text) == \
            ("maintenance", root, "ops-maint", True, "@board on it")
        assert fabric.posts == [("board", {"body": "@ops-maint sev1: checkout 500s", "thread_id": None}),
                                ("ops-maint", {"body": "@board on it", "thread_id": root})]
        assert t.room_id("maintenance") == rid and not t.errors
        kinds = [(r["kind"], r.get("to")) for r in t.transcript.records]
        assert kinds.count(("deliver", "ops-maint")) == 1 and kinds.count(("deliver", "board")) == 1
    finally:
        t.stop()


def test_switch_sender_is_agent_matches_ids_never_parses_them(fabric):
    fabric.host("board")
    rid = fabric.room("board", ["board", "synaps-dev"], ["Haseeb"])
    t = switch(fabric)
    got = []
    t.on_message("board", got.append)
    t.start()
    try:
        # a person (Switch reports a workspace role for a claimed person)
        fabric.say(rid, "@switch-mattermost-b1-haseeb:localhost", "Haseeb", "@board approve", role="owner")
        # an agent this process does not host, with an id of no recognisable shape
        dev_pid = "@opaque-7f3a:localhost"
        fabric.say(rid, dev_pid, "synaps-dev", "@board PR #12 green")
        # a person whose id merely looks like an agent's
        fabric.say(rid, "@switch-agent-deadbeef:localhost", "mallory", "@board approve")
        assert wait(lambda: len(got) == 3)
        assert [(m.sender, m.sender_is_agent) for m in got] == \
            [("Haseeb", False), ("synaps-dev", True), ("mallory", False)]
        learned = json.loads((fabric.state_dir / "transport" / "participants.json").read_text())
        assert learned == {"agents": {dev_pid: "synaps-dev"}}
    finally:
        t.stop()
    # a fresh process knows synaps-dev's id without asking the room
    t2 = switch(fabric)
    assert t2.is_agent(None, rid, {"message_id": "x", "sender": dev_pid, "sender_name": "renamed"}) is True


def test_switch_reconnect_and_restart_do_not_redeliver(fabric):
    fb = fabric.host("board")
    rid = fabric.room("board", ["board"], ["Haseeb"])
    human = "@switch-mattermost-b1-haseeb:localhost"
    t = switch(fabric)
    got = []
    t.on_message("board", got.append)
    t.start()
    try:
        m1 = fabric.say(rid, human, "Haseeb", "@board one", role="owner")
        assert wait(lambda: len(got) == 1)
        fb.drop_stream()                                   # socket cut; the connection survives
        assert wait(lambda: len(fb.opens) == 2 and t.links["board"].connected.is_set())
        m2 = fabric.say(rid, human, "Haseeb", "@board two", role="owner")
        assert wait(lambda: len(got) == 2)
        assert fb.opens[1]["last_event_id"] == str(fb.events[0]["sequence"])
        assert fb.opens[1]["query"]["connection_id"] == fb.opens[0]["query"]["connection_id"]
        # the same message id again under a new sequence (a re-sent frame) is dropped
        fabric.say(rid, human, "Haseeb", "@board two", role="owner", mid=m2)
        m3 = fabric.say(rid, human, "Haseeb", "@board three", role="owner")
        assert wait(lambda: len(got) == 3)
        time.sleep(0.3)
        assert [m.id for m in got] == [m1, m2, m3]
    finally:
        t.stop()
    m4 = fabric.say(rid, human, "Haseeb", "@board four, sent while board was down", role="owner")
    t2 = switch(fabric)
    got2 = []
    t2.on_message("board", got2.append)
    t2.start()
    try:
        assert wait(lambda: got2)
        time.sleep(0.3)
        assert [m.id for m in got2] == [m4]                # resumed at the saved cursor
        assert fb.opens[-1]["query"]["connection_id"] == fb.opens[0]["query"]["connection_id"]
        state = json.loads((fabric.state_dir / "transport" / "board.state.json").read_text())
        assert state["cursor"] == fb.events[-1]["sequence"] and m4 in state["delivered"]
    finally:
        t2.stop()


def test_switch_handler_error_does_not_redeliver_or_stop_the_stream(fabric):
    fabric.host("board")
    rid = fabric.room("board", ["board"], ["Haseeb"])
    t = switch(fabric)
    calls = []

    def boom(m):
        calls.append(m.id)
        if len(calls) == 1:
            raise RuntimeError("llm timeout")

    t.on_message("board", boom)
    t.start()
    try:
        fabric.say(rid, "@h:localhost", "Haseeb", "@board one", role="owner")
        fabric.say(rid, "@h:localhost", "Haseeb", "@board two", role="owner")
        assert wait(lambda: len(calls) == 2)
        time.sleep(0.3)
        assert len(calls) == 2 and "llm timeout" in t.errors[0]["error"]
    finally:
        t.stop()


def test_switch_registers_once_and_keeps_credentials_private(tmp_path):
    fb = FakeBridge().start()
    fb.ops_handlers["list_rooms"] = lambda a, c: []
    state = tmp_path / "state"
    logs = []
    try:
        t = SwitchTransport(fb.url, agents=["finance"], state_dir=state, registration_token=REG_TOKEN,
                            connect_timeout=WAIT, log=lambda *a: logs.append(" ".join(map(str, a))))
        t.start()
        t.stop()
        creds_file = state / "agents" / "finance.json"
        creds = json.loads(creds_file.read_text())
        assert stat.S_IMODE(os.stat(creds_file).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(state / "transport" / "finance.state.json").st_mode) == 0o600
        assert creds["id"] in fb.agents and creds["base_url"] == fb.url
        t2 = SwitchTransport(fb.url, agents=["finance"], state_dir=state, registration_token="unused",
                             connect_timeout=WAIT)
        t2.start()
        t2.stop()
        assert [a["name"] for a in fb.agents.values()].count("finance") == 1
        # credentials lost: registering the same name again is refused, never silently overwritten
        creds_file.unlink()
        with pytest.raises(sc.SwitchError) as e:
            SwitchTransport(fb.url, agents=["finance"], state_dir=state, registration_token=REG_TOKEN,
                            connect_timeout=WAIT).start()
        assert e.value.status == 409
        assert not any(creds["api_key"] in line or REG_TOKEN in line for line in logs)
    finally:
        fb.stop()


def test_switch_unverifiable_sender_fails_closed(fabric):
    fb = fabric.host("board")
    rid = fabric.room("board", ["board"], ["Haseeb"])
    t = switch(fabric)
    got = []
    t.on_message("board", got.append)
    t.start()
    try:
        def down(a, c):
            raise ValueError("participants unavailable")
        fb.ops_handlers["connect_to_room"] = down
        fabric.say(rid, "@who:localhost", "Haseeb", "@board approve")       # no role, roster unreadable
        assert wait(lambda: got)
        assert got[0].sender_is_agent is True                                # never taken for a person
        fabric.say(rid, "@h:localhost", "Haseeb", "@board approve", role="owner")
        assert wait(lambda: len(got) == 2) and got[1].sender_is_agent is False
    finally:
        t.stop()


def test_switch_rooms_by_name_unknown_and_pinned(fabric):
    fabric.host("board")
    old = fabric.room("board", ["board"])
    new = fabric.room("board", ["board"])               # a second room under the same name
    t = switch(fabric, agents=["board"])
    t.start()
    try:
        assert t.room_id("board") == old and any("ambiguous" in line for line in t.logs)
        with pytest.raises(KeyError):
            t.post("board", "no-such-room", "hello")
        t.pin_rooms({"board": new})
        assert t.room_id("board") == new and t.room_name(new) == "board"
        t.post("board", "board", "hello")
        assert fabric.bridges["board"].connection_rooms[t.links["board"].conn.connection_id] == new
    finally:
        t.stop()
