#!/usr/bin/env python3
"""Set up synaps-dev, the extension's engineering agent, in the private Synaps base dir.

  python3 tools/synaps_dev.py setup [--gen 1] [--github]     local stand-in origin (default) or the real repo
  python3 tools/synaps_dev.py entry [--github]               print the agent entry only

Local mode (the default while there is no push access to acme-shop): a bare clone of
github.com/adityapat958/acme-shop under <base>/m12/remote stands in for origin. It gets a `baseline`
tag and a `gen-<k>` branch (company/acme.py); synaps-dev clones it into its room work dir, pushes its
fix branch there, and reports a local PR ref. --github writes the entry for the real repo instead
(clone over ssh, `gh pr create`); it needs push access and ACME_ALLOW_GITHUB_WRITES=1 for the merge.

The entry goes into <base>/synaps-switch.json (other agents are kept). Restart the private daemon
afterwards (devstack/synapsdev.py daemon stop && ... daemon start) to load it. No secret is written:
the registration token stays in its file, read by the extension at registration.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from company import acme  # noqa: E402

BASE = Path(os.environ.get("SYNAPS_SWITCH_DEV_BASE") or "~/.synaps-switch-dev").expanduser()
VENV_BIN = Path("~/.local/share/synaps-switch/company-venv/bin").expanduser()
AUTHOR = ("Haseeb Khalid", "HaseebKhalid1507@users.noreply.github.com")

LOCAL_NOTE = """\
Team remote: `origin` is {remote}, a local bare clone standing in for github.com/{repo} (no push
access to GitHub yet). There is no GitHub PR: after you push `<base>-fix-<N>`, the PR is the local
PR ref `{remote}?head=<base>-fix-<N>&base=<base>`. Put that in the RESULT block's `pr`. Do not run
`gh` (it is not logged in) and do not add or change git remotes."""

GITHUB_NOTE = """\
Team remote: `origin` is github.com/{repo}. After you push `<base>-fix-<N>`, open the PR with
`gh pr create -R {repo} --base <base> --head <base>-fix-<N> --title "Fix #<N>: ..." --body "Closes #<N> ..."`
and put its URL in the RESULT block's `pr`. Never merge it."""


def entry(remote: str, repo: str, github: bool, bridge_url: str, token_file: str) -> dict:
    env = {"GIT_AUTHOR_NAME": AUTHOR[0], "GIT_AUTHOR_EMAIL": AUTHOR[1],
           "GIT_COMMITTER_NAME": AUTHOR[0], "GIT_COMMITTER_EMAIL": AUTHOR[1], "PYTHONDONTWRITEBYTECODE": "1"}
    if not github:
        gh_none = BASE / "m12" / "gh-none"
        gh_none.mkdir(parents=True, exist_ok=True)
        os.chmod(gh_none, 0o700)
        env.update({"GH_CONFIG_DIR": str(gh_none),      # gh logged out: no GitHub write is possible
                    "GIT_SSH_COMMAND": "false"})         # nor a push over ssh
    return {
        "name": "synaps-dev",
        "display_name": "synaps-dev",
        "description": "The engineering agent: fixes acme-shop issues in its own clone, tests, pushes a branch, "
                       "reports a RESULT block.",
        "bridge_url": bridge_url,
        "registration_token_file": token_file,
        "model": "claude-sonnet-4-6",
        "max_session_cost": 1.00,
        "thinking": "low",
        "skill_file": "skill/synaps-dev.md",
        "workdir_repo": {"url": remote, "dir": "acme-shop"},
        "session_env": env,
        "session_path": [str(VENV_BIN)],
        "instructions": (GITHUB_NOTE if github else LOCAL_NOTE).format(remote=remote, repo=repo),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=("setup", "entry"))
    p.add_argument("--github", action="store_true", help="the real repo (needs push access)")
    p.add_argument("--gen", type=int, default=1)
    p.add_argument("--remote-dir", default=str(BASE / "m12" / "remote"))
    p.add_argument("--bridge-url", default="http://127.0.0.1:18400")
    p.add_argument("--token-file", default=str(Path("~/Projects/synaps-switch/devstack/state/registration.token").expanduser()))
    a = p.parse_args()
    if BASE.resolve() == Path("~/.synaps-cli").expanduser().resolve():
        sys.exit("refusing: that is the live Synaps dir")

    cfg = acme.config()
    if a.github:
        remote = cfg.remote if not cfg.local else f"git@github.com:{cfg.repo}.git"
    else:
        remote = acme.make_local_origin(a.remote_dir) if a.cmd == "setup" else \
            "file://" + str(Path(a.remote_dir) / "acme-shop-origin.git")
    e = entry(remote, cfg.repo, a.github, a.bridge_url, a.token_file)
    if a.cmd == "entry":
        print(json.dumps(e, indent=1))
        return
    if not a.github:
        rc = acme.config(remote=remote)
        acme.ensure_baseline(rc)
        print(f"remote {remote}: baseline {acme.remote_refs(remote)['refs/tags/baseline'][:10]}, "
              f"branch {acme.start_generation(a.gen, rc)}")
    path = BASE / "synaps-switch.json"
    data = json.loads(path.read_text()) if path.exists() else {"agents": []}
    data["agents"] = [x for x in data.get("agents", []) if x.get("name") != "synaps-dev"] + [e]
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, path)
    print(f"{path}: agents {[x['name'] for x in data['agents']]}; restart the private daemon to load it")


if __name__ == "__main__":
    main()
