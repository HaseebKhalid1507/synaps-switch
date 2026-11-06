"""company/switch_env.py against an in-memory Switch (and once over HTTP, tests/fake_bridge.py)."""
import copy
import json
import sys
import uuid
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import switch_client as sc  # noqa: E402
from fake_bridge import FakeBridge  # noqa: E402

from company.core.env import apply_mutation, load_env  # noqa: E402
from company.switch_env import (  # noqa: E402
    DIRECT_LINE, HANDBOOK, LEDGER, REPORTS_TO, REQUIRED_OPS, ApplyError, Ops, apply_env, check_ops,
    ledger_markdown, load_state, plan_env, switch_template, teardown, update_ledger)

ENV1 = ROOT / "company/envs/env1.yaml"
ROOMS = ["board", "dev", "maintenance", "growth", "marketing", "finance"]


class FakeSwitch:
    """The Switch ops apply/teardown use, in memory, with Switch's rules: names must be registered,
    room ops need a connected room, documents are updated only by their creator."""

    def __init__(self, agents, *, missing_ops=(), skip_agent=None):
        self.registry = set(agents)
        self.rooms = {}          # id -> room
        self.groups = {}         # name -> id
        self.calls = []          # (op, args, room)
        self.fail = {}           # op -> [exceptions to raise, in order]
        self.missing_ops = set(missing_ops)
        self.skip_agent = skip_agent   # an agent the template "forgets" to add (Mattermost hiccup)

    def ops(self):
        from company.switch_env import OPTIONAL_OPS
        return {o: {} for o in (*REQUIRED_OPS, *OPTIONAL_OPS) if o not in self.missing_ops}

    def _room(self, name, description, instructions, agents, users, group=None):
        for a in agents:
            if a not in self.registry:
                raise sc.SwitchError(400, {"detail": f"unknown agent {a}"}, "op")
        rid = str(uuid.uuid4())
        self.rooms[rid] = {"id": rid, "name": name, "description": description, "instructions": instructions,
                           "agents": [a for a in agents if a != self.skip_agent],
                           "users": ["user" if u == "{$creator}" else u for u in users], "group": group,
                           "archived": False, "links": {}, "docs": {}, "channel": "ch-" + rid[:8]}
        return rid

    def handle(self, who, name, args, room=None):
        self.calls.append((name, copy.deepcopy(args), room))
        if self.fail.get(name):
            raise self.fail[name].pop(0)
        if name == "list_rooms":
            return [{"room_id": r["id"], "name": r["name"], "archived": r["archived"]} for r in self.rooms.values()
                    if who in r["agents"] and (args.get("include_archived") or not r["archived"])]
        if name == "create_room_from_yaml":
            doc = yaml.safe_load(args["yaml"])
            assert doc["version"] == 2
            res = doc["resources"]
            gkey = next(k for k, v in res.items() if v["type"] == "group")
            self.groups[res[gkey]["name"]] = gid = str(uuid.uuid4())
            ids = {}
            for k, v in res.items():
                if v["type"] == "room":
                    assert v["group"] == gkey
                    ids[k] = self._room(v["name"], v["description"], v.get("instructions"), v["agents"],
                                        v.get("users") or [], res[gkey]["name"])
            for link in res[gkey].get("links") or []:
                self.rooms[ids[link["from"]]]["links"][ids[link["to"]]] = link["label"]
            return {"group_id": gid, "group_name": res[gkey]["name"], "errors": [],
                    "rooms": [{"room_id": i, "room_name": self.rooms[i]["name"]} for i in ids.values()]}
        if name == "create_room":
            if args.get("group_name") and args["group_name"] not in self.groups:
                raise sc.SwitchError(400, {"detail": "unknown group"}, "op")
            rid = self._room(args["name"], args["description"], args.get("instructions"), args["agent_names"],
                             args.get("user_names") or [], args.get("group_name"))
            return {"id": rid, "name": args["name"]}
        if name == "list_room_groups":
            return [{"id": i, "name": n} for n, i in self.groups.items()]
        if name == "create_room_group":
            self.groups[args["name"]] = gid = str(uuid.uuid4())
            return {"id": gid, "name": args["name"]}
        if name == "list_bridges":
            return [{"id": "mm-1", "type": "mattermost", "status": "active", "is_default": True}]
        if name == "get_my_context":
            return {"messaging_apps": {"connected": [{"platform": "mattermost", "is_default": True,
                                                      "your_usernames": ["user"]}]}}
        if name in ("get_room_detail", "archive_room", "unarchive_room", "invite_agent_to_room", "add_users_to_room"):
            r = self.rooms[args["room_id"]]
            if who not in r["agents"] and name != "invite_agent_to_room":
                raise sc.SwitchError(403, {"detail": "not a member"}, "op")
            if name == "invite_agent_to_room":
                if args["agent_name"] not in self.registry:
                    raise sc.SwitchError(400, {"detail": "unknown agent"}, "op")
                r["agents"].append(args["agent_name"])
                return {"ok": True}
            if name == "add_users_to_room":
                r["users"] += args["user_names"]
                return {"ok": True, "added": args["user_names"], "unresolved": []}
            if name in ("archive_room", "unarchive_room"):
                r["archived"] = name == "archive_room"
            return {"id": r["id"], "name": r["name"], "agent_names": list(r["agents"]),
                    "connected_user_names": list(r["users"]), "external_channel_id": r["channel"],
                    "archived": r["archived"], "group_name": r["group"]}
        if name == "link_rooms":
            src = self.rooms[args["source_room_id"]]
            if args["target_room_id"] in src["links"]:
                raise sc.SwitchError(409, {"detail": "link already exists"}, "op")
            src["links"][args["target_room_id"]] = args["label"]
            return {"target_room_id": args["target_room_id"], "label": args["label"]}
        # ops in the connected room
        if room is None:
            raise sc.SwitchError(400, {"detail": "not connected to a room"}, "op")
        r = self.rooms[room]
        if name == "connect_to_room":
            return {"room_id": room, "name": r["name"], "participants": [],
                    "documents": [{"id": i, "name": d["name"]} for i, d in r["docs"].items()]}
        if name == "list_linked_rooms":
            return [{"target_room_id": t, "label": lab} for t, lab in r["links"].items()]
        if name == "create_room_document":
            if any(d["name"] == args["name"] for d in r["docs"].values()):
                raise sc.SwitchError(409, {"detail": "name taken"}, "op")
            did = str(uuid.uuid4())
            r["docs"][did] = {**args, "creator": who}
            return {"document_id": did}
        if name == "update_room_document":
            d = r["docs"][args["document_id"]]
            if d["creator"] != who:
                raise sc.SwitchError(403, {"detail": "only the creator"}, "op")
            d.update({k: v for k, v in args.items() if v is not None and k != "document_id"})
            return {"ok": True}
        raise sc.SwitchError(404, {"detail": f"unknown op {name}"}, "op")

    def by_name(self, name):
        return next(r for r in self.rooms.values() if r["name"] == name)

    def count(self, op):
        return sum(1 for c in self.calls if c[0] == op)


class Link:
    """What apply_env gets: op(name, args, room_id) + connect(room_id), as one agent (transport._AgentLink)."""

    def __init__(self, sw, name="board"):
        self.sw, self.name, self.current_room = sw, name, None

    @property
    def bridge(self):
        return self.sw

    def op(self, name, args=None, room_id=None):
        if room_id and room_id != self.current_room:
            self.connect(room_id)
        return self.sw.handle(self.name, name, args or {}, self.current_room)

    def connect(self, room_id):
        self.current_room = room_id
        return self.sw.handle(self.name, "connect_to_room", {"room_id": room_id}, room_id)


ENV_AGENTS = ["board", "ops-maint", "growth", "marketing", "finance", "synaps-dev", "reviewer"]


@pytest.fixture
def env():
    return load_env(ENV1)


@pytest.fixture
def sw():
    return FakeSwitch(ENV_AGENTS)


def quiet(*a):
    pass


# ───────────────────────── plan + template ─────────────────────────


def test_plan_names_rooms_per_generation_and_puts_the_operator_everywhere(env):
    p = plan_env(env, 3, operator="board")
    assert [r["switch_name"] for r in p["rooms"]] == [f"{r}-g3" for r in ROOMS]
    assert p["group"] == "env1-g3"
    dev = next(r for r in p["rooms"] if r["name"] == "dev")
    assert dev["agents"] == ["synaps-dev", "reviewer", "board"]
    assert all("board" in r["agents"] for r in p["rooms"])
    assert all(r["users"] == ["{$creator}"] for r in p["rooms"])
    # env1: no direct links, every department reports to #board
    assert p["links"] == [{"from": r, "to": "board", "label": REPORTS_TO} for r in ROOMS[1:]]
    m = next(r for r in p["rooms"] if r["name"] == "maintenance")
    assert "$4,000" in m["instructions"] and "#board-g3" in m["instructions"]


def test_plan_maps_agent_names_and_adds_direct_lines(env):
    env2 = apply_mutation(env, {"op": "link", "rooms": ["maintenance", "dev"]})
    p = plan_env(env2, 7, agent_names=lambda n: f"t-{n}", operator="t-board", room_prefix="t-")
    assert p["group"] == "t-env1-g7"
    assert next(r for r in p["rooms"] if r["name"] == "dev")["agents"] == ["t-synaps-dev", "t-reviewer", "t-board"]
    direct = [(l["from"], l["to"]) for l in p["links"] if l["label"] == DIRECT_LINE]
    assert direct == [("maintenance", "dev"), ("dev", "maintenance")]
    bad = apply_mutation(env, {"op": "link", "rooms": ["maintenance", "nowhere"]})
    with pytest.raises(ApplyError):
        plan_env(bad, 1)


def test_template_is_a_v2_group_with_rooms_and_links_and_creates_no_agents(env):
    p = plan_env(env, 2, operator="board")
    doc = yaml.safe_load(switch_template(p))
    assert doc["version"] == 2
    res = doc["resources"]
    assert {v["type"] for v in res.values()} == {"group", "room"}
    assert res["company"]["name"] == "env1-g2"
    assert {"from": "maintenance", "to": "board", "label": REPORTS_TO} in res["company"]["links"]
    rooms = {v["name"]: v for v in res.values() if v["type"] == "room"}
    assert set(rooms) == {f"{r}-g2" for r in ROOMS}
    assert rooms["dev-g2"]["agents"] == ["synaps-dev", "reviewer", "board"]
    assert rooms["dev-g2"]["users"] == ["{$creator}"]
    assert all(v["group"] == "company" and v["channel_type"] == "channel_public" for v in rooms.values())
    assert "kickoff" not in switch_template(p)   # applying must not wake anyone


def test_ledger_has_one_row_per_quarter(env):
    from company.market.sim import Metrics
    m = Metrics(revenue=41000.0, costs=44000.0, profit=-3000.0, customers=500, churn_rate=0.05, growth=-0.02,
                open_incident_severity=3.0, mean_fix_weeks=3.1, llm_cost_usd=6.2)
    text = ledger_markdown(env, 1, [{**vars(m), "quarter": 1, "seed": 7, "fitness": -11.4}])
    assert "| 1 | 7 | 41,000.00 | 44,000.00 | -3,000.00 | 500 |" in text
    assert "marketing $10,000" in text and "finance_veto_usd=5000" in text
    assert "no quarter settled yet" in ledger_markdown(env, 1)


# ───────────────────────── apply ─────────────────────────


def test_apply_makes_rooms_agents_links_and_docs_with_one_template(env, sw, tmp_path):
    rooms = apply_env(env, Link(sw), generation=2, state_dir=tmp_path, log=quiet)
    assert list(rooms) == ROOMS
    assert sw.count("create_room_from_yaml") == 1 and sw.count("create_room") == 0
    for name, rid in rooms.items():
        r = sw.rooms[rid]
        assert r["name"] == f"{name}-g2" and r["group"] == "env1-g2" and r["users"] == ["user"]
    assert sorted(sw.rooms[rooms["dev"]]["agents"]) == ["board", "reviewer", "synaps-dev"]
    assert sw.rooms[rooms["maintenance"]]["links"] == {rooms["board"]: REPORTS_TO}
    assert sw.count("link_rooms") == 0          # the template made them; apply only checked
    docs = {d["name"]: d for d in sw.rooms[rooms["board"]]["docs"].values()}
    assert set(docs) == {HANDBOOK, LEDGER}
    assert docs[HANDBOOK]["creator"] == "board"
    assert "#maintenance-g2 → #board-g2 (reports to)" in docs[HANDBOOK]["content"]
    assert "Ledger: env1, generation 2" in docs[LEDGER]["content"]
    st = load_state(tmp_path, "env1-g2")
    assert st["rooms"]["dev"]["id"] == rooms["dev"] and len(st["links"]) == 5
    assert set(st["documents"]) == {HANDBOOK, LEDGER} and st["archived_at"] is None


def test_apply_again_fills_gaps_only_and_keeps_the_ledger(env, sw, tmp_path):
    rooms = apply_env(env, Link(sw), generation=2, state_dir=tmp_path, log=quiet)
    update_ledger(env, Link(sw), [{"quarter": 1, "profit": 1234.0}], generation=2, state_dir=tmp_path)
    sw.rooms[rooms["growth"]]["agents"].remove("growth")       # someone removed an agent meanwhile
    n = len(sw.calls)
    again = apply_env(env, Link(sw), generation=2, state_dir=tmp_path, log=quiet)
    assert again == rooms and len(sw.rooms) == 6
    later = [c[0] for c in sw.calls[n:]]
    assert "create_room_from_yaml" not in later and "create_room" not in later
    assert "create_room_document" not in later and later.count("update_room_document") == 1   # the Handbook
    assert "growth" in sw.rooms[rooms["growth"]]["agents"]
    ledger = next(d for d in sw.rooms[rooms["board"]]["docs"].values() if d["name"] == LEDGER)
    assert "1,234.00" in ledger["content"]


def test_template_failure_falls_back_to_create_room_with_retries(env, sw, tmp_path):
    sw.fail["create_room_from_yaml"] = [sc.SwitchError(500, {"detail": "Internal Server Error"}, "op")]
    sw.fail["create_room"] = [sc.SwitchError(400, {"detail": "Bridge not running"}, "op")]
    env2 = apply_mutation(env, {"op": "link", "rooms": ["maintenance", "dev"]})
    logs = []
    rooms = apply_env(env2, Link(sw), generation=5, state_dir=tmp_path, log=logs.append, retry_wait=0)
    assert list(rooms) == ROOMS and len(sw.rooms) == 6
    assert sw.count("create_room") == 7                       # one transient refusal, retried
    creates = [c[1] for c in sw.calls if c[0] == "create_room"]
    assert all(c["bridge_id"] == "mm-1" and c["channel_type"] == "channel_public" and c["group_name"] == "env1-g5"
               and c["user_names"] == ["user"] for c in creates)
    assert "env1-g5" in sw.groups
    assert sw.count("link_rooms") == 7                        # 5 reporting lines + maintenance <-> dev
    assert sw.rooms[rooms["maintenance"]]["links"] == {rooms["board"]: REPORTS_TO, rooms["dev"]: DIRECT_LINE}
    assert sw.rooms[rooms["dev"]]["links"][rooms["maintenance"]] == DIRECT_LINE
    assert any("template refused (HTTP 500)" in m for m in logs)


def test_rooms_mode_skips_the_template_and_template_mode_does_not_fall_back(env, sw, tmp_path):
    apply_env(env, Link(sw), generation=1, mode="rooms", state_dir=tmp_path, log=quiet)
    assert sw.count("create_room_from_yaml") == 0 and sw.count("create_room") == 6
    sw2 = FakeSwitch(ENV_AGENTS)
    sw2.fail["create_room_from_yaml"] = [sc.SwitchError(400, {"detail": "kickoff refused"}, "op")]
    with pytest.raises(sc.SwitchError):
        apply_env(env, Link(sw2), generation=1, mode="template", state_dir=tmp_path / "b", log=quiet)
    assert sw2.count("create_room") == 0


def test_an_agent_switch_skipped_is_invited(env, tmp_path):
    sw = FakeSwitch(ENV_AGENTS, skip_agent="reviewer")
    rooms = apply_env(env, Link(sw), generation=1, state_dir=tmp_path, log=quiet)
    assert ("invite_agent_to_room", {"room_id": rooms["dev"], "agent_name": "reviewer"}, None) in sw.calls
    assert "reviewer" in sw.rooms[rooms["dev"]]["agents"]


def test_unregistered_agent_fails_loudly(env, tmp_path):
    sw = FakeSwitch([a for a in ENV_AGENTS if a != "synaps-dev"])
    with pytest.raises(sc.SwitchError, match="unknown agent"):
        apply_env(env, Link(sw), generation=1, mode="rooms", state_dir=tmp_path, log=quiet)


def test_fixup_hook_sees_every_room(env, sw, tmp_path):
    seen = []
    apply_env(env, Link(sw), generation=1, state_dir=tmp_path, log=quiet,
              fixup=lambda r, detail: seen.append((r["switch_name"], detail["external_channel_id"])))
    assert [s[0] for s in seen] == [f"{r}-g1" for r in ROOMS] and all(s[1] for s in seen)


# ───────────────────────── teardown, ledger, op registry ─────────────────────────


def test_teardown_archives_the_generation_and_apply_brings_it_back(env, sw, tmp_path):
    rooms = apply_env(env, Link(sw), generation=4, state_dir=tmp_path, log=quiet)
    other = apply_env(env, Link(sw), generation=5, state_dir=tmp_path, log=quiet)
    assert teardown(env, Link(sw), generation=4, state_dir=tmp_path, log=quiet) == rooms
    assert all(sw.rooms[r]["archived"] for r in rooms.values())
    assert not any(sw.rooms[r]["archived"] for r in other.values())   # only that generation
    assert load_state(tmp_path, "env1-g4")["archived_at"]
    assert apply_env(env, Link(sw), generation=4, state_dir=tmp_path, log=quiet) == rooms
    assert not any(sw.rooms[r]["archived"] for r in rooms.values()) and len(sw.rooms) == 12
    with pytest.raises(ApplyError):
        teardown(env, Link(sw), generation=9, state_dir=tmp_path, log=quiet)


def test_update_ledger_needs_the_operator(env, sw, tmp_path):
    rooms = apply_env(env, Link(sw), generation=1, state_dir=tmp_path, log=quiet)
    update_ledger(env, Link(sw), [{"quarter": 1, "fitness": 6.9}], generation=1, state_dir=tmp_path)
    ledger = next(d for d in sw.rooms[rooms["board"]]["docs"].values() if d["name"] == LEDGER)
    assert "| 1 |" in ledger["content"] and "6.90" in ledger["content"]
    with pytest.raises(sc.SwitchError, match="403"):
        update_ledger(env, Link(sw, "finance"), [], generation=1, state_dir=tmp_path)
    with pytest.raises(ApplyError):
        update_ledger(env, Link(sw), [], generation=2, state_dir=tmp_path)


def test_check_ops_logs_what_this_switch_lacks(sw):
    logs = []
    assert check_ops(Link(sw), logs.append)["missing_required"] == []
    assert "all" in logs[-1]
    old = FakeSwitch([], missing_ops=("create_room_from_yaml", "link_rooms"))
    out = check_ops(Link(old), logs.append)
    assert out["missing_required"] == ["link_rooms"] and out["missing_optional"] == ["create_room_from_yaml"]
    assert "MISSING required ['link_rooms']" in logs[-2]


# ───────────────────────── over HTTP: Ops + switch_client ─────────────────────────


def test_ops_adapter_over_http_opens_a_stream_only_for_room_ops(env, tmp_path):
    fb = FakeBridge().start()
    try:
        aid, key = fb.add_agent("board")
        sw = FakeSwitch(ENV_AGENTS)
        for op in (*REQUIRED_OPS, "create_room_from_yaml", "get_my_context", "list_bridges", "list_room_groups",
                   "create_room_group", "add_users_to_room", "unarchive_room"):
            fb.ops_handlers[op] = (lambda name: lambda args, conn:
                                   sw.handle("board", name, args, fb.connection_rooms.get(conn)))(op)
        ops = Ops(sc.Bridge(fb.url, aid, key), "board")
        rooms = apply_env(env, ops, generation=1, state_dir=tmp_path, log=quiet)
        teardown(env, ops, generation=1, state_dir=tmp_path, log=quiet)
        ops.close()
        assert list(rooms) == ROOMS and all(r["archived"] for r in sw.rooms.values())
        by_op = {}
        for name, _, conn in fb.ops_calls:
            by_op.setdefault(name, set()).add(conn)
        assert by_op["create_room_from_yaml"] == {None} and by_op["list_rooms"] == {None}   # before any room op
        assert None not in by_op["create_room_document"] and len(fb.opens) == 1
    finally:
        fb.stop()
