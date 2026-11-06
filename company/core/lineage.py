"""company/core/lineage.py — JSONL lineage log: append + read."""
from __future__ import annotations

import json
from pathlib import Path


def append_lineage(path, record: dict) -> None:
    """Append one record (dict) to a JSONL file at *path* (creates if missing)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def read_lineage(path) -> list[dict]:
    """Read all records from a JSONL lineage file. Returns [] if file missing."""
    p = Path(path)
    if not p.exists():
        return []
    records = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records
