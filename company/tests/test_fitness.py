"""Tests for company/core/fitness.py, lineage.py, and roster.py"""
import os
import tempfile

import pytest
import yaml

from company.core.fitness import fitness
from company.core.lineage import append_lineage, read_lineage
from company.core.roster import get_roster, load_roster, roster_dir, search, instructions_for
from company.core.env import load_env, apply_mutation, to_switch_template
from company.market.sim import Market, QuarterLog, Metrics, default_issues


WEIGHTS_PATH = "company/fitness.yaml"
ROSTER_ROOT = roster_dir()          # $COMPANY_ROSTER_DIR or company/vendor/agency-agents
needs_roster = pytest.mark.skipif(not ROSTER_ROOT.is_dir(), reason="no Agency roster: run company/fetch_roster.sh")
COMPANY = {"weeks_per_quarter": 4, "price": 29.0}
ISSUES = default_issues()


def _load_weights() -> dict:
    with open(WEIGHTS_PATH) as f:
        return yaml.safe_load(f)["weights"]


def _metrics(**kwargs) -> Metrics:
    defaults = dict(
        revenue=50000, costs=42000, profit=8000, customers=220,
        churn_rate=0.015, growth=0.12, open_incident_severity=0.0,
        mean_fix_weeks=0.8, llm_cost_usd=6.3,
    )
    defaults.update(kwargs)
    return Metrics(**defaults)


# ------------------------------------------------------------------
# fitness.py
# ------------------------------------------------------------------

def test_fitness_good_quarter_positive():
    w = _load_weights()
    m = _metrics(profit=8000, growth=0.12, open_incident_severity=0.0, llm_cost_usd=6.3)
    score = fitness(m, w)
    assert score > 0


def test_fitness_bad_quarter_negative():
    w = _load_weights()
    m = _metrics(profit=-3000, growth=0.0, open_incident_severity=6.0, llm_cost_usd=6.2)
    score = fitness(m, w)
    assert score < 0


def test_fitness_open_incidents_hurt():
    w = _load_weights()
    m_clean = _metrics(open_incident_severity=0.0)
    m_dirty = _metrics(open_incident_severity=6.0)
    assert fitness(m_clean, w) > fitness(m_dirty, w)


def test_fitness_llm_cost_hurts():
    w = _load_weights()
    m_cheap = _metrics(llm_cost_usd=1.0)
    m_pricey = _metrics(llm_cost_usd=20.0)
    assert fitness(m_cheap, w) > fitness(m_pricey, w)


def test_fitness_weights_yaml_has_required_keys():
    w = _load_weights()
    for key in ("profit", "growth", "incident_penalty", "llm_cost"):
        assert key in w, f"missing weight: {key}"


def test_fitness_deterministic():
    w = _load_weights()
    m = _metrics()
    assert fitness(m, w) == fitness(m, w)


# ------------------------------------------------------------------
# lineage.py
# ------------------------------------------------------------------

def test_lineage_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "lineage.jsonl")
        record = {
            "gen": 1, "env": "env2", "parent": "env1",
            "mutation": {"op": "link", "rooms": ["maintenance", "dev"]},
            "rationale": "Faster incident routing",
            "approver": "Aditya",
            "fitness": 6.9, "parent_fitness": -11.4, "kept": True,
            "ts": "2024-01-01T00:00:00",
        }
        append_lineage(path, record)
        records = read_lineage(path)
        assert len(records) == 1
        assert records[0]["gen"] == 1
        assert records[0]["fitness"] == 6.9
        assert records[0]["kept"] is True


def test_lineage_multiple_records():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "lineage.jsonl")
        for i in range(5):
            append_lineage(path, {"gen": i, "fitness": float(i)})
        records = read_lineage(path)
        assert len(records) == 5
        assert records[2]["gen"] == 2


def test_lineage_missing_file_returns_empty():
    records = read_lineage("/tmp/__nonexistent_lineage_xyz__.jsonl")
    assert records == []


def test_lineage_creates_parent_dirs():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "nested", "dir", "lineage.jsonl")
        append_lineage(path, {"gen": 0})
        assert os.path.exists(path)


# ------------------------------------------------------------------
# roster.py
# ------------------------------------------------------------------

@needs_roster
def test_roster_loads_over_200():
    roster = load_roster(ROSTER_ROOT)
    assert len(roster) > 200, f"only loaded {len(roster)} personas"


@needs_roster
def test_roster_persona_structure():
    roster = load_roster(ROSTER_ROOT)
    for pid, p in list(roster.items())[:5]:
        assert p["id"] == pid
        assert "/" in pid
        assert p["division"]
        assert p["name"]
        assert p["path"]
        assert isinstance(p["body"], str)


@needs_roster
def test_roster_no_excluded_divisions():
    roster = load_roster(ROSTER_ROOT)
    for pid in roster:
        division = pid.split("/")[0]
        assert division not in {"integrations", "strategy", "examples", "scripts"}, \
            f"excluded division leaked: {pid}"


@needs_roster
def test_roster_search_by_division():
    roster = load_roster(ROSTER_ROOT)
    results = search(roster, division="finance")
    assert len(results) > 0
    for r in results:
        assert r["division"] == "finance"


@needs_roster
def test_roster_search_by_text():
    roster = load_roster(ROSTER_ROOT)
    results = search(roster, text="incident")
    assert len(results) > 0


@needs_roster
def test_roster_search_combined():
    roster = load_roster(ROSTER_ROOT)
    results = search(roster, division="support", text="infrastructure")
    assert any("infrastructure" in r["id"] for r in results)


@needs_roster
def test_instructions_for_combines_role_and_body():
    roster = load_roster(ROSTER_ROOT)
    persona = roster["support/support-infrastructure-maintainer"]
    role_note = "You run maintenance for acme-shop. Address @board for escalations."
    out = instructions_for(persona, role_note)
    assert role_note in out
    assert "---" in out
    assert len(out) <= 4100  # ~4000 char limit with some slack


@needs_roster
def test_instructions_for_trims_long_body():
    roster = load_roster(ROSTER_ROOT)
    # Find a persona with a long body
    long_persona = max(roster.values(), key=lambda p: len(p["body"]))
    role_note = "Short note."
    out = instructions_for(long_persona, role_note)
    assert len(out) <= 4100


def test_get_roster_cached_and_env_override(tmp_path, monkeypatch):
    d = tmp_path / "agency"
    (d / "testing").mkdir(parents=True)
    (d / "testing" / "testing-x.md").write_text("---\nname: X\ndescription: d\n---\nbody\n")
    monkeypatch.setenv("COMPANY_ROSTER_DIR", str(d))
    assert roster_dir() == d
    r = get_roster()
    assert list(r) == ["testing/testing-x"]
    assert get_roster() is r                       # loaded once
    assert get_roster(tmp_path / "missing") == {}


# ------------------------------------------------------------------
# hire / swap_persona / fire mutation ops
# ------------------------------------------------------------------

def test_hire_mutation():
    env = load_env("company/envs/env1.yaml")
    env2 = apply_mutation(env, {
        "op": "hire",
        "persona": "testing/testing-reality-checker",
        "name": "qa-extra",
        "room": "dev",
        "role_note": "Review PRs in #dev. Address @synaps-dev with feedback.",
    })
    names = [a["name"] for a in env2.agents]
    assert "qa-extra" in names
    for r in env2.rooms:
        if r["name"] == "dev":
            assert "qa-extra" in r["agents"]
    # persona field stored on agent
    for a in env2.agents:
        if a["name"] == "qa-extra":
            assert a["persona"] == "testing/testing-reality-checker"


def test_swap_persona_mutation():
    env = load_env("company/envs/env1.yaml")
    env2 = apply_mutation(env, {
        "op": "swap_persona",
        "agent": "marketing",
        "persona": "marketing/marketing-social-media-strategist",
    })
    for a in env2.agents:
        if a["name"] == "marketing":
            assert a["persona"] == "marketing/marketing-social-media-strategist"
            break


def test_fire_removes_agent():
    env = load_env("company/envs/env1.yaml")
    env2 = apply_mutation(env, {"op": "fire", "agent": "reviewer"})
    names = [a["name"] for a in env2.agents]
    assert "reviewer" not in names
    for r in env2.rooms:
        assert "reviewer" not in r.get("agents", [])


# ------------------------------------------------------------------
# env1.yaml personas all resolve
# ------------------------------------------------------------------

@needs_roster
def test_env1_personas_all_resolve():
    roster = load_roster(ROSTER_ROOT)
    env = load_env("company/envs/env1.yaml")
    for agent in env.agents:
        persona_id = agent.get("persona")
        if persona_id:
            assert persona_id in roster, f"persona not found in roster: {persona_id}"
