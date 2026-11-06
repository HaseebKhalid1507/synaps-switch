#!/usr/bin/env python3
"""A room session's work as markdown: every tool call (name + input) and a short head of its result.
Thinking blocks are left out. Output goes through the shared evidence scrubber.

  python3 tools/session_trace.py SESSION_ID OUT.md [--base ~/.synaps-switch-dev] [--result-chars 600]
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "devstack"))
import dev  # noqa: E402

p = argparse.ArgumentParser()
p.add_argument("session")
p.add_argument("out")
p.add_argument("--base", default=str(dev.DEV_BASE))
p.add_argument("--result-chars", type=int, default=600)
a = p.parse_args()

s = json.loads((Path(a.base).expanduser() / "sessions" / f"{a.session}.json").read_text())
times = dict((i, t) for i, t in s.get("_message_times") or [])
md = [f"# Session {s['id']} ({s.get('name')})", "",
      f"model {s.get('model')}, thinking {s.get('thinking_level')}, cost ${s.get('session_cost', 0):.4f}, "
      f"{s.get('message_count')} messages", "",
      "Session env (names only): " + ", ".join(k for k, _ in s.get("env") or []), ""]


def text_of(c):
    if isinstance(c, str):
        return c
    out = []
    for b in c or []:
        if b.get("type") == "text":
            out.append(b["text"])
        elif b.get("type") == "tool_result":
            out.append(text_of(b.get("content")))
    return "\n".join(out)


for i, m in enumerate(s.get("api_messages") or []):
    when = (times.get(i) or "")[11:19]
    content = m.get("content")
    if m.get("role") == "user" and isinstance(content, str):
        md += [f"## `{when}` event", "", "```", content[:1500], "```", ""]
        continue
    for b in content if isinstance(content, list) else []:
        t = b.get("type")
        if t == "text" and b.get("text", "").strip():
            md += [f"**`{when}` {m['role']}:** " + b["text"].strip()[:1500], ""]
        elif t == "tool_use":
            inp = json.dumps(b.get("input"), ensure_ascii=False)
            md += [f"**`{when}` tool `{b.get('name')}`**", "", "```", inp[:2000], "```", ""]
        elif t == "tool_result":
            r = text_of(b.get("content")).strip()
            md += ["result:", "", "```", r[:a.result_chars] + (" …" if len(r) > a.result_chars else ""), "```", ""]

Path(a.out).write_text(dev.scrub("\n".join(md) + "\n"))
print(f"{a.out}: {len(s.get('api_messages') or [])} messages")
