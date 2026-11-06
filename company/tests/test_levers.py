"""#31: the registry of levers the quarter reads (company/board/levers.py), and --dev-per-* vs a dev_capacity
mutation (company/run.py)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from company.board import levers
from company.board.evolve import validate_mutation
from company.core.env import apply_mutation, load_env, save_env
from company.run import seed_capacity

ROOT = Path(__file__).resolve().parents[2]
ENV1 = str(ROOT / "company/envs/env1.yaml")
QA = "testing/testing-test-automation-engineer"


@pytest.fixture
def env():
    return load_env(ENV1)


def hire(persona, room="dev", name="new"):
    return {"op": "hire", "persona": persona, "name": name, "room": room, "role_note": "duties"}


def refused(env, m):
    new, err = validate_mutation(env, m)
    return err if err and err.startswith(levers.NO_LEVER) else None


@pytest.mark.parametrize("m", [
    {"op": "link", "rooms": ["maintenance", "dev"]},
    {"op": "set_dev_capacity", "per_week": 1},
    {"op": "set_dev_capacity", "per_quarter": 4},
    hire(QA),
    {"op": "set_policy", "key": "finance_veto_usd", "value": 2000},
    {"op": "set_budget", "dept": "marketing", "value": 8000},
    {"op": "set_price", "value": 25.0},
], ids=lambda m: m["op"])
def test_levers_pass(env, m):
    assert validate_mutation(env, m)[1] is None


@pytest.mark.parametrize("m,why", [
    ({"op": "link", "rooms": ["growth", "dev"]}, "only #maintenance ↔ #dev"),
    ({"op": "link", "rooms": ["growth", "marketing"]}, "only #maintenance ↔ #dev"),
    (hire("engineering/engineering-incident-response-commander", name="incident-commander"), "testing/*"),
    (hire(QA, room="growth"), "only a testing/* persona hired into #dev"),
    ({"op": "swap_persona", "agent": "ops-maint", "persona": QA}, "testing/* reviewer in #dev"),
    ({"op": "fire", "agent": "growth"}, "nobody to answer its market events"),
    ({"op": "set_policy", "key": "dev_capacity", "value": 3}, "set_dev_capacity"),
    ({"op": "set_policy", "key": "dev_capacity_per_quarter", "value": 4}, "set_dev_capacity"),
    ({"op": "set_policy", "key": "sev1_fix_weeks", "value": 1}, "read by nothing"),
    ({"op": "set_instructions", "agent": "marketing", "text": "cite shipped features"}, "no mechanic"),
], ids=lambda x: x if isinstance(x, str) else x.get("op"))
def test_no_lever_is_refused_with_the_reason(env, m, why):
    err = refused(env, m)
    assert err and why in err and "The quarter reads only these levers: direct line, dev capacity" in err


def test_firing_and_swapping_a_reviewer_are_levers(env):
    qa = apply_mutation(env, hire(QA, name="qa"))
    assert levers.read(qa)["reviewers"] == levers.read(env)["reviewers"] + ["qa"]
    assert refused(qa, {"op": "fire", "agent": "qa"}) is None
    assert refused(qa, {"op": "swap_persona", "agent": "qa", "persona": "engineering/engineering-sre"}) is None


def test_read_is_what_the_quarter_reads(env):
    r = levers.read(env)
    assert r == {"direct_lines": [], "dev_capacity": [3, None], "reviewers": r["reviewers"],
                 "finance_veto_usd": 5000.0, "budgets": {"marketing": 10000.0, "maintenance": 4000.0,
                                                         "growth": 3000.0}, "price": 29.0}
    assert all(levers.read(apply_mutation(env, m)) == r for m in (
        {"op": "link", "rooms": ["growth", "dev"]}, {"op": "set_policy", "key": "sev1_fix_weeks", "value": 1}))
    assert levers.moved(env, apply_mutation(env, {"op": "link", "rooms": ["dev", "maintenance"]})) == ["direct_lines"]


def test_prompt_lines_are_one_per_lever():
    lines = levers.prompt_lines().splitlines()
    assert len(lines) == len(levers.LEVERS) == 6 and all(" Effect: " in x for x in lines)


# ───────────────────────── --dev-per-* compose with a dev_capacity mutation ─────────────────────────


def test_flags_set_the_seed_baseline(env):
    bare = apply_mutation(env, {"op": "set_price", "value": 29.0})
    bare.company.pop("dev_capacity")
    assert seed_capacity(bare).company.get("dev_capacity") is None                  # no flags: unchanged
    assert seed_capacity(bare, 1, 2).company["dev_capacity"] == {"per_week": 1, "per_quarter": 2}
    assert seed_capacity(env, None, 2).company["dev_capacity"] == {"per_week": 3, "per_quarter": 2}  # key by key
    assert seed_capacity(env, 1, 1).company["dev_capacity"] == {"per_week": 1, "per_quarter": 1}
    assert env.company["dev_capacity"] == {"per_week": 3}                            # a new env, not in place


def _run(tmp_path, *args):
    r = subprocess.run([sys.executable, "-m", "company.run", "--transport", "fake", "--weeks", "3",
                        "--db", str(tmp_path / "board.db"), "--state-dir", str(tmp_path / "st"), *args],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    return r


def test_the_flags_reach_the_quarter_through_the_env(tmp_path):
    r = _run(tmp_path, "--env", ENV1, "--json", "--dev-per-week", "1", "--dev-per-quarter", "1")
    assert len(json.loads(r.stdout)["fixes"]) == 1


def test_a_dev_capacity_mutation_takes_effect_in_the_trial_despite_the_flags(tmp_path):
    """The trial env carries its own x-company.dev_capacity; the flags only set env1's."""
    out = tmp_path / "out"
    script = tmp_path / "evolve.py"
    script.write_text(
        "import sys\n"
        "from company.board import evolve as E\n"
        "class P:\n"
        "    def __init__(self, roster): pass\n"
        "    def __call__(self, summary, env, *, history=(), error=None):\n"
        "        return {'diagnosis': 'one fix a quarter', 'rationale': 'more capacity',\n"
        "                'mutation': {'op': 'set_dev_capacity', 'per_week': 2, 'per_quarter': 3}}, 0.0\n"
        "E.StubProposer = P\n"
        "from company.run import main\n"
        "sys.exit(main(sys.argv[1:]))\n")
    r = subprocess.run([sys.executable, str(script), "--env", ENV1, "--generations", "1", "--transport", "fake",
                        "--weeks", "3", "--seeds", "1", "--dev-per-week", "1", "--dev-per-quarter", "1",
                        "--out", str(out), "--db", str(tmp_path / "board.db"), "--state-dir", str(tmp_path / "st"),
                        "--evolve-db", str(tmp_path / "evolve.sqlite"), "--script", "approve"],
                       cwd=ROOT, capture_output=True, text=True, timeout=300,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stderr
    recs = [json.loads(x) for x in (out / "lineage.jsonl").read_text().splitlines()]
    seed, trial = recs
    assert trial["change"] == "dev capacity: 1/week, 1/quarter → 2/week, 3/quarter"
    assert trial["outcome"] == "kept" and trial["delta"] > 0.5
    log = (out / "run.log").read_text()
    assert "synaps-dev capacity 1/week, 1/quarter" in log and "synaps-dev capacity 2/week, 3/quarter" in log
    assert load_env(out / "envs" / "env2.yaml").company["dev_capacity"] == {"per_week": 2, "per_quarter": 3}
    save_env(load_env(out / "envs" / "env2.yaml"), tmp_path / "env2.yaml")
    again = _run(tmp_path, "--env", str(tmp_path / "env2.yaml"), "--json", "--seed", "1", "--fresh")
    assert len(json.loads(again.stdout)["fixes"]) == 2         # 1 under env1's 1/week, 1/quarter
