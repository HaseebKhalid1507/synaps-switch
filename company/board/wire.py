"""The company's view of a transport: env agent names in, Switch registry names out, plus activity.

On the shared Switch stack the company's agents are registered under a prefix (`m10-board`,
`m10-growth`, ...) so runs don't squat on global names. Departments, the board and realwork keep
speaking env names (`board`, `@growth`): CompanyTransport maps the poster, the handler's identity
and every `@name` in the text both ways. synaps-dev (and anyone not in the map) keeps its own name.
It also counts handlers in flight and the last post/delivery, so a week can end when the rooms go quiet.
"""
from __future__ import annotations

import re
import threading
import time

from company.depts.agent import transport_rooms
from company.transport import Msg, parse_mentions

DEV = "synaps-dev"
MARKET = "market"
_AT = re.compile(r"@([A-Za-z0-9._-]+)")


def company_rooms(env, humans, *, hub="board", market=MARKET) -> dict[str, list[str]]:
    """Room membership the quarter's agents assume, on FakeTransport and Switch alike: transport_rooms(env)
    (each room's agents + humans, board everywhere, every agent in #board, linked rooms share agents)
    plus the market identity in every room."""
    return {name: list(dict.fromkeys([*members, market]))
            for name, members in transport_rooms(env, humans, hub).items()}


def extra_members(env, rooms, reg, *, dev=DEV) -> dict[str, list[str]]:
    """What apply_env must add on top of the env's own room agents (Switch names), so Switch's membership
    matches company_rooms: every dept agent in #board, the market everywhere, linked rooms' agents.
    Except synaps-dev: it acts on whatever addresses it in a room it's in, so it stays where the env
    puts it (#dev) and gets work in #dev only: from the board's dispatch, or (#25) from a department
    agent whose room is linked to #dev, e.g. ops-maint with `links: [[maintenance, dev]]`."""
    names = {a["name"] for a in env.agents} | {MARKET}
    own = {r["name"]: set(r.get("agents") or []) for r in env.rooms}
    return {room: [reg(a) for a in members if a in names and a != dev and a not in own.get(room, set())]
            for room, members in rooms.items()}


class CompanyTransport:
    """Wraps a Transport (FakeTransport or SwitchTransport). names: {env name: registry name}."""

    def __init__(self, inner, names=None):
        self.inner = inner
        self.out = {k.lower(): v for k, v in (names or {}).items() if k != v}
        self.back = {v.lower(): k for k, v in (names or {}).items() if k != v}
        self._lock = threading.Lock()
        self.busy = 0
        self.last = time.monotonic()

    # names
    def reg(self, name):
        return self.out.get(name.lower(), name)

    def env_name(self, name):
        return self.back.get((name or "").lower(), name)

    @staticmethod
    def _rewrite(text, table):
        if not table or not text:
            return text

        def sub(m):
            tok = m.group(1)
            for core in (tok, tok.rstrip("._-")):
                if core.lower() in table:
                    return "@" + table[core.lower()] + tok[len(core):]
            return m.group(0)
        return _AT.sub(sub, text)

    def text_out(self, text):
        return self._rewrite(text, self.out)

    def text_in(self, text):
        return self._rewrite(text, self.back)

    def _in(self, msg: Msg) -> Msg:
        sender = self.env_name(msg.sender)
        is_agent = msg.sender_is_agent and sender != MARKET   # the market is outside the company: like a person
        if not self.back and is_agent == msg.sender_is_agent:
            return msg
        text = self.text_in(msg.text)
        return Msg(msg.id, msg.room, msg.thread, sender, is_agent, text, parse_mentions(text))

    def touch(self):
        with self._lock:
            self.last = time.monotonic()

    # Transport contract
    def post(self, as_agent, room, text, thread=None):
        self.touch()
        mid = self.inner.post(self.reg(as_agent), room, self.text_out(text), thread=thread)
        self.touch()
        return mid

    def on_message(self, agent, handler):
        def run(msg):
            with self._lock:
                self.busy += 1
                self.last = time.monotonic()
            try:
                handler(self._in(msg))
            finally:
                with self._lock:
                    self.busy -= 1
                    self.last = time.monotonic()
        self.inner.on_message(self.reg(agent), run)

    def start(self):
        self.inner.start()

    def stop(self):
        stop = getattr(self.inner, "stop", None)
        if stop:
            stop()

    def say(self, sender, room, text, thread=None, *, is_agent=False):
        """A person (FakeTransport only)."""
        self.touch()
        return self.inner.say(sender, room, self.text_out(text), thread, is_agent=is_agent)

    def wait_quiet(self, quiet_s, max_s, poll=0.2) -> tuple[bool, float]:
        """Block until no handler runs and nothing was posted or delivered for quiet_s, at most max_s.
        (went_quiet, seconds waited)."""
        t0 = time.monotonic()
        while True:
            now = time.monotonic()
            with self._lock:
                idle = self.busy == 0 and now - self.last >= quiet_s
            if idle:
                return True, round(now - t0, 1)
            if now - t0 >= max_s:
                return False, round(now - t0, 1)
            time.sleep(poll)

    # pass-throughs
    def __getattr__(self, name):          # records, errors, transcript, pin_rooms, _link, ...
        return getattr(self.inner, name)

    def link(self, agent):
        return self.inner._link(self.reg(agent))
