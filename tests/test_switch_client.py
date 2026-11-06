import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import switch_client as sc  # noqa: E402
from fake_bridge import REG_TOKEN, WAIT, FakeBridge  # noqa: E402

ROOM = "11111111-2222-3333-4444-555555555555"


def msg(mid, body="hi", addressed=True, thread=None):
    return {"addressed": addressed, "sender": "@switch-user-a1:localhost", "sender_name": "user",
            "message_id": mid, "body": body, "timestamp": int(time.time() * 1000),
            "thread_id": thread, "attachments": []}


class SSEParserTest(unittest.TestCase):
    def test_basic_and_split_chunks(self):
        p = sc.SSEParser()
        raw = b'id: 7\nevent: message\ndata: {"a":1}\n\n'
        out = []
        for i in range(len(raw)):
            out += p.feed(raw[i:i + 1])
        self.assertEqual(out, [{"event": "message", "data": '{"a":1}', "id": "7"}])
        self.assertEqual(p.last_id, "7")

    def test_crlf_cr_multiline_comment_nospace_bom(self):
        p = sc.SSEParser()
        out = p.feed(b"\xef\xbb\xbf: hello\r\ndata:one\r\ndata: two\r\r\n: keepalive\n\nevent: x\ndata\n\n")
        self.assertEqual(out[0], {"event": "message", "data": "one\ntwo", "id": None})
        self.assertEqual(out[1], {"event": "x", "data": "", "id": None})
        self.assertEqual(p.keepalives, 2)

    def test_lone_cr_split_across_chunks(self):
        p = sc.SSEParser()
        self.assertEqual(p.feed(b"data: a\r"), [])
        self.assertEqual(p.feed(b"\n\r\n"), [{"event": "message", "data": "a", "id": None}])

    def test_event_without_data_is_dropped(self):
        p = sc.SSEParser()
        self.assertEqual(p.feed(b"event: ping\n\n"), [])

    def test_eviction_code_fallback(self):
        self.assertEqual(sc.eviction_code({"code": "x"}), "x")
        self.assertEqual(sc.eviction_code({"reason": "heartbeat lapsed (6s)"}), sc.HEARTBEAT_LAPSED)
        self.assertEqual(sc.eviction_code({"reason": "another stream attached to c1"}), sc.TAKEN_OVER)
        self.assertEqual(sc.eviction_code({"reason": "closed by admin"}), sc.CLOSED)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.fb = FakeBridge().start()

    def tearDown(self):
        self.fb.stop()

    def test_register(self):
        out = sc.register(self.fb.url, REG_TOKEN, "synaps-a", "desc")
        self.assertIn(out["id"], self.fb.agents)
        reg = self.fb.agents[out["id"]]["registration"]
        self.assertEqual(reg["integration_profile"]["connection_model"], "always_on")
        self.assertEqual(reg["connector_type"], "custom")
        with self.assertRaises(sc.SwitchError) as cm:
            sc.register(self.fb.url, REG_TOKEN, "synaps-a", "desc")
        self.assertEqual(cm.exception.status, 409)
        with self.assertRaises(sc.SwitchError) as cm:
            sc.register(self.fb.url, "wrong", "synaps-b", "desc")
        self.assertEqual(cm.exception.status, 401)

    def test_ops(self):
        aid, key = self.fb.add_agent()
        b = sc.Bridge(self.fb.url, aid, key)
        self.assertTrue(b.health())
        self.assertIn("post_message", b.ops())
        with self.assertRaises(sc.SwitchError) as cm:
            b.op("post_message", {"body": "x"}, "c1")
        self.assertEqual(cm.exception.status, 400)
        b.op("connect_to_room", {"room_id": ROOM}, "c1")
        r = b.op("post_message", {"body": "x", "thread_id": None}, "c1")
        self.assertTrue(r["event_id"].startswith("$"))
        name, args, conn = self.fb.ops_calls[-1]
        self.assertEqual((name, args, conn), ("post_message", {"body": "x", "thread_id": None}, "c1"))
        with self.assertRaises(sc.SwitchError) as cm:
            b.op("nope", {})
        self.assertEqual(cm.exception.status, 404)
        with self.assertRaises(ValueError):
            b.op("../x", {})

    def test_repr_hides_key(self):
        b = sc.Bridge(self.fb.url, "a1", "secret-key-value")
        self.assertNotIn("secret-key-value", repr(b))


class ConnectionTest(unittest.TestCase):
    def setUp(self):
        self.fb = FakeBridge().start()
        aid, key = self.fb.add_agent()
        self.bridge = sc.Bridge(self.fb.url, aid, key)
        self.got = []
        self.controls = []
        self.cursors = []
        self.fatal = []

    def tearDown(self):
        if getattr(self, "conn", None):
            self.conn.stop()
        self.fb.stop()

    def connect(self, **kw):
        self.conn = sc.Connection(
            self.bridge, "conn-1", scope="all", filter="addressed",
            on_event=self.got.append, on_control=self.controls.append,
            on_cursor=lambda c, g: self.cursors.append((c, g)), on_fatal=self.fatal.append, **kw).start()
        self.assertTrue(self.conn.attached.wait(WAIT))
        return self.conn

    def test_open_params_state_and_beats(self):
        c = self.connect(rooms=[ROOM])
        n = len(self.fb.beats)  # beats sent while opening carry no cursor yet
        q = self.fb.opens[0]["query"]
        self.assertEqual(q["connection_id"], "conn-1")
        self.assertEqual((q["scope"], q["filter"], q["start_from"]), ("all", "addressed", "head"))
        self.assertEqual((q["protocol"], q["protocol_accepts"], q["client"]), (str(sc.PROTOCOL), "1", "synaps-switch"))
        self.assertEqual(q["rooms"], ROOM)
        self.assertIsNone(self.fb.opens[0]["last_event_id"])
        self.assertEqual(c.generation, 1)
        self.assertEqual(c.interval(), 0.2)  # the server's interval, not ours
        self.assertTrue(self.fb.wait(lambda: len(self.fb.beats) >= n + 3))
        self.assertEqual(self.fb.beats[-1], {"connection_id": "conn-1", "cursor": 100, "generation": 1})

    def test_events_in_order_and_cursor(self):
        self.connect()
        for i in range(5):
            self.fb.push("message", ROOM, msg(f"m{i}"))
        self.assertTrue(self.fb.wait(lambda: len(self.got) == 5))
        self.assertEqual([e["payload"]["message_id"] for e in self.got], [f"m{i}" for i in range(5)])
        self.assertEqual(self.conn.cursor, 105)
        self.assertEqual(self.cursors[-1], (105, 1))

    def test_reconnect_resumes_from_cursor_without_duplicates(self):
        self.connect()
        self.fb.push("message", ROOM, msg("a"))
        self.assertTrue(self.fb.wait(lambda: len(self.got) == 1))
        self.fb.drop_stream()
        time.sleep(0.05)
        self.fb.push("message", ROOM, msg("b"))   # buffered while the socket is down
        self.assertTrue(self.fb.wait(lambda: len(self.fb.opens) == 2))
        self.assertTrue(self.fb.wait(lambda: len(self.got) == 2))
        self.assertEqual(self.fb.opens[1]["last_event_id"], "101")
        self.assertEqual(self.fb.opens[1]["query"]["start_from"], "101")
        self.fb.push("message", ROOM, msg("c"))
        self.assertTrue(self.fb.wait(lambda: len(self.got) == 3))
        self.assertEqual([e["payload"]["message_id"] for e in self.got], ["a", "b", "c"])
        self.assertEqual(self.conn.generation, 2)

    def test_beat_refusal_reopens(self):
        self.connect()
        self.fb.beat_status, self.fb.beat_body = 409, {"detail": {"code": "no_stream", "message": "x"}}
        self.assertTrue(self.fb.wait(lambda: len(self.fb.opens) >= 2))
        self.fb.beat_status = None
        self.assertTrue(self.conn.attached.wait(WAIT))
        self.assertEqual(self.fatal, [])

    def test_beat_404_reopens(self):
        self.connect()
        self.fb.beat_status, self.fb.beat_body = 404, {"detail": "unknown connection"}
        self.assertTrue(self.fb.wait(lambda: len(self.fb.opens) >= 2))
        self.fb.beat_status = None
        self.assertEqual(self.fatal, [])

    def test_beat_taken_over_is_terminal(self):
        self.connect()
        self.fb.beat_status, self.fb.beat_body = 409, {"detail": {"code": "taken_over", "message": "x"}}
        self.assertTrue(self.fb.wait(lambda: bool(self.fatal)))
        self.assertEqual(self.fatal[0].code, sc.TAKEN_OVER)
        n = len(self.fb.opens)
        time.sleep(1.2)
        self.assertEqual(len(self.fb.opens), n)

    def test_evicted_taken_over_is_terminal(self):
        self.connect()
        self.fb.evict("taken_over")
        self.assertTrue(self.fb.wait(lambda: bool(self.fatal)))
        self.assertEqual(self.fatal[0].code, sc.TAKEN_OVER)
        time.sleep(1.2)
        self.assertEqual(len(self.fb.opens), 1)

    def test_evicted_heartbeat_lapsed_reopens(self):
        self.connect()
        self.fb.evict(None, "heartbeat lapsed")
        self.assertTrue(self.fb.wait(lambda: len(self.fb.opens) == 2))
        self.assertTrue(any(c.get("code") == sc.HEARTBEAT_LAPSED for c in self.controls))
        self.assertEqual(self.fatal, [])

    def test_gap_and_subscription_changed_are_surfaced(self):
        self.connect()
        # conn.covered as each frame is surfaced. Read later, it can be a beat reply's older room list.
        covered_at = {}
        self.conn.on_control = lambda f: (covered_at.setdefault(f["type"], list(self.conn.covered)),
                                          self.controls.append(f))
        self.fb.control({"type": "gap", "from_sequence": 5, "resumed_at": 90, "reason": "restart"})
        self.fb.control({"type": "subscription_changed", "rooms": [ROOM], "reason": "joined"})
        self.assertTrue(self.fb.wait(lambda: len(self.controls) >= 3))
        kinds = [c["type"] for c in self.controls]
        self.assertEqual(kinds[:3], ["connection_state", "gap", "subscription_changed"])
        self.assertEqual(covered_at["subscription_changed"], [ROOM])
        self.assertEqual(self.conn.cursor, 90)  # resumed where the server says

    def test_unauthorized_open_is_terminal(self):
        self.fb.open_status = (401, {"detail": "invalid API key"})
        c = sc.Connection(self.bridge, "conn-x", on_fatal=self.fatal.append).start()
        self.conn = c
        self.assertTrue(self.fb.wait(lambda: bool(self.fatal)))
        self.assertEqual(self.fatal[0].code, sc.CREDENTIALS_REJECTED)

    def test_handler_crash_does_not_advance_cursor(self):
        boom = {"n": 0}

        def on_event(e):
            if boom["n"] == 0:
                boom["n"] += 1
                raise RuntimeError("handler crashed")
            self.got.append(e)

        self.conn = sc.Connection(self.bridge, "conn-2", on_event=on_event).start()
        self.assertTrue(self.conn.attached.wait(WAIT))
        self.fb.push("message", ROOM, msg("x"))
        # The stream loop reopens from the old cursor and the event is re-delivered.
        self.assertTrue(self.fb.wait(lambda: len(self.got) == 1))
        self.assertEqual(self.got[0]["payload"]["message_id"], "x")
        self.assertEqual(self.fb.opens[-1]["last_event_id"], "100")

    def test_beats_start_before_connection_state(self):
        # A slow server: the connection exists (and its TTL runs) before connection_state is sent.
        # The fake holds connection_state until a beat has kept the new connection alive.
        self.fb.state_gate = threading.Event()
        self.conn = sc.Connection(self.bridge, "conn-slow", on_fatal=self.fatal.append).start()
        self.assertTrue(self.fb.wait(lambda: self.conn.beats_ok >= 1))
        self.assertFalse(self.conn.attached.is_set())
        self.fb.state_gate.set()
        self.assertTrue(self.conn.attached.wait(WAIT))
        self.assertEqual(len(self.fb.opens), 1)  # early refusals did not cause a reopen
        self.assertEqual(self.fatal, [])

    def test_stop_is_prompt(self):
        c = self.connect()
        t0 = time.monotonic()
        c.stop(join=WAIT)
        # Not prompt = the stream read waits for the next keepalive or its read timeout.
        self.assertLess(time.monotonic() - t0, min(self.fb.keepalive_s, c.read_timeout) / 2)
        self.assertFalse(any(t.is_alive() for t in c._threads))


class AgentTest(unittest.TestCase):
    """The library layer: on_message, auto-reply, loop guards, persistence."""

    def setUp(self):
        import tempfile
        self.fb = FakeBridge().start()
        self.dir = tempfile.mkdtemp(prefix="sc-agent-")
        self.got = []
        self.agents = []

    def tearDown(self):
        for a in self.agents:
            a.stop()
        self.fb.stop()
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def agent(self, handler=None, **kw):
        kw.setdefault("registration_token", REG_TOKEN)
        kw.setdefault("credentials_file", os.path.join(self.dir, "creds.json"))
        kw.setdefault("state_file", os.path.join(self.dir, "state.json"))
        a = sc.Agent(self.fb.url, "dept-bot", on_message=handler or self.got.append, **kw).start()
        self.agents.append(a)
        self.assertTrue(a.wait_connected(WAIT))
        return a

    def ops(self, name):
        return [c for c in self.fb.ops_calls if c[0] == name]

    def test_registers_once_and_keeps_credentials_private(self):
        a = self.agent()
        path = os.path.join(self.dir, "creds.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        a.stop()
        self.agents.clear()
        self.agent()
        self.assertEqual(len(self.fb.agents), 1)  # the second start reused the credentials

    def test_only_addressed_messages_reach_on_message(self):
        self.agent()
        self.fb.push("message", ROOM, msg("m1", addressed=False))
        self.fb.push("message", ROOM, msg("m2"))
        self.assertTrue(self.fb.wait(lambda: len(self.got) >= 1))
        time.sleep(0.3)
        self.assertEqual([m.message_id for m in self.got], ["m2"])
        m = self.got[0]
        self.assertEqual((m.room_id, m.thread, m.sender_name, m.body), (ROOM, "m2", "user", "hi"))

    def test_own_messages_are_ignored(self):
        self.agent()
        own = msg("m1")
        own["sender_name"] = "dept-bot"
        self.fb.push("message", ROOM, own)
        self.fb.push("message", ROOM, msg("m2"))
        self.assertTrue(self.fb.wait(lambda: len(self.got) >= 1))
        time.sleep(0.3)
        self.assertEqual([m.message_id for m in self.got], ["m2"])

    def test_returned_string_is_a_threaded_reply_as_the_room_session(self):
        self.agent(lambda m: f"echo {m.body}")
        self.fb.push("message", ROOM, msg("m1", body="ping", thread="t0"))
        self.assertTrue(self.fb.wait(lambda: self.ops("post_message")))
        name, args, _ = self.ops("post_message")[0]
        self.assertEqual(args, {"body": "echo ping", "thread_id": "t0"})
        self.assertIn(("connect_to_room", "room-" + ROOM), self.fb.op_sessions)
        self.assertIn(("post_message", "room-" + ROOM), self.fb.op_sessions)

    def test_same_message_id_is_handled_once(self):
        self.agent()
        self.fb.push("message", ROOM, msg("m1"))
        self.fb.push("message", ROOM, msg("m1"))
        self.fb.push("message", ROOM, msg("m2"))
        self.assertTrue(self.fb.wait(lambda: len(self.got) >= 2))
        time.sleep(0.3)
        self.assertEqual([m.message_id for m in self.got], ["m1", "m2"])

    def test_handled_ids_survive_a_restart(self):
        a = self.agent()
        self.fb.push("message", ROOM, msg("m1"))
        self.assertTrue(self.fb.wait(lambda: len(self.got) == 1))
        a.stop()
        self.agents.clear()
        st = os.path.join(self.dir, "state.json")
        import json
        with open(st) as f:
            d = json.load(f)
        d.pop("cursor")  # lose the cursor: the server would replay; the id list still says handled
        with open(st, "w") as f:
            json.dump(d, f)
        self.fb.seq = 100  # the fake replays from the start for a cursorless open at 100
        self.agent()
        time.sleep(0.5)
        self.assertEqual([m.message_id for m in self.got], ["m1"])

    def test_handler_error_is_not_redelivered_but_redeliver_is(self):
        calls = []

        def handler(m):
            calls.append(m.message_id)
            if m.message_id == "boom":
                raise ValueError("handler bug")
            if m.message_id == "again" and calls.count("again") == 1:
                raise sc.Redeliver()

        self.agent(handler)
        self.fb.push("message", ROOM, msg("boom"))
        self.fb.push("message", ROOM, msg("again"))
        self.assertTrue(self.fb.wait(lambda: calls.count("again") == 2))
        time.sleep(0.3)
        self.assertEqual(calls.count("boom"), 1)
        self.assertEqual(calls.count("again"), 2)

    def test_loop_guard_caps_one_thread(self):
        self.agent(max_thread_deliveries=3)
        for i in range(6):
            self.fb.push("message", ROOM, msg(f"m{i}", thread="t1"))
        self.fb.push("message", ROOM, msg("other", thread="t2"))
        self.assertTrue(self.fb.wait(lambda: any(m.message_id == "other" for m in self.got)))
        self.assertEqual([m.message_id for m in self.got], ["m0", "m1", "m2", "other"])

    def test_gap_rides_on_the_next_message(self):
        self.agent()
        self.fb.control({"type": "gap", "from_sequence": 1, "resumed_at": 100, "reason": "restart"})
        self.fb.push("message", ROOM, msg("m1"))
        self.fb.push("message", ROOM, msg("m2"))
        self.assertTrue(self.fb.wait(lambda: len(self.got) >= 2))
        self.assertEqual(self.got[0].gap["reason"], "restart")
        self.assertIsNone(self.got[1].gap)

    def test_targets_use_send_targeted_message(self):
        a = self.agent()
        a.post(ROOM, "over to you", thread_id="t9", targets=["@synaps-dev"])
        name, args, _ = self.ops("send_targeted_message")[0]
        self.assertEqual(args, {"body": "over to you", "target_names": ["synaps-dev"], "thread_id": "t9"})


if __name__ == "__main__":
    unittest.main()
