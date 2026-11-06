"""Switch-side pieces of a live quarter: the transport, apply + membership, the scripted #board human,
synaps-dev's session cost, evidence export and scrubbing.

Nothing here restarts anything. Switch and Mattermost are reached over HTTP (switch_client and
devstack/dev.py); the private Synaps daemon is only read (its session files, for synaps-dev's cost).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from company import acme

ROOT = Path(__file__).resolve().parents[2]
DEV_BASE = Path(os.environ.get("SYNAPS_SWITCH_DEV_BASE") or "~/.synaps-switch-dev").expanduser()


def devstack_dir(path=None) -> Path | None:
    for d in (path, os.environ.get("SWITCH_DEVSTACK"), ROOT / "devstack", Path.home() / "Projects/synaps-switch/devstack"):
        if d and (Path(d).expanduser() / ".env").exists():
            return Path(d).expanduser()
    return None


def load_dev(path=None):
    from company.switch_env import load_devstack
    d = devstack_dir(path)
    if d is None:
        raise RuntimeError("no devstack dir with a .env (set SWITCH_DEVSTACK)")
    return load_devstack(d)


# ───────────────────────── the scripted #board human ─────────────────────────


class MattermostApprover:
    """A person's decision on a merge request, posted as the devstack's Mattermost user (`dev.py mm-post`,
    a human account the gate accepts) in the request's thread. Scripted: it approves only a PASS
    review with green harness tests, else rejects. The board never approves anything itself.

    channel: the #board Mattermost channel name. Every decision is recorded in .decisions (evidence)."""

    def __init__(self, devstack, channel, *, human="user", find_timeout=90.0, log=print, policy="review"):
        self.devstack, self.channel, self.human = Path(devstack), channel, human
        self.find_timeout, self.log, self.policy = find_timeout, log, policy
        self.decisions: list[dict] = []

    def _dev(self, *args):
        out = subprocess.run([sys.executable, str(self.devstack / "dev.py"), *args], capture_output=True,
                             text=True, timeout=90)
        if out.returncode != 0:
            raise RuntimeError(f"dev.py {args[0]} failed: {(out.stderr or out.stdout).strip()[-300:]}")
        return json.loads(out.stdout)

    def root_of(self, needle, since_ms):
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.find_timeout:
            for p in self._dev("mm-thread", self.channel, "--since", str(since_ms)):
                if needle in p["message"] and not p["root_id"]:
                    return p["id"]
            time.sleep(2)
        return None

    def __call__(self, rt, thread, req):
        from company.board.stubs import review_ok
        decision = "approve" if review_ok(req.review) else "reject"
        needle = f"merge request: acme-shop#{req.issue} ({req.kind})"
        root = self.root_of(needle, int((rt.gate_asked.get(thread, time.time()) - 120) * 1000))
        if root is None:
            self.log(f"approver: no Mattermost post for {needle!r} in ~{self.channel}; no decision posted")
            return None
        text = f"@{rt.t.reg(rt.board)} {decision}"
        out = self._dev("mm-post", self.channel, text, "--root", root)
        rec = {"issue": req.issue, "decision": decision, "as": self.human, "channel": self.channel,
               "mm_root": root, "mm_post": out.get("id"), "text": text, "scripted": True,
               "rule": "approve iff review PASS and harness pytest green"}
        self.decisions.append(rec)
        self.log(f"approver (scripted human, {self.human} via dev.py mm-post): {decision} acme-shop#{req.issue}")
        return decision


# ───────────────────────── synaps-dev's cost ─────────────────────────


class SessionCost:
    """synaps-dev's spend in this generation's #dev room: the session_cost of its room session(s) in the
    private daemon's base dir (read-only). Sessions are named sw-<agent>-<room name>-<hash>."""

    def __init__(self, room_name, *, agent="synaps-dev", base=DEV_BASE, since=None):
        self.room, self.agent, self.base, self.since = room_name, agent, Path(base), since

    def sessions(self):
        out = []
        slug = re.sub(r"[^a-z0-9]+", "-", self.room.lower()).strip("-")
        for f in sorted((self.base / "sessions").glob("*.json")):
            try:
                s = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            name = s.get("name") or ""
            if name.startswith(f"sw-{self.agent}-{slug}-") or name == f"sw-{self.agent}-{slug}":
                out.append({"id": s.get("id"), "name": name, "cost": float(s.get("session_cost") or 0),
                            "messages": s.get("message_count")})
        return out

    def __call__(self) -> float:
        return round(sum(s["cost"] for s in self.sessions()), 6)


# ───────────────────────── local-remote only ─────────────────────────


def local_merge(pr) -> bool:
    """devworker.merge_pr, refused for anything but a local PR ref (no GitHub write, ever)."""
    if not acme.is_local_remote(pr or ""):
        raise acme.GitHubWriteRefused(f"live quarter runs local-remote only; not merging {pr!r}")
    from company.devworker import merge_pr
    return merge_pr(pr)


def no_fallback(issue, kind, base=None, **_):
    return {"pr_url": None, "summary": "synaps-dev did not answer in time (no fallback in the quarter loop)",
            "tests_passed": False, "cost_usd": 0.0}


def live_review(issue_titles):
    from company import realwork as rw

    def review(pr, *, issue, issue_text="", tests=None):
        p = acme.parse_pr_ref(pr)
        if not p["local"]:
            raise acme.GitHubWriteRefused(f"live quarter runs local-remote only; not reviewing {pr!r}")
        tests = acme.run_tests_at(p["remote"], p["head"])
        return rw.review_pr(pr, issue=issue, issue_text=issue_text or issue_titles.get(issue, ""), tests=tests)
    return review


# ───────────────────────── evidence ─────────────────────────


def scrubber(state_dir, devstack=None):
    """The repo's evidence scrubber (devstack/dev.py scrub, with the stack's .env secrets) plus every
    credential under this run's state dir (Switch agent api keys), so nothing secret survives."""
    dev = load_dev(devstack)
    extra = set()
    for f in Path(state_dir).glob("agents/*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        extra.update(str(v) for k, v in d.items() if any(x in k for x in ("key", "token", "secret")))
    wt_scrub = None
    here = ROOT / "devstack" / "dev.py"      # this checkout's dev.py: also knows company/state/agents
    if here.exists() and here.resolve() != Path(dev.__file__).resolve():
        import importlib.util
        spec = importlib.util.spec_from_file_location("company_board_live_dev", here)
        wt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wt)
        wt_scrub = wt.scrub

    def scrub(text: str) -> str:
        for v in sorted((v for v in extra if len(v) >= 6), key=len, reverse=True):
            text = text.replace(v, "[REDACTED]")
        text = dev.scrub(text)
        return wt_scrub(text) if wt_scrub else text
    return scrub


def export_channels(devstack, channels: dict[str, str], since_ms: int, out_dir) -> dict[str, int]:
    """dev.py mm-thread for each {env room: Mattermost channel name} -> out_dir/mattermost-<room>.json."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for room, ch in channels.items():
        r = subprocess.run([sys.executable, str(Path(devstack) / "dev.py"), "mm-thread", ch, "--since", str(since_ms)],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            counts[room] = -1
            continue
        posts = json.loads(r.stdout)
        (out_dir / f"mattermost-{room}.json").write_text(json.dumps(posts, indent=1, ensure_ascii=False) + "\n")
        counts[room] = len(posts)
    return counts
