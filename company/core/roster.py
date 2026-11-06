"""company/core/roster.py — Agency persona roster loader and helpers.

Personas sourced from https://github.com/msitarzewski/agency-agents (MIT).
Each .md file has YAML frontmatter (name, description) and a markdown body.
persona_id = "<division>/<filename-stem>", e.g. "support/support-infrastructure-maintainer".
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# Divisions to include (excludes integrations, strategy, examples, scripts)
_ALLOWED_DIVISIONS = {
    "academic", "design", "engineering", "finance", "game-development", "gis",
    "healthcare", "marketing", "paid-media", "product", "project-management",
    "research", "sales", "security", "spatial-computing", "specialized",
    "support", "testing", "tools",
}

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text). Minimal YAML parse (key: value only)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_raw = m.group(1)
    body = text[m.end():]
    fm: dict[str, Any] = {}
    for line in fm_raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip()
            v = v.strip()
            if v and not v.startswith("{") and not v.startswith("["):
                fm[k] = v.strip('"').strip("'")
    return fm, body


def load_roster(root) -> dict[str, dict]:
    """
    Load all persona .md files from *root* (the agency-agents repo clone).
    Returns dict mapping persona_id -> {id, division, name, description, path, body}.
    Skips integrations/, strategy/, examples/, scripts/ and non-.md files.
    """
    root = Path(root)
    roster: dict[str, dict] = {}

    for division_dir in sorted(root.iterdir()):
        if not division_dir.is_dir():
            continue
        division = division_dir.name
        if division not in _ALLOWED_DIVISIONS:
            continue
        for md_file in sorted(division_dir.glob("*.md")):
            stem = md_file.stem
            persona_id = f"{division}/{stem}"
            text = md_file.read_text(encoding="utf-8", errors="replace")
            fm, body = _parse_frontmatter(text)
            roster[persona_id] = {
                "id": persona_id,
                "division": division,
                "name": fm.get("name", stem),
                "description": fm.get("description", ""),
                "path": str(md_file),
                "body": body.strip(),
            }

    return roster


def search(roster: dict[str, dict], division: str | None = None, text: str | None = None) -> list[dict]:
    """
    Filter roster by division and/or text (case-insensitive substring match
    against name + description + id).
    Returns list of persona dicts, sorted by id.
    """
    results = []
    text_lower = text.lower() if text else None
    for persona in roster.values():
        if division and persona["division"] != division:
            continue
        if text_lower:
            haystack = (
                persona["name"] + " " + persona["description"] + " " + persona["id"]
            ).lower()
            if text_lower not in haystack:
                continue
        results.append(persona)
    return sorted(results, key=lambda p: p["id"])


def instructions_for(persona: dict, role_note: str) -> str:
    """
    Build agent instructions: role_note (company-specific duties + @-addressing rules)
    followed by the persona body, trimmed to ~4000 chars total.
    """
    header = role_note.rstrip() + "\n\n---\n\n"
    max_body = max(0, 4000 - len(header))
    body = persona["body"]
    if len(body) > max_body:
        body = body[:max_body].rstrip() + "\n… (truncated)"
    return header + body
