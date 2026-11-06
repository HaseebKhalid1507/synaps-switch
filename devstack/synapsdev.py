#!/usr/bin/env python3
"""Private Synaps for synaps-switch dev: its own base dir, auth broker and daemon.

Never touches ~/.synaps-cli except through `synaps auth-broker`, which reads the
live login in place and refreshes it under the same lock the live sessions use
(the synaps-bench pattern: no copy of auth.json, ever). The private daemon is a
broker client: it holds only short-lived access tokens, in memory.

  synapsdev.py setup              base dir, config, plugin link (idempotent)
  synapsdev.py agency             build the agency specialists plugin into the private plugins dir
  synapsdev.py broker start|stop|status
  synapsdev.py daemon start|stop|status
  synapsdev.py sessions           sessions in the private daemon
  synapsdev.py run -- ARGS...     run `synaps ARGS` against the private base dir
  synapsdev.py up | down          broker + daemon

Base dir: $SYNAPS_SWITCH_DEV_BASE or ~/.synaps-switch-dev. Broker: 127.0.0.1:18191.
"""
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path(os.path.expanduser("~"))
BASE = Path(os.environ.get("SYNAPS_SWITCH_DEV_BASE") or HOME / ".synaps-switch-dev")
LIVE = HOME / ".synaps-cli"
BROKER_DIR = BASE / "broker"
TOKEN = BROKER_DIR / "machine.token"
BROKER_HOST, BROKER_PORT = "127.0.0.1", int(os.environ.get("SYNAPS_SWITCH_BROKER_PORT", "18191"))
BROKER_URL = f"http://{BROKER_HOST}:{BROKER_PORT}"
SYNAPS = shutil.which("synaps") or "synaps"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CONFIG = """\
# synaps-switch private dev daemon (written by devstack/synapsdev.py setup)
model = claude-sonnet-4-6
thinking = low
client_input = shared
extension.synaps-switch.base_dir = {base}
extension.synaps-switch.state_dir = {base}/synaps-switch
extension.synaps-switch.config_file = {cfg}
"""


def die(msg, code=1):
    print(f"synapsdev: {msg}", file=sys.stderr)
    sys.exit(code)


def guard():
    if BASE.resolve() == LIVE.resolve() or LIVE.resolve() in BASE.resolve().parents:
        die(f"refusing: base dir {BASE} is the live Synaps dir")
    if BROKER_PORT == 18181:
        die("refusing: 18181 is the synaps-bench campaign broker")


def private_env(with_token=True):
    """A rebuilt environment: nothing of the caller's Synaps settings leaks in."""
    e = {k: os.environ[k] for k in ("HOME", "PATH", "LANG", "USER", "LOGNAME", "TERM", "SHELL", "TZ")
         if k in os.environ}
    e.update({"SYNAPS_BASE_DIR": str(BASE), "SYNAPS_AUTH_ENDPOINT": BROKER_URL,
              "SYNAPS_DISABLE_PROJECT_PLUGINS": "1", "SYNAPS_DAEMON_AUTOSPAWN": "0"})
    if with_token:
        try:
            e["SYNAPS_MACHINE_TOKEN"] = TOKEN.read_text().strip()
        except FileNotFoundError:
            die("no broker token: run `synapsdev.py broker start` first")
    return e


def setup(_=None):
    guard()
    for d in (BASE, BASE / "plugins", BASE / "work", BROKER_DIR):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    cfg_json = BASE / "synaps-switch.json"
    cfg = BASE / "config"
    want = CONFIG.format(cfg=cfg_json, base=BASE)
    if not cfg.exists() or "extension.synaps-switch.base_dir" not in cfg.read_text():
        cfg.write_text(want)
        os.chmod(cfg, 0o600)
    if not cfg_json.exists():
        dev_env = REPO / "devstack" / ".env"
        port = "18400"
        if dev_env.exists():
            for line in dev_env.read_text().splitlines():
                if line.startswith("API_HOST_PORT="):
                    port = line.split("=", 1)[1].strip()
        cfg_json.write_text(json.dumps({"agents": [{
            "name": "synaps",
            "display_name": "Synaps",
            "description": "A Synaps agent: reads the room, uses tools, answers in threads.",
            "bridge_url": f"http://127.0.0.1:{port}",
            "registration_token_file": str(REPO / "devstack" / "state" / "registration.token"),
            "model": "claude-sonnet-4-6",
            "max_session_cost": 0.50,
            "addressing": "owner_and_owner_agents",
        }]}, indent=1) + "\n")
        os.chmod(cfg_json, 0o600)
    link = BASE / "plugins" / "synaps-switch"
    if not link.exists() and not link.is_symlink():
        link.symlink_to(REPO)
    print(f"base dir {BASE}: config, synaps-switch.json, plugins/synaps-switch -> {os.readlink(link)}")
    agency()


def agency(_=None):
    """The `agency` specialists plugin (agency/build_plugin.py, PR #15) as a real directory in the
    private plugins dir, so room sessions can dispatch agent="agency:<slug>". Never the main install."""
    out = BASE / "plugins" / "agency"
    if out.is_symlink():
        die(f"{out} is a symlink; the agency plugin must be a real directory")
    if (out / ".agency-build").exists():
        print(f"agency plugin present: {out}")
        return
    script = REPO / "agency" / "build_plugin.py"
    if not script.exists():
        print("agency plugin: skipped (agency/build_plugin.py is not on this branch yet; "
              "see PR #15, or: git show origin/feat/agency:agency/build_plugin.py > /tmp/b.py && "
              f"python3 /tmp/b.py --out {out})")
        return
    subprocess.run([sys.executable, str(script), "--out", str(out)], check=True, timeout=600)


# ───────────────────────── broker ─────────────────────────


def healthz():
    try:
        with _OPENER.open(BROKER_URL + "/healthz", timeout=3) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return 0


def token_probe():
    req = urllib.request.Request(BROKER_URL + "/token?provider=anthropic&account=default")
    req.add_header("Authorization", f"Bearer {TOKEN.read_text().strip()}")
    try:
        with _OPENER.open(req, timeout=15) as r:
            body = json.loads(r.read())
            return r.status, {"ttl_s": int(body.get("ttl_ms", 0)) // 1000, "account": body.get("account")}
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read()[:200].decode("utf-8", "replace")}
    except (urllib.error.URLError, OSError) as e:
        return 0, {"error": type(e).__name__}


def cmdline(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def read_pid(p):
    try:
        return int(Path(p).read_text().strip())
    except (OSError, ValueError):
        return None


def broker_pid():
    pid = read_pid(BROKER_DIR / "broker.pid")
    if pid and "auth-broker" in cmdline(pid) and str(TOKEN) in cmdline(pid):
        return pid
    return None


def broker_start(_=None):
    guard()
    setup()
    if broker_pid() and healthz() == 200:
        print(f"broker already up on {BROKER_URL} (pid {broker_pid()})")
        return
    with socket.socket() as s:
        s.settimeout(0.5)
        if s.connect_ex((BROKER_HOST, BROKER_PORT)) == 0:
            die(f"{BROKER_HOST}:{BROKER_PORT} is in use by something we did not start; refusing")
    if not TOKEN.exists():
        fd = os.open(TOKEN, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    env = {k: os.environ[k] for k in ("HOME", "PATH", "LANG") if k in os.environ}  # live base dir, in place
    argv = [SYNAPS, "auth-broker", "--bind", f"{BROKER_HOST}:{BROKER_PORT}", "--machine-token-file", str(TOKEN)]
    log = open(BROKER_DIR / "broker.log", "ab")
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                         cwd=str(BROKER_DIR), start_new_session=True)
    (BROKER_DIR / "broker.pid").write_text(f"{p.pid}\n")
    for _ in range(100):
        if healthz() == 200:
            break
        if p.poll() is not None:
            die(f"broker exited rc={p.returncode}; see {BROKER_DIR}/broker.log")
        time.sleep(0.2)
    else:
        die(f"broker not healthy; see {BROKER_DIR}/broker.log")
    code, info = token_probe()
    print(f"broker up on {BROKER_URL} (pid {p.pid}); /token -> {code} {info}")
    if code != 200:
        die("the broker cannot vend a token: STOP (do not improvise with credentials)")


def broker_stop(_=None):
    pid = broker_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not cmdline(pid):
                break
            time.sleep(0.1)
    (BROKER_DIR / "broker.pid").unlink(missing_ok=True)
    if TOKEN.exists():
        with open(TOKEN, "r+b") as f:
            f.write(os.urandom(64))
        TOKEN.unlink()
    print(f"broker stopped (pid {pid}); machine token shredded")


def broker_status(_=None):
    st = {"url": BROKER_URL, "pid": broker_pid(), "healthz": healthz(), "token_file": TOKEN.exists()}
    if TOKEN.exists() and st["healthz"] == 200:
        st["token"] = token_probe()
    print(json.dumps(st))


# ───────────────────────── daemon ─────────────────────────


def synaps(args, check=False, capture=False, timeout=60):
    guard()
    return subprocess.run([SYNAPS, *args], env=private_env(), cwd=str(BASE / "work"),
                          stdin=subprocess.DEVNULL, capture_output=capture, text=True,
                          timeout=timeout, check=check)


def daemon_start(_=None):
    setup()
    if healthz() != 200:
        die("broker is down: `synapsdev.py broker start` first")
    st = synaps(["daemon", "status"], capture=True)
    if st.returncode == 0 and "running" in (st.stdout + st.stderr).lower() and "not running" not in (st.stdout + st.stderr).lower():
        print("private daemon already running")
        return
    t0 = time.time()
    log = open(BASE / "daemon.log", "ab")
    p = subprocess.Popen([SYNAPS, "daemon", "--foreground"], env={**private_env(), "SYNAPS_EXTENSIONS_TRACE": "1"},
                         cwd=str(BASE / "work"), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)
    (BASE / "daemon.pid").write_text(f"{p.pid}\n")
    sock = BASE / "run" / "daemon.sock"
    for _ in range(150):
        if sock.exists():
            break
        if p.poll() is not None:
            die(f"daemon exited rc={p.returncode}; see {BASE}/daemon.log")
        time.sleep(0.2)
    else:
        die(f"no daemon socket at {sock}; see {BASE}/daemon.log")
    # The extension must be talking to THIS base dir, never the live one: check its own word.
    elog = BASE / "synaps-switch" / "extension.log"
    want = f"started: base {BASE.resolve()},"
    for _ in range(75):
        lines = elog.read_text(errors="replace").splitlines()[-50:] if elog.exists() else []
        started = [l for l in lines if "] started: base " in l]
        if started and want in started[-1] and started[-1] >= time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0)):
            break
        time.sleep(0.2)
    else:
        daemon_stop()
        die(f"the extension did not report base {BASE} within 15 s: daemon stopped. See {elog}")
    print(f"private daemon up (pid {p.pid}, socket {sock}); extension on base {BASE}")


def daemon_stop(_=None):
    r = synaps(["daemon", "stop"], capture=True)
    print((r.stdout + r.stderr).strip() or f"exit {r.returncode}")
    pid = read_pid(BASE / "daemon.pid")
    if pid and "daemon" in cmdline(pid):
        for _ in range(100):
            if not cmdline(pid):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGTERM)
    (BASE / "daemon.pid").unlink(missing_ok=True)


def daemon_status(_=None):
    r = synaps(["daemon", "status"], capture=True)
    print((r.stdout + r.stderr).strip())


def sessions(_=None):
    r = synaps(["daemon", "sessions"], capture=True)
    print((r.stdout + r.stderr).strip())


def run(args):
    os.execvpe(SYNAPS, [SYNAPS, *args], private_env())


def main(argv):
    if not argv:
        die(__doc__.strip(), 2)
    cmd, rest = argv[0], argv[1:]
    table = {
        ("setup",): setup, ("agency",): agency, ("broker", "start"): broker_start, ("broker", "stop"): broker_stop,
        ("broker", "status"): broker_status, ("daemon", "start"): daemon_start,
        ("daemon", "stop"): daemon_stop, ("daemon", "status"): daemon_status, ("sessions",): sessions,
    }
    if cmd == "run":
        guard()
        os.chdir(BASE / "work")
        return run(rest[1:] if rest[:1] == ["--"] else rest)
    if cmd == "up":
        broker_start()
        return daemon_start()
    if cmd == "down":
        daemon_stop()
        return broker_stop()
    fn = table.get((cmd, *rest[:1])) or table.get((cmd,))
    if fn is None:
        die(__doc__.strip(), 2)
    fn()


if __name__ == "__main__":
    main(sys.argv[1:])
