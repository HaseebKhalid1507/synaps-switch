"""company/run.py — run quarters of the company from the command line.

    python -m company.run --env company/envs/env1.yaml --quarters 1 --transport fake
        offline: FakeTransport, stub LLM, stub synaps-dev and #board person; deterministic for a seed
    python -m company.run --env company/envs/env1.yaml --transport switch --weeks 3 --generation 7 \\
        --agent-prefix m10- --room-prefix m10- --acme-prefix m10- \\
        --acme-remote file:///abs/acme-shop-origin.git --issues acme --approver mattermost
        live: Switch rooms, Haiku dept agents, the extension's synaps-dev, a scripted #board person

Prints the quarter's Metrics and fitness (and the KPI table with --report). Checkpoints are in
--db (company/state/board.db), thread "<env>:<seed>": a killed run, started again with the same
arguments, resumes where it stopped; a finished one prints its stored result (--fresh reruns it).
With --quarters N the quarters use seeds seed..seed+N-1 under the same env and generation, and the
Ledger keeps one row per quarter.

    python -m company.run --env company/envs/env1.yaml --generations 2 --transport fake [--seeds 1,2] [--out DIR]
        the outer loop (#11, company.board.evolve) with this quarter as the trial runner: StubProposer and a
        scripted approver offline; on Switch LLMProposer (Haiku) and a TransportGate on the board's prefixed
        identity (opened only for the gate, released before every trial quarter). Prints the lineage table
        (company.lineage_report.render); lineage.jsonl, lineage.md and kept envs go to --out (default company/).
        A change is kept only if it beats its parent by more than --min-gain (default 0.5; evolve.DEFAULT_MIN_GAIN).

--dev-per-week / --dev-per-quarter set the baseline: they are written into the --env's x-company.dev_capacity
(replacing the file's value for that key), so with --generations every env evolved from it inherits them and a
set_dev_capacity mutation changes them in its trial. They are not passed to the quarter as overrides.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from dataclasses import asdict
from pathlib import Path

from company.board.quarter import QuarterConfig, ledger_row, run_quarter_full
from company.core.env import apply_mutation, load_env


def kill_hook(spec):
    """COMPANY_QUARTER_KILL_AT=node[:week]: SIGKILL this process when that node starts (resume tests)."""
    if not spec:
        return None
    node, _, week = spec.partition(":")

    def hook(n, w):
        if n == node and (not week or str(w) == week):
            os.kill(os.getpid(), signal.SIGKILL)
    return hook


def issues_from(arg, state_dir):
    if arg in (None, "", "default"):
        return None
    if arg == "acme":
        from company import acme
        return acme.load_issues(cache=Path(state_dir) / "acme-issues.json")
    data = json.loads(Path(arg).read_text())
    return {"incidents": data["incidents"], "features": data["features"]}


def seed_capacity(env, per_week=None, per_quarter=None, log=None):
    """The --dev-per-* flags as the seed env's baseline: its x-company.dev_capacity, key by key."""
    if per_week is None and per_quarter is None:
        return env
    from company.board.mechanics import cap_text, capacity
    new = apply_mutation(env, {"op": "set_dev_capacity", "per_week": per_week, "per_quarter": per_quarter})
    if log and capacity(env.company) != capacity(new.company):
        log(f"dev capacity: {env.name} x-company.dev_capacity {cap_text(*capacity(env.company))} -> "
            f"{cap_text(*capacity(new.company))} (--dev-per-week / --dev-per-quarter set the baseline)")
    return new


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m company.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", required=True)
    ap.add_argument("--quarters", type=int, default=1)
    ap.add_argument("--generations", type=int, default=None, help="run the outer evolution loop (#11) instead")
    ap.add_argument("--seeds", default="1,2", help="--generations: the trial seeds (same seeds for every env)")
    ap.add_argument("--budget-usd", type=float, default=None, help="--generations: stop when spent reaches this")
    ap.add_argument("--min-gain", type=float, default=None,
                    help="--generations: keep a change only if fitness beats the parent's by more than this (0.5)")
    ap.add_argument("--gate-timeout", type=float, default=None, help="--generations: seconds; none = auto-reject")
    ap.add_argument("--gate-room", default=None, help="--generations on Switch: gate room (default <room-prefix>board)")
    ap.add_argument("--script", default=None, help='--generations offline: scripted approver, "approve|reject: why|..."')
    ap.add_argument("--evolve-db", default="company/state/evolve.sqlite")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--generation", type=int, default=1)
    ap.add_argument("--transport", choices=("fake", "switch"), default="fake")
    ap.add_argument("--llm", choices=("stub", "haiku"), default=None, help="default: stub on fake, haiku on switch")
    ap.add_argument("--weeks", type=int, default=None, help="override x-company.weeks_per_quarter")
    ap.add_argument("--db", default="company/state/board.db")
    ap.add_argument("--thread-id", default=None)
    ap.add_argument("--fresh", action="store_true", help="drop this thread's checkpoints and start over")
    ap.add_argument("--state-dir", default="company/state")
    ap.add_argument("--out", default=None, help="dir for run.log + transcript.jsonl (--generations: lineage, envs/)")
    ap.add_argument("--issues", default=None, help="default | acme (real acme-shop issues, cached) | PATH.json")
    ap.add_argument("--approver", default=None, choices=("stub", "mattermost", "none"),
                    help="who answers merge requests in #board (default: stub on fake, none on switch)")
    ap.add_argument("--approvers", default=None, help="comma list of people whose decision counts")
    ap.add_argument("--humans", default=None, help="comma list of the #board people")
    ap.add_argument("--dev-per-week", type=int, default=None)
    ap.add_argument("--dev-per-quarter", type=int, default=None)
    ap.add_argument("--dev-timeout", type=float, default=900.0)
    ap.add_argument("--approve-timeout", type=float, default=180.0)
    ap.add_argument("--week-wall", type=float, default=60.0, help="live: max seconds per week of chat")
    ap.add_argument("--quiet", type=float, default=12.0, help="live: a week ends after this many quiet seconds")
    ap.add_argument("--agent-prefix", default="")
    ap.add_argument("--room-prefix", default="")
    ap.add_argument("--acme-remote", default=None)
    ap.add_argument("--acme-prefix", default="")
    ap.add_argument("--switch-url", default=None)
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--devstack", default=None)
    ap.add_argument("--report", action="store_true", help="also print the KPI table")
    ap.add_argument("--json", action="store_true", help="print one JSON object per quarter instead")
    ap.add_argument("-v", "--verbose", action="store_true", help="run log on stderr")
    a = ap.parse_args(argv)

    env = seed_capacity(load_env(a.env), a.dev_per_week, a.dev_per_quarter,
                        log=(lambda line: print(line, file=sys.stderr, flush=True)) if a.verbose else None)
    live = a.transport == "switch"
    llm_kind = a.llm or ("haiku" if live else "stub")
    llm = None
    if llm_kind == "haiku":
        from company.llm import complete_json
        llm = complete_json
    elif live:
        from company.board.stubs import stub_llm
        llm = stub_llm
    split = (lambda s: tuple(x.strip() for x in s.split(",") if x.strip()) if s else None)
    qopts = dict(   # QuarterConfig fields shared by every quarter of this command
        transport=a.transport, llm=llm, llm_label=llm_kind, weeks=a.weeks, db=a.db, state_dir=a.state_dir,
        issues=issues_from(a.issues, a.state_dir), approver=a.approver or ("stub" if not live else None),
        approvers=split(a.approvers),   # --dev-per-*: in env (seed_capacity), so a dev_capacity mutation counts
        dev_timeout=a.dev_timeout, approve_timeout=a.approve_timeout, week_wall_s=a.week_wall, quiet_s=a.quiet,
        agent_prefix=a.agent_prefix, room_prefix=a.room_prefix, acme_remote=a.acme_remote, acme_prefix=a.acme_prefix,
        switch_url=a.switch_url, token_file=a.token_file, devstack=a.devstack,
        hook=kill_hook(os.environ.get("COMPANY_QUARTER_KILL_AT")),
        log=(lambda line: print(line, file=sys.stderr, flush=True)) if a.verbose else None)
    if split(a.humans):
        qopts["humans"] = split(a.humans)
    if a.generations:
        return generations(a, env, qopts, llm_kind, split)
    rows = []
    for q in range(a.quarters):
        seed = a.seed + q
        cfg = QuarterConfig(**qopts, thread_id=(f"{a.thread_id}:{q + 1}" if a.thread_id and a.quarters > 1
                                                else a.thread_id),
                            fresh=a.fresh, out_dir=a.out, ledger_rows=list(rows), quarter_label=q + 1)
        r = run_quarter_full(env, seed=seed, generation=a.generation, config=cfg)
        rows.append(ledger_row(q + 1, seed, r.metrics, r.fitness))
        if a.json:
            print(json.dumps({"quarter": q + 1, "seed": seed, "thread_id": r.thread_id, "run": r.resumed,
                              "metrics": asdict(r.metrics), "fitness": round(r.fitness, 6),
                              "fixes": r.log.fixes, "features": r.log.features}, sort_keys=True))
            continue
        print(f"quarter {q + 1} ({env.name}, seed {seed}, generation {a.generation}, {a.transport})")
        print(r.metrics)
        print(f"fitness: {r.fitness:.4f}")
        if a.report:
            print(r.report)
    return 0


def generations(a, env, qopts, llm_kind, split) -> int:
    """The outer loop (company.board.evolve) with this quarter as its trial runner. Each trial quarter keeps
    its own "<env>:<seed>" thread (no thread_id / fresh here); --fresh restarts the evolution thread."""
    from company.board.evolve import (DEFAULT_SCRIPT, LLMProposer, ScriptedGate, StubProposer, TransportGate,
                                      _parse_script, env_index, evolve, load_quarter_runner, switch_room_reader)
    from company.depts.agent import load_personas
    from company.lineage_report import render

    say = (lambda *x: print(" ".join(map(str, x)), file=sys.stderr, flush=True)) if a.verbose else None
    run_q = load_quarter_runner("quarter", **qopts, out_dir=a.out)   # every quarter appends run.log, transcript.jsonl
    roster = load_personas()
    approvers = split(a.approvers)
    read_rooms = None
    if a.transport == "switch":
        from company.switch_env import load_state
        from company.transport import SwitchTransport
        board = a.agent_prefix + "board"
        gate = TransportGate(       # opened for the gate only; evolve releases it before every trial quarter,
            factory=lambda: SwitchTransport(a.switch_url, agents=[board], state_dir=a.state_dir,   # which opens
                                            registration_token_file=a.token_file),                 # the same board
            board=board, room=a.gate_room or a.room_prefix + "board", approvers=approvers, log=say or (lambda *x: None))

        def read_rooms(e):          # the trial's own rooms: <room-prefix><room>-g<k>, k = the env's index
            st = load_state(a.state_dir, f"{a.room_prefix}{e.name}-g{env_index(e.name)}") or {}
            ids = {n: r["id"] for n, r in (st.get("rooms") or {}).items()} or None
            return switch_room_reader(lambda: gate.t, board, ids, room_prefix=a.room_prefix)(e)
    else:
        gate = ScriptedGate(_parse_script(a.script) if a.script else DEFAULT_SCRIPT)
        approvers = approvers or ("haseeb",)
    out = Path(a.out) if a.out else None
    kw = {} if a.budget_usd is None else {"budget_usd": a.budget_usd}
    if a.min_gain is not None:
        kw["min_gain"] = a.min_gain
    pkw = {} if a.min_gain is None else {"min_gain": a.min_gain}
    res = evolve(env, generations=a.generations, seeds=[int(s) for s in a.seeds.split(",")], run_quarter=run_q,
                 propose=LLMProposer(roster=roster, **pkw) if llm_kind == "haiku" else StubProposer(roster),
                 gate=gate, read_rooms=read_rooms, roster=roster, approvers=approvers,
                 lineage_path=out / "lineage.jsonl" if out else "company/lineage.jsonl",
                 envs_dir=out / "envs" if out else "company/envs", gate_timeout_s=a.gate_timeout,
                 checkpoint=a.evolve_db, fresh=a.fresh, log=say, **kw)
    md = render(res["records"])
    if out:
        (out / "lineage.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"best: {res['best']} (fitness {res['best_fitness']}); stop: {res['stop_reason']}; "
          f"spent ${float(res['spent_usd'] or 0):.4f}; generations {res['generations']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
