"""Tests for company/core/env.py"""
import os
import tempfile

import pytest
import yaml

from company.core.env import (
    Env, apply_mutation, env_diff, load_env, save_env, to_switch_template,
)

SAMPLE_ENV = Env(
    name="env1",
    parent=None,
    agents=[
        {"name": "board", "addressing": "anyone", "instructions": "Run the board."},
        {"name": "ops-maint", "addressing": "anyone", "instructions": "Triage incidents."},
        {"name": "marketing", "addressing": "anyone", "instructions": "Run campaigns."},
    ],
    rooms=[
        {"name": "board", "agents": ["board"], "users": ["{$creator}"]},
        {"name": "maintenance", "agents": ["ops-maint"], "users": ["{$creator}"]},
        {"name": "marketing", "agents": ["marketing"], "users": ["{$creator}"]},
    ],
    company={
        "links": [],
        "budgets": {"marketing": 10000, "maintenance": 4000},
        "policies": {"finance_veto_usd": 5000},
        "weeks_per_quarter": 4,
        "price": 29.0,
    },
)


def _copy(env: Env) -> Env:
    import copy
    return copy.deepcopy(env)


# ------------------------------------------------------------------
# load_env / save_env roundtrip
# ------------------------------------------------------------------

def test_save_load_roundtrip():
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        path = f.name
    try:
        save_env(SAMPLE_ENV, path)
        loaded = load_env(path)
        assert loaded.name == SAMPLE_ENV.name
        assert loaded.parent == SAMPLE_ENV.parent
        assert len(loaded.agents) == len(SAMPLE_ENV.agents)
        assert len(loaded.rooms) == len(SAMPLE_ENV.rooms)
        assert loaded.company["price"] == 29.0
    finally:
        os.unlink(path)


def test_load_env1_yaml():
    env = load_env("company/envs/env1.yaml")
    assert env.name == "env1"
    room_names = [r["name"] for r in env.rooms]
    for expected in ["board", "dev", "maintenance", "growth", "marketing", "finance"]:
        assert expected in room_names, f"missing room {expected}"
    agent_names = [a["name"] for a in env.agents]
    for expected in ["board", "ops-maint", "growth", "marketing", "finance", "synaps-dev"]:
        assert expected in agent_names, f"missing agent {expected}"


# ------------------------------------------------------------------
# Mutation ops
# ------------------------------------------------------------------

def test_set_instructions():
    new_env = apply_mutation(_copy(SAMPLE_ENV), {
        "op": "set_instructions", "agent": "marketing", "text": "New campaign rules."
    })
    for a in new_env.agents:
        if a["name"] == "marketing":
            assert a["instructions"] == "New campaign rules."
    # original unchanged
    for a in SAMPLE_ENV.agents:
        if a["name"] == "marketing":
            assert a["instructions"] != "New campaign rules."


def test_add_agent():
    new_env = apply_mutation(_copy(SAMPLE_ENV), {
        "op": "add_agent",
        "agent": {"name": "qa", "addressing": "anyone", "instructions": "Test everything."},
        "room": "maintenance",
    })
    names = [a["name"] for a in new_env.agents]
    assert "qa" in names
    for r in new_env.rooms:
        if r["name"] == "maintenance":
            assert "qa" in r["agents"]


def test_remove_agent():
    new_env = apply_mutation(_copy(SAMPLE_ENV), {"op": "remove_agent", "agent": "ops-maint"})
    names = [a["name"] for a in new_env.agents]
    assert "ops-maint" not in names
    for r in new_env.rooms:
        assert "ops-maint" not in r.get("agents", [])


def test_fire_alias():
    new_env = apply_mutation(_copy(SAMPLE_ENV), {"op": "fire", "agent": "ops-maint"})
    names = [a["name"] for a in new_env.agents]
    assert "ops-maint" not in names


def test_link_unlink():
    env2 = apply_mutation(_copy(SAMPLE_ENV), {"op": "link", "rooms": ["maintenance", "dev"]})
    assert ["maintenance", "dev"] in env2.company["links"]
    # idempotent
    env3 = apply_mutation(env2, {"op": "link", "rooms": ["maintenance", "dev"]})
    assert env3.company["links"].count(["maintenance", "dev"]) == 1
    env4 = apply_mutation(env3, {"op": "unlink", "rooms": ["maintenance", "dev"]})
    assert ["maintenance", "dev"] not in env4.company["links"]


def test_set_policy():
    new_env = apply_mutation(_copy(SAMPLE_ENV), {"op": "set_policy", "key": "finance_veto_usd", "value": 2000})
    assert new_env.company["policies"]["finance_veto_usd"] == 2000


def test_set_budget():
    new_env = apply_mutation(_copy(SAMPLE_ENV), {"op": "set_budget", "dept": "marketing", "value": 8000})
    assert new_env.company["budgets"]["marketing"] == 8000


def test_set_price():
    new_env = apply_mutation(_copy(SAMPLE_ENV), {"op": "set_price", "value": 39.0})
    assert new_env.company["price"] == 39.0


def test_mutation_does_not_mutate_original():
    orig_price = SAMPLE_ENV.company["price"]
    apply_mutation(_copy(SAMPLE_ENV), {"op": "set_price", "value": 99.0})
    assert SAMPLE_ENV.company["price"] == orig_price


# ------------------------------------------------------------------
# env_diff
# ------------------------------------------------------------------

def test_env_diff_shows_change():
    env2 = apply_mutation(_copy(SAMPLE_ENV), {"op": "set_price", "value": 39.0})
    env2.name = "env2"
    diff = env_diff(SAMPLE_ENV, env2)
    assert "39.0" in diff
    assert "29.0" in diff
    assert diff.startswith("---")


def test_env_diff_identical_is_empty():
    diff = env_diff(SAMPLE_ENV, _copy(SAMPLE_ENV))
    assert diff == ""


# ------------------------------------------------------------------
# to_switch_template
# ------------------------------------------------------------------

def test_to_switch_template_valid_yaml():
    tpl = to_switch_template(SAMPLE_ENV)
    parsed = yaml.safe_load(tpl)
    assert isinstance(parsed, dict)


def test_to_switch_template_no_x_company():
    tpl = to_switch_template(SAMPLE_ENV)
    assert "x-company" not in tpl
    assert "x-name" not in tpl


def test_to_switch_template_has_required_fields():
    tpl = to_switch_template(SAMPLE_ENV)
    parsed = yaml.safe_load(tpl)
    assert "agents" in parsed
    assert "rooms" in parsed
    assert "kickoff" in parsed


def test_to_switch_template_no_persona_field():
    """Persona field must not leak into the Switch template."""
    env = load_env("company/envs/env1.yaml")
    tpl = to_switch_template(env)
    parsed = yaml.safe_load(tpl)
    for agent in parsed["agents"]:
        assert "persona" not in agent, f"agent {agent['name']} has persona field"
