#!/usr/bin/env python3
"""Live: one short quarter of env1 on the local Switch stack (company/board/quarter.py, --transport switch).

    python evidence/m4/quarter/live_quarter.py --generation 1 [--weeks 3]

Real Haiku dept agents (company/llm.py via the private broker), the market as its own `m10-market` agent,
the extension's synaps-dev (Sonnet, its room session in #m10-dev-g<k>) fixing acme-shop on the LOCAL
remote only, the testing-reality-checker review, and a SCRIPTED human approval: live.MattermostApprover
posts "@m10-board approve|reject" as the devstack's seeded Mattermost user (`dev.py mm-post`), approving
only a PASS review with green harness tests. The board never approves anything itself.

Writes (scrubbed) into this directory: run.log, transcript.jsonl (Switch side), mattermost-<room>.json
(dev.py mm-thread), kpi.md, quarter.json, cost.json, acme-git.txt. Prints no secret.
"""
import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

PRIV = Path("~/.synaps-switch-dev").expanduser()
for k, v in {"COMPANY_SYNAPS_BASE_DIR": PRIV / "company-llm", "COMPANY_BROKER_TOKEN_FILE": PRIV / "broker/machine.token",
             "COMPANY_BROKER_URL": "http://127.0.0.1:18191"}.items():
    os.environ.setdefault(k, str(v))
os.environ.pop("ACME_ALLOW_GITHUB_WRITES", None)

from company import acme  # noqa: E402
from company.board import live  # noqa: E402
from company.board.quarter import QuarterConfig, run_quarter_full  # noqa: E402
from company.core.env import load_env  # noqa: E402
from company.llm import complete_json  # noqa: E402

OUT = Path(__file__).resolve().parent
REMOTE = "file://" + str(PRIV / "m12" / "remote" / "acme-shop-origin.git")   # synaps-dev's workdir_repo origin
TOKEN = Path("~/Projects/synaps-switch/devstack/state/registration.token").expanduser()
PREFIX = "m10-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generation", type=int, required=True)
    ap.add_argument("--weeks", type=int, default=3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--dev-per-week", type=int, default=1)
    ap.add_argument("--dev-per-quarter", type=int, default=2)
    ap.add_argument("--week-wall", type=float, default=90.0)
    ap.add_argument("--quiet", type=float, default=15.0)
    a = ap.parse_args()

    raw = ROOT / "company/state/m10-live" / f"g{a.generation}"
    raw.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    since_ms = int(t0 * 1000) - 60_000
    issues = acme.load_issues(cache=ROOT / "company/state/acme-issues.json")
    cfg = QuarterConfig(
        transport="switch", llm=complete_json, llm_label="haiku", weeks=a.weeks, db="company/state/board.db",
        state_dir="company/state", out_dir=str(raw), issues={"incidents": issues["incidents"],
                                                             "features": issues["features"]},
        humans=("user",), approvers=("user",), approver="mattermost",
        dev_per_week=a.dev_per_week, dev_per_quarter=a.dev_per_quarter, dev_timeout=900.0, approve_timeout=240.0,
        week_wall_s=a.week_wall, quiet_s=a.quiet, agent_prefix=PREFIX, room_prefix=PREFIX, acme_remote=REMOTE,
        acme_prefix=PREFIX, switch_url="http://127.0.0.1:18400", token_file=str(TOKEN),
        log=lambda line: print(line, flush=True))
    r = run_quarter_full(load_env(ROOT / "company/envs/env1.yaml"), seed=a.seed, generation=a.generation, config=cfg)
    wall = round(time.time() - t0)

    st = r.state
    setup, work = st.get("setup") or {}, st.get("work") or {}
    scrub = live.scrubber("company/state")
    # Mattermost side of every room of the generation
    d = live.devstack_dir()
    counts = live.export_channels(d, setup.get("channels") or {}, since_ms, raw)
    # acme-shop local remote: what landed on the generation branch
    base = setup.get("base")
    git_txt = ""
    if base:
        bare = acme.local_path(REMOTE)
        git_txt = acme.git("log", "--format=%h %an %s", f"refs/tags/{acme.BASELINE_TAG}..refs/heads/{base}",
                           cwd=bare).stdout
        git_txt += "\n" + acme.git("diff", "--stat", f"refs/tags/{acme.BASELINE_TAG}", f"refs/heads/{base}",
                                   cwd=bare).stdout
        git_txt += "\n" + acme.git("diff", f"refs/tags/{acme.BASELINE_TAG}", f"refs/heads/{base}", cwd=bare).stdout
    dev_sessions = live.SessionCost(setup.get("dev_room_name") or f"{PREFIX}dev-g{a.generation}").sessions()
    cost = {"total_usd": r.log.llm_cost_usd, "synaps_dev_usd": work.get("dev_usd", 0.0),
            "reviews_usd": work.get("review_usd", 0.0),
            "dept_agents_and_judge_usd": round(r.log.llm_cost_usd - work.get("dev_usd", 0.0) - work.get("review_usd", 0.0), 6),
            "synaps_dev_sessions": [{k: s[k] for k in ("name", "cost", "messages")} for s in dev_sessions],
            "wall_seconds": wall}
    summary = {"thread_id": r.thread_id, "run": r.resumed, "env": "env1", "seed": a.seed, "generation": a.generation,
               "weeks": a.weeks, "metrics": asdict(r.metrics), "fitness": r.fitness, "setup": setup,
               "dev_tasks": work.get("tasks"), "approvals": work.get("approvals"), "skipped_routes": work.get("skipped"),
               "pending_at_end": st.get("pending_at_end"), "weeks_done": st.get("weeks_done"),
               "quarter_log": asdict(r.log), "board_inbox": st.get("board_inbox"), "mattermost_posts": counts,
               "cost": cost, "approval_note": "SCRIPTED human approval: live.MattermostApprover posted as the "
               "devstack's Mattermost user via dev.py mm-post; rule: approve iff review PASS and harness pytest green"}
    for f in raw.glob("*"):
        if f.is_file():
            (OUT / f.name).write_text(scrub(f.read_text(encoding="utf-8")), encoding="utf-8")
    (OUT / "kpi.md").write_text(scrub(r.report) + "\n")
    (OUT / "quarter.json").write_text(scrub(json.dumps(summary, indent=1, ensure_ascii=False, default=str)) + "\n")
    (OUT / "cost.json").write_text(json.dumps(cost, indent=1) + "\n")
    (OUT / "acme-git.txt").write_text(scrub(git_txt))
    print(r.report)
    print(json.dumps(cost))


if __name__ == "__main__":
    main()
