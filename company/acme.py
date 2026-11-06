"""company/acme.py — acme-shop, the company's real product: issues, remotes, PR refs, trial reset.

The market is built from the real acme-shop issues (read-only `gh issue list`). The work is pushed
to a git remote that is either the real repo or a local bare clone standing in for it:

    ACME_REPO                  owner/name, issues are read from here         (default adityapat958/acme-shop)
    ACME_REMOTE                where branches are pushed and PRs are merged  (default git@github.com:<repo>.git)
                               file:///abs/path.git or /abs/path.git = LOCAL mode: a PR is a branch on that
                               bare repo, its ref is "file:///abs/path.git?head=<branch>&base=<base>",
                               a merge is a squash merge pushed to <base>. No GitHub write ever happens.
    ACME_ALLOW_GITHUB_WRITES   "1" to allow pushes/PRs/merges on GitHub. Without it the GitHub
                               path refuses before calling git push / gh (the real-repo path stays
                               behind config until the account has push access).

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
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

REPO_DEFAULT = "adityapat958/acme-shop"
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
    return AcmeConfig(repo=repo, remote=remote, allow_github_writes=bool(allow))


def is_local_remote(url: str) -> bool:
    return bool(url) and (url.startswith("file://") or url.startswith("/"))


def local_path(url: str) -> Path:
    return Path(urlparse(url).path if url.startswith("file://") else url)


def guard_write(cfg: AcmeConfig, what: str) -> None:
    if not cfg.local and not cfg.allow_github_writes:
        raise GitHubWriteRefused(f"refusing {what} on {cfg.remote_url}: set ACME_ALLOW_GITHUB_WRITES=1 "
                                 "(needs push access to the repo), or point ACME_REMOTE at a local bare clone")


# ───────────────────────── git helpers ─────────────────────────


def git(*args, cwd=None, check=True, timeout=120, env=None) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
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


def fetch_issues(repo: str, label: str, *, run=subprocess.run) -> list[dict]:
    """Read-only: gh issue list -R repo --label label (open and closed, so every run sees the same set)."""
    r = run(["gh", "issue", "list", "-R", repo, "--label", label, "--state", "all", "--limit", "100",
             "--json", "number,title,labels,body,state"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise AcmeError(f"gh issue list --label {label} failed: {(r.stderr or '').strip()[-300:]}")
    return json.loads(r.stdout or "[]")


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


def run_tests_at(remote: str, ref: str, *, python: str | None = None, timeout=300) -> dict:
    """Fresh clone of ref, `python -m pytest -q` in it. {passed, sha, summary, output}."""
    with tempfile.TemporaryDirectory(prefix="acme-tests-") as tmp:
        co = clone(remote, Path(tmp) / "co", ref)
        sha = git("rev-parse", "HEAD", cwd=co).stdout.strip()
        r = subprocess.run([python or sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                           cwd=co, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    out = (r.stdout + r.stderr).strip()
    tail = [l for l in out.splitlines() if re.search(r"\d+ (passed|failed|error)", l)]
    return {"passed": r.returncode == 0, "sha": sha, "summary": (tail[-1] if tail else out[-200:]).strip("= "),
            "output": out[-3000:]}


def merge_local(pr: str, *, message: str | None = None, author="Haseeb Khalid",
                email="HaseebKhalid1507@users.noreply.github.com") -> dict:
    """Squash-merge a local PR into its base and push the base (a plain, fast-forward push)."""
    p = parse_pr_ref(pr)
    if not p["local"]:
        raise AcmeError("merge_local takes a local PR ref")
    cfg = AcmeConfig(remote=p["remote"])
    ident = {"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": email,
             "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": email}
    with tempfile.TemporaryDirectory(prefix="acme-merge-") as tmp:
        co = clone(p["remote"], Path(tmp) / "co", p["base"])
        git("fetch", "-q", "origin", f"refs/heads/{p['head']}:refs/remotes/origin/{p['head']}", cwd=co)
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
    return f"https://github.com/{p['repo']}.git", (r.stdout.strip() or "main")


def tests_after_merge(pr: str) -> dict:
    """tests_passed for the quarter log: pytest on the merged base, from a fresh clone."""
    remote, base = base_of(pr)
    return run_tests_at(remote, base)

