"""Apply an Env to Switch: rooms, agents, links, and the #board documents. One generation at a time.

    from company.switch_env import apply_env, teardown, update_ledger
    rooms = apply_env(env, board_link, generation=2)   # {"board": "<room id>", "dev": ..., ...}
    transport.pin_rooms(rooms)                          # the transport keeps addressing rooms by env name
    ...
    update_ledger(env, board_link, rows, generation=2)  # the quarter's KPI row(s) -> the "Ledger" doc
    teardown(env, board_link, generation=2)             # archive the generation's rooms

    python -m company.switch_env ops
    python -m company.switch_env apply company/envs/env1.yaml [--gen K] [--agent-prefix P] [--room-prefix P]
    python -m company.switch_env teardown company/envs/env1.yaml [--gen K] [...]

What one apply makes, all named for the generation so each env's transcripts stay separable:
- a room group `<prefix><env>-g<k>` and one Mattermost-bridged room per env room, `<prefix><room>-g<k>`,
  with the env's agents (registry names via `agent_names`), the env's users (`{$creator}` = the
  owner's Mattermost username), and the operator agent;
- reporting lines: every department room links to #board ("reports to"), and each `x-company.links`
  pair links both ways ("direct line");
- two documents in #board, created by the operator (Switch lets only the creator update them):
  "Handbook" (the env: agents, rooms, links, budgets, policies) and "Ledger" (the KPI table).

The operator is the agent whose bridge is passed (the board in a company run). It is put in every
room because Switch requires membership to read a room, archive it, and see its links. It must be
an agent with an owner (registered with a registration token), so the rooms belong to that owner.

Rooms come from one `create_room_from_yaml` call (a version-2 template: a group, its rooms and its
links; agents are referenced by name, never created). If that fails or leaves rooms out, the missing
rooms are made one by one with `create_room` (retried while Mattermost is slow, as tools/mkroom.py
does), and every room is then checked: absent agents are invited, absent users added, missing links
linked. Apply is idempotent per generation: run it again and it only fills gaps and refreshes the docs.
What was made is recorded in <state_dir>/switch_env/<group>.json; teardown archives those rooms.

`bridge` is anything with `op(name, args=None, room_id=None)` and `connect(room_id) -> context`:
SwitchTransport's per-agent link (`transport._link("board")`) or `Ops(switch_client.Bridge(...))`.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

try:
    import switch_client as sc
except ImportError:  # run from somewhere other than the repo root
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import switch_client as sc

from company.core.env import Env, load_env

ROOT = Path(__file__).resolve().parent.parent

# What apply/teardown call. Checked against GET /agents/{id}/ops at startup.
REQUIRED_OPS = ("list_rooms", "create_room", "get_room_detail", "invite_agent_to_room", "connect_to_room",
                "link_rooms", "list_linked_rooms", "create_room_document", "update_room_document", "archive_room")
OPTIONAL_OPS = ("create_room_from_yaml", "run_template", "add_users_to_room", "list_bridges", "get_my_context",
                "create_room_group", "list_room_groups", "load_internal_documents", "unarchive_room",
                "unlink_rooms", "define_role", "list_agents")

REPORTS_TO = "reports to"
DIRECT_LINE = "direct line"
HANDBOOK, LEDGER = "Handbook", "Ledger"
LEDGER_COLUMNS = ("quarter", "seed", "revenue", "costs", "profit", "customers", "churn_rate", "growth",
                  "open_incident_severity", "mean_fix_weeks", "llm_cost_usd", "fitness")


class ApplyError(RuntimeError):
    pass


# ───────────────────────── ops adapter ─────────────────────────


class Ops:
    """Switch ops as one agent over a switch_client.Bridge. Ops outside a room go without a
    connection; ops in a room (documents, links) need a session bound to it, so the first
    `connect(room_id)` opens this adapter's own event stream (Switch refuses a connection id
    with no open stream). Same shape as transport._AgentLink."""

    def __init__(self, bridge, name=None, *, log=None, connect_timeout=30.0):
        self.bridge = bridge
        self.name = name
        self.log = log or (lambda *a: None)
        self.connect_timeout = connect_timeout
        self.conn = None
        self.current_room = None
        self.lock = threading.RLock()
        self._ready = threading.Event()

    def op(self, name, args=None, room_id=None):
        with self.lock:
            if room_id and room_id != self.current_room:
                self.connect(room_id)
            return self.bridge.op(name, args or {}, self.conn.connection_id if self.conn else None)

    def connect(self, room_id):
        with self.lock:
            if self.conn is None:
                self._open_stream()
            ctx = self.bridge.op("connect_to_room", {"room_id": room_id, "include_general_instructions": False},
                                 self.conn.connection_id)
            self.current_room = room_id
        if isinstance(ctx, dict) and ctx.get("warning"):
            self.log(f"connect_to_room {room_id}: {ctx['warning']}")
        return ctx

    def _on_control(self, f):
        if f.get("type") == "connection_state":
            self.current_room = None      # a fresh stream: rooms bind again
            self._ready.set()

    def _open_stream(self):
        self.conn = sc.Connection(self.bridge, str(uuid.uuid4()), scope="all", filter="addressed",
                                  on_control=self._on_control)
        self.conn.start()
        if not self._ready.wait(self.connect_timeout):
            self.close()
            raise ApplyError(f"no connection_state from Switch after {self.connect_timeout:.0f}s")

    def close(self):
        if self.conn is not None:
            self.conn.stop()
            self.conn = None


def as_ops(bridge, log=None):
    if isinstance(bridge, sc.Bridge):
        return Ops(bridge, log=log)
    return bridge


# ───────────────────────── plan (pure) ─────────────────────────


def _key(name):
    k = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return k if k[:1].isalpha() else f"r_{k}"


def _agent_map(env, agent_names):
    names = [a["name"] for a in env.agents]
    if agent_names is None:
        return {n: n for n in names}
    if callable(agent_names):
        return {n: agent_names(n) for n in names}
    return {n: agent_names.get(n, n) for n in names}


def plan_env(env: Env, generation: int, *, agent_names=None, operator=None, room_prefix="",
             board_room="board") -> dict:
    """Everything apply will make, as data. No I/O."""
    amap = _agent_map(env, agent_names)
    group = f"{room_prefix}{env.name}-g{generation}"
    room_names = [r["name"] for r in env.rooms]
    switch_name = {r: f"{room_prefix}{r}-g{generation}" for r in room_names}
    budgets = (env.company or {}).get("budgets") or {}
    direct = {}
    for pair in (env.company or {}).get("links") or []:
        a, b = list(pair)[:2]
        if a not in switch_name or b not in switch_name:
            raise ApplyError(f"x-company.links names a room the env does not have: {pair}")
        direct.setdefault(a, []).append(b)
        direct.setdefault(b, []).append(a)

    rooms = []
    for r in env.rooms:
        name = r["name"]
        agents = [amap.get(a, a) for a in r.get("agents") or []]
        if operator and operator not in agents:
            agents.append(operator)
        brief = [f"#{switch_name[name]} is the {name} room of the acme-shop company, {env.name} generation {generation}.",
                 "Agents here: " + ", ".join(f"@{a}" for a in agents) + "."]
        if name in budgets:
            brief.append(f"Quarter budget: ${budgets[name]:,}.")
        if name != board_room and board_room in switch_name:
            brief.append(f"Reports to #{switch_name[board_room]}.")
        if direct.get(name):
            brief.append("Direct line to " + ", ".join(f"#{switch_name[d]}" for d in direct[name]) + ".")
        brief.append(f"The Handbook and the Ledger are documents in #{switch_name.get(board_room, board_room)}.")
        rooms.append({"name": name, "key": _key(name), "switch_name": switch_name[name],
                      "description": f"acme-shop {name} ({env.name}, generation {generation})",
                      "instructions": " ".join(brief), "agents": agents, "users": list(r.get("users") or [])})

    links = []
    if board_room in switch_name:
        links += [{"from": n, "to": board_room, "label": REPORTS_TO} for n in room_names if n != board_room]
    for a, bs in direct.items():
        links += [{"from": a, "to": b, "label": DIRECT_LINE} for b in bs]
    return {"env": env.name, "generation": generation, "group": group, "operator": operator,
            "board_room": board_room if board_room in switch_name else None, "agents": amap,
            "rooms": rooms, "links": links}


def switch_template(plan) -> str:
    """A version-2 Switch template: one group, its rooms and links. Agents are existing ones, named."""
    import yaml
    res = {"company": {"type": "group", "name": plan["group"],
                       "description": f"acme-shop company, {plan['env']} generation {plan['generation']}",
                       "links": [{"from": _key(l["from"]), "to": _key(l["to"]), "label": l["label"]}
                                 for l in plan["links"]]}}
    for r in plan["rooms"]:
        room = {"type": "room", "name": r["switch_name"], "description": r["description"],
                "instructions": r["instructions"], "channel_type": "channel_public",
                "agents": r["agents"], "group": "company"}
        if r["users"]:
            room["users"] = r["users"]
        res[r["key"]] = room
    return yaml.safe_dump({"version": 2, "resources": res}, sort_keys=False, allow_unicode=True, width=1000)


def handbook_markdown(env: Env, plan) -> str:
    sn = {r["name"]: r["switch_name"] for r in plan["rooms"]}
    out = [f"# Handbook: {env.name}, generation {plan['generation']}", ""]
    if env.parent:
        out.append(f"Parent env: {env.parent}.")
    out += ["The org design this company runs under. Every change to it is one mutation, approved in "
            f"#{sn.get(plan['board_room'] or '', 'board')} and recorded in the lineage.", "", "## Rooms", "",
            "| room | Switch room | agents | users |", "|---|---|---|---|"]
    for r in plan["rooms"]:
        out.append(f"| {r['name']} | #{r['switch_name']} | {', '.join(r['agents'])} | {', '.join(u.replace('{$creator}', 'owner') for u in r['users']) or '-'} |")
    out += ["", "## Reporting lines", ""]
    out += [f"- #{sn[l['from']]} → #{sn[l['to']]} ({l['label']})" for l in plan["links"]] or ["- none"]
    c = env.company or {}
    out += ["", "## Budgets (per quarter)", ""]
    out += [f"- {k}: ${v:,}" for k, v in (c.get("budgets") or {}).items()] or ["- none"]
    out += ["", "## Policies", ""]
    out += [f"- {k}: {v}" for k, v in (c.get("policies") or {}).items()] or ["- none"]
    if "price" in c or "weeks_per_quarter" in c:
        out += ["", f"Price ${c.get('price')}, {c.get('weeks_per_quarter')} weeks per quarter."]
    out += ["", "## Agents", ""]
    for a in env.agents:
        name = plan["agents"].get(a["name"], a["name"])
        alias = f" (env name `{a['name']}`)" if name != a["name"] else ""
        rooms = [r["switch_name"] for r in plan["rooms"] if name in r["agents"]]
        out += [f"### @{name}{alias}", "",
                f"{a.get('description', '')} Provider: {a.get('provider', '?')}"
                + (f", persona `{a['persona']}`" if a.get("persona") else "")
                + (". Rooms: " + ", ".join(f"#{x}" for x in rooms) if rooms else "") + ".", "",
                (a.get("instructions") or "").strip(), ""]
    return "\n".join(out).rstrip() + "\n"


def _cell(v):
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 1 else f"{v:,.2f}"
    return "-" if v is None else str(v)


def ledger_markdown(env: Env, generation: int, rows=()) -> str:
    """The KPI ledger: budgets and policies, then one row per quarter (dicts or Metrics-like objects)."""
    c = env.company or {}
    out = [f"# Ledger: {env.name}, generation {generation}", "",
           "Budgets: " + (", ".join(f"{k} ${v:,}" for k, v in (c.get("budgets") or {}).items()) or "none") + ".",
           "Policies: " + (", ".join(f"{k}={v}" for k, v in (c.get("policies") or {}).items()) or "none") + ".",
           "", "| " + " | ".join(LEDGER_COLUMNS) + " |", "|" + "---|" * len(LEDGER_COLUMNS)]
    for row in rows:
        d = row if isinstance(row, dict) else vars(row)
        out.append("| " + " | ".join(_cell(d.get(k)) for k in LEDGER_COLUMNS) + " |")
    if not rows:
        out.append("| (no quarter settled yet) |" + " |" * (len(LEDGER_COLUMNS) - 1))
    return "\n".join(out) + "\n"


# ───────────────────────── state ─────────────────────────


def state_path(state_dir, group):
    return Path(state_dir) / "switch_env" / f"{group}.json"


def load_state(state_dir, group):
    try:
        return json.loads(state_path(state_dir, group).read_text())
    except (OSError, ValueError):
        return None


def _save_state(state_dir, state):
    p = state_path(state_dir, state["group"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, p)


# ───────────────────────── op registry ─────────────────────────


def check_ops(bridge, log=print, required=REQUIRED_OPS, optional=OPTIONAL_OPS):
    """GET /agents/{id}/ops; logs every op apply/teardown would use that this Switch lacks."""
    b = getattr(bridge, "bridge", bridge)
    have = set(b.ops())
    missing_req = [o for o in required if o not in have]
    missing_opt = [o for o in optional if o not in have]
    if missing_req:
        log(f"switch ops: MISSING required {missing_req}")
    if missing_opt:
        log(f"switch ops: missing optional {missing_opt} (falling back)")
    if not missing_req and not missing_opt:
        log(f"switch ops: all {len(required) + len(optional)} ops apply/teardown use are present ({len(have)} in the registry)")
    return {"count": len(have), "missing_required": missing_req, "missing_optional": missing_opt, "ops": sorted(have)}


# ───────────────────────── apply ─────────────────────────


def _rooms_of(ops):
    out = ops.op("list_rooms", {})
    return out if isinstance(out, list) else (out or {}).get("rooms", [])


def _transient(e):
    return isinstance(e, sc.SwitchError) and (e.status in (0, 500, 502, 503, 504) or "not running" in str(e.detail))


def _is_dup(e):
    return isinstance(e, sc.SwitchError) and (e.status == 409 or "already" in str(e.detail).lower())


def _creator_usernames(ops, log):
    try:
        ctx = ops.op("get_my_context", {}) or {}
    except sc.SwitchError as e:
        log(f"get_my_context failed, {{$creator}} unresolved: {e}")
        return []
    names = []
    for app in ((ctx.get("messaging_apps") or {}).get("connected") or []):
        if app.get("is_default") or not names:
            names = list(app.get("your_usernames") or [])
    return names


def _resolve_users(users, creator):
    out = []
    for u in users:
        out += creator if u == "{$creator}" else [u]
    return list(dict.fromkeys(out))


def apply_env(env: Env, bridge, *, generation: int = 1, agent_names=None, room_prefix="", board_room="board",
              mode="auto", state_dir="company/state", log=print, fixup=None, retries=5, retry_wait=10.0,
              trace=None) -> dict:
    """Make (or complete) generation `generation` of env in Switch; returns {env room name: room id}.

    mode: "auto" (template, then per-room for whatever it left out), "template" (template only),
    "rooms" (per-room create_room only). fixup(room_plan, detail) is an optional hook run per room
    after the Switch-side checks (the devstack one adds Mattermost members Switch skipped).
    trace(op, args, result) sees every Switch call (evidence)."""
    ops = as_ops(bridge, log)
    operator = getattr(ops, "name", None)
    plan = plan_env(env, generation, agent_names=agent_names, operator=operator, room_prefix=room_prefix,
                    board_room=board_room)
    tr = trace or (lambda *a: None)

    def call(name, args=None, room_id=None):
        out = ops.op(name, args or {}, room_id=room_id)
        tr(name, {**(args or {}), **({"@room": room_id} if room_id else {})}, out)
        return out

    state = load_state(state_dir, plan["group"]) or {"group": plan["group"], "env": env.name,
                                                      "generation": generation, "rooms": {}, "documents": {}}
    if state.get("archived_at"):     # applied before and torn down: bring the same rooms back
        for name, r in state["rooms"].items():
            try:
                call("unarchive_room", {"room_id": r["id"]})
                log(f"unarchived {r['switch_name']}")
            except sc.SwitchError as e:
                log(f"unarchive {r['switch_name']}: {sc.short_detail(e.detail)}")
    state.update({"operator": operator, "agents": plan["agents"], "room_prefix": room_prefix,
                  "board_room": plan["board_room"],
                  "applied_at": int(time.time()), "archived_at": None})
    by_switch = {r["switch_name"]: r for r in plan["rooms"]}

    def existing():
        found = {}
        for r in _rooms_of(ops):
            if r.get("name") in by_switch and not r.get("archived"):
                found.setdefault(r["name"], r.get("room_id") or r.get("id"))
        return found

    have = existing()
    missing = [r for r in plan["rooms"] if r["switch_name"] not in have]

    # 1. the template: group + rooms + links in one call
    if missing and mode in ("auto", "template") and len(missing) == len(plan["rooms"]):
        try:
            out = call("create_room_from_yaml", {"yaml": switch_template(plan)}) or {}
            state["group_id"] = out.get("group_id") or state.get("group_id")
            state["template"] = {"ok": not out.get("errors"), "errors": out.get("errors") or []}
            for e in out.get("errors") or []:
                log(f"template: {e}")
            log(f"template: group {out.get('group_name')!r}, {len(out.get('rooms') or [])} rooms")
        except sc.SwitchError as e:
            state["template"] = {"ok": False, "errors": [f"HTTP {e.status}: {sc.short_detail(e.detail)}"]}
            log(f"template refused (HTTP {e.status}): {sc.short_detail(e.detail)}")
            if mode == "template":
                raise
            if _transient(e):
                time.sleep(retry_wait)       # Mattermost was slow: the template may have made rooms anyway
        have = existing()
        missing = [r for r in plan["rooms"] if r["switch_name"] not in have]
        if missing and mode == "template":
            raise ApplyError(f"template left out {[r['name'] for r in missing]}")

    # 2. per room, for anything still missing
    if missing:
        creator = _creator_usernames(ops, log)
        group_name = _ensure_group(call, plan, state, log)
        bridge_id = _mattermost_bridge(call)
        for r in missing:
            have[r["switch_name"]] = _create_room(call, r, group_name, bridge_id, _resolve_users(r["users"], creator),
                                                  existing, log, retries, retry_wait)

    # 3. verify every room: agents, users, the Mattermost side
    creator = None
    for r in plan["rooms"]:
        rid = have[r["switch_name"]]
        detail = call("get_room_detail", {"room_id": rid}) or {}
        in_room = set(detail.get("agent_names") or [])
        for a in r["agents"]:
            if a not in in_room:
                call("invite_agent_to_room", {"room_id": rid, "agent_name": a})
                log(f"{r['switch_name']}: invited {a}")
        users = r["users"]
        if users:
            creator = creator if creator is not None else _creator_usernames(ops, log)
            want = _resolve_users(users, creator)
            have_users = {u.lower() for u in detail.get("connected_user_names") or []}
            todo = [u for u in want if u.lower() not in have_users]
            if todo:
                try:
                    out = call("add_users_to_room", {"room_id": rid, "user_names": todo}) or {}
                    if out.get("unresolved"):
                        log(f"{r['switch_name']}: users not on the bridge: {out['unresolved']}")
                except sc.SwitchError as e:
                    log(f"{r['switch_name']}: add_users_to_room {todo} failed: {sc.short_detail(e.detail)}")
        if fixup:
            fixup(r, detail)
        state["rooms"][r["name"]] = {"id": rid, "switch_name": r["switch_name"], "agents": r["agents"],
                                     "channel_id": detail.get("external_channel_id")}
    rooms = {r["name"]: state["rooms"][r["name"]]["id"] for r in plan["rooms"]}

    # 4. links (directed; what the template made is found, not made twice)
    state["links"] = _ensure_links(call, plan, rooms, log)

    # 5. documents in #board: Handbook + Ledger, by the operator
    if plan["board_room"]:
        brid = rooms[plan["board_room"]]
        docs = _room_documents(ops, brid, tr)
        hb = handbook_markdown(env, plan)
        state["documents"][HANDBOOK] = _put_doc(call, brid, docs, state["documents"].get(HANDBOOK), HANDBOOK,
                                                f"The org design of {env.name} (generation {generation})",
                                                "Read before acting: who is in which room, reporting lines, "
                                                "budgets and policies for this quarter.", hb)
        if state["documents"].get(LEDGER) and docs.get(LEDGER) == state["documents"][LEDGER]:
            pass   # keep the quarters already written; update_ledger owns it from here
        else:
            state["documents"][LEDGER] = _put_doc(call, brid, docs, state["documents"].get(LEDGER), LEDGER,
                                                  f"KPI ledger of {env.name} (generation {generation})",
                                                  "One row per settled quarter. The board updates it; "
                                                  "finance reads it for burn vs budget.",
                                                  ledger_markdown(env, generation))
    _save_state(state_dir, state)
    log(f"applied {env.name} generation {generation}: {len(rooms)} rooms, {len(state['links'])} links, "
        f"documents {sorted(state['documents'])}")
    return rooms


def _ensure_group(call, plan, state, log):
    try:
        groups = call("list_room_groups", {}) or []
        if any(g.get("name") == plan["group"] for g in groups):
            return plan["group"]
        out = call("create_room_group", {"name": plan["group"],
                                         "description": f"acme-shop company, {plan['env']} generation {plan['generation']}"})
        state["group_id"] = (out or {}).get("id")
        return plan["group"]
    except sc.SwitchError as e:
        log(f"room group {plan['group']!r}: {sc.short_detail(e.detail)} (rooms stay standalone)")
        return None


def _mattermost_bridge(call):
    try:
        bl = call("list_bridges", {})
    except sc.SwitchError:
        return None
    bl = bl if isinstance(bl, list) else (bl or {}).get("bridges", [])
    for b in bl:
        if b.get("type") == "mattermost" and b.get("status", "active") == "active":
            return b.get("id")
    return next((b.get("id") for b in bl if b.get("is_default")), None)


def _create_room(call, r, group_name, bridge_id, users, existing, log, retries, retry_wait):
    args = {"name": r["switch_name"], "description": r["description"], "agent_names": r["agents"],
            "instructions": r["instructions"]}
    if users:
        args["user_names"] = users
    if bridge_id:
        args.update(bridge_id=bridge_id, channel_type="channel_public")
    if group_name:
        args["group_name"] = group_name
    # Switch calls Mattermost with a 10 s timeout; on a loaded machine that fails (500, "Bridge not
    # running") and the room may exist after all. Retry, looking for it first (tools/mkroom.py).
    for attempt in range(1, retries + 1):
        try:
            out = call("create_room", args) or {}
            rid = out.get("id") or out.get("room_id")
            log(f"created {r['switch_name']}: {rid}")
            return rid
        except sc.SwitchError as e:
            if not _transient(e) or attempt == retries:
                raise
            log(f"create_room {r['switch_name']} attempt {attempt}: HTTP {e.status}, retrying")
            time.sleep(retry_wait * attempt)
            rid = existing().get(r["switch_name"])
            if rid:
                return rid
    raise AssertionError("unreachable")


def _ensure_links(call, plan, rooms, log):
    made = []
    current = {}
    for l in plan["links"]:
        src, dst = rooms[l["from"]], rooms[l["to"]]
        if src not in current:
            out = call("list_linked_rooms", {}, room_id=src)
            out = out if isinstance(out, list) else (out or {}).get("linked_rooms", [])
            current[src] = {x.get("target_room_id") for x in out}
        if dst not in current[src]:
            try:
                call("link_rooms", {"source_room_id": src, "target_room_id": dst, "label": l["label"]})
                current[src].add(dst)
            except sc.SwitchError as e:
                if not _is_dup(e):
                    raise
        made.append({"from": l["from"], "to": l["to"], "label": l["label"], "source_room_id": src,
                     "target_room_id": dst})
    return made


def _room_documents(ops, room_id, tr):
    ctx = ops.connect(room_id) or {}
    tr("connect_to_room", {"room_id": room_id}, {k: ctx.get(k) for k in ("name", "documents", "linked_rooms")})
    return {d.get("name"): d.get("id") for d in ctx.get("documents") or [] if d.get("name")}


def _put_doc(call, room_id, docs, known_id, name, description, instructions, content):
    doc_id = known_id if known_id and known_id in docs.values() else docs.get(name)
    if doc_id:
        call("update_room_document", {"document_id": doc_id, "description": description,
                                      "instructions": instructions, "content": content}, room_id=room_id)
        return doc_id
    out = call("create_room_document", {"name": name, "description": description, "instructions": instructions,
                                        "content": content}, room_id=room_id) or {}
    return out.get("document_id") or out.get("id")


def update_ledger(env: Env, bridge, rows, *, generation: int = 1, room_prefix="", state_dir="company/state",
                  content=None) -> str:
    """Rewrite the generation's Ledger document with `rows` (one per settled quarter). The bridge
    must be the operator that applied the env: only a document's creator may update it."""
    ops = as_ops(bridge)
    group = f"{room_prefix}{env.name}-g{generation}"
    state = load_state(state_dir, group)
    if not state or not state.get("documents", {}).get(LEDGER):
        raise ApplyError(f"no Ledger recorded for {group}: apply the env first")
    board = state["rooms"][state["board_room"]]
    ops.op("update_room_document", {"document_id": state["documents"][LEDGER],
                                    "content": content or ledger_markdown(env, generation, rows)},
           room_id=board["id"])
    return state["documents"][LEDGER]


# ───────────────────────── teardown ─────────────────────────


def teardown(env: Env, bridge, *, generation: int = 1, room_prefix="", state_dir="company/state", log=print,
             trace=None) -> dict:
    """Archive the generation's rooms (reversible: unarchive_room). Returns {env room name: room id}."""
    ops = as_ops(bridge, log)
    tr = trace or (lambda *a: None)
    group = f"{room_prefix}{env.name}-g{generation}"
    state = load_state(state_dir, group)
    if not state:
        raise ApplyError(f"nothing recorded for {group} in {state_path(state_dir, group)}")
    done = {}
    order = [r["name"] for r in env.rooms if r["name"] in state["rooms"]]
    for name in order + sorted(set(state["rooms"]) - set(order)):
        r = state["rooms"][name]
        try:
            out = ops.op("archive_room", {"room_id": r["id"]})
            tr("archive_room", {"room_id": r["id"]}, out)
            log(f"archived {r['switch_name']}")
        except sc.SwitchError as e:
            if not _is_dup(e) and "archived" not in str(e.detail).lower():
                raise
            log(f"{r['switch_name']}: already archived")
        done[name] = r["id"]
    state["archived_at"] = int(time.time())
    _save_state(state_dir, state)
    return done


# ───────────────────────── devstack hook ─────────────────────────


def load_devstack(devstack_dir):
    spec = importlib.util.spec_from_file_location("company_switch_env_devstack", Path(devstack_dir) / "dev.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def devstack_fixup(dev, log=print):
    """Mattermost side of each room (devstack only): the agents' bots and the seeded user in the
    channel. Switch skips adding a bot when its Mattermost adapter is restarting under load."""
    e = dev.env()

    def fix(r, detail):
        cid = detail.get("external_channel_id")
        if not cid:
            log(f"{r['switch_name']}: no Mattermost channel")
            return
        members = {m["user_id"] for m in dev.mm(e, "GET", f"/channels/{cid}/members?per_page=200", who="admin")}
        want = list(r["agents"]) + [e["MATTERMOST_USER"] if u == "{$creator}" else u for u in r["users"]]
        for uname in want:
            u = dev.mm(e, "GET", f"/users/username/{uname}", who="admin")
            if u["id"] not in members:
                dev.mm(e, "POST", f"/channels/{cid}/members", {"user_id": u["id"]}, who="admin")
                log(f"{r['switch_name']}: added {uname} to the Mattermost channel")
    return fix


# ───────────────────────── CLI ─────────────────────────


def _default_devstack():
    for d in (os.environ.get("SWITCH_DEVSTACK"), ROOT / "devstack", Path.home() / "Projects/synaps-switch/devstack"):
        if d and (Path(d) / ".env").exists():
            return Path(d)
    return None


def _ensure_agents(env, amap, base_url, token_file, state_dir, log, stand_in, only=None):
    """Register the env's agents that have no credentials yet (company/state/agents/<name>.json, as
    SwitchTransport does). Synaps-provider agents are someone else's identity: they must exist,
    unless stand_in (tests) registers a plain agent under the mapped name."""
    from company.transport import _AgentLink
    token = lambda: Path(token_file).read_text().strip() if token_file else None  # noqa: E731
    creds = {}
    for a in env.agents:
        name = amap[a["name"]]
        if (only and a["name"] not in only) or (a.get("provider") == "synaps" and not stand_in):
            continue
        link = _AgentLink(name, creds_path=Path(state_dir) / "agents" / f"{name}.json",
                          state_path=Path(state_dir) / "switch_env" / f".{name}.unused.json", base_url=base_url,
                          registration_token=token, description=a.get("description"), log=log)
        try:
            creds[name] = link.credentials()
        except sc.SwitchError as e:
            if e.status == 409:
                raise ApplyError(f"agent {name!r} exists in Switch but {link.creds_path} has no credentials; "
                                 "pick another --agent-prefix") from e
            raise
    return creds


def _cli_ops(a, env):
    amap = {n: f"{a.agent_prefix}{n}" for n in (x["name"] for x in env.agents)}
    if a.operator not in amap:
        raise ApplyError(f"operator {a.operator!r} is not an agent of {env.name}")
    op_name = amap[a.operator]
    if a.cmd == "apply":
        _ensure_agents(env, amap, a.url, a.token_file, a.state_dir, _log, a.stand_in_synaps, only={a.operator})
    c = json.loads((Path(a.state_dir) / "agents" / f"{op_name}.json").read_text())
    ops = Ops(sc.Bridge(a.url, c["id"], c["api_key"]), op_name, log=_log)
    if a.cmd == "apply":
        for x in env.agents:   # someone else's agents (synaps-dev) must exist before anything is made
            name = amap[x["name"]]
            if x.get("provider") == "synaps" and not a.stand_in_synaps and \
                    name not in {y.get("name") for y in ops.op("list_agents", {"name_contains": name}) or []}:
                raise ApplyError(f"{name!r} ({x.get('provider')}) is not registered in Switch: start it first "
                                 "(synaps-dev is the Synaps extension's agent), or --stand-in-synaps for a test")
        _ensure_agents(env, amap, a.url, a.token_file, a.state_dir, _log, a.stand_in_synaps)
    return ops, amap


def _log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m company.switch_env", description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=os.environ.get("SWITCH_URL", "http://127.0.0.1:18400"))
    p.add_argument("--token-file", default=os.environ.get("SWITCH_REGISTRATION_TOKEN_FILE"))
    p.add_argument("--state-dir", default="company/state")
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("apply", "teardown"):
        s = sub.add_parser(cmd)
        s.add_argument("env")
        s.add_argument("--gen", type=int, help="generation k (rooms <room>-g<k>); default: the env's number")
        s.add_argument("--agent-prefix", default="", help="registry name = prefix + env agent name")
        s.add_argument("--room-prefix", default="")
        s.add_argument("--operator", default="board", help="env agent that creates and owns the rooms")
        s.add_argument("--trace", help="write every Switch call and response here (jsonl)")
        if cmd == "apply":
            s.add_argument("--mode", choices=("auto", "template", "rooms"), default="auto")
            s.add_argument("--devstack", help="devstack dir (with .env) for the Mattermost check; 'none' to skip")
            s.add_argument("--stand-in-synaps", action="store_true",
                           help="register synaps-provider agents as plain agents too (live tests only)")
    s = sub.add_parser("ops", help="check the op registry, as an agent with saved credentials")
    s.add_argument("--agent", default="board")
    a = p.parse_args(argv)
    if a.token_file is None:
        d = _default_devstack()
        if d and (d / "state/registration.token").exists():
            a.token_file = str(d / "state/registration.token")

    if a.cmd == "ops":
        f = Path(a.state_dir) / "agents" / f"{a.agent}.json"
        if not f.exists():
            p.error(f"no credentials at {f}")
        c = json.loads(f.read_text())
        print(json.dumps(check_ops(sc.Bridge(a.url, c["id"], c["api_key"]), _log), indent=1))
        return 0

    env = load_env(a.env)
    gen = a.gen if a.gen is not None else int((re.search(r"(\d+)$", env.name) or [None, 1])[1])
    ops, amap = _cli_ops(a, env)
    trace_f = open(a.trace, "a", encoding="utf-8") if a.trace else None

    def trace(op, args, out):
        if trace_f:
            trace_f.write(json.dumps({"ts": round(time.time(), 3), "op": op, "args": args, "result": out},
                                     ensure_ascii=False) + "\n")
            trace_f.flush()
    try:
        check = check_ops(ops, _log)
        if check["missing_required"]:
            raise ApplyError(f"this Switch lacks {check['missing_required']}")
        if a.cmd == "apply":
            fix = None
            if a.devstack != "none":
                d = Path(a.devstack) if a.devstack else _default_devstack()
                fix = devstack_fixup(load_devstack(d), _log) if d else None
            rooms = apply_env(env, ops, generation=gen, agent_names=amap, room_prefix=a.room_prefix, mode=a.mode,
                              state_dir=a.state_dir, log=_log, fixup=fix, trace=trace)
        else:
            rooms = teardown(env, ops, generation=gen, room_prefix=a.room_prefix, state_dir=a.state_dir, log=_log,
                             trace=trace)
        print(json.dumps(rooms, indent=1))
    finally:
        ops.close()
        if trace_f:
            trace_f.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ApplyError as e:
        _log(f"error: {e}")
        sys.exit(2)
