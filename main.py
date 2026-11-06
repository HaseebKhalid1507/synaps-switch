#!/usr/bin/env python3
"""synaps-switch: a Synaps daemon extension that puts Synaps sessions into Switch rooms.

One process per daemon (extensions are daemon-wide). For every configured
Switch agent it is that agent's *watcher*: it holds the agent's one connection
(event stream + heartbeat, switch_client.Connection), gives each room a pinned
daemon session of its own (created over the daemon socket), delivers addressed
messages to that session as events, and serves tools that act in the calling
session's room (POST /ops/{name} with the connection and a session selector).

Config (extension config keys, resolved by Synaps):
  config_file   JSON file: {"agents": [ {agent}, ... ]}  (a list: N agents, v0 runs them all)
  state_dir     default <SYNAPS_BASE_DIR>/synaps-switch

Agent entry: name, description, display_name, bridge_url, registration_token_file
(read once, at registration; never logged), model, max_session_cost, thinking,
instructions, rooms (optional allowlist of room ids).

stdout is JSON-RPC only. Logs go to stderr and <state_dir>/extension.log, never a credential.
"""
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import switch_client as sc  # noqa: E402

PLUGIN_ID = "synaps-switch"
DAEMON_PROTOCOL = 3
CLIENT_NAME = "synaps-switch"
# Extensions get a scrubbed environment (no SYNAPS_BASE_DIR), so the daemon's base dir
# is a config key: base_dir. It decides which daemon socket and run dir we talk to.
CONFIG = {"config_file": "", "state_dir": "", "base_dir": ""}
BASE = None
SKILL = (HERE / "skill" / "SKILL.md").read_text(encoding="utf-8") if (HERE / "skill" / "SKILL.md").exists() else ""
SESSION_ENV_KEYS = ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "SHELL", "TZ")
DELIVERED_KEEP = 500

_log_lock = threading.Lock()
_log_file = None


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def log(*a):
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} [{PLUGIN_ID}] " + " ".join(str(x) for x in a)
    with _log_lock:
        print(line, file=sys.stderr, flush=True)
        if _log_file is not None:
            try:
                _log_file.write(line + "\n")
                _log_file.flush()
            except OSError:
                pass


def state_dir():
    return Path(CONFIG["state_dir"] or BASE / "synaps-switch")


def write_private(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def slug(s, n=24):
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:n].strip("-") or "room"


# ───────────────────────── daemon socket ─────────────────────────


class DaemonError(Exception):
    pass


def daemon_socket():
    prof = os.environ.get("SYNAPS_PROFILE") or ""
    return BASE / "run" / (f"daemon-{prof}.sock" if prof else "daemon.sock")


class DaemonClient:
    """Newline-delimited JSON over the daemon's unix socket (session/wire.rs), bounded by one deadline."""

    def __init__(self, deadline_s=20.0, cwd="/"):
        self.deadline = time.monotonic() + deadline_s
        self.buf = b""
        self.cwd = str(cwd)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self._left())
        try:
            self.sock.connect(str(daemon_socket()))
        except OSError as e:
            self.sock.close()
            raise DaemonError(f"no daemon at {daemon_socket()}: {e}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.bye()
        self.close()

    def _left(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise DaemonError("timed out talking to the daemon")
        return left

    def send(self, frame):
        self.sock.settimeout(self._left())
        try:
            self.sock.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        except OSError as e:
            raise DaemonError(f"daemon socket: {e}")

    def recv(self):
        while b"\n" not in self.buf:
            self.sock.settimeout(self._left())
            try:
                chunk = self.sock.recv(1 << 16)
            except socket.timeout:
                raise DaemonError("timed out talking to the daemon")
            except OSError as e:
                raise DaemonError(f"daemon socket: {e}")
            if not chunk:
                raise DaemonError("daemon closed the connection")
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        if not line.strip():
            return self.recv()
        try:
            return json.loads(line)
        except ValueError:
            return {"type": "?"}

    def hello(self, env=None):
        frame = {"type": "hello", "protocol_version": DAEMON_PROTOCOL,
                 "client": {"kind": "server", "terminal": None, "instance": f"{CLIENT_NAME}-{os.getpid()}",
                            "history": "digest", "tail_items": 0, "name": CLIENT_NAME},
                 "cwd": self.cwd, "client_version": f"{CLIENT_NAME}/{sc.CLIENT_VERSION}"}
        if env is not None:
            frame["env"] = env
        self.send(frame)
        f = self.recv()
        if f.get("type") == "refused":
            raise DaemonError(f"daemon refused the handshake: {f.get('message')}")
        if f.get("type") != "welcome":
            raise DaemonError(f"unexpected first frame {f.get('type')!r}")
        return f

    def create(self, config):
        """attach create → the session's meta. The caller says bye right after (detach)."""
        self.send({"type": "attach", "attach": "create", "config": config, "mode": "mirror"})
        while True:
            f = self.recv()
            t = f.get("type")
            if t == "attached":
                return f.get("meta") or {}
            if t in ("error", "refused", "bye"):
                raise DaemonError(f"create refused: {f.get('message') or f}")

    def pin(self, sid, on=True):
        self.send({"type": "pin", "session_id": sid, "on": on})
        while True:
            f = self.recv()
            if f.get("type") in ("pinned", "refused", "error", "bye"):
                return f

    def bye(self):
        try:
            self.send({"type": "bye"})
        except DaemonError:
            pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def daemon_sessions():
    """{session_id: SessionMeta} from the daemon's Welcome (hello, read, bye: no attach)."""
    with DaemonClient(10.0) as c:
        w = c.hello()
    return {s.get("id"): s for s in (w.get("sessions") or []) if s.get("id")}


def session_env():
    """What a room session's tools see: a short allowlist, never the daemon's credentials."""
    env = [(k, os.environ[k]) for k in SESSION_ENV_KEYS if os.environ.get(k)]
    env.append(("TERM", "dumb"))
    return sorted(env)


def registry_entry(sid):
    run = BASE / "run"
    try:
        names = os.listdir(run)
    except OSError:
        return None
    for name in names:
        if not name.endswith(".json") or name == "daemon.json":
            continue
        reg = read_json(run / name, None)
        if isinstance(reg, dict) and reg.get("session_id") == sid and reg.get("kind", "session") == "session":
            return reg
    return None


def wake(sid, event, wait_s=8.0):
    """Write one event to the session's event socket (unparks it, starts a turn). True on success."""
    t0 = time.monotonic()
    while True:
        reg = registry_entry(sid)
        path = (reg or {}).get("socket_path") or ""
        if path:
            run = os.path.realpath(BASE / "run")
            if not os.path.realpath(path).startswith(run + os.sep):
                log("refusing a socket outside the run dir for", sid)
                return False
            try:
                if stat.S_ISSOCK(os.stat(path).st_mode):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                        s.settimeout(2.0)
                        s.connect(path)
                        s.sendall(json.dumps(event).encode())
                        s.shutdown(socket.SHUT_WR)
                    return True
            except OSError as e:
                log("wake failed", sid, type(e).__name__, e)
        if time.monotonic() - t0 > wait_s:
            return False
        time.sleep(0.25)


# ───────────────────────── one Switch agent ─────────────────────────


class Watcher:
    """One Switch agent: its credentials, connection, rooms and their sessions."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg["name"]
        self.dir = state_dir() / "agents" / slug(self.name, 40)
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.state_path = self.dir / "state.json"
        self.creds_path = self.dir / "credentials.json"
        self.state = read_json(self.state_path, {})
        self.state.setdefault("rooms", {})
        self.state.setdefault("delivered", [])
        self.lock = threading.RLock()
        self.room_locks = {}
        self.bridge = None
        self.conn = None
        self.pending_gap = None
        self.status = "starting"
        self.last_error = None

    # persistence
    def save(self):
        with self.lock:
            write_private(self.state_path, self.state)

    def selector(self, room_id):
        """The Switch-side session id for a room's session: stable across Synaps session restarts."""
        return f"synaps-{room_id}"[:64]

    def room_of_session(self, sid):
        for rid, r in self.state["rooms"].items():
            if r.get("session_id") == sid:
                return rid
        return None

    # lifecycle
    def start(self):
        threading.Thread(target=self._run, name=f"watcher-{self.name}", daemon=True).start()

    def _run(self):
        try:
            self._connect()
        except Exception as e:  # noqa: BLE001
            self.status, self.last_error = "failed", f"{type(e).__name__}: {e}"
            log(f"[{self.name}] could not start: {self.last_error}")

    def _credentials(self):
        creds = read_json(self.creds_path, {})
        if creds.get("id") and creds.get("api_key"):
            return creds
        tf = self.cfg.get("registration_token_file")
        token = Path(os.path.expanduser(tf)).read_text().strip() if tf else os.environ.get(
            self.cfg.get("registration_token_env") or "SWITCH_REGISTRATION_TOKEN", "")
        if not token:
            raise RuntimeError("no credentials and no registration token (registration_token_file)")
        reg = sc.register(self.cfg["bridge_url"], token, self.name,
                          self.cfg.get("description") or "A Synaps agent.",
                          display_name=self.cfg.get("display_name"),
                          overwrite=bool(self.cfg.get("overwrite")))
        creds = {"id": reg["id"], "api_key": reg["api_key"], "registered_at": now_iso()}
        write_private(self.creds_path, creds)
        log(f"[{self.name}] registered with Switch: agent id {reg['id']}")
        return creds

    def _connect(self):
        creds = self._credentials()
        self.bridge = sc.Bridge(self.cfg["bridge_url"], creds["id"], creds["api_key"])
        with self.lock:
            if not self.state.get("connection_id"):
                self.state["connection_id"] = str(uuid.uuid4())
            self.state["agent_id"] = creds["id"]
            self.save()
        rooms = sorted(self.state["rooms"])
        self.conn = sc.Connection(
            self.bridge, self.state["connection_id"], scope="all", filter="addressed", rooms=rooms,
            cursor=self.state.get("cursor"), on_event=self.on_event, on_control=self.on_control,
            on_cursor=self.on_cursor, on_fatal=self.on_fatal,
            log=lambda *a: log(f"[{self.name}]", *a))
        self.conn.start()
        self.status = "connecting"
        log(f"[{self.name}] connecting: connection {self.conn.connection_id}, cursor {self.state.get('cursor')}, "
            f"{len(rooms)} known room(s)")

    def stop(self):
        if self.conn:
            self.conn.stop()
        self.save()

    # connection callbacks (stream thread)
    def on_cursor(self, cursor, generation):
        with self.lock:
            self.state["cursor"], self.state["generation"] = cursor, generation
            self.save()

    def on_control(self, f):
        t = f.get("type")
        if t == "connection_state":
            self.status = "connected"
            log(f"[{self.name}] connected: rooms={len(f.get('rooms') or [])} cursor={f.get('cursor')} "
                f"protocol={f.get('protocol')} beat={f.get('heartbeat_interval_seconds')}s")
        elif t == "gap":
            self.pending_gap = f
            log(f"[{self.name}] gap: from {f.get('from_sequence')} resumed at {f.get('resumed_at')} ({f.get('reason')})")
        elif t == "evicted":
            log(f"[{self.name}] evicted: {f.get('code')} {f.get('reason')}")
        elif t in ("subscription_changed", "room_released"):
            log(f"[{self.name}] {t}: {json.dumps({k: v for k, v in f.items() if k != 'type'})[:300]}")

    def on_fatal(self, exc):
        self.status, self.last_error = "stopped", str(exc)

    def on_event(self, ev):
        if ev.get("type") != "message":
            return
        p = ev.get("payload") or {}
        mid, room_id = p.get("message_id"), ev.get("room_id")
        if not p.get("addressed") or not mid or not room_id:
            return
        if p.get("sender_name") == self.name:
            return
        allow = self.cfg.get("rooms")
        if allow and room_id not in allow:
            log(f"[{self.name}] ignoring room {room_id}: not in the allowlist")
            return
        with self.lock:
            if mid in self.state["delivered"]:
                log(f"[{self.name}] already delivered {mid}; skipping")
                return
        sid = self.ensure_session(room_id)
        thread = p.get("thread_id") or mid
        text = self.render(ev, p, thread)
        event = {
            "id": str(uuid.uuid4()),
            "timestamp": now_iso(),
            "source": {"source_type": "switch", "name": PLUGIN_ID, "callback": None},
            "channel": {"id": room_id, "name": self.state["rooms"][room_id].get("room_name") or room_id},
            "sender": {"id": str(p.get("sender") or ""), "name": str(p.get("sender_name") or "someone"),
                       "sender_type": "agent" if p.get("sender_kind") == "agent" else "user"},
            "content": {"text": text, "content_type": "message", "severity": "Medium", "data": None},
            "expects_response": True,
            "reply_to": None,
        }
        if not wake(sid, event):
            # Leave the cursor where it is: the stream reopens and the event comes again.
            raise RuntimeError(f"could not deliver {mid} to session {sid}")
        with self.lock:
            self.state["delivered"] = (self.state["delivered"] + [mid])[-DELIVERED_KEEP:]
            r = self.state["rooms"][room_id]
            r["last_message_id"], r["last_delivered_at"], r["deliveries"] = mid, now_iso(), r.get("deliveries", 0) + 1
            self.save()
        self.pending_gap = None
        log(f"[{self.name}] delivered {mid} (thread {thread}) from {p.get('sender_name')} to session {sid}")

    def render(self, ev, p, thread):
        room = self.state["rooms"][ev["room_id"]]
        nonce = secrets.token_hex(6)
        head = (f"[Switch] {p.get('sender_name') or 'someone'} addressed you in room "
                f"{room.get('room_name') or ev['room_id']} (message_id {p.get('message_id')}, thread_id {thread}):")
        lines = []
        if self.pending_gap:
            lines.append("[Switch] History was lost since you last heard from this room "
                         f"({self.pending_gap.get('reason') or 'gap'}): call switch_read_context before answering.")
        lines += [head, f"BEGIN SWITCH MESSAGE {nonce}", str(p.get("body") or ""), f"END SWITCH MESSAGE {nonce}"]
        att = p.get("attachments") or []
        if att:
            names = ", ".join(str(a.get("filename") or a.get("mxc")) for a in att[:10])
            lines.append(f"(attachments, not downloaded: {names})")
        lines.append(f'(Reply with switch_post_message, thread_id="{thread}". Your final text is not shown in the room.)')
        return "\n".join(lines)

    # sessions
    def ensure_session(self, room_id):
        with self.lock:
            lk = self.room_locks.setdefault(room_id, threading.Lock())
        with lk:
            r = self.state["rooms"].get(room_id)
            live = daemon_sessions()
            if r and r.get("session_id") in live:
                return r["session_id"]
            ctx = self.connect_room(room_id)
            if r and r.get("session_id"):
                sid = self._create(room_id, ctx, resume=r["session_id"])
            else:
                sid = self._create(room_id, ctx)
            return sid

    def connect_room(self, room_id):
        """connect_to_room as the room's session selector: binds that selector to the room."""
        ctx = self.bridge.op("connect_to_room", {"room_id": room_id, "include_general_instructions": False},
                             self.conn.connection_id, session_id=self.selector(room_id))
        if ctx.get("warning"):
            log(f"[{self.name}] connect_to_room {room_id}: {ctx['warning']}")
        return ctx

    def system_prompt(self, room_id, ctx):
        who = self.cfg.get("display_name") or self.name
        parts = [SKILL.strip(),
                 f"# This session\n\nYou are **{who}** (Switch agent `{self.name}`), running in Synaps. "
                 f"This session is the agent's session for the Switch room **{ctx.get('name') or room_id}** "
                 f"(room id `{room_id}`). People mention you as @{self.name}."]
        if self.cfg.get("instructions"):
            parts.append("# Operator instructions\n\n" + self.cfg["instructions"].strip())
        room = [f"Name: {ctx.get('name')}", f"Description: {ctx.get('description') or '(none)'}"]
        ps = ctx.get("participants") or []
        if ps:
            room.append("Participants: " + ", ".join(
                f"{x.get('name') or x.get('display_name')} ({x.get('kind') or x.get('type') or '?'})"
                if isinstance(x, dict) else str(x) for x in ps[:30]))
        for key in ("documents", "references", "roles", "linked_rooms"):
            items = ctx.get(key) or []
            if items:
                room.append(f"{key}: " + json.dumps(items, ensure_ascii=False)[:1500])
        if ctx.get("instructions"):
            room.append("Room instructions (these override the defaults above):\n" + str(ctx["instructions"]).strip())
        parts.append("# The room\n\n" + "\n".join(room))
        return "\n\n".join(parts)

    def _create(self, room_id, ctx, resume=None):
        rname = ctx.get("name") or room_id
        work = state_dir() / "rooms" / f"{slug(self.name, 16)}-{slug(rname)}-{room_id[:8]}"
        work.mkdir(parents=True, exist_ok=True)
        name = f"sw-{slug(self.name, 12)}-{slug(rname, 18)}-{hashlib.sha1(room_id.encode()).hexdigest()[:4]}"[:40]
        config = {"cwd": str(work), "keep_warm": True, "persist": True, "client_input": "shared",
                  "await_extensions": True, "auto_compact": True}
        if self.cfg.get("model"):
            config["model_override"] = self.cfg["model"]
        if self.cfg.get("max_session_cost"):
            config["max_session_cost"] = float(self.cfg["max_session_cost"])
        if resume:
            config["continue_session"] = resume
        else:
            config["system"] = self.system_prompt(room_id, ctx)
            config["name"] = name
        with DaemonClient(30.0, cwd=work) as c:
            c.hello(env=session_env())
            meta = c.create(config)
        sid = meta.get("id")
        if not sid:
            raise RuntimeError("the daemon created a session without an id")
        if not meta.get("pinned"):
            with DaemonClient(15.0) as c:
                c.hello()
                f = c.pin(sid, True)
                if f.get("type") != "pinned":
                    log(f"[{self.name}] pin {sid}: {f}")
        with self.lock:
            r = self.state["rooms"].setdefault(room_id, {})
            r.update({"session_id": sid, "session_name": meta.get("name") or name, "room_name": rname,
                      "work_dir": str(work), "created_at": r.get("created_at") or now_iso()})
            if resume:
                r["resumed_at"] = now_iso()
            self.save()
        log(f"[{self.name}] {'resumed' if resume else 'created'} session {sid} ({meta.get('name') or name}) "
            f"for room {rname} ({room_id}), pinned")
        return sid

    # ops for tools
    def op(self, name, args, room_id=None):
        if self.bridge is None or self.conn is None:
            raise ToolError(f"agent {self.name} is not connected to Switch ({self.status}: {self.last_error})")
        sel = self.selector(room_id) if room_id else None
        try:
            return self.bridge.op(name, args, self.conn.connection_id, session_id=sel)
        except sc.SwitchError as e:
            if room_id and e.status in (400, 409) and "connect" in str(e.detail).lower():
                self.connect_room(room_id)  # the binding went with an old connection: rebind, retry once
                return self.bridge.op(name, args, self.conn.connection_id, session_id=sel)
            raise


# ───────────────────────── tools ─────────────────────────


class ToolError(Exception):
    pass


WATCHERS = {}


def obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


ROOM_ARG = {"room_id": {"type": "string", "description": "Only outside a room session: the Switch room id to act in."},
            "agent": {"type": "string", "description": "Only with several agents and outside a room session: the agent name."}}

TOOLS = [
    {"name": "switch_post_message",
     "description": "Post a message in this session's Switch room. Pass thread_id (from the [Switch] event) to reply in its thread. "
                    "targets: names of people/agents to @-address (uses send_targeted_message).",
     "input_schema": obj({"body": {"type": "string", "description": "Plain text. Do not prefix @names."},
                          "thread_id": {"type": "string", "description": "Message id to reply into (any message of the thread)."},
                          "targets": {"type": "array", "items": {"type": "string"}, "description": "Names to address."},
                          **ROOM_ARG}, ["body"])},
    {"name": "switch_read_context",
     "description": "Read the room's recent conversation, grouped into threads (most recent last). `truncated` means older history exists.",
     "input_schema": obj({"limit": {"type": "integer", "description": "Max entries (default 30)."},
                          "since": {"type": "string", "description": "ISO-8601: only newer entries."},
                          "before": {"type": "string", "description": "ISO-8601: only older entries."}, **ROOM_ARG})},
    {"name": "switch_list_participants",
     "description": "Who is in this room: people, agents, their roles and status.",
     "input_schema": obj({**ROOM_ARG})},
    {"name": "switch_room_info",
     "description": "This room's detail: name, description, instructions, documents, references, roles, linked rooms.",
     "input_schema": obj({**ROOM_ARG})},
    {"name": "switch_list_rooms",
     "description": "The Switch rooms this agent is assigned to.",
     "input_schema": obj({"agent": ROOM_ARG["agent"]})},
    {"name": "switch_list_ops",
     "description": "The Switch operation catalog: every operation switch_op can call, with its parameters. "
                    "Pass `name` for one operation's full description and schema.",
     "input_schema": obj({"name": {"type": "string"}, "agent": ROOM_ARG["agent"]})},
    {"name": "switch_op",
     "description": "Call any Switch operation by name (POST /ops/{name}) as this room's session, e.g. create_room, "
                    "add_users_to_room, invite_agent_to_room, link_rooms, create_room_document, assume_role, "
                    "update_room. `args` is the operation's arguments object (see switch_list_ops).",
     "input_schema": obj({"name": {"type": "string", "description": "Operation name."},
                          "args": {"type": "object", "description": "The operation's arguments."}, **ROOM_ARG},
                         ["name"])},
    {"name": "switch_status",
     "description": "synaps-switch's own state: agents, connection, rooms and their sessions.",
     "input_schema": obj({})},
]


def caller(params, inp):
    """(watcher, room_id) for the calling session; a room session is bound to its room."""
    sid = params.get("session_id")
    for w in WATCHERS.values():
        rid = w.room_of_session(sid) if sid else None
        if rid:
            return w, rid
    want = inp.get("agent")
    if want:
        w = WATCHERS.get(want)
        if w is None:
            raise ToolError(f"no agent named {want!r}; configured: {', '.join(WATCHERS) or 'none'}")
    elif len(WATCHERS) == 1:
        w = next(iter(WATCHERS.values()))
    elif not WATCHERS:
        raise ToolError("no Switch agents are configured")
    else:
        raise ToolError(f"several agents are configured ({', '.join(WATCHERS)}): pass `agent`")
    return w, inp.get("room_id") or None


def need_room(rid):
    if not rid:
        raise ToolError("this session is not a Switch room session: pass room_id")
    return rid


def result(x):
    return x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, indent=1)


def t_post(params, inp):
    w, rid = caller(params, inp)
    rid = need_room(rid)
    body = inp.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ToolError("body is required")
    thread = inp.get("thread_id") or None
    targets = [t.lstrip("@") for t in (inp.get("targets") or []) if isinstance(t, str) and t.strip()]
    if targets:
        out = w.op("send_targeted_message", {"body": body, "target_names": targets, "thread_id": thread}, rid)
    else:
        out = w.op("post_message", {"body": body, "thread_id": thread}, rid)
    log(f"[{w.name}] posted in room {rid} (thread {thread}): {out}")
    return f"posted (event {(out or {}).get('event_id')})" + (f" in thread {thread}" if thread else "")


def t_read(params, inp):
    w, rid = caller(params, inp)
    args = {"limit": int(inp.get("limit") or 30), "room_id": need_room(rid)}
    for k in ("since", "before"):
        if inp.get(k):
            args[k] = inp[k]
    return result(w.op("read_context", args, rid))


def t_participants(params, inp):
    w, rid = caller(params, inp)
    return result(w.op("list_participants", {}, need_room(rid)))


def t_room_info(params, inp):
    w, rid = caller(params, inp)
    return result(w.op("get_room_detail", {"room_id": need_room(rid)}, rid))


def t_list_rooms(params, inp):
    w, rid = caller(params, inp)
    return result(w.op("list_rooms", {}, rid))


_ops_cache = {}


def t_list_ops(params, inp):
    w, _ = caller(params, inp)
    if w.bridge is None:
        raise ToolError(f"agent {w.name} is not connected")
    if w.name not in _ops_cache:
        _ops_cache[w.name] = w.bridge.ops()
    ops = _ops_cache[w.name]
    name = inp.get("name")
    if name:
        if name not in ops:
            raise ToolError(f"no operation {name!r}")
        return result({name: ops[name]})
    lines = []
    for n in sorted(ops):
        d = (ops[n].get("description") or "").strip().split("\n")[0][:140]
        props = list(((ops[n].get("input_schema") or ops[n].get("parameters") or {}).get("properties") or {}))
        lines.append(f"- {n}({', '.join(props)}): {d}")
    return "\n".join(lines)


def t_op(params, inp):
    w, rid = caller(params, inp)
    name = inp.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
        raise ToolError("name must be an operation name like create_room (see switch_list_ops)")
    args = inp.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            raise ToolError("args must be a JSON object")
    if not isinstance(args, dict):
        raise ToolError("args must be an object")
    out = w.op(name, args, rid)
    log(f"[{w.name}] op {name} from room {rid}: ok")
    return result(out)


def t_status(params, inp):
    out = {"base_dir": str(BASE), "state_dir": str(state_dir()), "agents": {}}
    for n, w in WATCHERS.items():
        c = w.conn
        out["agents"][n] = {
            "status": w.status, "error": w.last_error, "agent_id": w.state.get("agent_id"),
            "connection_id": w.state.get("connection_id"), "cursor": w.state.get("cursor"),
            "opens": c.opens if c else 0, "beats_ok": c.beats_ok if c else 0,
            "covered_rooms": c.covered if c else [],
            "rooms": {rid: {k: r.get(k) for k in ("room_name", "session_id", "session_name", "deliveries")}
                      for rid, r in w.state["rooms"].items()},
        }
    sid = params.get("session_id")
    out["caller"] = {"session_id": sid, "room": next((w.room_of_session(sid) for w in WATCHERS.values()
                                                       if sid and w.room_of_session(sid)), None)}
    return result(out)


TOOL_FNS = {"switch_post_message": t_post, "switch_read_context": t_read, "switch_list_participants": t_participants,
            "switch_room_info": t_room_info, "switch_list_rooms": t_list_rooms, "switch_list_ops": t_list_ops,
            "switch_op": t_op, "switch_status": t_status}


def on_tool_call(params):
    fn = TOOL_FNS.get(params.get("name"))
    if fn is None:
        raise ToolError(f"unknown tool {params.get('name')}")
    inp = params.get("input") if isinstance(params.get("input"), dict) else {}
    try:
        return fn(params, inp)
    except sc.SwitchError as e:
        raise ToolError(f"Switch refused: {e}")


# ───────────────────────── config + JSON-RPC ─────────────────────────


def load_agents():
    cf = CONFIG["config_file"] or str(state_dir() / "agents.json")
    data = read_json(os.path.expanduser(cf), None)
    if data is None:
        log(f"no agents config at {cf}: serving tools only")
        return []
    agents = data.get("agents") if isinstance(data, dict) else data
    out = []
    for a in agents or []:
        if not isinstance(a, dict) or not a.get("name") or not a.get("bridge_url"):
            log("skipping an agent entry without name/bridge_url")
            continue
        if a.get("enabled", True):
            out.append(a)
    return out


def resolve_base():
    b = CONFIG["base_dir"] or os.environ.get("SYNAPS_BASE_DIR") or str(Path.home() / ".synaps-cli")
    return Path(os.path.expanduser(b)).resolve()


def start():
    global _log_file
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    os.chmod(sd, 0o700)
    _log_file = open(sd / "extension.log", "a", encoding="utf-8")
    os.chmod(sd / "extension.log", 0o600)
    for a in load_agents():
        if a["name"] in WATCHERS:
            log(f"duplicate agent {a['name']}: skipped")
            continue
        w = Watcher(a)
        WATCHERS[a["name"]] = w
        w.start()
    log(f"started: base {BASE}, {len(WATCHERS)} agent(s): {', '.join(WATCHERS)}")


def read_message(stream):
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            if length is None:
                continue
            break
        k, _, v = line.decode("ascii", "replace").partition(":")
        if k.strip().lower() == "content-length":
            length = int(v.strip())
    return json.loads(stream.read(length))


_out_lock = threading.Lock()


def write_message(payload):
    body = json.dumps(payload, separators=(",", ":")).encode()
    with _out_lock:
        sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        sys.stdout.buffer.flush()


def serve():
    global PLUGIN_ID, BASE
    stdin = sys.stdin.buffer
    started = False
    while True:
        msg = read_message(stdin)
        if msg is None:
            break
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        try:
            if method == "initialize":
                PLUGIN_ID = params.get("plugin_id") or PLUGIN_ID
                for k, v in (params.get("config") or {}).items():
                    if k in CONFIG and v not in (None, ""):
                        CONFIG[k] = str(v)
                BASE = resolve_base()
                res = {"protocol_version": 1, "capabilities": {"tools": TOOLS}}
                write_message({"jsonrpc": "2.0", "id": mid, "result": res})
                if not started:
                    started = True
                    threading.Thread(target=start, name="start", daemon=True).start()
                continue
            if method == "tool.call":
                try:
                    res = {"content": on_tool_call(params)}
                except ToolError as e:
                    res = {"content": f"synaps-switch: {e}"}
            elif method == "hook.handle":
                res = {"action": "continue"}
            elif method == "shutdown":
                for w in WATCHERS.values():
                    w.stop()
                if mid is not None:
                    write_message({"jsonrpc": "2.0", "id": mid, "result": None})
                return
            elif mid is None:
                continue
            else:
                write_message({"jsonrpc": "2.0", "id": mid,
                               "error": {"code": -32601, "message": f"unknown method {method}"}})
                continue
        except Exception as e:  # noqa: BLE001
            log("error in", method, type(e).__name__, e, traceback.format_exc(limit=3).replace("\n", " | "))
            if mid is not None:
                write_message({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)}})
            continue
        if mid is not None:
            write_message({"jsonrpc": "2.0", "id": mid, "result": res})
    for w in WATCHERS.values():
        w.stop()


if __name__ == "__main__":
    serve()
