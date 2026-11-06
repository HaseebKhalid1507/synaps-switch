"""Chat transport for the company: many agents, one process.

    t = FakeTransport(rooms={"board": ["board", "Haseeb"], "dev": ["synaps-dev", "ops-maint"]})
    t = SwitchTransport(base_url="http://127.0.0.1:18400", registration_token_file="...")
    t.on_message("board", handler)            # handler(Msg): messages that @-address "board"
    t.start()
    mid = t.post("board", "board", "@synaps-dev take the checkout outage", thread=None)

Both transports follow Switch's addressing rule: a message reaches an agent only when it
names it as `@name` at a token boundary (case-insensitive, `*`/`~` emphasis ignored), and an
agent never receives its own message. Rooms are addressed by name (the env's room names);
SwitchTransport maps names to Switch room ids with `list_rooms` (or an explicit `rooms=` map).

`Msg.sender_is_agent` is what the router trusts to stop agent<->agent loops and to take
approvals only from people, so SwitchTransport decides it by matching the sender's
participant id, never by parsing it (agent-protocol.md, "Participant ids"):

1. the message id is one this transport posted                      -> agent
2. the sender id was already learned as an agent's id               -> agent
3. the payload says `sender_kind` "agent" (or "user"/"platform")    -> agent (or not)
4. the payload carries `sender_role` (a person's workspace role)    -> not an agent
5. the sender's name is an agent in the room's participant list     -> agent, id learned
6. otherwise                                                        -> not an agent
   (if the participant lookup itself fails, the sender counts as an agent: fail closed)

Learned agent ids persist in <state_dir>/transport/participants.json.

switch_client.py on this branch is the low-level client (register, Bridge, Connection);
`_AgentLink` below is the thin per-agent layer on top of it (credentials, cursor, dedupe,
connect-then-post). When switch_client grows its own library layer, swap `_AgentLink` for it.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Protocol

try:
    import switch_client as sc
except ImportError:  # run from somewhere other than the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import switch_client as sc

# ───────────────────────── message + contract ─────────────────────────


@dataclass
class Msg:
    id: str
    room: str
    thread: str | None
    sender: str
    sender_is_agent: bool
    text: str
    mentions: list[str] = field(default_factory=list)


class Transport(Protocol):
    def post(self, as_agent: str, room: str, text: str, thread: str | None = None) -> str: ...
    def on_message(self, agent: str, handler: Callable[[Msg], None]) -> None: ...
    def start(self) -> None: ...


# Switch's mention rule (switch_core/clients/mentions.py): a name is [A-Za-z0-9._-]+ and an
# `@name` only counts when the next character is not a name character.
_NAME_CHAR = r"[A-Za-z0-9._-]"
_MENTION = re.compile(rf"@({_NAME_CHAR}+)")
_EMPHASIS = re.compile(r"[*~]")


def parse_mentions(text: str) -> list[str]:
    """Every `@name` in text, in order, de-duplicated case-insensitively."""
    seen, out = set(), []
    for tok in _MENTION.findall(_EMPHASIS.sub("", text or "")):
        if tok.lower() not in seen:
            seen.add(tok.lower())
            out.append(tok)
    return out


def addresses(msg_mentions: Iterable[str], name: str) -> bool:
    return name.lower() in {m.lower() for m in msg_mentions}


class _Transcript:
    """In-memory list of records, mirrored to a jsonl file when a path is given."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.records: list[dict] = []
        self._lock = threading.Lock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, rec: dict):
        rec = {"ts": round(time.time(), 3), **rec}
        with self._lock:
            self.records.append(rec)
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _call(handler, msg, errors, log, who):
    try:
        handler(msg)
    except Exception as e:  # noqa: BLE001 - one bad handler must not stop delivery
        errors.append({"agent": who, "msg": msg.id, "error": f"{type(e).__name__}: {e}",
                       "traceback": traceback.format_exc()})
        log(f"handler for {who} failed on {msg.id}: {type(e).__name__}: {e}")


# ───────────────────────── FakeTransport ─────────────────────────


class FakeTransport:
    """In-memory rooms for tests and offline runs. Deterministic and single-threaded.

    rooms: {name: [members]} or the env's room list ([{name, agents, users}]). With rooms,
    posting needs membership and only members are addressed; without, any room goes.
    Delivery starts at start(); a handler that posts queues its message, and the queue is
    drained in the posting thread, breadth-first, so replies never recurse.
    """

    def __init__(self, rooms=None, transcript_path=None, *, max_deliveries=10_000, log=None):
        self.rooms = _room_members(rooms)
        self.transcript = _Transcript(transcript_path)
        self.handlers: dict[str, list[Callable[[Msg], None]]] = {}
        self.errors: list[dict] = []
        self.max_deliveries = max_deliveries
        self.log = log or (lambda *a: None)
        self.started = False
        self._n = 0
        self._roots: dict[str, str] = {}
        self._queue: deque = deque()
        self._lock = threading.RLock()
        self._draining = False

    @property
    def records(self):
        return self.transcript.records

    def on_message(self, agent, handler):
        with self._lock:
            self.handlers.setdefault(agent, []).append(handler)

    def start(self):
        self.started = True
        self._drain()

    def stop(self):
        self.started = False

    def post(self, as_agent, room, text, thread=None):
        if self.rooms is not None and as_agent not in self.rooms.get(room, ()):
            raise ValueError(f"{as_agent} is not a member of room {room!r}")
        return self._send(as_agent, True, room, text, thread)

    def say(self, sender, room, text, thread=None, *, is_agent=False):
        """A message from outside the company's agents: a person, the market, synaps-dev."""
        if self.rooms is not None and room not in self.rooms:
            raise KeyError(f"unknown room {room!r}")
        return self._send(sender, is_agent, room, text, thread)

    def _send(self, sender, is_agent, room, text, thread):
        with self._lock:
            self._n += 1
            mid = f"fake-{self._n}"
            root = self._roots.get(thread, thread) if thread else None
            self._roots[mid] = root or mid
            msg = Msg(mid, room, root, sender, is_agent, text, parse_mentions(text))
            members = self.rooms.get(room, ()) if self.rooms is not None else self.handlers
            to = [a for a in members if a != sender and addresses(msg.mentions, a)]
            self.transcript.add({"kind": "post", **asdict(msg), "to": to})
            self._queue.extend((a, msg) for a in to)
        self._drain()
        return mid

    def _drain(self):
        with self._lock:
            if not self.started or self._draining:
                return
            self._draining = True
        n = 0
        try:
            while True:
                with self._lock:
                    if not self._queue:
                        return
                    agent, msg = self._queue.popleft()
                    hs = list(self.handlers.get(agent, ()))
                n += 1
                if n > self.max_deliveries:
                    with self._lock:
                        dropped = len(self._queue) + 1
                        self._queue.clear()
                    self.errors.append({"agent": agent, "msg": msg.id,
                                        "error": f"delivery storm: over {self.max_deliveries} deliveries, "
                                                 f"dropped {dropped}"})
                    self.log(self.errors[-1]["error"])
                    return
                for h in hs:
                    _call(h, msg, self.errors, self.log, agent)
        finally:
            with self._lock:
                self._draining = False


def _room_members(rooms):
    if rooms is None:
        return None
    if isinstance(rooms, dict):
        return {k: list(v) for k, v in rooms.items()}
    return {r["name"]: list(r.get("agents") or []) + list(r.get("users") or []) for r in rooms}


# ───────────────────────── SwitchTransport ─────────────────────────


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_private(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


class _AgentLink:
    """One agent identity: credentials, one Connection (scope=all, filter=addressed), cursor
    and delivered ids persisted, ops bound to a room by connect_to_room. Shim over
    switch_client's low-level API until its library layer lands."""

    def __init__(self, name, *, creds_path, state_path, base_url=None, registration_token=None,
                 description=None, on_frame=None, on_room=None, log=None, keep=1000):
        self.name = name
        self.creds_path = Path(creds_path)
        self.state_path = Path(state_path)
        self.base_url = base_url
        self.registration_token = registration_token   # callable -> str, called only to register
        self.description = description or f"{name}: a department agent of the synaps-switch company"
        self.on_frame = on_frame or (lambda link, ev: None)
        self.on_room = on_room or (lambda link, room_id, ctx: None)
        self.log = log or (lambda *a: None)
        self.keep = keep
        self.lock = threading.RLock()         # one op sequence (connect + post) at a time
        self._state_lock = threading.Lock()
        self.state = _read_json(self.state_path, {})
        self.delivered = dict.fromkeys((self.state.get("delivered") or [])[-keep:])  # ordered set
        self.posted: dict = {}                # ids this agent posted (ordered set, written under lock)
        self.bridge = None
        self.conn = None
        self.agent_id = None
        self.current_room = None
        self.connected = threading.Event()

    # credentials: registered once, kept 0600, never logged
    def credentials(self):
        creds = _read_json(self.creds_path, {})
        if creds.get("id") and creds.get("api_key"):
            return creds
        if not self.base_url:
            raise RuntimeError(f"{self.name}: no credentials at {self.creds_path} and no base_url to register at")
        token = self.registration_token() if self.registration_token else None
        if not token:
            raise RuntimeError(f"{self.name}: no credentials at {self.creds_path} and no registration token")
        reg = sc.register(self.base_url, token, self.name, self.description)
        creds = {"name": self.name, "id": reg["id"], "api_key": reg["api_key"], "base_url": self.base_url,
                 "registered_at": int(time.time())}
        _write_private(self.creds_path, creds)
        self.log(f"registered {self.name}: agent id {reg['id']}")
        return creds

    def start(self):
        creds = self.credentials()
        base = self.base_url or creds.get("base_url")
        if not base:
            raise RuntimeError(f"{self.name}: no base_url")
        self.agent_id = creds["id"]
        self.bridge = sc.Bridge(base, creds["id"], creds["api_key"])
        with self._state_lock:
            self.state.setdefault("connection_id", str(uuid.uuid4()))
            self.state["agent_id"] = self.agent_id
        self.save()
        self.conn = sc.Connection(self.bridge, self.state["connection_id"], scope="all", filter="addressed",
                                  cursor=self.state.get("cursor"), on_event=self._on_event,
                                  on_control=self._on_control, on_cursor=self._on_cursor,
                                  on_fatal=self._on_fatal, log=lambda *a: self.log(self.name, *a))
        self.conn.start()
        return self

    def stop(self):
        if self.conn:
            self.conn.stop()
        self.save()

    def save(self):
        with self._state_lock:
            self.state["delivered"] = list(self.delivered)
            _write_private(self.state_path, self.state)

    @staticmethod
    def _remember(ordered, key, keep):
        ordered[key] = None
        while len(ordered) > keep:
            del ordered[next(iter(ordered))]

    # stream thread
    def _on_cursor(self, cursor, generation):
        with self._state_lock:
            self.state["cursor"], self.state["generation"] = cursor, generation
        self.save()

    def _on_control(self, f):
        t = f.get("type")
        if t == "connection_state":
            self.current_room = None     # a fresh stream: bind the room again before the next op
            self.connected.set()
        elif t == "evicted":
            self.connected.clear()
        elif t == "gap":
            self.log(f"{self.name}: gap {f.get('from_sequence')} -> {f.get('resumed_at')} ({f.get('reason')})")

    def _on_fatal(self, exc):
        self.connected.clear()
        self.log(f"{self.name}: connection stopped for good: {exc}")

    def _on_event(self, ev):
        if ev.get("type") != "message":
            return
        p = ev.get("payload") or {}
        mid = p.get("message_id")
        if not mid or not p.get("addressed") or mid in self.posted:
            return
        if mid in self.delivered:
            return   # a replay or a re-sent frame: at most once per message id
        try:
            self.on_frame(self, ev)
        except Exception as e:  # noqa: BLE001 - never let a frame wedge the stream
            self.log(f"{self.name}: dropped {mid}: {type(e).__name__}: {e}")
        with self._state_lock:
            self._remember(self.delivered, mid, self.keep)
        self.save()

    # ops
    def op(self, name, args=None, room_id=None):
        if self.conn is None:
            raise RuntimeError(f"{self.name} is not started")
        with self.lock:
            if room_id and room_id != self.current_room:
                self.connect(room_id)
            try:
                return self.bridge.op(name, args or {}, self.conn.connection_id)
            except sc.SwitchError as e:
                if not room_id or e.status not in (400, 404, 409):
                    raise
                self.connect(room_id)    # the room binding went with an old stream: rebind once
                return self.bridge.op(name, args or {}, self.conn.connection_id)

    def connect(self, room_id):
        with self.lock:
            ctx = self.bridge.op("connect_to_room", {"room_id": room_id, "include_general_instructions": False},
                                 self.conn.connection_id)
            self.current_room = room_id
        if isinstance(ctx, dict):
            if ctx.get("warning"):
                self.log(f"{self.name}: connect_to_room {room_id}: {ctx['warning']}")
            self.on_room(self, room_id, ctx)
        return ctx

    def post(self, room_id, body, thread_id=None):
        with self.lock:
            out = self.op("post_message", {"body": body, "thread_id": thread_id}, room_id)
            mid = (out or {}).get("event_id")
            if mid:
                self._remember(self.posted, mid, self.keep)
        return mid


class SwitchTransport:
    """One Switch agent connection per company agent, all in this process.

    Credentials: <state_dir>/agents/<name>.json (0600), registered once with the registration
    token if missing. Per-agent cursor, connection id and delivered ids:
    <state_dir>/transport/<name>.state.json. base_url defaults to $SWITCH_URL, then to the
    base_url saved with each agent's credentials.
    """

    def __init__(self, base_url=None, *, agents=(), rooms=None, state_dir="company/state",
                 registration_token=None, registration_token_file=None, transcript_path=None,
                 connect_timeout=30.0, log=None):
        self.base_url = base_url or os.environ.get("SWITCH_URL")
        self.state_dir = Path(state_dir)
        self._token = registration_token
        self._token_file = registration_token_file or os.environ.get("SWITCH_REGISTRATION_TOKEN_FILE")
        self.transcript = _Transcript(transcript_path)
        self.connect_timeout = connect_timeout
        self.log = log or (lambda *a: None)
        self.handlers: dict[str, list[Callable[[Msg], None]]] = {}
        self.errors: list[dict] = []
        self.links: dict[str, _AgentLink] = {}
        self._wanted = list(agents)
        self._lock = threading.RLock()
        self._started = False
        self._room_ids: dict[str, str] = dict(rooms or {})                 # name -> id
        self._room_names: dict[str, str] = {v: k for k, v in self._room_ids.items()}
        self._roster: dict[str, dict[str, bool]] = {}                      # room id -> {name: is_agent}
        self._pfile = self.state_dir / "transport" / "participants.json"
        self._agent_ids: dict[str, str] = dict(_read_json(self._pfile, {}).get("agents") or {})

    # contract
    def on_message(self, agent, handler):
        with self._lock:
            self.handlers.setdefault(agent, []).append(handler)
            started = self._started
        if started:
            self._link(agent)

    def start(self):
        with self._lock:
            self._started = True
            names = list(dict.fromkeys([*self._wanted, *self.handlers]))
        for n in names:
            self._link(n)
        self.refresh_rooms()

    def post(self, as_agent, room, text, thread=None):
        link = self._link(as_agent)
        rid = self.room_id(room, via=link)
        mid = link.post(rid, text, thread)
        self.transcript.add({"kind": "post", "id": mid, "room": self._room_names.get(rid, room), "thread": thread,
                             "sender": as_agent, "sender_is_agent": True, "text": text,
                             "mentions": parse_mentions(text)})
        return mid

    def stop(self):
        for link in list(self.links.values()):
            link.stop()

    def pin_rooms(self, rooms: dict[str, str]):
        """Room name -> Switch room id, overriding list_rooms (whoever created the rooms knows)."""
        with self._lock:
            for name, rid in rooms.items():
                self._room_ids[name] = rid
                self._room_names[rid] = name

    # rooms
    def refresh_rooms(self, via=None):
        for link in ([via] if via else list(self.links.values())):
            rooms = link.op("list_rooms", {})
            rooms = rooms if isinstance(rooms, list) else (rooms or {}).get("rooms", [])
            for r in rooms:
                rid, name = r.get("room_id") or r.get("id"), r.get("name")
                if not rid or not name or r.get("archived"):
                    continue
                with self._lock:
                    self._room_names.setdefault(rid, name)
                    have = self._room_ids.setdefault(name, rid)
                if have != rid:
                    self.log(f"room name {name!r} is ambiguous ({have}, {rid}): using {have}; pass rooms= to pin")

    def room_id(self, room, via=None):
        with self._lock:
            if room in self._room_names:
                return room
            if room in self._room_ids:
                return self._room_ids[room]
        if via is not None:
            self.refresh_rooms(via)
            with self._lock:
                if room in self._room_ids:
                    return self._room_ids[room]
        raise KeyError(f"unknown room {room!r}")

    def room_name(self, room_id, via=None):
        if room_id not in self._room_names and via is not None:
            try:
                self.refresh_rooms(via)
            except sc.SwitchError as e:
                self.log(f"list_rooms failed: {e}")
        return self._room_names.get(room_id, room_id)

    # agents
    def _token_value(self):
        if self._token:
            return self._token
        if os.environ.get("SWITCH_REGISTRATION_TOKEN"):
            return os.environ["SWITCH_REGISTRATION_TOKEN"]
        if self._token_file:
            return Path(self._token_file).expanduser().read_text().strip()
        return None

    def _link(self, name):
        with self._lock:
            link = self.links.get(name)
            if link is None:
                link = _AgentLink(name, creds_path=self.state_dir / "agents" / f"{name}.json",
                                  state_path=self.state_dir / "transport" / f"{name}.state.json",
                                  base_url=self.base_url, registration_token=self._token_value,
                                  on_frame=self._on_frame, on_room=self._on_room, log=self.log)
                link.start()
                self.links[name] = link
        if not link.connected.wait(self.connect_timeout):
            raise TimeoutError(f"{name}: not connected to Switch after {self.connect_timeout:.0f}s")
        return link

    # participants
    def _on_room(self, link, room_id, ctx):
        roster = {}
        for p in ctx.get("participants") or []:
            is_agent = p.get("type") == "agent"
            for key in (p.get("name"), p.get("display_name"), p.get("alias")):
                if key:
                    roster[key.lower()] = roster.get(key.lower(), False) or is_agent
        with self._lock:
            self._roster[room_id] = roster
            if ctx.get("name"):
                self._room_names.setdefault(room_id, ctx["name"])

    def _learn(self, sender, name):
        if not sender or self._agent_ids.get(sender) == name:
            return
        with self._lock:
            self._agent_ids[sender] = name
            _write_private(self._pfile, {"agents": self._agent_ids})

    def is_agent(self, link, room_id, payload):
        """Whether a message's sender is an agent (see the module docstring for the rules)."""
        mid, sender = payload.get("message_id"), payload.get("sender")
        name = payload.get("sender_name") or ""
        for other in list(self.links.values()):
            if mid in other.posted:
                self._learn(sender, other.name)
                return True
        if sender in self._agent_ids:
            return True
        kind = payload.get("sender_kind")
        if kind == "agent":
            self._learn(sender, name)
            return True
        if kind in ("user", "platform") or payload.get("sender_role"):
            return False
        try:
            link.connect(room_id)      # refreshes the room's participant list via _on_room
        except sc.SwitchError as e:
            self.log(f"{link.name}: participant lookup in {room_id} failed, treating {name!r} as an agent: {e}")
            return True                # fail closed: an unverified sender is never taken for a person
        if self._roster.get(room_id, {}).get(name.lower()):
            self._learn(sender, name)
            return True
        return False

    def _on_frame(self, link, ev):
        p = ev.get("payload") or {}
        rid = ev.get("room_id")
        body = p.get("body") or ""
        msg = Msg(p["message_id"], self.room_name(rid, via=link), p.get("thread_id"),
                  p.get("sender_name") or p.get("sender") or "", self.is_agent(link, rid, p), body,
                  parse_mentions(body))
        self.transcript.add({"kind": "deliver", "to": link.name, **asdict(msg)})
        for h in list(self.handlers.get(link.name, ())):
            _call(h, msg, self.errors, self.log, link.name)
