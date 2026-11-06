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
Ledger keeps one row per quarter. --generations (the outer evolution loop) is #11.
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
from company.core.env import load_env


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m company.run", description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", required=True)
    ap.add_argument("--quarters", type=int, default=1)
    ap.add_argument("--generations", type=int, default=None, help="outer evolution loop (#11, not here)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--generation", type=int, default=1)
    ap.add_argument("--transport", choices=("fake", "switch"), default="fake")
    ap.add_argument("--llm", choices=("stub", "haiku"), default=None, help="default: stub on fake, haiku on switch")
    ap.add_argument("--weeks", type=int, default=None, help="override x-company.weeks_per_quarter")
    ap.add_argument("--db", default="company/state/board.db")
    ap.add_argument("--thread-id", default=None)
    ap.add_argument("--fresh", action="store_true", help="drop this thread's checkpoints and start over")
    ap.add_argument("--state-dir", default="company/state")
    ap.add_argument("--out", default=None, help="dir for run.log + transcript.jsonl")
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

    if a.generations:
        print("--generations is the outer evolution loop (#11: company.board.evolve), not built here", file=sys.stderr)
        return 2
    env = load_env(a.env)
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
    rows = []
    hook = kill_hook(os.environ.get("COMPANY_QUARTER_KILL_AT"))
    for q in range(a.quarters):
        seed = a.seed + q
        cfg = QuarterConfig(
            transport=a.transport, llm=llm, llm_label=llm_kind, weeks=a.weeks, db=a.db,
            thread_id=(f"{a.thread_id}:{q + 1}" if a.thread_id and a.quarters > 1 else a.thread_id),
            fresh=a.fresh, state_dir=a.state_dir, out_dir=a.out, issues=issues_from(a.issues, a.state_dir),
            approver=a.approver or ("stub" if not live else None), approvers=split(a.approvers),
            dev_per_week=a.dev_per_week, dev_per_quarter=a.dev_per_quarter, dev_timeout=a.dev_timeout,
            approve_timeout=a.approve_timeout, week_wall_s=a.week_wall, quiet_s=a.quiet,
            agent_prefix=a.agent_prefix, room_prefix=a.room_prefix, acme_remote=a.acme_remote,
            acme_prefix=a.acme_prefix, switch_url=a.switch_url, token_file=a.token_file, devstack=a.devstack,
            ledger_rows=list(rows), quarter_label=q + 1, hook=hook,
            log=(lambda line: print(line, file=sys.stderr, flush=True)) if a.verbose else None)
        if split(a.humans):
            cfg.humans = split(a.humans)
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


if __name__ == "__main__":
    sys.exit(main())
