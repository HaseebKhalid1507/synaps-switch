"""company/core/env.py — Env dataclass, load/save, mutation, diff, Switch template export."""
from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Stable YAML dumper — insertion order, block strings, no aliases
# ---------------------------------------------------------------------------

class _Dumper(yaml.Dumper):
    pass

def _str_rep(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)

_Dumper.add_representer(str, _str_rep)
_Dumper.add_representer(
    dict,
    lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v.items()),
)


def _dump(obj) -> str:
    return yaml.dump(obj, Dumper=_Dumper, allow_unicode=True, sort_keys=False)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class Env:
    name: str
    parent: str | None
    agents: list[dict]
    rooms: list[dict]
    company: dict   # the x-company block


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_env(path) -> Env:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Env(
        name=raw.get("x-name", "env1"),
        parent=raw.get("x-parent", None),
        agents=raw.get("agents", []),
        rooms=raw.get("rooms", []),
        company=raw.get("x-company", {}),
    )


def save_env(env: Env, path):
    doc: dict[str, Any] = {}
    doc["x-name"] = env.name
    if env.parent is not None:
        doc["x-parent"] = env.parent
    doc["agents"] = env.agents
    doc["rooms"] = env.rooms
    doc["x-company"] = env.company
    with open(path, "w") as f:
        f.write(_dump(doc))


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def apply_mutation(env: Env, m: dict) -> Env:
    """Return a NEW Env with mutation applied. name/parent set by caller."""
    e = copy.deepcopy(env)
    op = m["op"]

    if op == "set_instructions":
        agent_name = m["agent"]
        for a in e.agents:
            if a["name"] == agent_name:
                a["instructions"] = m["text"]
                break
        else:
            raise ValueError(f"agent not found: {agent_name}")

    elif op in ("add_agent", "hire"):
        if op == "hire":
            # hire: resolve persona into an agent dict
            persona_id = m["persona"]
            agent_name = m["name"]
            role_note = m.get("role_note", "")
            # Try to load roster; fall back to stub if /tmp/agency-agents not present
            try:
                from company.core.roster import load_roster, instructions_for
                roster = load_roster("/tmp/agency-agents")
                persona = roster[persona_id]
                instructions = instructions_for(persona, role_note)
                description = persona["description"]
            except Exception:
                instructions = role_note
                description = persona_id
            agent_def = {
                "name": agent_name,
                "persona": persona_id,
                "addressing": "anyone",
                "provider": "langgraph",
                "description": description,
                "instructions": instructions,
            }
        else:
            agent_def = copy.deepcopy(m["agent"])

        e.agents.append(agent_def)
        room_name = m.get("room")
        if room_name:
            for r in e.rooms:
                if r["name"] == room_name:
                    agents_list = r.setdefault("agents", [])
                    if agent_def["name"] not in agents_list:
                        agents_list.append(agent_def["name"])
                    break

    elif op in ("remove_agent", "fire"):
        agent_name = m["agent"]
        e.agents = [a for a in e.agents if a["name"] != agent_name]
        for r in e.rooms:
            if "agents" in r:
                r["agents"] = [a for a in r["agents"] if a != agent_name]

    elif op == "swap_persona":
        agent_name = m["agent"]
        new_persona_id = m["persona"]
        for a in e.agents:
            if a["name"] == agent_name:
                a["persona"] = new_persona_id
                try:
                    from company.core.roster import load_roster, instructions_for
                    roster = load_roster("/tmp/agency-agents")
                    persona = roster[new_persona_id]
                    # Preserve any existing role_note (first paragraph before ---)
                    old_instructions = a.get("instructions", "")
                    role_note = old_instructions.split("---")[0].strip() if "---" in old_instructions else old_instructions
                    a["instructions"] = instructions_for(persona, role_note)
                    a["description"] = persona["description"]
                except Exception:
                    pass
                break
        else:
            raise ValueError(f"agent not found: {agent_name}")

    elif op == "link":
        rooms_pair = list(m["rooms"])
        links: list = e.company.setdefault("links", [])
        if rooms_pair not in links:
            links.append(rooms_pair)

    elif op == "unlink":
        rooms_pair = list(m["rooms"])
        links: list = e.company.get("links", [])
        e.company["links"] = [l for l in links if l != rooms_pair]

    elif op == "set_policy":
        e.company.setdefault("policies", {})[m["key"]] = m["value"]

    elif op == "set_budget":
        e.company.setdefault("budgets", {})[m["dept"]] = m["value"]

    elif op == "set_price":
        e.company["price"] = m["value"]

    else:
        raise ValueError(f"unknown mutation op: {op}")

    return e


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def _env_to_yaml_str(env: Env) -> str:
    doc: dict[str, Any] = {}
    doc["x-name"] = env.name
    if env.parent is not None:
        doc["x-parent"] = env.parent
    doc["agents"] = env.agents
    doc["rooms"] = env.rooms
    doc["x-company"] = env.company
    return _dump(doc)


def env_diff(a: Env, b: Env) -> str:
    ya = _env_to_yaml_str(a).splitlines(keepends=True)
    yb = _env_to_yaml_str(b).splitlines(keepends=True)
    return "".join(difflib.unified_diff(ya, yb, fromfile=a.name, tofile=b.name))


# ---------------------------------------------------------------------------
# Switch template export
# ---------------------------------------------------------------------------

def to_switch_template(env: Env) -> str:
    """
    Emit a Switch room-template YAML accepted by create_room_from_yaml.
    Fields: agents (with inlined instructions), rooms, kickoff.
    Drops x-company / x-name / x-parent / persona fields.
    If an agent has a 'persona' field, instructions_for() is called to inline it.
    """
    # Resolve personas inline
    roster_cache: dict | None = None

    def _resolve_agent(a: dict) -> dict:
        nonlocal roster_cache
        out = {k: v for k, v in a.items() if k != "persona"}
        persona_id = a.get("persona")
        if persona_id:
            # Try resolving; fall back gracefully
            try:
                if roster_cache is None:
                    from company.core.roster import load_roster
                    roster_cache = load_roster("/tmp/agency-agents")
                from company.core.roster import instructions_for
                persona = roster_cache[persona_id]
                # Extract the role_note (everything before --- separator)
                existing = a.get("instructions", "")
                role_note = existing.split("---")[0].strip() if "---" in existing else existing
                out["instructions"] = instructions_for(persona, role_note)
            except Exception:
                pass  # keep existing instructions
        return out

    agents_out = [_resolve_agent(copy.deepcopy(a)) for a in env.agents]

    doc: dict[str, Any] = {}
    doc["agents"] = agents_out
    doc["rooms"] = env.rooms

    agent_names = [a["name"] for a in env.agents]
    mentions = " ".join(f"@{n}" for n in agent_names)
    doc["kickoff"] = (
        f"{mentions} — company quarter starting. "
        "Board will post the first market events. "
        "Agents: check your room and await instructions."
    )
    return _dump(doc)
