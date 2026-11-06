"""#25: org changes matter. company/board/mechanics.py + the real quarter (company/board/quarter.py), offline."""
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from company.board import mechanics as mech
from company.board import quarter as qm
from company.core.env import apply_mutation, load_env
from company.transport import FakeTransport, Msg

ROOT = Path(__file__).resolve().parents[2]
ENV1 = str(ROOT / "company/envs/env1.yaml")
LINK = {"op": "link", "rooms": ["maintenance", "dev"]}
QA = {"op": "hire", "persona": "testing/testing-test-automation-engineer", "name": "qa", "room": "dev",
      "role_note": "QA in #dev."}


@pytest.fixture
def env():
    return load_env(ENV1)


def run(env, tmp_path, seed=42, name="q.db", **kw):
    return qm.run_quarter_full(env, seed=seed, generation=1, db=str(tmp_path / name),
                               state_dir=str(tmp_path / "state"), **kw)


def whats(r, type_=None):
    return [d["what"] for d in r.log.decisions if d.get("what") and (type_ is None or d["type"] == type_)]


def fixed(r):
    return {f["issue"]: f["week_fixed"] for f in r.log.fixes}


# ───────────────────────── links ─────────────────────────


def test_route_plan_links_and_urgency(env):
    rooms = qm.company_rooms(env, ("Haseeb",))
    inc1, inc3 = {"kind": "incident", "severity": 1}, {"kind": "incident", "severity": 3}
    feat = {"kind": "feature", "demand": 0.9}
    p = mech.route_plan(env, "ops-maint", inc1, members=rooms)
    assert p["path"] == "board_relay" and p["asker"] == "board" and p["delay"] == mech.RELAY_WEEKS
    assert "no #maintenance ↔ #dev link" in p["why"]
    linked = apply_mutation(env, LINK)
    lrooms = qm.company_rooms(linked, ("Haseeb",))
    assert "ops-maint" in lrooms["dev"]                      # the link puts ops-maint in #dev (and on Switch)
    p = mech.route_plan(linked, "ops-maint", inc1, members=lrooms)
    assert p == {**p, "path": "direct", "asker": "ops-maint", "delay": 0}
    assert mech.route_plan(linked, "ops-maint", inc3, members=lrooms)["path"] == "board_relay"   # sev3: board triages
    assert "not urgent" in mech.route_plan(linked, "ops-maint", inc3, members=lrooms)["why"]
    assert mech.route_plan(linked, "growth", feat, members=lrooms)["path"] == "board_relay"
    # a link that the room membership doesn't reflect is no direct line
    assert mech.route_plan(linked, "ops-maint", inc1, members=rooms)["path"] == "board_relay"
    assert mech.route_plan(env, "board", inc1)["delay"] == 0


def test_without_the_link_incidents_wait_for_the_board_relay(env, tmp_path):
    r = run(env, tmp_path)
    assert fixed(r)[3] == 2 and fixed(r)[7] == 2
    w = whats(r, "dispatch")
    assert "#3 (sev1) waited for board relay: raised wk1 by ops-maint in #maintenance, dispatched wk2" in w
    assert all(t["path"] == "board_relay" and t["asker"] == "board" for t in r.state["work"]["tasks"])
    assert r.state["work"]["mech"]["relayed"] == 6 and not r.state["work"]["mech"].get("direct")


def test_with_the_link_ops_maint_hands_urgent_incidents_to_synaps_dev_the_same_week(env, tmp_path):
    linked = apply_mutation(env, LINK)
    r = run(linked, tmp_path, out_dir=str(tmp_path / "out"))
    assert fixed(r)[3] == 1 and fixed(r)[7] == 1 and fixed(r)[11] == 1
    w = whats(r, "dispatch")
    assert "#3 (sev1) routed direct by ops-maint (#maintenance ↔ #dev): raised wk1, dispatched wk1" in w
    assert any(x.startswith("#15 (sev3) waited for board relay") and x.endswith("dispatched wk2") for x in w)
    tasks = {t["issue"]: t for t in r.state["work"]["tasks"]}
    assert tasks[3]["path"] == "direct" and tasks[3]["asker"] == "ops-maint"
    # the direct line is real chat: ops-maint addresses synaps-dev in #dev and synaps-dev answers ops-maint
    recs = [json.loads(x) for x in (tmp_path / "out/transcript.jsonl").read_text().splitlines()]
    posts = [x for x in recs if x["kind"] == "post"]
    ask = next(x for x in posts if x["sender"] == "ops-maint" and x["text"].startswith("@synaps-dev fix acme-shop#3"))
    assert ask["room"] == "dev" and ask["to"] == ["synaps-dev"]
    reply = next(x for x in posts if x["sender"] == "synaps-dev" and x["thread"] == ask["id"])
    assert "@ops-maint" in reply["text"] and "RESULT" in reply["text"] and reply["to"] == ["ops-maint"]
    assert not any(x["sender"] == "board" and "Relaying acme-shop#3" in x["text"] for x in posts)
    # ...and the merge still needs a person in #board
    assert any(d["type"] == "merge" and d["issue"] == 3 and d["approver"] == "Haseeb" for d in r.log.decisions)


@pytest.mark.parametrize("seed", [1, 2, 42])
def test_the_link_raises_profit_growth_and_fitness_on_the_same_seed(env, tmp_path, seed):
    a = run(env, tmp_path, seed=seed, name="a.db")
    b = run(apply_mutation(env, LINK), tmp_path, seed=seed, name="b.db")
    assert b.metrics.mean_fix_weeks < a.metrics.mean_fix_weeks
    assert b.metrics.profit > a.metrics.profit and b.metrics.growth > a.metrics.growth
    assert b.fitness > a.fitness + 1


def test_a_dept_agent_ignores_synaps_devs_result():
    from company.depts.agent import Departments
    from company.market.sim import QuarterLog
    env = apply_mutation(load_env(ENV1), LINK)
    calls = []
    t = FakeTransport(rooms=qm.company_rooms(env, ("Haseeb",)))
    Departments(env, t, QuarterLog(), llm=lambda s, p: (calls.append(1), ({"say": "ok"}, 0.0))[1], roster={})
    t.start()
    t.post("synaps-dev", "dev", 'Fixed.\n@ops-maint\nRESULT\n```json\n{"status": "proposed", "pr": "x"}\n```')
    assert calls == []
    t.post("synaps-dev", "dev", "@ops-maint which coupon code fails?")
    assert calls


# ───────────────────────── capacity ─────────────────────────


def test_capacity_comes_from_the_env_and_the_wait_is_logged(env, tmp_path):
    assert mech.capacity(env.company) == (3, None)                    # env1: x-company.dev_capacity.per_week
    env.company["dev_capacity"] = {"per_week": 1}
    r = run(apply_mutation(env, LINK), tmp_path)
    assert [t["week"] for t in r.state["work"]["tasks"]] == [1, 2, 3, 4]
    assert fixed(r)[3] == 1 and fixed(r)[7] == 2
    w = whats(r, "dispatch")
    assert "#7 (sev2) routed direct by ops-maint (#maintenance ↔ #dev): raised wk1, dispatched wk2 " \
           "(+1 wk waiting for dev capacity, 1/week)" in w
    assert "synaps-dev capacity 1/week" in whats(r, "org")[0]
    assert r.state["work"]["mech"]["capacity_weeks"] > 0
    assert any("never reached synaps-dev" in x for x in whats(r))


def test_the_cli_capacity_overrides_the_env(env, tmp_path):
    r = run(env, tmp_path, dev_per_week=2)
    weeks = [t["week"] for t in r.state["work"]["tasks"]]
    assert all(weeks.count(w) <= 2 for w in set(weeks))
    assert mech.capacity(env.company, per_week=2) == (2, None)


# ───────────────────────── testers (hire qa) ─────────────────────────


def test_quality_is_a_pure_function_of_env_seed_and_issue(env):
    qa = apply_mutation(env, QA)
    assert mech.testers(env) == ["reviewer"] and mech.testers(qa) == ["reviewer", "qa"]
    for n in (3, 7, 11, 15):
        a, b = mech.quality(env, 42, n, "incident"), mech.quality(qa, 42, n, "incident")
        assert a == mech.quality(env, 42, n, "incident")
        assert a["latent"] == b["latent"]                         # same luck: only the org differs
        assert a["extra_rounds"] == 0 and b["extra_rounds"] == 1
        if a["caught_by"]:
            assert b["caught_by"]                                  # a second tester never catches less
    assert not mech.quality(env, 42, 20, "feature")["latent"]


def test_hiring_qa_catches_regressions_but_slows_merges(env, tmp_path):
    base = apply_mutation(env, LINK)
    qa = apply_mutation(base, QA)
    a = run(base, tmp_path, seed=1, name="a.db")
    b = run(qa, tmp_path, seed=1, name="b.db")
    ma, mb = a.state["work"]["mech"], b.state["work"]["mech"]
    assert ma.get("reopened") == 1 and not mb.get("reopened")
    assert mb["review_rounds"] == 6 and mb["slipped"] >= 1 and not ma.get("review_rounds")
    assert any(x.startswith("#7: fix regressed after the wk1 merge") for x in whats(a, "regression"))
    assert any("qa caught a regression" in x or "reviewer caught a regression" in x for x in whats(b))
    assert any("the merge slips to wk" in x for x in whats(b, "review_slip"))
    assert b.state["work"]["review_usd"] > a.state["work"]["review_usd"]


def test_a_held_merge_survives_a_crash_and_resume(env, tmp_path):
    qa = apply_mutation(apply_mutation(env, LINK), QA)
    clean = run(qa, tmp_path, seed=1, name="clean.db")
    assert clean.state["work"]["mech"].get("slipped")

    class Crash(Exception):
        pass

    def hook(n, w):
        if n == "work" and w == 2:
            raise Crash
    with pytest.raises(Crash):
        run(qa, tmp_path, seed=1, name="r.db", hook=hook)
    r = run(qa, tmp_path, seed=1, name="r.db")
    assert r.resumed == "resumed"
    assert asdict(r.metrics) == asdict(clean.metrics) and asdict(r.log) == asdict(clean.log)


# ───────────────────────── determinism, policies, explanations ─────────────────────────


def test_mechanics_are_deterministic(env, tmp_path):
    e = apply_mutation(env, LINK)
    a, b = run(e, tmp_path, seed=2, name="a.db"), run(e, tmp_path, seed=2, name="b.db")
    assert asdict(a.log) == asdict(b.log) and asdict(a.metrics) == asdict(b.metrics)
    assert mech.draw(2, "regress", 7) == mech.draw(2, "regress", 7) != mech.draw(3, "regress", 7)


def test_the_quarter_explains_what_reached_the_market(env, tmp_path):
    r = run(env, tmp_path)
    org = whats(r, "org")
    assert org == ["org: direct lines none (every hand-off to #dev goes through #board); synaps-dev capacity "
                   "3/week; PR reviewers in #dev: reviewer"]
    mi = whats(r, "market_inputs")[0]
    assert mi.startswith("to the market: ") and "approved spend marketing $" in mi and "price $" in mi
    m = next(d for d in r.log.decisions if d["type"] == "mechanics")
    assert m["stats"]["relayed"] == 6 and "items relayed by the board: 6" in m["what"]
    assert sum(float(c["spend"]) for c in r.log.campaigns) > 0             # budgets flow to settle


def test_why_moved_names_the_mechanics(env, tmp_path):
    a = run(env, tmp_path, name="a.db")
    b = run(apply_mutation(env, LINK), tmp_path, name="b.db")
    ev = lambda r: {"metrics": asdict(r.metrics), "per_seed": [{"log": asdict(r.log)}]}  # noqa: E731
    text = mech.why_moved(ev(a), ev(b))
    assert text.startswith("fix time ") and "items handed to synaps-dev directly 0 → 3" in text
    assert "items relayed by the board 6 → 3" in text


# ───────────────────────── the demo story, offline ─────────────────────────


def test_three_generations_keep_the_link_revert_qa_and_a_human_rejects(tmp_path):
    r = subprocess.run([sys.executable, "-m", "company.run", "--env", ENV1, "--generations", "3", "--transport", "fake",
                        "--seeds", "1,2", "--out", str(tmp_path / "out"), "--db", str(tmp_path / "board.db"),
                        "--state-dir", str(tmp_path / "st"), "--evolve-db", str(tmp_path / "evolve.sqlite")],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    recs = [json.loads(x) for x in (tmp_path / "out/lineage.jsonl").read_text().splitlines()]
    assert [x["outcome"] for x in recs] == ["seed", "kept", "reverted", "rejected"]
    link, qa, veto = recs[1:]
    assert link["mutation"]["op"] == "link" and link["delta"] > 2
    assert "items handed to synaps-dev directly 0 → 6" in link["reason"]
    assert qa["mutation"]["op"] == "hire" and qa["delta"] < 0 and "merges slipped a week by review" in qa["reason"]
    assert veto["mutation"]["op"] == "set_policy" and veto["approver"] == "Haseeb" and veto["fitness"] is None
    assert "| 1 | env2 | env1 | link #maintenance ↔ #dev |" in r.stdout and "✅ kept" in r.stdout
