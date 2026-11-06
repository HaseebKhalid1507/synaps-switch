"""company/acme.py — acme-shop, the company's real product: issues, remotes, PR refs, trial reset.

The market is built from the acme-shop issues (read-only `gh issue list`). The work is pushed to a git
remote that is either a GitHub repo (the fork) or a local bare clone standing in for it:

    ACME_REPO                  owner/name, issues are read from here and PRs live here
                               (default adityapat958/acme-shop: the upstream, read-only for us)
    ACME_REMOTE                where branches are pushed and PRs are merged  (default git@github.com:<repo>.git)
                               file:///abs/path.git or /abs/path.git = LOCAL mode: a PR is a branch on that
                               bare repo, its ref is "file:///abs/path.git?head=<branch>&base=<base>",
                               a merge is a squash merge pushed to <base>. No GitHub write ever happens.
    ACME_ALLOW_GITHUB_WRITES   "1" to allow pushes/PRs/merges on GitHub. Without it the GitHub path refuses
                               before calling git push / gh. The upstream (adityapat958/acme-shop) is
                               refused always, and so is a remote that is not ACME_REPO.
    ACME_DEPLOY_KEY            private key file for the remote (a deploy key with write access to that one
                               repo). Every git call of this module then uses only that key over ssh (no
                               ssh agent, no ~/.ssh/config), never the operator's own keys.
    ACME_ISSUE_AUTHORS         comma-separated GitHub logins whose issues the market reads
                               (default HaseebKhalid1507,adityapat958). The fork is public: anyone can file
                               an issue there, and an issue from anyone else is dropped, never shown to an agent.

GitHub mode (the fork, #26) is `python3 tools/synaps_dev.py setup --github`: it writes the synaps-dev entry
(deploy-key pushes, gh logged out in the room session) and an env file with the four ACME_* settings
above for the company process, which opens and merges the PRs with the operator's own gh.

Trial reset (so env_k and env_{k+1} face the same bugs): the remote gets a `baseline` tag once,
and every generation k works on its own branch `gen-k` cut from `baseline`. Work branches are
`gen-k-fix-<issue>`, PRs target `gen-k`, merges land on `gen-k`, never on main. A generation that
starts over gets a fresh `gen-k` (or a new prefix), so fixes merged in one trial never leak into the next.

Pushes are plain pushes. Only a branch this process created (start_generation, or a work branch
first pushed by this run) may be force-updated, and then only with --force-with-lease.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

REPO_DEFAULT = "adityapat958/acme-shop"
UPSTREAM = "adityapat958/acme-shop"          # read-only for us: issues only, never a push, PR or merge
FORK_DEFAULT = "HaseebKhalid1507/acme-shop"   # GitHub mode (#26)
ISSUE_AUTHORS_DEFAULT = ("HaseebKhalid1507", "adityapat958")
BASELINE_TAG = "baseline"
PRIORITY_DEMAND = {"high": 0.9, "medium": 0.6, "low": 0.3}
DEFAULT_DEMAND = 0.5
DEFAULT_SEVERITY = 3

# Branches this process created on a remote: {(remote, branch)}. The only ones it may force-update.
CREATED_THIS_RUN: set[tuple[str, str]] = set()


class AcmeError(RuntimeError):
    pass


class GitHubWriteRefused(AcmeError):
    """A write to the real GitHub repo without ACME_ALLOW_GITHUB_WRITES=1."""


# ───────────────────────── config ─────────────────────────


@dataclass(frozen=True)
class AcmeConfig:
    repo: str = REPO_DEFAULT
    remote: str = ""
    allow_github_writes: bool = False
    deploy_key: str = ""

    @property
    def local(self) -> bool:
        return is_local_remote(self.remote)

    @property
    def remote_url(self) -> str:
        return self.remote or f"git@github.com:{self.repo}.git"


def config(**over) -> AcmeConfig:
    e = os.environ
    repo = over.get("repo") or e.get("ACME_REPO") or REPO_DEFAULT
    remote = over.get("remote") or e.get("ACME_REMOTE") or f"git@github.com:{repo}.git"
    if remote.startswith("/"):
        remote = "file://" + remote
    allow = over.get("allow_github_writes")
    if allow is None:
        allow = e.get("ACME_ALLOW_GITHUB_WRITES") == "1"
    key = over.get("deploy_key") or e.get("ACME_DEPLOY_KEY") or ""
    return AcmeConfig(repo=repo, remote=remote, allow_github_writes=bool(allow),
                      deploy_key=os.path.expanduser(key) if key else "")


def issue_authors() -> tuple[str, ...]:
    raw = os.environ.get("ACME_ISSUE_AUTHORS")
    names = raw.split(",") if raw is not None else ISSUE_AUTHORS_DEFAULT
    return tuple(n.strip().lstrip("@") for n in names if n.strip())


def ssh_command(key: str) -> str:
    """GIT_SSH_COMMAND that offers only `key`: no ssh agent, no ~/.ssh/config identities."""
    return (f"ssh -i {shlex.quote(key)} -o IdentitiesOnly=yes -o IdentityAgent=none -F /dev/null "
            "-o BatchMode=yes")


def git_env(key: str | None = None) -> dict:
    """Extra env for git: with a deploy key (arg or ACME_DEPLOY_KEY), ssh uses that key only."""
    key = key if key is not None else os.path.expanduser(os.environ.get("ACME_DEPLOY_KEY") or "")
    return {"GIT_SSH_COMMAND": ssh_command(key)} if key else {}


def github_repo_of(url: str) -> str | None:
    """owner/name of a GitHub remote URL (ssh or https), else None."""
    m = re.fullmatch(r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)([^/]+/[^/]+?)(?:\.git)?/?",
                     (url or "").strip())
    return m.group(1) if m else None


def is_local_remote(url: str) -> bool:
    return bool(url) and (url.startswith("file://") or url.startswith("/"))


def local_path(url: str) -> Path:
    return Path(urlparse(url).path if url.startswith("file://") else url)


def guard_write(cfg: AcmeConfig, what: str) -> None:
    if cfg.local:
        return
    if not cfg.allow_github_writes:
        raise GitHubWriteRefused(f"refusing {what} on {cfg.remote_url}: set ACME_ALLOW_GITHUB_WRITES=1 "
                                 "(needs push access to the repo), or point ACME_REMOTE at a local bare clone")
    target = github_repo_of(cfg.remote_url)
    if (target or "").lower() == UPSTREAM.lower() or cfg.repo.lower() == UPSTREAM.lower():
        raise GitHubWriteRefused(f"refusing {what}: {UPSTREAM} is the upstream, read-only; writes go to the fork "
                                 f"(ACME_REPO={FORK_DEFAULT})")
    if target is None or target.lower() != cfg.repo.lower():
        raise GitHubWriteRefused(f"refusing {what}: the remote {cfg.remote_url} is not ACME_REPO {cfg.repo}")


# ───────────────────────── git helpers ─────────────────────────


def git(*args, cwd=None, check=True, timeout=120, env=None) -> subprocess.CompletedProcess:
    e = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **git_env(), **(env or {})}
    if "GIT_SSH_COMMAND" in git_env():
        e.pop("SSH_AUTH_SOCK", None)                 # deploy-key mode: the agent's keys are never offered
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=e)
    if check and r.returncode != 0:
        raise AcmeError(f"git {' '.join(args[:3])} failed: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


def remote_refs(remote: str) -> dict[str, str]:
    """{ref name: sha} from `git ls-remote` (heads and tags)."""
    out = git("ls-remote", "--heads", "--tags", remote, timeout=60).stdout
    refs = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.endswith("^{}"):
            refs[ref[:-3]] = sha          # a peeled annotated tag: the commit wins
        else:
            refs.setdefault(ref, sha)
    return refs


def push(checkout, branch: str, cfg: AcmeConfig, *, src: str | None = None, remote_name="origin",
         old_sha: str | None = None) -> str:
    """Push src (default: branch) to refs/heads/<branch>. Plain push; a branch this run created may be
    rewritten with --force-with-lease. Returns "created", "updated" or "forced"."""
    guard_write(cfg, f"git push {branch}")
    src = src or branch
    existed = f"refs/heads/{branch}" in remote_refs(cfg.remote_url)
    argv = ["push", remote_name, f"{src}:refs/heads/{branch}"]
    r = git(*argv, cwd=checkout, check=False)
    if r.returncode == 0:
        if not existed:
            CREATED_THIS_RUN.add((cfg.remote_url, branch))
        return "updated" if existed else "created"
    if (cfg.remote_url, branch) not in CREATED_THIS_RUN:
        raise AcmeError(f"push of {branch} rejected and the branch is not one this run created; "
                        f"not forcing: {(r.stderr or r.stdout).strip()[-300:]}")
    lease = f"--force-with-lease=refs/heads/{branch}" + (f":{old_sha}" if old_sha else "")
    git("push", lease, remote_name, f"{src}:refs/heads/{branch}", cwd=checkout)
    return "forced"


def clone(remote: str, dest, ref: str | None = None, *, timeout=180) -> Path:
    dest = Path(dest)
    argv = ["clone", "-q"]
    if ref:
        argv += ["--branch", ref]
    git(*argv, remote, str(dest), timeout=timeout)
    return dest


def make_local_origin(dest_dir, source: str | None = None) -> str:
    """A bare clone of the real repo (read-only fetch over https) that stands in for origin.
    Returns its file:// URL."""
    dest = Path(dest_dir) / "acme-shop-origin.git"
    if not dest.exists():
        src = source or f"https://github.com/{config().repo}.git"
        git("clone", "-q", "--bare", src, str(dest), timeout=180)
        git("remote", "remove", "origin", cwd=dest, check=False)   # no way back to GitHub from it
    return "file://" + str(dest.resolve())


# ───────────────────────── issues → market ─────────────────────────


def fetch_issues(repo: str, label: str, *, run=subprocess.run, authors=None) -> list[dict]:
    """Read-only: gh issue list -R repo --label label (open and closed, so every run sees the same set).
    Only issues authored by an allowed login (ACME_ISSUE_AUTHORS) are kept: the fork is public."""
    r = run(["gh", "issue", "list", "-R", repo, "--label", label, "--state", "all", "--limit", "100",
             "--json", "number,title,labels,body,state,author"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AcmeError(f"gh issue list --label {label} failed: {(r.stderr or '').strip()[-300:]}")
    return [i for i in json.loads(r.stdout or "[]") if allowed_author(i, authors)]


def author_of(issue: dict) -> str:
    a = issue.get("author") if "author" in issue else issue.get("user")
    return str((a.get("login") if isinstance(a, dict) else a) or "")


def allowed_author(issue: dict, authors=None) -> bool:
    allow = {a.lower() for a in (authors if authors is not None else issue_authors())}
    login = author_of(issue).lower()
    return bool(login) and login in allow


def fetch_issue(repo: str, number: int, *, run=subprocess.run, authors=None) -> dict:
    """Read-only: one issue {number, title, body, author}. Refuses an issue by an author not on the list."""
    r = run(["gh", "issue", "view", str(int(number)), "-R", repo, "--json", "number,title,body,author,labels"],
            capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AcmeError(f"gh issue view {number} failed: {(r.stderr or '').strip()[-300:]}")
    issue = json.loads(r.stdout or "{}")
    if not allowed_author(issue, authors):
        raise AcmeError(f"{repo}#{number} is by {author_of(issue) or 'nobody'!r}, not an allowed issue author "
                        "(ACME_ISSUE_AUTHORS): not read")
    return {"number": issue.get("number"), "title": issue.get("title") or "", "body": issue.get("body") or "",
            "author": author_of(issue)}


def _labels(issue: dict) -> list[str]:
    return [(l.get("name") if isinstance(l, dict) else str(l)).lower() for l in issue.get("labels") or []]


def severity_of(issue: dict) -> int:
    for name in _labels(issue):
        m = re.fullmatch(r"sev-?([1-4])", name)
        if m:
            return int(m.group(1))
    m = re.search(r"SEV-?([1-4])", issue.get("body") or "", re.I)
    return int(m.group(1)) if m else DEFAULT_SEVERITY


def demand_of(issue: dict) -> float:
    m = re.search(r"\*\*Priority:\*\*\s*(\w+)", issue.get("body") or "", re.I) \
        or re.search(r"Priority:\s*(\w+)", issue.get("body") or "", re.I)
    return PRIORITY_DEMAND.get(m.group(1).lower(), DEFAULT_DEMAND) if m else DEFAULT_DEMAND


def market_issues(incidents: list[dict], features: list[dict]) -> dict:
    """gh issue JSON → the Market(issues=...) shape. Sorted by number, so it's stable."""
    return {
        "incidents": [{"issue": i["number"], "title": i["title"], "severity": severity_of(i)}
                      for i in sorted(incidents, key=lambda x: x["number"])],
        "features": [{"issue": f["number"], "title": f["title"], "demand": demand_of(f)}
                     for f in sorted(features, key=lambda x: x["number"])],
    }


def load_issues(repo: str | None = None, *, cache: str | os.PathLike | None = None, refresh=False,
                run=subprocess.run) -> dict:
    """The market's issue set from the real repo. With cache, the first fetch is saved and reused, so
    every trial of a run (and an offline rerun) gets exactly the same issues."""
    repo = repo or config().repo
    if cache and not refresh and Path(cache).exists():
        return json.loads(Path(cache).read_text())
    data = market_issues(fetch_issues(repo, "incident", run=run), fetch_issues(repo, "feature", run=run))
    data["repo"] = repo
    data["authors"] = list(issue_authors())
    if cache:
        Path(cache).parent.mkdir(parents=True, exist_ok=True)
        Path(cache).write_text(json.dumps(data, indent=1) + "\n")
    return data


def build_market(seed: int, company: dict, *, issues: dict | None = None, repo: str | None = None,
                 cache=None, run=subprocess.run):
    from company.market.sim import Market
    issues = issues or load_issues(repo, cache=cache, run=run)
    return Market(seed, company, issues={"incidents": issues["incidents"], "features": issues["features"]})


def issue_kind(issues: dict, number: int) -> str | None:
    if any(i["issue"] == number for i in issues.get("incidents", [])):
        return "incident"
    if any(f["issue"] == number for f in issues.get("features", [])):
        return "feature"
    return None


# ───────────────────────── trial reset ─────────────────────────


def gen_branch(k: int, prefix: str = "") -> str:
    return f"{prefix}gen-{int(k)}"


def work_branch(issue: int, base: str) -> str:
    return f"{base}-fix-{int(issue)}"


_GEN = re.compile(r"(?:[A-Za-z0-9._-]*-)?gen-\d+")


def is_gen_branch(name: str) -> bool:
    """gen-k or <prefix>gen-k: the only bases a PR may target (never main)."""
    return bool(_GEN.fullmatch(name or ""))


def check_pr_branches(head: str, base: str, issue: int | None = None) -> None:
    """A PR is <base>-fix-<N> into a generation branch, nothing else."""
    if not is_gen_branch(base):
        raise AcmeError(f"refusing a PR into {base!r}: PRs target gen-k branches only, never main")
    want = re.fullmatch(re.escape(base) + r"-fix-(\d+)", head or "")
    if not want or (issue is not None and int(want.group(1)) != int(issue)):
        raise AcmeError(f"refusing head {head!r}: expected {work_branch(issue, base) if issue else base + '-fix-<N>'}")


def ensure_baseline(cfg: AcmeConfig, ref: str = "main") -> str:
    """Tag `baseline` on the remote at ref (once). Returns the baseline commit sha."""
    refs = remote_refs(cfg.remote_url)
    tag = f"refs/tags/{BASELINE_TAG}"
    if tag in refs:
        return refs[tag]
    if f"refs/heads/{ref}" not in refs:
        raise AcmeError(f"{cfg.remote_url} has no branch {ref}")
    guard_write(cfg, "tagging baseline")
    with tempfile.TemporaryDirectory(prefix="acme-baseline-") as tmp:
        co = clone(cfg.remote_url, Path(tmp) / "co", ref)
        git("tag", BASELINE_TAG, cwd=co)
        git("push", "origin", f"refs/tags/{BASELINE_TAG}", cwd=co)
        return git("rev-parse", "HEAD", cwd=co).stdout.strip()


def start_generation(k: int, cfg: AcmeConfig, *, prefix: str = "") -> str:
    """Cut gen-k from baseline on the remote and return its name. An existing gen-k is reused only
    when it still equals baseline; one with other commits is reset only if this run created it."""
    branch = gen_branch(k, prefix)
    base_sha = ensure_baseline(cfg)
    refs = remote_refs(cfg.remote_url)
    cur = refs.get(f"refs/heads/{branch}")
    if cur == base_sha:
        return branch
    if cur and (cfg.remote_url, branch) not in CREATED_THIS_RUN:
        raise AcmeError(f"{branch} already exists on {cfg.remote_url} with other commits and this run did "
                        "not create it; use another prefix rather than rewrite it")
    with tempfile.TemporaryDirectory(prefix="acme-gen-") as tmp:
        co = Path(tmp) / "co"
        git("init", "-q", str(co))
        git("remote", "add", "origin", cfg.remote_url, cwd=co)
        git("fetch", "-q", "origin", f"refs/tags/{BASELINE_TAG}:refs/tags/{BASELINE_TAG}", cwd=co)
        push(co, branch, cfg, src=f"{BASELINE_TAG}^{{commit}}", old_sha=cur)
    return branch


# ───────────────────────── PR refs (local mode) ─────────────────────────


def local_pr_ref(remote: str, head: str, base: str) -> str:
    remote = remote if remote.startswith("file://") else "file://" + remote
    return f"{remote}?head={quote(head)}&base={quote(base)}"


def parse_pr_ref(ref: str) -> dict:
    """A local PR ref → {"local": True, "remote", "head", "base"};
    a GitHub PR URL → {"local": False, "url", "repo", "number"}."""
    ref = (ref or "").strip().strip("<>`")
    if ref.startswith("file://") or ref.startswith("/"):
        u = urlparse(ref if ref.startswith("file://") else "file://" + ref)
        q = parse_qs(u.query)
        head = (q.get("head") or [u.fragment or ""])[0]
        base = (q.get("base") or [""])[0]
        if not base:
            m = re.fullmatch(r"(.+)-fix-\d+", head)
            base = m.group(1) if m else ""
        if not head or not base:
            raise AcmeError(f"local PR ref needs head and base: {ref!r}")
        return {"local": True, "remote": f"file://{u.path}", "head": head, "base": base}
    m = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)/?", ref)
    if m:
        return {"local": False, "url": ref, "repo": m.group(1), "number": int(m.group(2))}
    raise AcmeError(f"not a PR ref: {ref!r}")


def pr_info(pr: str, *, run=subprocess.run) -> dict:
    """Read-only: a GitHub PR's {repo, number, url, state, head, base, head_sha, cross_repo}."""
    p = parse_pr_ref(pr)
    if p["local"]:
        return {**p, "state": "OPEN", "cross_repo": False}
    r = run(["gh", "pr", "view", p["url"], "--json",
             "number,url,state,baseRefName,headRefName,headRefOid,isCrossRepository"],
            capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AcmeError(f"gh pr view failed: {(r.stderr or '').strip()[-300:]}")
    d = json.loads(r.stdout or "{}")
    return {"local": False, "repo": p["repo"], "number": d.get("number", p["number"]), "url": d.get("url") or p["url"],
            "state": d.get("state"), "head": d.get("headRefName"), "base": d.get("baseRefName"),
            "head_sha": d.get("headRefOid"), "cross_repo": bool(d.get("isCrossRepository", True))}


def read_url(repo: str, cfg: AcmeConfig | None = None) -> str:
    """Where to clone a GitHub repo from for reading: the configured remote (deploy key) when it is that
    repo, else anonymous https (acme-shop and its fork are public)."""
    cfg = cfg or config()
    if not cfg.local and (github_repo_of(cfg.remote_url) or "").lower() == repo.lower():
        return cfg.remote_url
    return f"https://github.com/{repo}.git"


def open_pr(cfg: AcmeConfig, head: str, base: str, *, issue: int, title: str = "", body: str = "",
            run=subprocess.run) -> str:
    """The PR for a pushed work branch, opened by the company process, never by the room session.
    Local mode: the local PR ref. GitHub: `gh pr create -R <repo> --base <gen-k> --head <gen-k>-fix-N`
    with the operator's gh (an open same-repo PR for that head/base is reused). Returns the ref / URL."""
    check_pr_branches(head, base, issue)
    refs = remote_refs(cfg.remote_url)
    if f"refs/heads/{head}" not in refs:
        raise AcmeError(f"{head} is not on {cfg.remote_url}: nothing to open a PR from")
    if f"refs/heads/{base}" not in refs:
        raise AcmeError(f"{base} is not on {cfg.remote_url}")
    if cfg.local:
        return local_pr_ref(cfg.remote_url, head, base)
    guard_write(cfg, "gh pr create")
    r = run(["gh", "pr", "list", "-R", cfg.repo, "--head", head, "--base", base, "--state", "open",
             "--json", "url,isCrossRepository"], capture_output=True, text=True, timeout=60)
    if r.returncode == 0:
        same = [x for x in json.loads(r.stdout or "[]") if not x.get("isCrossRepository")]
        if same:
            return same[0]["url"]
    r = run(["gh", "pr", "create", "-R", cfg.repo, "--base", base, "--head", head,
             "--title", title or f"Fix #{issue}", "--body", body or f"Refs #{issue}"],
            capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise AcmeError(f"gh pr create failed: {(r.stderr or r.stdout or '').strip()[-300:]}")
    url = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
    p = parse_pr_ref(url)
    if p["local"] or p["repo"].lower() != cfg.repo.lower():
        raise AcmeError(f"gh pr create returned {url!r}, not a PR on {cfg.repo}")
    return url


def check_mergeable(pr: str, cfg: AcmeConfig | None = None, *, run=subprocess.run) -> dict:
    """Before a GitHub merge: the PR is on ACME_REPO, open, not from another fork, <gen-k>-fix-N into gen-k."""
    cfg = cfg or config()
    info = pr_info(pr, run=run)
    if info["repo"].lower() != cfg.repo.lower():
        raise AcmeError(f"refusing to merge {pr}: not on ACME_REPO {cfg.repo}")
    if info["cross_repo"]:
        raise AcmeError(f"refusing to merge {pr}: its head is in another fork")
    if info.get("state") != "OPEN":
        raise AcmeError(f"refusing to merge {pr}: state {info.get('state')}")
    check_pr_branches(info["head"], info["base"])
    return info


def pr_diff(pr: str, *, run=subprocess.run) -> str:
    p = parse_pr_ref(pr)
    if not p["local"]:
        r = run(["gh", "pr", "diff", p["url"]], capture_output=True, text=True, timeout=60)   # read-only
        if r.returncode != 0:
            raise AcmeError(f"gh pr diff failed: {(r.stderr or '').strip()[-300:]}")
        return r.stdout
    bare = local_path(p["remote"])
    return git("diff", f"refs/heads/{p['base']}...refs/heads/{p['head']}", cwd=bare).stdout


def pr_files(pr: str) -> list[str]:
    p = parse_pr_ref(pr)
    if not p["local"]:
        return re.findall(r"^\+\+\+ b/(.+)$", pr_diff(pr), re.M)
    out = git("diff", "--name-only", f"refs/heads/{p['base']}...refs/heads/{p['head']}",
              cwd=local_path(p["remote"])).stdout
    return [l for l in out.splitlines() if l.strip()]


# ───────────────────────── tests + merge ─────────────────────────


def harness_env(home) -> dict:
    """The environment pytest runs in when the harness tests an agent's branch: the code is untrusted, so
    none of the operator's credentials (no ssh agent, gh logged out, no tokens, HOME a temp dir)."""
    e = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if os.environ.get(k)}
    gh_none = Path(home) / "gh-none"
    gh_none.mkdir(exist_ok=True)
    e.update({"HOME": str(home), "GH_CONFIG_DIR": str(gh_none), "GIT_SSH_COMMAND": "false",
              "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"})
    return e


def run_tests_at(remote: str, ref: str, *, python: str | None = None, timeout=300) -> dict:
    """Fresh clone of ref, `python -m pytest -q` in it (in harness_env). {passed, sha, summary, output}."""
    with tempfile.TemporaryDirectory(prefix="acme-tests-") as tmp:
        co = clone(remote, Path(tmp) / "co", ref)
        sha = git("rev-parse", "HEAD", cwd=co).stdout.strip()
        home = Path(tmp) / "home"
        home.mkdir()
        r = subprocess.run([python or sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                           cwd=co, capture_output=True, text=True, timeout=timeout, env=harness_env(home))
    out = (r.stdout + r.stderr).strip()
    tail = [l for l in out.splitlines() if re.search(r"\d+ (passed|failed|error)", l)]
    return {"passed": r.returncode == 0, "sha": sha, "summary": (tail[-1] if tail else out[-200:]).strip("= "),
            "output": out[-3000:]}


def merge_local(pr: str, *, message: str | None = None, author="Haseeb Khalid",
                email="HaseebKhalid1507@users.noreply.github.com", head_sha: str | None = None) -> dict:
    """Squash-merge a local PR into its base and push the base (a plain, fast-forward push).
    With head_sha (the commit that was reviewed), a head that moved since is not merged."""
    p = parse_pr_ref(pr)
    if not p["local"]:
        raise AcmeError("merge_local takes a local PR ref")
    cfg = AcmeConfig(remote=p["remote"])
    ident = {"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": email,
             "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": email}
    with tempfile.TemporaryDirectory(prefix="acme-merge-") as tmp:
        co = clone(p["remote"], Path(tmp) / "co", p["base"])
        git("fetch", "-q", "origin", f"refs/heads/{p['head']}:refs/remotes/origin/{p['head']}", cwd=co)
        cur = git("rev-parse", f"origin/{p['head']}", cwd=co).stdout.strip()
        if head_sha and cur != head_sha:
            return {"merged": False, "error": f"head moved since the review ({head_sha[:10]} -> {cur[:10]})", **p}
        r = git("merge", "--squash", f"origin/{p['head']}", cwd=co, check=False, env=ident)
        if r.returncode != 0:
            return {"merged": False, "error": (r.stderr or r.stdout).strip()[-300:], **p}
        if not git("status", "--porcelain", cwd=co).stdout.strip():
            return {"merged": False, "error": "nothing to merge", **p}
        msg = message or f"Merge {p['head']} into {p['base']} (squash)"
        git("commit", "-q", "-m", msg, cwd=co, env=ident)
        sha = git("rev-parse", "HEAD", cwd=co).stdout.strip()
        push(co, p["base"], cfg, src="HEAD")
    return {"merged": True, "sha": sha, **p}


def base_of(pr: str) -> tuple[str, str]:
    """(remote, base branch) a PR merges into."""
    p = parse_pr_ref(pr)
    if p["local"]:
        return p["remote"], p["base"]
    r = subprocess.run(["gh", "pr", "view", p["url"], "--json", "baseRefName", "-q", ".baseRefName"],
                       capture_output=True, text=True, timeout=60)
    return read_url(p["repo"]), (r.stdout.strip() or "main")


def tests_after_merge(pr: str) -> dict:
    """tests_passed for the quarter log: pytest on the merged base, from a fresh clone."""
    remote, base = base_of(pr)
    return run_tests_at(remote, base)

