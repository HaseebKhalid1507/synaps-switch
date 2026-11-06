"""company/board/quarter.py: the LangGraph inner loop, offline (FakeTransport, stub LLM / synaps-dev / person)."""
import inspect
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from company.board import quarter as qm
from company.board.live import SessionCost, local_merge
from company.board.stubs import stub_llm
from company.board.wire import MARKET, CompanyTransport, company_rooms, extra_members
from company.core.env import load_env
from company.market.sim import Metrics, QuarterLog
from company.switch_env import plan_env
from company.transport import FakeTransport

ROOT = Path(__file__).resolve().parents[2]
ENV1 = str(ROOT / "company/envs/env1.yaml")


@pytest.fixture
def env():
    return load_env(ENV1)


def opts(tmp_path, name="board.db", **kw):
    return {"db": str(tmp_path / name), "state_dir": str(tmp_path / "state"), **kw}


def run(env, tmp_path, seed=42, **kw):
    return qm.run_quarter_full(env, seed=seed, generation=1, **opts(tmp_path, **kw))


class Crash(Exception):
    pass


def crash_at(node, week=None):
    def hook(n, w):
        if n == node and (week is None or w == week):
            raise Crash(f"{n}:{w}")
    return hook


# ───────────────────────── the quarter ─────────────────────────


def test_run_quarter_is_the_runner_evolve_injects(env, tmp_path):
    sig = inspect.signature(qm.run_quarter)
    assert list(sig.parameters)[:3] == ["env", "seed", "generation"]
    assert sig.parameters["seed"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["generation"].kind is inspect.Parameter.KEYWORD_ONLY
    m, log = qm.run_quarter(env, seed=7, generation=2, **opts(tmp_path))
    assert isinstance(m, Metrics) and isinstance(log, QuarterLog)


def test_offline_quarter_runs_the_company_and_is_deterministic(env, tmp_path):
    a = run(env, tmp_path, name="a.db", out_dir=str(tmp_path / "out"))
    b = run(env, tmp_path, name="b.db")
    assert a.resumed == b.resumed == "new"
    assert asdict(a.metrics) == asdict(b.metrics) and a.fitness == b.fitness and a.report == b.report
    assert asdict(a.log) == asdict(b.log)
    # incidents routed by ops-maint, relayed by the board (env1 has no #maintenance ↔ #dev link: a week late),
    # fixed by (stub) synaps-dev, merged after a person's approve. On seed 42 #15's fix regresses (#25 mechanics).
    assert sorted(f["issue"] for f in a.log.fixes) == [3, 7, 11]
    assert all(f["week_fixed"] == 2 and f["tests_passed"] for f in a.log.fixes)
    whats = [d.get("what", "") for d in a.log.decisions]
    assert "#3 (sev1) waited for board relay: raised wk1 by ops-maint in #maintenance, dispatched wk2" in whats
    assert any(w.startswith("#15: fix regressed after the wk3 merge") for w in whats)
    assert sorted(f["issue"] for f in a.log.features) == [20, 23]          # demand >= 0.8 got built
    merges = [d for d in a.log.decisions if d["type"] == "merge"]
    assert merges and all(d["approver"] == "Haseeb" and d["decision"] == "approve" for d in merges)
    assert a.log.campaigns and a.log.price_changes
    # costs: dept agents + judge + reviews + synaps-dev, all in llm_cost_usd
    w = a.state["work"]
    assert w["dev_usd"] == pytest.approx(0.05 * 6) and w["review_usd"] > 0
    assert a.log.llm_cost_usd == a.metrics.llm_cost_usd > w["dev_usd"] + w["review_usd"]
    assert a.fitness == pytest.approx(qm.fitness_of(a.metrics, qm.load_weights()))
    # the KPI table went to #board and the Ledger
    recs = [json.loads(line) for line in (tmp_path / "out/transcript.jsonl").read_text().splitlines()]
    kpi = [r for r in recs if r["kind"] == "post" and r["room"] == "board" and "KPI" in r["text"]]
    assert kpi and kpi[-1]["sender"] == "board" and f"**{a.fitness:.4f}**" in kpi[-1]["text"]
    ledger = (tmp_path / "state/ledger/env1-g1.md").read_text()
    assert "| 1 | 42 |" in ledger and f"| {a.fitness:.2f} |" in ledger
    # market events come from the market identity, dept work from dept agents
    senders = {r["sender"] for r in recs if r["kind"] == "post"}
    assert {"market", "board", "ops-maint", "growth", "synaps-dev", "Haseeb"} <= senders
    assert all(r["sender"] == "market" for r in recs if r["kind"] == "post" and "customers report" in r["text"])


def test_a_different_seed_is_a_different_quarter(env, tmp_path):
    a = run(env, tmp_path, seed=42, name="a.db")
    b = run(env, tmp_path, seed=43, name="b.db")
    assert asdict(a.metrics) != asdict(b.metrics)


def test_cli_prints_metrics_and_fitness_deterministically(tmp_path):
    def cli(db):
        r = subprocess.run([sys.executable, "-m", "company.run", "--env", ENV1, "--quarters", "1",
                            "--transport", "fake", "--db", str(tmp_path / db), "--state-dir", str(tmp_path / "st")],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        return r.stdout
    one, two = cli("a.db"), cli("b.db")
    assert one == two
    lines = one.strip().splitlines()
    assert lines[1].startswith("Metrics(revenue=$") and lines[2].startswith("fitness: ")


def test_cli_generations_runs_the_outer_loop_on_this_quarter(tmp_path):
    r = subprocess.run([sys.executable, "-m", "company.run", "--env", ENV1, "--generations", "2", "--transport", "fake",
                        "--out", str(tmp_path / "out"), "--db", str(tmp_path / "board.db"),
                        "--state-dir", str(tmp_path / "st"), "--evolve-db", str(tmp_path / "evolve.sqlite")],
                       cwd=ROOT, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr
    assert "| gen | env | parent |" in r.stdout and "| 0 | env1 |" in r.stdout and "| 2 | env3 |" in r.stdout
    assert "quarters by `company.board.quarter.run_quarter`" in r.stdout
    recs = [json.loads(line) for line in (tmp_path / "out/lineage.jsonl").read_text().splitlines()]
    assert [x["gen"] for x in recs] == [0, 1, 2] and recs[0]["outcome"] == "seed"
    assert all(x["runner"] == "company.board.quarter.run_quarter" for x in recs)
    assert (tmp_path / "out/lineage.md").exists()
    # every trial quarter is its own "<env>:<seed>" thread in the quarter db
    import sqlite3
    threads = {t for (t,) in sqlite3.connect(tmp_path / "board.db").execute("select distinct thread_id from checkpoints")}
    assert {"env1:1", "env1:2", "env2:1", "env2:2", "env3:1", "env3:2"} <= threads


def test_cli_quarters(tmp_path):
    from company.run import main
    assert main(["--env", ENV1, "--quarters", "2", "--json", "--db", str(tmp_path / "q.db"),
                 "--state-dir", str(tmp_path / "st")]) == 0
    ledger = (tmp_path / "st/ledger/env1-g1.md").read_text()
    assert "| 1 | 42 |" in ledger and "| 2 | 43 |" in ledger


# ───────────────────────── checkpoints: kill + resume ─────────────────────────


def test_a_crashed_run_resumes_where_it_stopped(env, tmp_path):
    clean = run(env, tmp_path, name="clean.db")
    with pytest.raises(Crash):
        run(env, tmp_path, name="r.db", hook=crash_at("work", 2))
    seen = []
    r = run(env, tmp_path, name="r.db", hook=lambda n, w: seen.append((n, w)))
    assert r.resumed == "resumed" and r.thread_id == "env1:42"
    assert seen[0] == ("work", 2) and ("operate", 1) not in seen and ("open_quarter", None) not in seen
    assert asdict(r.metrics) == asdict(clean.metrics) and r.fitness == clean.fitness
    assert asdict(r.log) == asdict(clean.log)


def test_a_finished_thread_returns_its_result_and_fresh_reruns(env, tmp_path):
    first = run(env, tmp_path)
    seen = []
    again = run(env, tmp_path, hook=lambda n, w: seen.append(n))
    assert again.resumed == "cached" and seen == [] and asdict(again.metrics) == asdict(first.metrics)
    fresh = run(env, tmp_path, fresh=True, hook=lambda n, w: seen.append(n))
    assert fresh.resumed == "new" and seen[0] == "open_quarter"
    assert asdict(fresh.metrics) == asdict(first.metrics)


def test_a_changed_env_starts_the_thread_over(env, tmp_path):
    run(env, tmp_path)
    env.company["price"] = 31.0
    r = run(env, tmp_path)
    assert r.resumed == "new"


def test_a_killed_process_resumes(tmp_path):
    argv = [sys.executable, "-m", "company.run", "--env", ENV1, "--transport", "fake",
            "--db", str(tmp_path / "k.db"), "--state-dir", str(tmp_path / "st")]
    clean = subprocess.run([*argv[:-4], "--db", str(tmp_path / "c.db"), "--state-dir", str(tmp_path / "st")],
                           cwd=ROOT, capture_output=True, text=True, timeout=120)
    killed = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=120,
                            env={**os.environ, "COMPANY_QUARTER_KILL_AT": "operate:3"})
    assert killed.returncode == -9 and "fitness" not in killed.stdout
    resumed = subprocess.run([*argv, "-v"], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert resumed.returncode == 0, resumed.stderr
    assert "resuming at ['operate'] (week 3)" in resumed.stderr
    assert resumed.stdout == clean.stdout


# ───────────────────────── the board's rules ─────────────────────────


def test_pending_items_at_quarter_end_count_as_not_approved(env, tmp_path):
    def llm(system, prompt):
        if "You are @marketing" in system and "Policy check" not in prompt:
            return {"say": "Big launch.", "action": {"type": "campaign", "text": "Shop acme today.", "spend": 9000,
                                                      "cites_feature": False}}, 0.001
        return stub_llm(system, prompt)
    r = run(env, tmp_path, llm=llm, llm_label="big-campaign", approver=lambda rt, tid, req: None)
    # nobody answered: no merge, no fix, every merge request logged as not approved
    assert r.log.fixes == [] and r.log.features == []
    nots = [d for d in r.log.decisions if d["type"] == "merge_not_approved"]
    assert sorted(d["issue"] for d in nots) == [3, 7, 11, 15, 20, 23]
    # the $9,000 campaign escalated to the humans, unanswered: not approved, never booked
    v = [d for d in r.log.decisions if d["type"] == "verdict" and d["status"] == "not_approved"]
    assert v and all(d["by"] == "quarter_end" for d in v)
    assert not r.log.campaigns and all(not s["approved"] for s in r.log.spend)
    assert any(p["what"] == "merge" for p in r.state["pending_at_end"])
    assert "| not approved at quarter end | " in r.report


def test_a_rejected_merge_is_not_booked(env, tmp_path):
    def reject(rt, tid, req):
        rt.t.say("Aditya", rt.hub, "@board reject", thread=tid)
        return "reject"
    r = run(env, tmp_path, approver=reject)
    assert r.log.fixes == [] and r.log.features == []
    assert {d["decision"] for d in r.log.decisions if d["type"] == "merge"} == {"reject"}


def test_only_a_person_can_approve_a_merge(env, tmp_path):
    def agent_approves(rt, tid, req):
        rt.t.post("growth", rt.hub, "@board approve", thread=tid)       # an agent: ignored by the gate
        return None
    r = run(env, tmp_path, approver=agent_approves)
    assert r.log.fixes == [] and not [d for d in r.log.decisions if d["type"] == "merge"]


def test_dev_capacity_limits_the_queue_and_leftovers_are_logged(env, tmp_path):
    r = run(env, tmp_path, dev_per_week=1, dev_per_quarter=3)
    assert [t["issue"] for t in r.state["work"]["tasks"]] == [3, 7, 11]     # sev1 first, then sev2s
    assert [f["week_fixed"] for f in r.log.fixes] == [2, 3, 4]              # relayed a week late, then 1/week
    left = [d["issue"] for d in r.log.decisions if d["type"] == "route_not_dispatched"]
    assert sorted(left) == [15, 20, 23]
    assert any("never reached synaps-dev" in d.get("what", "") for d in r.log.decisions)


def test_the_board_records_its_mail(env, tmp_path):
    r = run(env, tmp_path)
    inbox = r.state["board_inbox"]
    assert any(m["sender"] == "ops-maint" and "@board" in m["text"] for m in inbox)


# ───────────────────────── wiring ─────────────────────────


def test_company_transport_maps_names_both_ways_and_the_market_is_not_an_agent(env):
    rooms = company_rooms(env, ("user",))
    names = {a["name"]: "m10-" + a["name"] for a in env.agents if a["name"] != "synaps-dev"}
    names[MARKET] = "m10-market"
    inner = FakeTransport(rooms={r: [names.get(a, a) for a in m] for r, m in rooms.items()})
    t = CompanyTransport(inner, names)
    got = []
    t.on_message("growth", got.append)
    t.start()
    t.post("market", "growth", "@growth Competitor cut price. cc @synaps-dev, @finance.")
    rec = inner.records[-1]
    assert rec["sender"] == "m10-market" and "@m10-growth" in rec["text"] and "@m10-finance." in rec["text"]
    assert "@synaps-dev" in rec["text"]
    assert got and got[0].sender == "market" and not got[0].sender_is_agent
    assert got[0].text.startswith("@growth ") and "growth" in got[0].mentions
    t.post("board", "growth", "@growth hello")
    assert got[-1].sender == "board" and got[-1].sender_is_agent


def test_extra_members_bring_every_dept_into_board_but_not_synaps_dev(env):
    rooms = company_rooms(env, ("user",))
    extra = extra_members(env, rooms, lambda a: "m10-" + a)
    assert {"m10-ops-maint", "m10-growth", "m10-marketing", "m10-finance", "m10-reviewer", "m10-market"} <= \
        set(extra["board"])
    assert all("m10-synaps-dev" not in v and "synaps-dev" not in v for v in extra.values())
    assert all("m10-market" in v for v in extra.values())
    plan = plan_env(env, 5, agent_names={a["name"]: "m10-" + a["name"] for a in env.agents if a["name"] != "synaps-dev"},
                    operator="m10-board", room_prefix="m10-", extra_members=extra)
    board = next(r for r in plan["rooms"] if r["name"] == "board")
    dev = next(r for r in plan["rooms"] if r["name"] == "dev")
    assert "m10-growth" in board["agents"] and "synaps-dev" not in board["agents"]
    assert "synaps-dev" in dev["agents"] and dev["switch_name"] == "m10-dev-g5"


def test_live_merge_is_local_remote_only():
    import company.acme as acme
    with pytest.raises(acme.GitHubWriteRefused):
        local_merge("https://github.com/adityapat958/acme-shop/pull/1")


def test_synaps_dev_cost_is_its_room_sessions(tmp_path):
    s = tmp_path / "sessions"
    s.mkdir()
    for i, (name, cost) in enumerate([("sw-synaps-dev-m10-dev-g5-ab12", 0.31), ("sw-synaps-dev-m10-dev-g4-ff00", 9.0),
                                      ("sw-synaps-dev-m10-dev-g5-cd34", 0.04), ("sw-synaps-m10-dev-g5-0000", 5.0)]):
        (s / f"{i}.json").write_text(json.dumps({"id": str(i), "name": name, "session_cost": cost}))
    assert SessionCost("m10-dev-g5", base=tmp_path)() == pytest.approx(0.35)
