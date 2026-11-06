"""The extension's room-session setup for a working agent (synaps-dev): skill file, per-room clone, session env."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

ROOM = "11111111-2222-3333-4444-555555555555"


def git(*a, cwd=None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x.invalid"}
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=env)


class FakeDaemon:
    created = []

    def __init__(self, deadline_s=20.0, cwd="/"):
        self.cwd = str(cwd)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def hello(self, env=None):
        self.env = dict(env or [])
        return {"type": "welcome", "sessions": []}

    def create(self, config):
        FakeDaemon.created.append({"config": config, "env": self.env, "cwd": self.cwd})
        return {"id": f"sess-{len(FakeDaemon.created)}", "pinned": True, "name": config.get("name")}


class RoomSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ext-room-"))
        main.CONFIG.update({"state_dir": str(self.tmp / "state"), "base_dir": str(self.tmp)})
        main.BASE = self.tmp
        src = self.tmp / "src"
        src.mkdir()
        git("init", "-q", "-b", "main", cwd=src)
        (src / "README.md").write_text("acme\n")
        git("add", "-A", cwd=src)
        git("commit", "-qm", "init", cwd=src)
        git("branch", "gen-1", cwd=src)
        self.bare = self.tmp / "origin.git"
        git("clone", "-q", "--bare", str(src), str(self.bare))
        (self.tmp / "skill.md").write_text("# synaps-dev\nEnd with a RESULT block.\n")
        FakeDaemon.created = []

    def tearDown(self):
        main.CONFIG.update({"state_dir": "", "base_dir": ""})

    def watcher(self, **extra):
        cfg = {"name": "synaps-dev", "bridge_url": "http://127.0.0.1:9", "registration_token_file": "/nonexistent",
               "model": "claude-sonnet-4-6", "max_session_cost": 1.2, "instructions": "Team remote: origin.",
               "skill_file": str(self.tmp / "skill.md"),
               "workdir_repo": {"url": "file://" + str(self.bare), "dir": "acme-shop"},
               "session_env": {"GH_CONFIG_DIR": str(self.tmp / "gh-none"), "GIT_AUTHOR_NAME": "Haseeb Khalid",
                               "GITHUB_TOKEN": "nope", "SYNAPS_MACHINE_TOKEN": "nope", "bad name": "x"},
               "session_path": [str(self.tmp / "venv" / "bin")]}
        cfg.update(extra)
        return main.Watcher(cfg)

    def test_session_env_allowlist_extra_and_path(self):
        env = dict(main.session_env({"GH_CONFIG_DIR": "~/x", "API_KEY": "s", "MY_TOKEN": "s", "PATH": "/evil",
                                     "LD_PRELOAD": "/x.so"}, ["/opt/venv/bin"]))
        self.assertEqual(env["GH_CONFIG_DIR"], os.path.expanduser("~/x"))
        for k in ("API_KEY", "MY_TOKEN", "LD_PRELOAD"):
            self.assertNotIn(k, env)
        self.assertTrue(env["PATH"].startswith("/opt/venv/bin" + os.pathsep))
        self.assertNotIn("/evil", env["PATH"])
        self.assertEqual(env["TERM"], "dumb")
        self.assertEqual(dict(main.session_env()).keys() - set(main.SESSION_ENV_KEYS), {"TERM"})

    def test_session_never_gets_the_ssh_agent(self):
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/run/user/1000/agent.sock", "SSH_AGENT_PID": "42"}):
            env = dict(main.session_env({"SSH_AUTH_SOCK": "/run/user/1000/agent.sock", "SSH_AGENT_PID": "42",
                                         "GIT_SSH_COMMAND": "ssh -i /k -o IdentityAgent=none"}))
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn("SSH_AGENT_PID", env)
        self.assertEqual(env["GIT_SSH_COMMAND"], "ssh -i /k -o IdentityAgent=none")

    def test_workdir_clone_runs_with_the_session_env(self):
        work = self.tmp / "work"
        work.mkdir()
        seen = {}
        real = subprocess.run

        def spy(argv, **kw):
            seen.update(kw.get("env") or {})
            return real(argv, **kw)
        senv = main.session_env({"GIT_SSH_COMMAND": "ssh -i /deploy-key", "GH_CONFIG_DIR": "/gh-none"})
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/run/agent.sock", "GH_TOKEN": "x"}), \
                mock.patch.object(main.subprocess, "run", spy):
            main.prepare_workdir(work, {"url": "file://" + str(self.bare), "dir": "acme-shop"}, senv)
        self.assertEqual(seen["GIT_SSH_COMMAND"], "ssh -i /deploy-key")
        self.assertNotIn("SSH_AUTH_SOCK", seen)
        self.assertNotIn("GH_TOKEN", seen)

    def test_prepare_workdir_clones_once(self):
        work = self.tmp / "work"
        work.mkdir()
        repo = {"url": "file://" + str(self.bare), "dir": "acme-shop", "branch": "gen-1"}
        d = main.prepare_workdir(work, repo)
        self.assertEqual(d, work / "acme-shop")
        head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=d, capture_output=True, text=True)
        self.assertEqual(head.stdout.strip(), "gen-1")
        (d / "local-change.txt").write_text("kept")
        self.assertEqual(main.prepare_workdir(work, repo), d)            # existing clone left alone
        self.assertTrue((d / "local-change.txt").exists())
        self.assertEqual(main.prepare_workdir(work, None), work)
        with self.assertRaises(RuntimeError):
            main.prepare_workdir(work, {"url": "file:///nonexistent/repo.git", "dir": "x"})

    def test_new_room_session_runs_in_its_own_clone_with_the_skill(self):
        w = self.watcher()
        with mock.patch.object(main, "DaemonClient", FakeDaemon):
            sid = w._create(ROOM, {"name": "m12-dev", "participants": []})
        self.assertEqual(sid, "sess-1")
        c = FakeDaemon.created[0]
        cwd = Path(c["config"]["cwd"])
        self.assertEqual(cwd.name, "acme-shop")
        self.assertTrue((cwd / ".git").exists() and (cwd / "README.md").exists())
        self.assertEqual(c["cwd"], str(cwd))
        self.assertIn("End with a RESULT block.", c["config"]["system"])
        self.assertIn("Team remote: origin.", c["config"]["system"])
        self.assertLess(c["config"]["system"].index("RESULT block"), c["config"]["system"].index("Operator instructions"))
        self.assertEqual(c["config"]["model_override"], "claude-sonnet-4-6")
        self.assertEqual(c["config"]["max_session_cost"], 1.2)
        self.assertEqual(c["env"]["GH_CONFIG_DIR"], str(self.tmp / "gh-none"))
        self.assertEqual(c["env"]["GIT_AUTHOR_NAME"], "Haseeb Khalid")
        for k in ("GITHUB_TOKEN", "SYNAPS_MACHINE_TOKEN", "bad name"):
            self.assertNotIn(k, c["env"])
        self.assertTrue(c["env"]["PATH"].startswith(str(self.tmp / "venv" / "bin")))
        self.assertEqual(w.state["rooms"][ROOM]["cwd"], str(cwd))

        # a resumed session keeps its clone (and its branches), no new clone
        (cwd / "wip.txt").write_text("x")
        with mock.patch.object(main, "DaemonClient", FakeDaemon):
            w._create(ROOM, {"name": "m12-dev"}, resume="sess-1")
        c2 = FakeDaemon.created[1]
        self.assertEqual(c2["config"]["cwd"], str(cwd))
        self.assertEqual(c2["config"]["continue_session"], "sess-1")
        self.assertTrue((cwd / "wip.txt").exists())

    def test_plain_agent_unchanged(self):
        w = self.watcher(name="synaps", skill_file=None, workdir_repo=None, session_env=None, session_path=None)
        with mock.patch.object(main, "DaemonClient", FakeDaemon):
            w._create(ROOM, {"name": "lab"})
        c = FakeDaemon.created[0]
        self.assertFalse((Path(c["config"]["cwd"]) / ".git").exists())
        self.assertEqual(set(c["env"]) - set(main.SESSION_ENV_KEYS), {"TERM"})
        self.assertNotIn("RESULT block", c["config"]["system"])


class SynapsDevEntryTest(unittest.TestCase):
    def load(self, tmp):
        import importlib.util
        with mock.patch.dict(os.environ, {"SYNAPS_SWITCH_DEV_BASE": tmp}):
            spec = importlib.util.spec_from_file_location("synaps_dev_tool", Path(__file__).parent.parent / "tools" / "synaps_dev.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        return mod

    def test_local_entry_has_no_secret_and_blocks_github(self):
        tmp = tempfile.mkdtemp(prefix="sd-entry-")
        mod = self.load(tmp)
        e = mod.entry("file:///x/origin.git", "o/acme-shop", False, "http://127.0.0.1:18400", "/t/registration.token")
        self.assertEqual(e["name"], "synaps-dev")
        self.assertEqual(e["skill_file"], "skill/synaps-dev.md")
        self.assertTrue((Path(__file__).parent.parent / e["skill_file"]).exists())
        self.assertEqual(e["session_env"]["GIT_SSH_COMMAND"], "false")
        self.assertIn("gh-none", e["session_env"]["GH_CONFIG_DIR"])
        self.assertIn("?head=<base>-fix-<N>&base=<base>", e["instructions"])
        blob = json.dumps(e)
        self.assertNotIn("api_key", blob)
        self.assertEqual(e["max_session_cost"], 3.0)
        self.assertTrue(e["model"].startswith("claude-sonnet"))
        env = dict(main.session_env(e["session_env"], e["session_path"]))
        self.assertEqual(env["GIT_AUTHOR_EMAIL"], "HaseebKhalid1507@users.noreply.github.com")

    def test_github_entry_pushes_with_the_deploy_key_only_and_opens_no_pr(self):
        tmp = tempfile.mkdtemp(prefix="sd-entry-")
        mod = self.load(tmp)
        key = os.path.join(tmp, "deploy", "acme-shop")
        g = mod.entry("git@github.com:HaseebKhalid1507/acme-shop.git", "HaseebKhalid1507/acme-shop", True,
                      "http://127.0.0.1:18400", "/t/registration.token", deploy_key=key)
        se = g["session_env"]
        self.assertTrue(se["GH_CONFIG_DIR"].endswith("gh-none") and not os.listdir(se["GH_CONFIG_DIR"]))
        self.assertIn(f"-i {key} ", se["GIT_SSH_COMMAND"])
        for opt in ("IdentitiesOnly=yes", "IdentityAgent=none", "-F /dev/null"):
            self.assertIn(opt, se["GIT_SSH_COMMAND"])
        self.assertNotIn("SSH_AUTH_SOCK", se)
        self.assertEqual(g["workdir_repo"]["url"], "git@github.com:HaseebKhalid1507/acme-shop.git")
        self.assertNotIn("gh pr create", g["instructions"])
        self.assertIn("BRANCH NAME", g["instructions"])
        self.assertIn("HaseebKhalid1507, adityapat958", g["instructions"])
        self.assertEqual(g["max_session_cost"], 3.0)
        self.assertNotIn("BEGIN", json.dumps(g))                       # the key is a path, never its content
        env = dict(main.session_env(se, g["session_path"]))           # what the extension really passes
        self.assertEqual(env["GIT_SSH_COMMAND"], se["GIT_SSH_COMMAND"])
        self.assertNotIn("SSH_AUTH_SOCK", env)
        genv = mod.github_env("HaseebKhalid1507/acme-shop", key)
        self.assertEqual(genv["ACME_REMOTE"], "git@github.com:HaseebKhalid1507/acme-shop.git")
        self.assertEqual(genv["ACME_ALLOW_GITHUB_WRITES"], "1")
        with self.assertRaises(ValueError):
            mod.entry("git@github.com:o/r.git", "o/r", True, "u", "t")


if __name__ == "__main__":
    unittest.main()
