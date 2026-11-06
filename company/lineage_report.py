"""company/lineage_report.py — the lineage table (README / demo) from lineage.jsonl.

    python -m company.lineage_report [company/lineage.jsonl] [--run RUN | --all] [--out lineage.md]

One table per evolution run (`run` = the evolve thread id; the latest run by default), one row per
generation: the seed, then every proposal with its outcome: kept (fitness improved on the same seeds),
reverted (trialed, did not improve), rejected / timeout (the #board humans said no, or nobody answered:
not trialed), invalid (the board never produced a valid mutation). Below the table, the why of each row.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from company.core.lineage import read_lineage

RESULT = {"seed": "seed", "kept": "✅ kept", "reverted": "↩️ reverted (fitness)", "rejected": "❌ rejected (human)",
          "timeout": "⏱️ auto-rejected (timeout)", "invalid": "⚠️ invalid proposal"}


def _money(v) -> str:
    if v is None:
        return "—"
    return f"{'−' if v < 0 else ''}${abs(v) / 1000:.1f}k"


def _row(r: dict) -> str:
    m = r.get("metrics") or {}
    f, d = r.get("fitness"), r.get("delta")
    cells = [
        str(r.get("gen")), str(r.get("env")), str(r.get("parent") or "—"),
        str(r.get("change") or "seed").replace("|", "\\|"),
        str(r.get("approver") or ("—" if r.get("outcome") in ("seed", "invalid", "timeout") else "?")),
        _money(m.get("profit")) if m else "—",
        f"{m['growth']:+.1%}" if m else "—",
        f"{m['mean_fix_weeks']:.2f} wk" if m else "—",
        f"${m['llm_cost_usd']:.2f}" if m else "—",
        f"{f:.2f}" if f is not None else "—",
        f"{d:+.2f}" if d is not None else "—",
        RESULT.get(r.get("outcome"), str(r.get("outcome"))),
    ]
    return "| " + " | ".join(cells) + " |"


def render_run(records: list[dict]) -> str:
    recs = sorted(records, key=lambda r: r.get("gen", 0))
    run = recs[0].get("run") if recs else "?"
    seeds = recs[0].get("seeds") if recs else []
    best = None
    for r in recs:
        if r.get("outcome") in ("seed", "kept"):
            best = r
    out = [f"### Lineage `{run}` (seeds {seeds}, mean over seeds)", "",
           "| gen | env | parent | change | decided by | profit | growth | fix time | LLM $ | fitness | Δ parent | result |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    out += [_row(r) for r in recs]
    out.append("")
    if best:
        out.append(f"Best: **{best['env']}**, fitness {best['fitness']:.2f}.")
    spent = sum(sum((r.get("cost_usd") or {}).get(k, 0) for k in ("quarters", "proposal")) for r in recs)
    runner = recs[0].get("runner") if recs else None
    out.append(f"Spend (QuarterLog.llm_cost_usd over all quarters + proposals): ${spent:.4f}"
               + (f"; quarters by `{runner}`" + (" (simulated $)" if "stub" in str(runner) else "") if runner else "")
               + ".")
    out += ["", "Why:"]
    for r in recs:
        if r.get("outcome") == "seed":
            continue
        line = f"- gen {r['gen']} ({r['env']}, {r.get('change')}): {r.get('outcome')} — {r.get('reason')}"
        if r.get("rationale"):
            line += f". Board: {str(r['rationale']).rstrip('. ')}"
        ign = ((r.get("gate") or {}).get("ignored")) or []
        if ign:
            line += "; ignored at the gate: " + ", ".join(f"{i.get('sender')} ({i.get('why')})" for i in ign)
        out.append(line)
    return "\n".join(out) + "\n"


def render(records: list[dict], run: str | None = None, all_runs: bool = False) -> str:
    if not records:
        return "_no lineage records_\n"
    runs: dict[str, list[dict]] = {}
    for r in records:
        runs.setdefault(r.get("run") or "?", []).append(r)
    if run:
        picked = [run] if run in runs else []
    elif all_runs:
        picked = list(runs)
    else:
        picked = [records[-1].get("run") or "?"]
    if not picked:
        return f"_no records for run {run}_\n"
    return "\n".join(render_run(runs[k]) for k in picked)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m company.lineage_report", description=__doc__.split("\n\n")[0])
    ap.add_argument("path", nargs="?", default="company/lineage.jsonl")
    ap.add_argument("--run")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    md = render(read_lineage(a.path), run=a.run, all_runs=a.all)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
    sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
