"""A fake Switch agent bridge for tests (stdlib http.server, loopback, no network).

Speaks just enough of the agent protocol: POST /agents (register), the event
stream, the heartbeat, POST /ops/{name}, POST /message. Tests drive it:

    fb = FakeBridge().start()
    fb.push("message", room, {...})       # buffered + written to the open stream
    fb.drop_stream()                      # cut the socket (connection survives)
    fb.evict("taken_over")                # evicted frame, then close
    fb.ops_handlers["read_context"] = lambda args, conn: {...}
"""
import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

REG_TOKEN = "reg-token-for-tests"


class FakeBridge:
    def __init__(self):
        self.agents = {}            # id -> {name, api_key}
        self.events = []            # buffered domain events (dicts with sequence)
        self.seq = 100
        self.opens = []             # {query, last_event_id, generation}
        self.beats = []
        self.ops_calls = []         # (name, args, connection_id)
        self.messages = []          # direct POST /message
        self.ops_handlers = {}
        self.generation = 0
        self.stream_q = None        # queue of bytes for the live stream, None = no stream
        self.lock = threading.Lock()
        self.beat_status = None     # force a beat status (int) or None = normal
        self.beat_body = None
        self.open_status = None     # force an open refusal: (status, body)
        self.connection_rooms = {}  # connection id -> current room (connect_to_room)
        self.keepalive_s = 15
        self.covered_rooms = []
        self.state_delay = 0.0      # seconds between creating the connection and sending connection_state
        self.server = None

    # ── control ──
    def start(self):
        fb = self

        class H(Handler):
            bridge = fb

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.drop_stream()
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def add_agent(self, name="synaps-test"):
        aid, key = str(uuid.uuid4()), "ak-" + uuid.uuid4().hex
        self.agents[aid] = {"name": name, "api_key": key}
        return aid, key

    def frame(self, data, event=None, sid=None):
        out = ""
        if sid is not None:
            out += f"id: {sid}\n"
        out += f"event: {event or data.get('type', 'message')}\n"
        out += "data: " + json.dumps(data) + "\n\n"
        return out.encode()

    def push(self, kind, room_id, payload):
        with self.lock:
            self.seq += 1
            ev = {"type": kind, "room_id": room_id, "bridge_id": "b1", "channel_type": "channel_public",
                  "payload": payload, "sequence": self.seq}
            self.events.append(ev)
            q = self.stream_q
        if q is not None:
            q.put(self.frame(ev, kind, ev["sequence"]))
        return ev

    def control(self, data):
        if data.get("type") == "subscription_changed":
            self.covered_rooms = list(data.get("rooms") or [])
        q = self.stream_q
        if q is not None:
            q.put(self.frame(data))

    def drop_stream(self):
        with self.lock:
            q, self.stream_q = self.stream_q, None
        if q is not None:
            q.put(None)

    def evict(self, code, reason=None):
        q = self.stream_q
        if q is not None:
            data = {"type": "evicted", "reason": reason or code}
            if code:
                data["code"] = code
            q.put(self.frame(data))
        self.drop_stream()

    def wait(self, pred, timeout=5.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return True
            time.sleep(0.02)
        return False


class Handler(BaseHTTPRequestHandler):
    bridge: FakeBridge
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _agent(self, aid):
        a = self.bridge.agents.get(aid)
        if a is None or self.headers.get("Authorization") != f"Bearer {a['api_key']}":
            self._json(401, {"detail": "invalid API key"})
            return None
        return a

    def do_GET(self):
        u = urlsplit(self.path)
        parts = u.path.strip("/").split("/")
        if u.path == "/health":
            return self._json(200, {"status": "ok"})
        if len(parts) == 3 and parts[0] == "agents" and parts[2] == "events":
            if self._agent(parts[1]) is None:
                return
            return self._stream(parts[1], parse_qs(u.query))
        if len(parts) == 3 and parts[0] == "agents" and parts[2] == "ops":
            if self._agent(parts[1]) is None:
                return
            return self._json(200, {"operations": {"post_message": {"description": "post", "input_schema": {}}}})
        self._json(404, {"detail": "not found"})

    def _stream(self, aid, q):
        fb = self.bridge
        if fb.open_status:
            status, body = fb.open_status
            return self._json(status, body)
        lei = self.headers.get("Last-Event-ID")
        cursor = int(lei) if lei else (fb.seq if q.get("start_from", ["head"])[0] == "head"
                                         else int(q["start_from"][0]))
        old = None
        with fb.lock:
            fb.generation += 1
            gen = fb.generation
            old, fb.stream_q = fb.stream_q, queue.Queue()
            sq = fb.stream_q
            fb.opens.append({"query": {k: v[0] for k, v in q.items()}, "last_event_id": lei, "generation": gen})
            backlog = [e for e in fb.events if e["sequence"] > cursor]
        if old is not None:
            old.put(fb.frame({"type": "evicted", "code": "taken_over", "reason": "another stream attached"}))
            old.put(None)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        rooms = sorted({e["room_id"] for e in fb.events}) or []
        state = {"type": "connection_state", "connection_id": q["connection_id"][0], "agent_id": aid,
                 "scope": q.get("scope", ["single"])[0], "filter": q.get("filter", ["all"])[0],
                 "rooms": rooms, "cursor": cursor, "protocol": 2, "generation": gen,
                 "heartbeat_interval_seconds": 0.2}
        try:
            self.wfile.flush()
            if fb.state_delay:
                time.sleep(fb.state_delay)
            self.wfile.write(fb.frame(state))
            for e in backlog:
                self.wfile.write(fb.frame(e, e["type"], e["sequence"]))
            self.wfile.flush()
            while True:
                try:
                    item = sq.get(timeout=fb.keepalive_s)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                self.wfile.write(item)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with fb.lock:
                if fb.stream_q is sq:
                    fb.stream_q = None
        self.close_connection = True

    def do_POST(self):
        fb = self.bridge
        u = urlsplit(self.path)
        parts = u.path.strip("/").split("/")
        body = self._body()
        if parts == ["agents"]:
            if self.headers.get("Authorization") != f"Bearer {REG_TOKEN}":
                return self._json(401, {"detail": "invalid registration token"})
            for aid, a in fb.agents.items():
                if a["name"] == body["name"] and not body.get("overwrite"):
                    return self._json(409, {"detail": f"agent {body['name']} exists"})
            aid, key = fb.add_agent(body["name"])
            fb.agents[aid]["registration"] = body
            return self._json(200, {"id": aid, "api_key": key})
        if len(parts) >= 3 and parts[0] == "agents":
            if self._agent(parts[1]) is None:
                return
            rest = parts[2:]
            if rest == ["connection", "beat"]:
                fb.beats.append(body)
                if fb.beat_status:
                    return self._json(fb.beat_status, fb.beat_body or {"detail": "forced"})
                if fb.stream_q is None:
                    return self._json(409, {"detail": {"code": "no_stream", "message": "no stream attached"}})
                if body.get("generation") not in (None, fb.generation):
                    return self._json(409, {"detail": {"code": "taken_over", "message": "superseded"}})
                return self._json(200, {"ok": True, "rooms": fb.covered_rooms, "cursor": body.get("cursor")})
            if rest == ["message"]:
                fb.messages.append(body)
                return self._json(200, {"ok": True, "event_id": "$" + uuid.uuid4().hex})
            if len(rest) == 2 and rest[0] == "ops":
                name, conn = rest[1], self.headers.get("X-Switch-Connection-Id")
                fb.ops_calls.append((name, body, conn))
                if name == "connect_to_room":
                    fb.connection_rooms[conn] = body["room_id"]
                h = fb.ops_handlers.get(name)
                if h is None:
                    if name == "connect_to_room":
                        return self._json(200, {"result": {"room_id": body["room_id"], "name": "room",
                                                           "instructions": "be nice", "participants": [],
                                                           "warning": None}})
                    if name == "post_message":
                        if conn not in fb.connection_rooms:
                            return self._json(400, {"detail": "not connected to a room"})
                        return self._json(200, {"result": {"event_id": "$" + uuid.uuid4().hex}})
                    return self._json(404, {"detail": f"unknown operation {name}"})
                try:
                    return self._json(200, {"result": h(body, conn)})
                except ValueError as e:
                    return self._json(400, {"detail": str(e)})
        self._json(404, {"detail": "not found"})
