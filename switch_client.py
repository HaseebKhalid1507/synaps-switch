"""Switch agent-protocol client (stdlib only).

The contract is Switch's agent protocol (docs/official/internals/agent-protocol.md,
switch-v0.31.0): one SSE stream down, HTTP up, a heartbeat every
`heartbeat_interval_seconds`, operations at POST /agents/{id}/ops/{name}.

    bridge = Bridge(base_url, agent_id, api_key)
    conn = Connection(bridge, connection_id, on_event=..., scope="all", filter="addressed")
    conn.start()            # stream + beat threads; reconnects with the cursor
    bridge.op("post_message", {...}, connection_id)

Nothing here prints or logs a credential.
"""
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CLIENT_NAME = "synaps-switch"
CLIENT_VERSION = "0.1.0"
# Agent-protocol revision this client declares. Revision 1: no incarnation fence,
# so a beat may go out before `connection_state` arrives. That matters on a slow
# server: the connection's 6 s TTL starts when the server creates it, and under
# load `connection_state` can take longer than that to reach us. We still send
# `generation` once we know it, and read `code` on refusals and evictions
# (falling back to the prose a revision-1 server sends).
PROTOCOL = 1
PROTOCOL_ACCEPTS = 1

DOMAIN_KINDS = {"message", "command", "room_join", "task_delegate", "task_accept",
                "task_update", "task_finalise", "task_cancel"}
CONTROL_KINDS = {"connection_state", "gap", "evicted", "subscription_changed", "room_released"}

TAKEN_OVER = "taken_over"
HEARTBEAT_LAPSED = "heartbeat_lapsed"
CLOSED = "closed"
CREDENTIALS_REJECTED = "credentials_rejected"

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SwitchError(Exception):
    def __init__(self, status, detail, what=""):
        self.status = status
        self.detail = detail
        self.code = refusal_code(detail)
        super().__init__(f"{what} -> HTTP {status}: {short_detail(detail)}".strip())


def short_detail(detail, n=300):
    s = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


def refusal_code(detail):
    """`code` off a structured refusal (`{"detail": {"code": ...}}`), else None."""
    if isinstance(detail, dict):
        d = detail.get("detail", detail)
        if isinstance(d, dict) and isinstance(d.get("code"), str) and d["code"]:
            return d["code"]
    return None


def eviction_code(data):
    """Read the code off an `evicted` frame; a revision-1 server sends only prose."""
    code = data.get("code")
    if isinstance(code, str) and code:
        return code
    reason = str(data.get("reason") or "")
    if reason.startswith("heartbeat lapsed"):
        return HEARTBEAT_LAPSED
    if reason.startswith("another stream attached"):
        return TAKEN_OVER
    return CLOSED


# ───────────────────────── HTTP ─────────────────────────


def http_json(method, url, body=None, headers=None, timeout=10.0, opener=None):
    """(status, parsed body). Status 0 = unreachable. Never raises on HTTP errors."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", f"{CLIENT_NAME}/{CLIENT_VERSION}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with (opener or _OPENER).open(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
        e.close()
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        return 0, {"error": f"unreachable: {type(e).__name__}: {e}"}
    if not raw:
        return status, {}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"raw": raw[:500].decode("utf-8", "replace")}


def register(base_url, registration_token, name, description, display_name=None,
             connection_model="always_on", overwrite=False, timeout=15.0):
    """POST /agents with the registration token. Returns {id, api_key}; the key is shown once."""
    body = {
        "name": name,
        "description": description,
        "connector_type": "custom",
        "overwrite": bool(overwrite),
        "integration_profile": {
            "connection_model": connection_model,
            "message_exchange": True,
            "pre_invocation_mediation": [],
            "post_invocation_mediation": [],
            "event_reporting": [],
        },
        "metadata": {"client": CLIENT_NAME, "client_version": CLIENT_VERSION},
    }
    if display_name:
        body["display_name"] = display_name
    status, out = http_json("POST", base_url.rstrip("/") + "/agents", body,
                            {"Authorization": f"Bearer {registration_token}"}, timeout)
    if status != 200 or not out.get("id") or not out.get("api_key"):
        raise SwitchError(status, out, "register")
    return {"id": out["id"], "api_key": out["api_key"]}


class Bridge:
    """The agent bridge, as one agent (its id and API key)."""

    def __init__(self, base_url, agent_id, api_key, timeout=60.0):
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self._api_key = api_key
        self.timeout = timeout

    def __repr__(self):
        return f"Bridge({self.base_url!r}, agent={self.agent_id!r})"

    def auth(self):
        return {"Authorization": f"Bearer {self._api_key}"}

    def url(self, path):
        return f"{self.base_url}/agents/{self.agent_id}{path}"

    def call(self, method, path, body=None, headers=None, timeout=None):
        return http_json(method, self.url(path), body, {**self.auth(), **(headers or {})},
                         timeout or self.timeout)

    def health(self):
        return http_json("GET", self.base_url + "/health", timeout=5)[0] == 200

    def ops(self):
        status, out = self.call("GET", "/ops")
        if status != 200:
            raise SwitchError(status, out, "GET /ops")
        return out.get("operations") or {}

    def op(self, name, args=None, connection_id=None, timeout=None, session_id=None, host_id=CLIENT_NAME):
        """POST /ops/{name}; the arguments object itself, unwrapped. Returns `result`.

        connection_id names the open connection the call travels; session_id (with it)
        names which of the agent's sessions on that connection is calling, so several
        sessions in different rooms can share one connection.
        """
        if not name or "/" in name or "?" in name:
            raise ValueError(f"bad operation name {name!r}")
        headers = {"X-Switch-Connection-Id": connection_id} if connection_id else {}
        if session_id:
            if not connection_id:
                raise ValueError("a session selector needs the connection id")
            headers.update({"X-Switch-Session-Id": session_id, "X-Switch-Session-Host-Id": host_id,
                            "X-Switch-Session-Epoch": "1"})
        status, out = self.call("POST", f"/ops/{urllib.parse.quote(name)}", args or {}, headers, timeout)
        if status != 200:
            raise SwitchError(status, out, f"op {name}")
        return out.get("result")

    def beat(self, connection_id, cursor=None, generation=None):
        body = {"connection_id": connection_id}
        if cursor is not None:
            body["cursor"] = cursor
        if generation is not None:
            body["generation"] = generation
        return self.call("POST", "/connection/beat", body, timeout=5.0)

    def send_message(self, room_id, content):
        """POST /agents/{id}/message: needs no connection, cannot thread."""
        status, out = self.call("POST", "/message", {"room_id": room_id, "content": content})
        if status != 200:
            raise SwitchError(status, out, "POST /message")
        return out


# ───────────────────────── SSE ─────────────────────────


class SSEParser:
    """Incremental text/event-stream parser (WHATWG rules that matter here).

    feed(bytes) -> list of {"event", "data", "id"}; comments count as keepalives.
    Handles CRLF/CR/LF, split chunks, multi-line data, a field with no colon,
    the single optional space after the colon, and a UTF-8 BOM.
    """

    def __init__(self):
        self._buf = b""
        self._data = []
        self._event = ""
        self._id = None
        self.last_id = None
        self.keepalives = 0
        self._started = False

    def feed(self, chunk):
        self._buf += chunk
        out = []
        while True:
            i = self._eol()
            if i is None:
                break
            line, rest = self._buf[:i], self._buf[i:]
            if rest.startswith(b"\r\n"):
                rest = rest[2:]
            elif rest[:1] == b"\r" and len(rest) == 1:
                break  # a lone CR at the end of the chunk may be half a CRLF
            else:
                rest = rest[1:]
            self._buf = rest
            ev = self._line(line.decode("utf-8", "replace"))
            if ev is not None:
                out.append(ev)
        return out

    def _eol(self):
        n, r = self._buf.find(b"\n"), self._buf.find(b"\r")
        hits = [x for x in (n, r) if x >= 0]
        return min(hits) if hits else None

    def _line(self, line):
        if not self._started:
            self._started = True
            if line.startswith("\ufeff"):
                line = line[1:]
        if line == "":
            if not self._data:
                self._event = ""
                return None
            ev = {"event": self._event or "message", "data": "\n".join(self._data), "id": self._id}
            if self._id is not None:
                self.last_id = self._id
            self._data, self._event, self._id = [], "", None
            return ev
        if line.startswith(":"):
            self.keepalives += 1
            return None
        field, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if field == "data":
            self._data.append(value)
        elif field == "event":
            self._event = value
        elif field == "id" and "\0" not in value:
            self._id = value
        return None


# ───────────────────────── connection ─────────────────────────


class Fatal(Exception):
    """The connection must not be reopened (taken over, credentials rejected)."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


class Connection:
    """One agent connection: the event stream plus its heartbeat.

    on_event(frame) is called on the stream thread for every domain event, in
    order; the cursor moves past an event only after on_event returns, so a
    crash re-delivers it (callers dedupe by message id). on_control(frame) gets
    control frames (connection_state, gap, subscription_changed, room_released,
    evicted). on_cursor(cursor, generation) is called whenever they change, for
    persistence. on_fatal(exc) when the connection stops for good.
    """

    def __init__(self, bridge, connection_id=None, *, scope="all", filter="addressed", rooms=(),
                 cursor=None, on_event=None, on_control=None, on_cursor=None, on_fatal=None,
                 log=None, read_timeout=45.0, beat_interval=None, spawn_capable=False):
        self.bridge = bridge
        self.connection_id = connection_id or str(uuid.uuid4())
        self.scope = scope
        self.filter = filter
        self.rooms = list(rooms)
        self.cursor = cursor
        self.generation = None
        self.state = None  # last connection_state payload
        self.covered = []
        self.on_event = on_event or (lambda f: None)
        self.on_control = on_control or (lambda f: None)
        self.on_cursor = on_cursor or (lambda c, g: None)
        self.on_fatal = on_fatal or (lambda e: None)
        self.log = log or (lambda *a: None)
        self.read_timeout = read_timeout
        self.beat_interval = beat_interval
        self.spawn_capable = spawn_capable
        self.attached = threading.Event()   # a stream is open and confirmed (connection_state seen)
        self.opening = threading.Event()    # an open is in flight: beat early, ignore refusals
        self.stopped = threading.Event()
        self.fatal = None
        self.opens = 0
        self.beats_ok = 0
        self._resp = None
        self._conn = None
        self._sock = None
        self._lock = threading.Lock()
        self._threads = []

    # lifecycle
    def start(self):
        for target, name in ((self._stream_loop, "switch-stream"), (self._beat_loop, "switch-beat")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self, join=2.0):
        self.stopped.set()
        self._drop()
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(join)

    def _drop(self):
        """Close the current socket so the stream loop reopens (or exits)."""
        with self._lock:
            conn, sock, self._conn, self._sock, self._resp = self._conn, self._sock, None, None, None
        self.attached.clear()
        self.opening.clear()
        if sock is not None:
            try:
                sock.shutdown(2)  # wakes a read blocked on the stream
            except OSError:
                pass
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass

    def _halt(self, exc):
        if self.fatal is None:
            self.fatal = exc
            self.log("connection stopped for good:", exc)
            self.stopped.set()
            self._drop()
            try:
                self.on_fatal(exc)
            except Exception as e:  # noqa: BLE001
                self.log("on_fatal raised", repr(e))

    # stream
    def open_url(self):
        q = {"connection_id": self.connection_id, "scope": self.scope, "filter": self.filter,
             "start_from": "head" if self.cursor is None else str(self.cursor),
             "protocol": str(PROTOCOL), "protocol_accepts": str(PROTOCOL_ACCEPTS),
             "client": CLIENT_NAME, "client_version": CLIENT_VERSION}
        if self.rooms:
            q["rooms"] = ",".join(self.rooms)
        if self.spawn_capable:
            q["spawn_capable"] = "true"
        return f"/agents/{self.bridge.agent_id}/events?" + urllib.parse.urlencode(q)

    def _stream_loop(self):
        backoff = 1.0
        while not self.stopped.is_set():
            t0 = time.monotonic()
            try:
                self._stream_once()
            except Fatal as e:
                self._halt(e)
                return
            except Exception as e:  # noqa: BLE001 — any socket/HTTP trouble: reopen
                if not self.stopped.is_set():
                    self.log("stream:", type(e).__name__, str(e)[:200])
                    retry = getattr(e, "retry_after", None)
                    if retry is not None:
                        self.stopped.wait(retry)
                        continue
            finally:
                self._drop()
            if self.stopped.is_set():
                return
            if time.monotonic() - t0 > 30:
                backoff = 1.0   # it was a healthy stream: reopen at once-ish
            self.stopped.wait(backoff)
            backoff = min(backoff * 2, 30.0)

    def _stream_once(self):
        u = urllib.parse.urlsplit(self.bridge.base_url)
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        conn = cls(u.hostname, u.port, timeout=10)
        path = (u.path.rstrip("/") or "") + self.open_url()
        headers = {**self.bridge.auth(), "Accept": "text/event-stream", "Cache-Control": "no-cache",
                   "User-Agent": f"{CLIENT_NAME}/{CLIENT_VERSION}"}
        if self.cursor is not None:
            headers["Last-Event-ID"] = str(self.cursor)
        conn.connect()
        with self._lock:
            # http.client drops conn.sock once a will-close response owns it: keep our own handle.
            self._conn, self._sock = conn, conn.sock
        if self.stopped.is_set():
            return
        self.opening.set()
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        if resp.status != 200:
            raw = resp.read(4096)
            try:
                detail = json.loads(raw)
            except ValueError:
                detail = raw.decode("utf-8", "replace")
            err = SwitchError(resp.status, detail, "open stream")
            if resp.status in (401, 403):
                raise Fatal(CREDENTIALS_REJECTED if resp.status == 401 else "forbidden", str(err))
            if resp.status == 409 and err.code == TAKEN_OVER:
                raise Fatal(TAKEN_OVER, str(err))
            if resp.status == 503 and isinstance(detail, dict):
                d = detail.get("detail") if isinstance(detail.get("detail"), dict) else {}
                if isinstance(d.get("retry_after_s"), (int, float)):
                    err.retry_after = float(d["retry_after_s"])
            raise err
        if self._sock is not None:
            self._sock.settimeout(self.read_timeout)  # keepalive comments every 15 s
        with self._lock:
            self._resp = resp
        self.opens += 1
        parser = SSEParser()
        while not self.stopped.is_set():
            try:
                chunk = resp.read1(65536)
            except (AttributeError, ValueError) as e:  # the socket was dropped under us
                raise ConnectionError(f"stream dropped ({type(e).__name__})")
            if not chunk:
                raise ConnectionError("stream closed by the server")
            for ev in parser.feed(chunk):
                self._frame(ev)

    def _frame(self, ev):
        try:
            data = json.loads(ev["data"]) if ev["data"] else {}
        except ValueError:
            self.log("unparseable frame dropped:", ev["event"], ev["data"][:120])
            return
        if not isinstance(data, dict):
            return
        kind = data.get("type") or ev["event"]
        data.setdefault("type", kind)
        if kind == "connection_state":
            self.state = data
            self.covered = list(data.get("rooms") or [])
            gen = data.get("generation", data.get("incarnation"))
            if isinstance(gen, int):
                self.generation = gen
            iv = data.get("heartbeat_interval_seconds")
            if isinstance(iv, (int, float)) and iv > 0 and self.beat_interval is None:
                self._server_interval = float(iv)
            cur = data.get("cursor")
            if isinstance(cur, int):
                self.cursor = cur
            self.attached.set()
            self.opening.clear()
            self.on_cursor(self.cursor, self.generation)
            self.on_control(data)
            return
        if kind == "evicted":
            code = eviction_code(data)
            self.on_control({**data, "code": code})
            if code == TAKEN_OVER:
                raise Fatal(TAKEN_OVER, str(data.get("reason") or "another stream attached"))
            raise ConnectionError(f"evicted ({code})")
        if kind in DOMAIN_KINDS or "sequence" in data:
            seq = data.get("sequence")
            if seq is None and ev.get("id") is not None:
                try:
                    seq = int(ev["id"])
                except ValueError:
                    seq = None
            if isinstance(seq, int) and self.cursor is not None and seq <= self.cursor:
                return  # already past it (a replay overlap)
            self.on_event(data)
            if isinstance(seq, int):
                self.cursor = seq
                self.on_cursor(self.cursor, self.generation)
            return
        if kind == "subscription_changed":
            self.covered = list(data.get("rooms") or [])
        if kind == "gap":
            # The server cannot serve our position (restart, overflow): read from where it resumed.
            # The gap is surfaced to the caller, which re-reads history before answering.
            r = data.get("resumed_at")
            if isinstance(r, int):
                self.cursor = r
                self.on_cursor(self.cursor, self.generation)
        self.on_control(data)

    # heartbeat
    _server_interval = 2.0

    def interval(self):
        return self.beat_interval or self._server_interval

    def _beat_loop(self):
        while not self.stopped.is_set():
            if not (self.attached.is_set() or self.opening.is_set()):
                self.stopped.wait(0.1)
                continue
            t0 = time.monotonic()
            confirmed = self.attached.is_set()
            status, out = self.bridge.beat(self.connection_id, self.cursor, self.generation)
            if self.stopped.is_set():
                return
            if not confirmed and status != 200:
                # Still opening: the server may not have created the connection yet. Try again soon.
                self.stopped.wait(0.5)
                continue
            if status == 200:
                self.beats_ok += 1
                if isinstance(out.get("rooms"), list):
                    self.covered = list(out["rooms"])
            elif status in (404, 409) and not self.attached.is_set():
                pass  # a reopen started while this beat was in flight: its answer is about the old stream
            elif status in (404, 409):
                code = refusal_code(out)
                if code == TAKEN_OVER:
                    self._halt(Fatal(TAKEN_OVER, "the beat says another client holds this connection"))
                    return
                # unknown / dead / no stream / unfenced: reopen, never back off
                self.log(f"beat refused ({status} {code or short_detail(out, 120)}): reopening")
                self._drop()
            elif status == 401:
                self._halt(Fatal(CREDENTIALS_REJECTED, "the beat was refused: API key rejected"))
                return
            elif status:
                self.log(f"beat -> {status} {short_detail(out, 120)}")
            self.stopped.wait(max(0.05, self.interval() - (time.monotonic() - t0)))
