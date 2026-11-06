"""Tests for company/board/evolve.py (outer loop), stub_quarter.py and company/lineage_report.py."""
import json
import sys

import pytest

from company.board import evolve as E
from company.board.evolve import (DEFAULT_SCRIPT, LLMProposer, Paused, ScriptedGate, StubProposer, TransportGate,
                                  classify, evolve, validate_mutation)
from company.board.stub_quarter import run_quarter as stub_run
from company.core.env import apply_mutation, load_env
from company.core.lineage import read_lineage
from company.lineage_report import main as report_main, render
from company.transport import FakeTransport

ENV1 = "company/envs/env1.yaml"


def _env():
    return load_env(ENV1)


def _strip(recs):
    return [{k: v for k, v in r.items() if k != "ts"} for r in recs]


def _run(tmp_path, name="a", **kw):
    d = tmp_path / name
    kw.setdefault("gate", ScriptedGate(DEFAULT_SCRIPT))
    kw.setdefault("generations", 3)
    return evolve(ENV1, seeds=(1, 2), run_quarter=kw.pop("run_quarter", stub_run), lineage_path=d / "lineage.jsonl",
                  envs_dir=d / "envs", checkpoint=kw.pop("checkpoint", d / "cp.sqlite"), roster=kw.pop("roster", {}),
                  **kw), d


class Counting:
    def __init__(self, fn=stub_run):
        self.fn, self.calls = fn, []

    def __call__(self, env, *, seed, generation):
        self.calls.append((env.name, seed, generation))
        return self.fn(env, seed=seed, generation=generation)


# ───────────────────────── stub quarter ─────────────────────────


def test_stub_quarter_is_deterministic_and_org_changes_move_it():
    env = _env()
    m1, l1 = stub_run(env, seed=1, generation=1)
    m2, l2 = stub_run(env, seed=1, generation=7)
    assert m1 == m2 and l1 == l2
    linked = apply_mutation(env, {"op": "link", "rooms": ["maintenance", "dev"]})
    for seed in (1, 2):
        base, _ = stub_run(env, seed=seed)
        fast, _ = stub_run(linked, seed=seed)
        assert fast.mean_fix_weeks < base.mean_fix_weeks
    qa = apply_mutation(linked, {"op": "hire", "persona": "testing/testing-test-automation-engineer", "name": "qa",
                                 "room": "dev", "role_note": "QA"})
    assert stub_run(qa, seed=1)[1].llm_cost_usd > stub_run(linked, seed=1)[1].llm_cost_usd


def test_evolve_does_not_import_quarter_at_import_time():
    import subprocess
    code = ("import sys, company.board.evolve, company.board.stub_quarter; "
            "sys.exit(int('company.board.quarter' in sys.modules))")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


# ───────────────────────── the offline Done-when ─────────────────────────


def test_offline_three_generations_with_a_reject_and_a_revert(tmp_path):
    res, d = _run(tmp_path)
    recs = read_lineage(d / "lineage.jsonl")
    assert [r["outcome"] for r in recs] == ["seed", "kept", "reverted", "rejected"]
    assert [r["env"] for r in recs] == ["env1", "env2", "env3", "env4"]
    assert [r["parent"] for r in recs] == [None, "env1", "env2", "env2"]
    assert recs[1]["fitness"] > recs[1]["parent_fitness"] and recs[1]["kept"]
    assert recs[2]["fitness"] <= recs[2]["parent_fitness"] and not recs[2]["kept"]
    assert "< parent" in recs[2]["reason"] and recs[2]["delta"] < 0
    assert recs[3]["fitness"] is None and recs[3]["approver"] == "Haseeb" and "campaign" in recs[3]["reason"]
    assert recs[1]["gate"]["ignored"][0]["why"] == "sender is an agent"
    assert sorted(p.name for p in (d / "envs").iterdir()) == ["env1.yaml", "env2.yaml"]
    assert load_env(d / "envs" / "env2.yaml").parent == "env1"
    assert res["best"] == "env2" and res["stop_reason"] == "3 generations done"
    md = render(recs)
    assert "| 2 | env3 | env2 | hire qa" in md and "reverted" in md and "rejected (human)" in md


def test_offline_run_is_deterministic(tmp_path):
    r1, d1 = _run(tmp_path, "a")
    r2, d2 = _run(tmp_path, "b")
    a, b = read_lineage(d1 / "lineage.jsonl"), read_lineage(d2 / "lineage.jsonl")
    assert _strip(a) == _strip(b)
    assert render(a) == render(b)
    assert (d1 / "envs" / "env2.yaml").read_text() == (d2 / "envs" / "env2.yaml").read_text()


def test_cli_offline_writes_lineage_table_and_envs(tmp_path, capsys):
    out = tmp_path / "out"
    assert E.main(["--env", ENV1, "--generations", "3", "--offline", "--out", str(out),
                   "--checkpoint", str(tmp_path / "cp.sqlite")]) == 0
    assert (out / "lineage.md").read_text().count("\n| ") == 5          # header + 4 rows
    assert (out / "envs" / "env2.yaml").exists() and (out / "run.log").exists()
    capsys.readouterr()
    assert report_main([str(out / "lineage.jsonl")]) == 0
    assert "env2" in capsys.readouterr().out


# ───────────────────────── gate ─────────────────────────


def test_classify_only_an_approving_person_decides():
    th = "t1"
    base = {"thread": th, "id": "m"}
    c = lambda v: classify({**base, **v}, thread=th, approvers=("haseeb",), timeout_s=60)  # noqa: E731
    assert c({"sender": "board", "sender_is_agent": True, "text": "@board approve"})["why"] == "sender is an agent"
    assert c({"sender": "haseeb", "text": "approve"})["why"] == "sender is an agent"          # no flag: fail closed
    assert c({"sender": "intern", "sender_is_agent": False, "text": "approve"})["why"] == "not on the approvers list"
    assert c({"sender": "Haseeb", "sender_is_agent": False, "text": "@board why?"})["why"] == "not approve/reject"
    assert c({"sender": "Haseeb", "sender_is_agent": False, "text": "approve", "thread": "t2"})["why"] == "another thread"
    ok = c({"sender": "Haseeb", "sender_is_agent": False, "text": "@board approve"})
    assert ok["decision"] == "approve" and ok["approver"] == "Haseeb"
    no = c({"sender": "@haseeb", "sender_is_agent": False, "text": "@board reject: too slow"})
    assert no["decision"] == "reject" and no["reason"] == "too slow"
    assert classify({"timeout": True}, thread=th, approvers=(), timeout_s=60)["decision"] == "timeout"


def _fake(approvers=("haseeb",)):
    t = FakeTransport(rooms={"board": ["board", "Haseeb", "intern", "ops-maint"]})
    gate = TransportGate(t, board="board", room="board", approvers=approvers)
    return t, gate


def test_gate_over_transport_only_a_human_approver_resumes(tmp_path):
    t, gate = _fake()

    def human(msg):
        if msg.sender != "board" or "Generation" not in msg.text:
            return
        th = msg.thread or msg.id
        t.post("ops-maint", "board", "@board approve", thread=th)               # an agent
        t.say("intern", "board", "@board approve", thread=th)                   # a person, not an approver
        t.say("Haseeb", "board", "@board what does it cost?", thread=th)        # an approver, no decision
        t.say("Haseeb", "board", "@board approve", thread=th)

    t.on_message("Haseeb", human)
    t.start()
    runner = Counting()
    res, d = _run(tmp_path, gate=gate, generations=1, run_quarter=runner, gate_timeout_s=30)
    rec = read_lineage(d / "lineage.jsonl")[-1]
    assert rec["outcome"] in ("kept", "reverted") and rec["approver"] == "Haseeb"
    assert [i["why"] for i in rec["gate"]["ignored"]] == ["sender is an agent", "not on the approvers list",
                                                         "not approve/reject"]
    assert len(runner.calls) == 4                                             # baseline + trial, 2 seeds
    posts = [r for r in t.records if r["kind"] == "post" and r["sender"] == "board"]
    assert len(posts) == 2                                                    # the ask (once) + the outcome
    assert "```diff" in posts[0]["text"] and "@haseeb" in posts[0]["text"]
    assert posts[1]["thread"] == posts[0]["id"] and posts[1]["text"].startswith(("Kept", "Reverted"))


def test_gate_agent_approve_never_resumes_and_timeout_auto_rejects(tmp_path):
    t, gate = _fake()

    def agent_only(msg):
        if msg.sender == "board" and "Generation" in msg.text:
            t.post("ops-maint", "board", "@board approve", thread=msg.thread or msg.id)

    t.on_message("Haseeb", agent_only)
    t.start()
    runner = Counting()
    res, d = _run(tmp_path, gate=gate, generations=1, run_quarter=runner, gate_timeout_s=0.3)
    rec = read_lineage(d / "lineage.jsonl")[-1]
    assert rec["outcome"] == "timeout" and not rec["kept"] and rec["approver"] is None
    assert "auto-reject" in rec["reason"] and rec["fitness"] is None
    assert [i["sender"] for i in rec["gate"]["ignored"]] == ["ops-maint"]
    assert len(runner.calls) == 2                                             # baseline only: never trialed
    assert res["best"] == "env1"


def test_scripted_silence_is_a_timeout(tmp_path):
    res, d = _run(tmp_path, gate=ScriptedGate([[]]), generations=1)
    assert read_lineage(d / "lineage.jsonl")[-1]["outcome"] == "timeout"


# ───────────────────────── select ─────────────────────────


class Scripted:
    """A proposer that returns the given mutations in order (one per call)."""

    def __init__(self, *muts):
        self.muts, self.calls = list(muts), []

    def __call__(self, summary, env, *, history=(), error=None):
        self.calls.append({"env": env.name, "error": error, "history": list(history)})
        m = self.muts.pop(0)
        return {"diagnosis": "d", "mutation": m, "rationale": f"r{len(self.calls)}"}, 0.001


def test_select_reverts_a_worse_env_and_the_next_gen_builds_on_the_parent(tmp_path):
    qa = {"op": "hire", "persona": "testing/testing-test-automation-engineer", "name": "qa", "room": "dev",
          "role_note": "QA"}                      # unlinked, a tester only slows the merges: -1.67 on the stub quarter
    prop = Scripted(qa, {"op": "link", "rooms": ["maintenance", "dev"]})
    res, d = _run(tmp_path, propose=prop, gate=ScriptedGate([["approve"], ["approve"]]), generations=2)
    recs = read_lineage(d / "lineage.jsonl")
    assert [r["outcome"] for r in recs] == ["seed", "reverted", "kept"]
    assert recs[1]["reason"].startswith("fitness") and recs[1]["delta"] < 0
    assert recs[2]["parent"] == "env1" and prop.calls[1]["env"] == "env1"
    assert prop.calls[1]["history"][0]["outcome"] == "reverted"
    assert sorted(p.name for p in (d / "envs").iterdir()) == ["env1.yaml", "env3.yaml"]
    assert load_env(d / "envs" / "env3.yaml").parent == "env1"
    assert recs[2]["cost_usd"]["proposal"] == pytest.approx(0.001)


def test_invalid_proposal_is_re_asked_once(tmp_path):
    prop = Scripted({"op": "fire", "agent": "nobody"}, {"op": "link", "rooms": ["maintenance", "dev"]})
    gate = ScriptedGate([["approve"]])
    _run(tmp_path, propose=prop, gate=gate, generations=1)
    assert "no agent 'nobody'" in prop.calls[1]["error"]
    assert len(gate.asked) == 1 and gate.asked[0]["change"] == "link #maintenance ↔ #dev"


def test_invalid_twice_is_recorded_and_never_gated(tmp_path):
    prop = Scripted({"op": "fire", "agent": "board"}, {"op": "teleport"})
    gate = ScriptedGate([["approve"]])
    res, d = _run(tmp_path, propose=prop, gate=gate, generations=1)
    rec = read_lineage(d / "lineage.jsonl")[-1]
    assert rec["outcome"] == "invalid" and "unknown op" in rec["reason"] and gate.asked == []


def test_validate_mutation():
    env = _env()
    roster = {"testing/testing-api-tester": {"name": "API Tester", "description": "", "body": "b"}}
    v = lambda m, **kw: validate_mutation(env, m, roster=roster, **kw)[1]  # noqa: E731
    assert v({"op": "teleport"}).startswith("unknown op")
    assert "cannot be fired" in v({"op": "fire", "agent": "synaps-dev"})
    assert v({"op": "link", "rooms": ["dev", "dev"]})
    assert v({"op": "unlink", "rooms": ["maintenance", "dev"]}) == "['maintenance', 'dev'] are not linked"
    assert "not in the Agency roster" in v({"op": "hire", "persona": "x/y", "name": "qa", "room": "dev"})
    assert "already exists" in v({"op": "hire", "persona": "testing/testing-api-tester", "name": "growth",
                                  "room": "dev"})
    assert v({"op": "set_policy", "key": "finance_veto_usd", "value": "2000"})
    assert v({"op": "set_policy", "key": "finance_veto_usd", "value": 5000}) == "the mutation changes nothing"
    assert v({"op": "set_budget", "dept": "growth", "value": 1}, tried=[
        {"gen": 1, "outcome": "rejected", "mutation": {"op": "set_budget", "dept": "growth", "value": 1}}
    ]).startswith("already tried in generation 1")
    new, err = validate_mutation(env, {"op": "hire", "persona": "testing/testing-api-tester", "name": "qa",
                                       "room": "dev"}, roster=roster)
    assert err is None and "qa" in [a["name"] for a in new.agents]
    linked = apply_mutation(env, {"op": "link", "rooms": ["dev", "maintenance"]})
    assert validate_mutation(linked, {"op": "unlink", "rooms": ["maintenance", "dev"]}, roster=roster)[1] is None


# ───────────────────────── LLM proposer ─────────────────────────


def test_llm_proposer_uses_the_board_persona_and_books_cost(tmp_path):
    from company.llm import LLMError
    seen = []

    def llm(system, prompt):
        seen.append((system, prompt))
        if len(seen) == 1:
            raise LLMError("no JSON object after 2 attempts", 0.003)
        return {"diagnosis": "fixes wait in #board", "mutation": {"op": "link", "rooms": ["maintenance", "dev"]},
                "rationale": "direct line"}, 0.002

    prop = LLMProposer(llm=llm, roster={})
    res, d = _run(tmp_path, propose=prop, gate=ScriptedGate([["approve"]]), generations=1)
    rec = read_lineage(d / "lineage.jsonl")[-1]
    system, prompt = seen[0]
    assert "You are the board agent for acme-shop" in system and "exactly ONE change" in system
    assert "fitness" in prompt and "testing/testing-test-automation-engineer" in prompt and "Highlights:" in prompt
    assert "previous proposal was invalid: proposer failed: LLMError" in seen[1][1]
    assert rec["cost_usd"]["proposal"] == pytest.approx(0.005) and rec["diagnosis"] == "fixes wait in #board"


def test_llm_proposer_accepts_a_flat_mutation():
    prop = LLMProposer(llm=lambda s, p: ({"op": "set_price", "value": 31, "rationale": "r"}, 0.0), roster={})
    out, _ = prop(E.summarize(_env(), E.evaluate(_env(), [1], stub_run, E.load_weights(), 1)), _env())
    assert out["mutation"] == {"op": "set_price", "value": 31}


# ───────────────────────── checkpoints ─────────────────────────


def test_resume_from_checkpoint_while_paused_at_the_gate(tmp_path):
    full, d0 = _run(tmp_path, "ref")
    cp = tmp_path / "kill" / "cp.sqlite"
    g1 = ScriptedGate(DEFAULT_SCRIPT, kill_after_ask={1})
    with pytest.raises(Paused):
        _run(tmp_path, "kill", gate=g1, checkpoint=cp)
    d = tmp_path / "kill"
    assert [r["gen"] for r in read_lineage(d / "lineage.jsonl")] == [0]
    assert [p["gen"] for p in g1.asked] == [1]

    # a new process: same checkpoint, a fresh gate; gen 1's ask is NOT posted again
    g2 = ScriptedGate(DEFAULT_SCRIPT)
    runner = Counting()
    res, _ = _run(tmp_path, "kill", gate=g2, checkpoint=cp, run_quarter=runner)
    assert [p["gen"] for p in g2.asked] == [2, 3]
    assert ("env1", 1, 1) not in runner.calls                                # the baseline is not rerun
    assert _strip(read_lineage(d / "lineage.jsonl")) == _strip(read_lineage(d0 / "lineage.jsonl"))
    # and a finished run asked again just returns its result
    again, _ = _run(tmp_path, "kill", gate=ScriptedGate(), checkpoint=cp)
    assert again["best"] == "env2" and len(again["records"]) == 4


def test_finished_run_continues_with_more_generations(tmp_path):
    cp = tmp_path / "c" / "cp.sqlite"
    _run(tmp_path, "c", generations=1, checkpoint=cp)
    res, d = _run(tmp_path, "c", generations=3, checkpoint=cp)
    assert [r["gen"] for r in read_lineage(d / "lineage.jsonl")] == [0, 1, 2, 3]


def test_fresh_drops_the_thread_and_its_lineage_rows(tmp_path):
    cp = tmp_path / "f" / "cp.sqlite"
    _run(tmp_path, "f", generations=1, checkpoint=cp)
    res, d = _run(tmp_path, "f", generations=1, checkpoint=cp, fresh=True)
    assert [r["gen"] for r in read_lineage(d / "lineage.jsonl")] == [0, 1]


# ───────────────────────── budget ─────────────────────────


def test_budget_stops_before_a_generation_it_cannot_pay_for(tmp_path):
    res, d = _run(tmp_path, "b1", budget_usd=5.0)       # baseline ≈ $4.47 (simulated): no room for a trial
    assert [r["gen"] for r in read_lineage(d / "lineage.jsonl")] == [0]
    assert res["stop_reason"].startswith("budget")
    res, d = _run(tmp_path, "b2", budget_usd=10.0)
    assert [r["gen"] for r in read_lineage(d / "lineage.jsonl")] == [0, 1]
    assert res["stop_reason"].startswith("budget") and res["spent_usd"] <= 10.0


# ───────────────────────── lineage report ─────────────────────────


def test_lineage_report_runs_and_filters(tmp_path, capsys):
    _, d1 = _run(tmp_path, "r1", generations=1)
    recs = read_lineage(d1 / "lineage.jsonl")
    other = [{**r, "run": "evolve:other"} for r in recs]
    p = tmp_path / "both.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs + other))
    assert render(read_lineage(p)).count("### Lineage") == 1
    assert "evolve:other" in render(read_lineage(p))                         # latest run by default
    assert render(read_lineage(p), all_runs=True).count("### Lineage") == 2
    assert "evolve:env1:1-2" in render(read_lineage(p), run="evolve:env1:1-2")
    assert render([]) == "_no lineage records_\n"
    report_main([str(p), "--all", "--out", str(tmp_path / "t.md")])
    assert (tmp_path / "t.md").read_text().count("### Lineage") == 2


# ───────────────────────── plugging in #10's quarter runner ─────────────────────────


def test_quarter_adapter_imports_lazily_and_binds_options(monkeypatch, tmp_path):
    import types
    calls = []

    def fake_run_quarter(env, *, seed, generation=1, **kw):
        calls.append((env.name, seed, generation, kw))
        return stub_run(env, seed=seed, generation=generation)

    fake_run_quarter.__module__ = "company.board.quarter"
    mod = types.ModuleType("company.board.quarter")
    mod.run_quarter = fake_run_quarter
    monkeypatch.setitem(sys.modules, "company.board.quarter", mod)
    import company.board
    monkeypatch.setattr(company.board, "quarter", mod, raising=False)
    runner = E.load_quarter_runner("quarter", transport="fake", state_dir="x")
    res, d = _run(tmp_path, run_quarter=runner, gate=ScriptedGate([["approve"]]), generations=1)
    assert calls[0] == ("env1", 1, 1, {"transport": "fake", "state_dir": "x"})
    assert [c[:3] for c in calls] == [("env1", 1, 1), ("env1", 2, 1), ("env2", 1, 2), ("env2", 2, 2)]
    assert read_lineage(d / "lineage.jsonl")[0]["runner"] == "company.board.quarter.fake_run_quarter"


def test_quarter_adapter_without_quarter_py(monkeypatch):
    import company.board
    monkeypatch.setitem(sys.modules, "company.board.quarter", None)
    monkeypatch.delattr(company.board, "quarter", raising=False)   # quarter.py (#10) exists and may be imported
    with pytest.raises(RuntimeError, match="not on this branch"):
        E.load_quarter_runner("quarter")


def test_transport_gate_factory_is_released_before_every_trial(tmp_path):
    t = FakeTransport(rooms={"board": ["board", "Haseeb"]})
    t.start()
    live = {"open": False}
    stops = []

    class Lease:
        def on_message(self, agent, h):
            t.on_message(agent, h)

        def start(self):
            live["open"] = True

        def stop(self):
            live["open"] = False
            stops.append(1)

        def post(self, *a, **kw):
            return t.post(*a, **kw)

    def human(msg):
        if msg.sender == "board" and "Generation" in msg.text:
            t.say("Haseeb", "board", "@board approve", thread=msg.thread or msg.id)

    t.on_message("Haseeb", human)
    gate = TransportGate(factory=Lease, board="board", room="board", approvers=("haseeb",))
    seen = []

    def runner(env, *, seed, generation):
        seen.append(live["open"])
        return stub_run(env, seed=seed, generation=generation)

    _run(tmp_path, gate=gate, run_quarter=runner, generations=2, gate_timeout_s=30)
    assert seen == [False] * 6                    # no quarter ever ran with the gate's board identity connected
    assert gate.opened == len(stops) == 3         # gen 1 ask, gen 1 outcome + gen 2 ask, gen 2 outcome
    assert not live["open"]                       # and closed when evolve returns


# ───────────────────────── #31: effective levers only, min-gain ─────────────────────────


def _noisy(bill_delta):
    """The stub quarter, but every env except env1 gets a different LLM bill: the only thing that varies
    between two runs of one org on a fixed seed (#13's gen 2: $0.06)."""
    import dataclasses

    def run(env, *, seed, generation):
        m, log = stub_run(env, seed=seed, generation=generation)
        if env.name != "env1":
            m = dataclasses.replace(m, llm_cost_usd=round(m.llm_cost_usd + bill_delta, 4))
        return m, log
    return run


NO_OP_VETO = {"op": "set_policy", "key": "finance_veto_usd", "value": 6000}   # a lever; every campaign is under both


def test_a_noise_level_gain_is_no_effect_and_the_parent_stays(tmp_path):
    prop = Scripted(NO_OP_VETO, {"op": "link", "rooms": ["maintenance", "dev"]})
    res, d = _run(tmp_path, propose=prop, gate=ScriptedGate([["approve"], ["approve"]]), generations=2,
                  run_quarter=_noisy(-0.06))
    recs = read_lineage(d / "lineage.jsonl")
    assert [r["outcome"] for r in recs] == ["seed", "no_effect", "kept"]
    tie = recs[1]
    assert tie["delta"] == pytest.approx(0.06) and not tie["kept"] and tie["min_gain"] == E.DEFAULT_MIN_GAIN == 0.5
    assert tie["reason"].startswith("↔ no effect (Δ +0.06 < min-gain 0.5)") and "the parent stays" in tie["reason"]
    assert recs[2]["parent"] == "env1" and prop.calls[1]["env"] == "env1"       # the next gen builds on the parent
    assert prop.calls[1]["history"][0]["outcome"] == "no_effect"
    assert sorted(p.name for p in (d / "envs").iterdir()) == ["env1.yaml", "env3.yaml"]
    md = render(recs)
    assert "| +0.06 | ↔ no effect (Δ +0.06 < min-gain 0.5) |" in md and "reverted" not in md
    assert res["best"] == "env3"


def test_min_gain_is_the_margin_and_a_loss_is_still_reverted(tmp_path):
    _, d = _run(tmp_path, "low", propose=Scripted(NO_OP_VETO), gate=ScriptedGate([["approve"]]), generations=1,
                run_quarter=_noisy(-0.06), min_gain=0.05)
    assert read_lineage(d / "lineage.jsonl")[-1]["outcome"] == "kept"
    _, d = _run(tmp_path, "loss", propose=Scripted(NO_OP_VETO), gate=ScriptedGate([["approve"]]), generations=1,
                run_quarter=_noisy(+0.06))
    rec = read_lineage(d / "lineage.jsonl")[-1]
    assert rec["outcome"] == "reverted" and "↩️ reverted (fitness)" in render([rec])
    _, d = _run(tmp_path, "big", propose=Scripted({"op": "link", "rooms": ["maintenance", "dev"]}),
                gate=ScriptedGate([["approve"]]), generations=1, run_quarter=_noisy(-0.06))
    rec = read_lineage(d / "lineage.jsonl")[-1]
    assert rec["outcome"] == "kept" and rec["delta"] > 0.5 and "min-gain 0.5" in rec["reason"]


def test_a_no_lever_proposal_is_re_asked_once_then_gated(tmp_path):
    commander = {"op": "hire", "persona": "engineering/engineering-incident-response-commander",
                 "name": "incident-commander", "room": "dev", "role_note": "coordinate incidents"}
    prop = Scripted(commander, {"op": "link", "rooms": ["maintenance", "dev"]})
    gate = ScriptedGate([["approve"]])
    _run(tmp_path, propose=prop, gate=gate, generations=1)
    assert prop.calls[1]["error"].startswith("no lever:") and "testing/*" in prop.calls[1]["error"]
    assert len(gate.asked) == 1 and gate.asked[0]["change"] == "link #maintenance ↔ #dev"


def test_two_no_lever_proposals_skip_the_generation_without_a_gate_or_a_trial(tmp_path):
    commander = {"op": "hire", "persona": "engineering/engineering-incident-response-commander",
                 "name": "incident-commander", "room": "dev", "role_note": "coordinate incidents"}
    prop = Scripted(commander, {"op": "set_policy", "key": "dev_capacity_per_quarter", "value": 4})   # #13's gen 2s
    gate, runner = ScriptedGate([["approve"]]), Counting()
    res, d = _run(tmp_path, propose=prop, gate=gate, generations=1, run_quarter=runner)
    rec = read_lineage(d / "lineage.jsonl")[-1]
    assert rec["outcome"] == "skipped" and not rec["kept"] and rec["fitness"] is None and rec["approver"] is None
    assert "dev_capacity_per_quarter" in rec["reason"] and "set_dev_capacity" in rec["reason"]
    assert gate.asked == [] and {c[0] for c in runner.calls} == {"env1"}        # no gate, no trial quarter
    assert "| 1 | env2 | env1 |" in render([rec]) and "⏭️ skipped (no lever)" in render([rec])


def test_set_dev_capacity_is_a_lever_and_describes_itself():
    env = _env()                                            # x-company.dev_capacity: {per_week: 3}
    new, err = validate_mutation(env, {"op": "set_dev_capacity", "per_quarter": 4})
    assert err is None and new.company["dev_capacity"] == {"per_week": 3, "per_quarter": 4}
    assert E.describe({"op": "set_dev_capacity", "per_quarter": 4}, env) == "dev capacity: 3/week → 3/week, 4/quarter"
    assert "whole numbers" in validate_mutation(env, {"op": "set_dev_capacity", "per_week": 0})[1]
    assert validate_mutation(env, {"op": "set_dev_capacity", "per_week": 3})[1] == "the mutation changes nothing"


def test_llm_proposer_prompt_lists_exactly_the_levers():
    from company.board import levers
    seen = []

    def llm(system, prompt):
        seen.append((system, prompt))
        return {"diagnosis": "d", "mutation": {"op": "link", "rooms": ["maintenance", "dev"]}, "rationale": "r"}, 0.0

    prop = LLMProposer(llm=llm, roster={})
    env = _env()
    summary = E.summarize(env, E.evaluate(env, (1,), stub_run, E.load_weights(), 1))
    prop(summary, env, history=[], error=f"{levers.NO_LEVER} policy 'sev1_fix_weeks' is read by nothing")
    system, prompt = seen[0]
    for name, how, effect in levers.LEVERS:
        assert f"- {name}: {how}. Effect: {effect}." in system
    assert "min-gain" in system and "0.5" in system
    assert "set_instructions" not in system and "sev1_fix_weeks" not in system
    assert "refused before #board, no lever:" in prompt
    shortlist = prompt.split("Roster shortlist")[1]
    assert "testing/" in shortlist and "engineering/" not in shortlist and "design/" not in shortlist


def test_stub_proposer_never_proposes_a_no_lever_change(tmp_path):
    res, d = _run(tmp_path, gate=ScriptedGate([["approve"]] * 6), generations=6)
    recs = read_lineage(d / "lineage.jsonl")
    assert all(r["outcome"] != "skipped" for r in recs)
    assert not any((r.get("mutation") or {}).get("rooms") == ["growth", "dev"] for r in recs)
