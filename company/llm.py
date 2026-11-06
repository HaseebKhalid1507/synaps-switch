"""company/llm.py — cheap one-shot LLM calls through a private Synaps auth-broker.

Every call is one fresh headless `synaps chat` process:

    synaps chat --profile <profile> --no-extensions -s <system>      (prompt on stdin)

Credentials come only from an auth-broker: the subprocess gets the broker's machine
token (read from a file) and the broker URL, and nothing else from the caller's
environment. No auth.json is ever read, written or copied by this module.

Configuration (environment, read at call time):

    COMPANY_SYNAPS_PROFILE      the name the default "company" profile has here (default "company")
    COMPANY_BROKER_TOKEN_FILE   broker machine-token file      (default ~/.synaps-cli/company/broker/token)
    COMPANY_BROKER_URL          broker base URL                (default http://127.0.0.1:18282)
    COMPANY_SYNAPS_BASE_DIR     SYNAPS_BASE_DIR for the call   (default: synaps's own default)
    COMPANY_SYNAPS_BIN          synaps binary                  (default: `synaps` on PATH)

The defaults are the ones in company/INTERFACES.md. On bella the private broker is the one
devstack/synapsdev.py runs (127.0.0.1:18191, token ~/.synaps-switch-dev/broker/machine.token)
and the profile lives in its own base dir, e.g.:

    export COMPANY_SYNAPS_BASE_DIR=~/.synaps-switch-dev/company-llm
    export COMPANY_BROKER_TOKEN_FILE=~/.synaps-switch-dev/broker/machine.token
    export COMPANY_BROKER_URL=http://127.0.0.1:18191
    python -m company.llm setup      # writes <base>/<profile>/{config,system.md,mcp.json}
    python -m company.llm ping       # one live JSON call, prints the reply and its cost

The profile is a Haiku-class model with thinking off, a tiny system.md, an empty mcp.json,
every built-in tool disabled (a completion call needs none, and market/room text must not
reach a shell), and auth.remote_endpoint = the broker.

Headless `synaps chat` submits each stdin line as its own turn and treats a line starting
with "/" as a slash command, so the prompt is sent as ONE line: newlines become the two
characters "\\n" and a leading "/" is escaped. Reply text is stdout without banner lines;
the cost is the trailing "session saved: <id> ($X)" line.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_PROFILE = "company"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_TOKEN_FILE = "~/.synaps-cli/company/broker/token"
DEFAULT_BROKER_URL = "http://127.0.0.1:18282"

# Every built-in tool name synaps 0.11 registers; unknown names are ignored by synaps.
NO_TOOLS = (
    "read,write,edit,grep,find,ls,bash,shell_start,shell_send,shell_end,powershell,"
    "subagent,subagent_start,subagent_status,subagent_steer,subagent_collect,subagent_resume,"
    "subagent_models,subagent_model_authorize,search_tools,activate_tools,memory_context,"
    "memory_search,memory_fetch,memory_store,memory_forget,forum_post,forum_read,forum_forget,"
    "context_checkpoint,send_channel,respond,watcher_exit"
)

PROFILE_CONFIG = """\
# company/llm.py profile: cheap one-shot completions through a private auth-broker.
# No credentials here: the machine token is passed per call from a file.
model = {model}
thinking = off
auth.remote_endpoint = {broker}
disabled_tools = {tools}
"""
PROFILE_SYSTEM = "You are a concise assistant inside a simulated company. Answer briefly.\n"
PROFILE_MCP = '{"mcpServers": {}}\n'

JSON_SYSTEM = "Reply with a single JSON object and nothing else: no prose, no code fences."
INLINE_SYSTEM_MAX = 64 * 1024   # longer system prompts go through a file (argv limit)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_COST = re.compile(r"session saved: \S+ \(\$([0-9]+(?:\.[0-9]+)?)\)")
_BANNER = re.compile(
    r"^(synaps \d+\.\d+\S* \| .* \| session \S+"
    r"|session saved: .*"
    r"|↳ .*"
    r"|🎭 Agent: .*"
    r"|warning: .*unsent attachment.*)$"
)
_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?\s*```\s*$", re.S)


class LLMError(RuntimeError):
    """A call failed. `cost_usd` is what was spent anyway (book it)."""

    def __init__(self, msg: str, cost_usd: float = 0.0):
        super().__init__(msg)
        self.cost_usd = cost_usd


# ───────────────────────── config ─────────────────────────


def _cfg(profile: str = DEFAULT_PROFILE) -> dict:
    e = os.environ
    base = e.get("COMPANY_SYNAPS_BASE_DIR")
    if not profile or profile == DEFAULT_PROFILE:          # COMPANY_SYNAPS_PROFILE renames "company"
        profile = e.get("COMPANY_SYNAPS_PROFILE") or DEFAULT_PROFILE
    return {
        "profile": profile,
        "token_file": Path(os.path.expanduser(e.get("COMPANY_BROKER_TOKEN_FILE") or DEFAULT_TOKEN_FILE)),
        "broker": (e.get("COMPANY_BROKER_URL") or DEFAULT_BROKER_URL).rstrip("/"),
        "base_dir": Path(os.path.expanduser(base)) if base else None,
        "bin": e.get("COMPANY_SYNAPS_BIN") or shutil.which("synaps") or "synaps",
    }


def _read_token(path: Path) -> str:
    try:
        tok = path.read_text().strip()
    except OSError as exc:
        raise LLMError(f"no broker machine token at {path} ({type(exc).__name__}); "
                       "start the private broker or set COMPANY_BROKER_TOKEN_FILE") from None
    if not tok:
        raise LLMError(f"broker machine token file {path} is empty")
    return tok


def _env(cfg: dict, token: str) -> dict:
    """A rebuilt environment: none of the caller's SYNAPS_* settings leak in."""
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "LC_ALL", "USER", "LOGNAME", "TZ")
           if k in os.environ}
    env.update({
        "SYNAPS_MACHINE_TOKEN": token,
        "SYNAPS_AUTH_ENDPOINT": cfg["broker"],   # broker only, even if the profile config is missing
        "SYNAPS_DISABLE_PROJECT_PLUGINS": "1",
        "SYNAPS_DAEMON_AUTOSPAWN": "0",
        "NO_COLOR": "1",
    })
    if cfg["base_dir"] is not None:
        env["SYNAPS_BASE_DIR"] = str(cfg["base_dir"])
    return env


# ───────────────────────── parsing ─────────────────────────


def _one_line(prompt: str) -> str:
    text = prompt.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("empty prompt")
    text = "\\n".join(text.split("\n"))
    return "\\" + text if text.startswith("/") else text


def _parse_cost(text: str) -> float | None:
    hits = _COST.findall(_ANSI.sub("", text))
    return float(hits[-1]) if hits else None


def _strip_banner(text: str) -> str:
    lines = [l for l in _ANSI.sub("", text).splitlines() if not _BANNER.match(l.strip())]
    return "\n".join(lines).strip()


def _extract_json(text: str):
    t = text.strip()
    m = _FENCE.match(t)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if 0 <= i < j:
            return json.loads(t[i:j + 1])
        raise


def _tail(s: str, n: int = 400) -> str:
    return _ANSI.sub("", s).strip()[-n:]


# ───────────────────────── calls ─────────────────────────


def complete(system: str, prompt: str, *, profile: str = DEFAULT_PROFILE, timeout: int = 120) -> tuple[str, float]:
    """One completion. Returns (reply text without banner lines, cost_usd).

    profile "company" resolves to $COMPANY_SYNAPS_PROFILE when that is set. Raises LLMError on a missing
    token, a timeout, a non-zero exit or an empty reply; ValueError on an empty prompt.
    """
    line = _one_line(prompt)
    cfg = _cfg(profile)
    env = _env(cfg, _read_token(cfg["token_file"]))
    with tempfile.TemporaryDirectory(prefix="company-llm-") as cwd:   # neutral cwd: no project context
        argv = [cfg["bin"], "chat", "--profile", cfg["profile"], "--no-extensions"]
        if system and system.strip():
            if len(system.encode()) > INLINE_SYSTEM_MAX:
                p = Path(cwd) / "system.md"
                p.write_text(system)
                system = str(p)
            argv += ["-s", system]
        try:
            r = subprocess.run(argv, input=line + "\n", capture_output=True, text=True,
                               timeout=timeout, env=env, cwd=cwd)
        except subprocess.TimeoutExpired:
            raise LLMError(f"synaps chat timed out after {timeout}s (profile {cfg['profile']})") from None
        except FileNotFoundError:
            raise LLMError(f"synaps binary not found: {cfg['bin']}") from None
    out, err = r.stdout or "", r.stderr or ""
    cost = _parse_cost(err + "\n" + out)
    if cost is None:
        log.warning("company.llm: no 'session saved' cost line (exit %s); booking $0", r.returncode)
        cost = 0.0
    if r.returncode != 0:
        raise LLMError(f"synaps chat exited {r.returncode}: {_tail(err or out)}", cost)
    text = _strip_banner(out)
    if not text:
        raise LLMError(f"synaps chat returned no text: {_tail(err)}", cost)
    return text, cost


def complete_json(system: str, prompt: str, **kw) -> tuple[dict, float]:
    """complete() that must return a JSON object: strips ``` fences, json.loads, one retry.

    The cost is summed over both attempts; on a second failure LLMError carries it.
    """
    sys_prompt = f"{system.rstrip()}\n\n{JSON_SYSTEM}" if system and system.strip() else JSON_SYSTEM
    total = 0.0
    ask, last = prompt, ""
    for attempt in (1, 2):
        try:
            text, cost = complete(sys_prompt, ask, **kw)
        except LLMError as exc:
            exc.cost_usd += total
            raise
        total += cost
        try:
            obj = _extract_json(text)
            if isinstance(obj, dict):
                return obj, total
            last = f"got {type(obj).__name__}, not an object"
        except json.JSONDecodeError as exc:
            last = str(exc)
        ask = (f"{prompt}\n\nYour previous reply was not a valid JSON object ({last}). "
               "Reply again with ONLY the JSON object.")
    raise LLMError(f"no JSON object after 2 attempts: {last}", total)


# ───────────────────────── profile setup / ping ─────────────────────────


def ensure_profile(base_dir: Path, profile: str, broker: str, model: str = DEFAULT_MODEL) -> Path:
    """Write <base>/<profile>/{config,system.md,mcp.json} if missing. Never touches auth."""
    base_dir = Path(base_dir).expanduser()
    live = Path("~/.synaps-cli").expanduser()
    if base_dir.resolve() == live.resolve():
        raise LLMError(f"refusing to write a profile into the live Synaps dir {live}")
    d = base_dir / profile
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(base_dir, 0o700)
    os.chmod(d, 0o700)
    files = {"config": PROFILE_CONFIG.format(model=model, broker=broker.rstrip("/"), tools=NO_TOOLS),
             "system.md": PROFILE_SYSTEM, "mcp.json": PROFILE_MCP}
    for name, body in files.items():
        p = d / name
        if not p.exists():
            p.write_text(body)
            os.chmod(p, 0o600)
    return d


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "setup":
        cfg = _cfg()
        if cfg["base_dir"] is None:
            print("set COMPANY_SYNAPS_BASE_DIR to a private base dir first", file=sys.stderr)
            return 2
        print(ensure_profile(cfg["base_dir"], cfg["profile"], cfg["broker"]))
        return 0
    if cmd == "ping":
        obj, cost = complete_json(
            "You are the finance department of a small online shop.",
            'Reply with {"dept": "finance", "ok": true, "note": "<five words or fewer>"}.')
        print(json.dumps({"reply": obj, "cost_usd": cost}))
        return 0
    print("usage: python -m company.llm setup|ping", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
