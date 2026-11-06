"""company/devworker.py — engineering agent worker (the fallback when synaps-dev is not up).

Clones/fetches acme-shop, creates a branch, invokes
`synaps chat --profile company-dev --no-extensions` with a prompt to fix or
implement a GitHub issue, then commits, pushes, and opens a PR.

Contracts (from INTERFACES.md):
    work_issue(issue, kind, *, repo, workdir, remote=None, base=None) -> dict
        {issue, pr_url, tests_passed, summary, cost_usd}
    merge_pr(pr_url) -> bool

Where the work goes is company/acme.py's config: ACME_REMOTE (a file:// bare clone = local mode,
PRs are local refs and merges are local squash merges) and ACME_ALLOW_GITHUB_WRITES (the GitHub
path refuses without it). Credentials come only from the private auth-broker, configured the same
way as company/llm.py (COMPANY_BROKER_TOKEN_FILE, COMPANY_BROKER_URL, COMPANY_SYNAPS_BASE_DIR);
the profile is $COMPANY_DEV_PROFILE (default company-dev; `python -m company.devworker setup` writes it).

Headless `synaps chat` takes every stdin line as its own turn, so the prompt goes in as ONE line
(company.llm._one_line). Pushes are plain; only a branch this run created may be force-updated
(--force-with-lease).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from company import acme
from company import llm

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_DEFAULT = acme.REPO_DEFAULT
WORKDIR_DEFAULT = "company/state/work"
TIMEOUT_SECONDS = 15 * 60          # 15 min per issue
DEV_PROFILE_DEFAULT = "company-dev"
DEV_MODEL_DEFAULT = "claude-sonnet-4-6"
DEV_MAX_SESSION_COST = 1.00
_COST_RE = re.compile(r"\(\$([0-9]+\.[0-9]+)\)")

DEV_PROFILE_CONFIG = """\
# company/devworker.py profile: the fallback coding agent (Sonnet, tools on), private auth-broker only.
model = {model}
thinking = low
max_session_cost = {cap:.2f}
auth.remote_endpoint = {broker}
"""
DEV_PROFILE_SYSTEM = "You are the engineering agent for acme-shop. Work in the current directory only.\n"


def _dev_profile() -> str:
    return os.environ.get("COMPANY_DEV_PROFILE") or DEV_PROFILE_DEFAULT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_token() -> str:
    """The private broker's machine token (COMPANY_BROKER_TOKEN_FILE, as company/llm.py)."""
    return llm._read_token(llm._cfg()["token_file"])


def _agent_env(token: str) -> dict:
    cfg = llm._cfg(_dev_profile())
    env = llm._env(cfg, token)
    env.pop("NO_COLOR", None)
    return env


def _run(cmd: list[str], *, cwd: str | None = None,
         env: dict | None = None, input: str | None = None,
         timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        input=input,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _fetch_issue(issue: int, repo: str) -> tuple[str, str]:
    """Return (title, body) of a GitHub issue via gh (read-only)."""
    result = _run(["gh", "issue", "view", str(issue),
                   "--repo", repo,
                   "--json", "title,body",
                   "-q", ".title,.body"],
                  timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"gh issue view failed: {result.stderr}")
    out = result.stdout.strip()
    full = _run(["gh", "issue", "view", str(issue), "--repo", repo], timeout=30)
    return out, full.stdout.strip()


def _clone_or_update(repo: str, workdir: str, remote: str | None = None, base: str = "main") -> str:
    """Clone the remote into workdir/<repo_name> if not present, else fetch and reset to origin/<base>.
    Returns the local checkout path."""
    repo_name = repo.split("/")[-1]
    checkout = str(Path(workdir) / repo_name)
    Path(workdir).mkdir(parents=True, exist_ok=True)
    url = remote or f"git@github.com:{repo}.git"

    if Path(checkout).exists():
        _run(["git", "remote", "set-url", "origin", url], cwd=checkout, timeout=30)
        _run(["git", "fetch", "origin"], cwd=checkout, timeout=60)
        _run(["git", "checkout", "-B", base, f"origin/{base}"], cwd=checkout, timeout=30)
        _run(["git", "reset", "--hard", f"origin/{base}"], cwd=checkout, timeout=30)
    else:
        _run(["git", "clone", "--branch", base, url, checkout], timeout=120)

    return checkout


def _make_branch(issue: int, checkout: str, base: str | None = None) -> str:
    branch = acme.work_branch(issue, base) if base and "gen-" in base else f"issue-{issue}"
    _run(["git", "checkout", "-B", branch], cwd=checkout, timeout=30)
    return branch


def _build_prompt(issue: int, kind: str, issue_text: str) -> str:
    return f"""\
You are the engineering agent for acme-shop. Your job is to resolve GitHub issue #{issue}.

ISSUE TYPE: {kind}  (incident = bug fix; feature = new functionality)

ISSUE DETAILS:
{issue_text}

INSTRUCTIONS:
1. Read the issue carefully. Understand the symptom and expected behaviour.
2. Locate the root cause in the codebase (catalog.py, cart.py, pricing.py, checkout.py, etc.).
3. Apply a minimal, clean fix (or implement the feature) in the appropriate source file(s).
4. Write a regression test (or acceptance tests for a feature) in the tests/ directory that
   - FAILS before your fix on the unpatched code (proves the bug existed / feature was missing)
   - PASSES after your fix
   Name the test file tests/test_issue_{issue}.py.
5. Run `python3 -m pytest -q` to verify ALL tests pass. If they don't, iterate until they do.
6. Do NOT push or interact with git remotes. Do NOT run `git push`, `git remote`, or `gh` commands.
   The devworker process will handle committing and pushing after you finish.
7. When done, print a one-paragraph summary of what you changed and why.
"""


def _parse_cost(text: str) -> float:
    """Extract cost from 'session saved: … ($0.0123)' line."""
    matches = _COST_RE.findall(text)
    if matches:
        return float(matches[-1])
    return 0.0


def _push(branch: str, checkout: str, cfg: acme.AcmeConfig | None = None) -> str:
    """Plain push. Only a branch this run created may be rewritten, and only with --force-with-lease."""
    cfg = cfg or acme.config()
    acme.guard_write(cfg, f"git push {branch}")
    listed = _run(["git", "ls-remote", "--heads", "origin", branch], cwd=checkout, timeout=60)
    existed = bool((listed.stdout or "").strip())
    r = _run(["git", "push", "-u", "origin", branch], cwd=checkout, timeout=60)
    if r.returncode == 0:
        if not existed:
            acme.CREATED_THIS_RUN.add((cfg.remote_url, branch))
        return "updated" if existed else "created"
    if (cfg.remote_url, branch) not in acme.CREATED_THIS_RUN:
        raise RuntimeError(f"git push of {branch} was rejected and this run did not create that branch; "
                           f"not forcing: {(r.stderr or r.stdout).strip()[-300:]}")
    r = _run(["git", "push", "--force-with-lease", "-u", "origin", branch], cwd=checkout, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"git push --force-with-lease {branch} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return "forced"


def _commit_and_push(issue: int, branch: str, checkout: str, summary: str,
                     cfg: acme.AcmeConfig | None = None) -> None:
    _run(["git", "add", "-A"], cwd=checkout, timeout=30)
    msg = f"fix: resolve issue #{issue}\n\n{summary[:500]}"
    result = _run(["git", "commit", "-m", msg], cwd=checkout, timeout=30)
    if result.returncode != 0 and "nothing to commit" in result.stdout + result.stderr:
        raise RuntimeError("Agent made no changes — nothing to commit")
    _push(branch, checkout, cfg)


def _open_pr(issue: int, branch: str, checkout: str, repo: str, summary: str,
             pytest_tail: str, base: str = "main", cfg: acme.AcmeConfig | None = None) -> str:
    cfg = cfg or acme.config(repo=repo)
    if cfg.local:
        return acme.local_pr_ref(cfg.remote_url, branch, base)
    acme.guard_write(cfg, "gh pr create")
    body = (
        f"Closes #{issue}\n\n"
        f"## Summary\n{summary}\n\n"
        f"## Test output\n```\n{pytest_tail[-1500:]}\n```"
    )
    result = _run(
        ["gh", "pr", "create",
         "--repo", repo,
         "--head", branch,
         "--base", base,
         "--title", f"Fix issue #{issue}",
         "--body", body],
        cwd=checkout,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr create failed: {result.stderr}")
    # gh pr create prints the PR URL as the last line
    return result.stdout.strip().splitlines()[-1]


def _run_pytest(checkout: str) -> tuple[bool, str]:
    result = _run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=checkout,
        timeout=120,
    )
    passed = result.returncode == 0
    output = (result.stdout + result.stderr).strip()
    return passed, output


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def work_issue(
    issue: int,
    kind: str,
    *,
    repo: str = REPO_DEFAULT,
    workdir: str = WORKDIR_DEFAULT,
    remote: str | None = None,
    base: str | None = None,
) -> dict:
    """Clone/fetch acme-shop, branch, invoke coding agent, commit, push, open PR.

    remote/base default to ACME_REMOTE / "main". Returns:
        {issue, pr_url, tests_passed, summary, cost_usd}
    """
    pr_url = ""
    cost_usd = 0.0
    cfg = acme.config(repo=repo, remote=remote) if remote else acme.config(repo=repo)
    base = base or "main"

    try:
        # 1. Prepare checkout
        checkout = _clone_or_update(repo, workdir, cfg.remote_url, base)
        branch = _make_branch(issue, checkout, base)

        # 2. Fetch issue text
        _, issue_text = _fetch_issue(issue, repo)
        prompt = _build_prompt(issue, kind, issue_text)

        # 3. Build env for synaps chat (private broker only)
        env = _agent_env(_read_token())

        # 4. Run the coding agent: the whole prompt is ONE stdin line (one turn)
        agent_result = subprocess.run(
            ["synaps", "chat", "--profile", _dev_profile(), "--no-extensions"],
            cwd=checkout,
            env=env,
            input=llm._one_line(prompt) + "\n",
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        agent_output = agent_result.stdout + agent_result.stderr
        cost_usd = _parse_cost(agent_output)

        # Extract last paragraph as summary (after agent finishes)
        paragraphs = [p.strip() for p in llm._strip_banner(agent_output).split("\n\n") if p.strip()]
        summary = paragraphs[-1][:800] if paragraphs else "Agent produced no summary."

        # 5. Run pytest ourselves to verify
        tests_passed, pytest_output = _run_pytest(checkout)

        # 6. Commit + push + open PR
        _commit_and_push(issue, branch, checkout, summary, cfg)
        pr_url = _open_pr(issue, branch, checkout, repo, summary, pytest_output, base, cfg)

        return {
            "issue": issue,
            "pr_url": pr_url,
            "tests_passed": tests_passed,
            "summary": summary,
            "cost_usd": cost_usd,
        }

    except subprocess.TimeoutExpired:
        return {
            "issue": issue,
            "pr_url": pr_url,
            "tests_passed": False,
            "summary": f"Timed out after {TIMEOUT_SECONDS}s",
            "cost_usd": cost_usd,
        }
    except Exception as exc:
        return {
            "issue": issue,
            "pr_url": pr_url,
            "tests_passed": False,
            "summary": f"Error: {exc}",
            "cost_usd": cost_usd,
        }


def merge_pr(pr_url: str) -> bool:
    """Squash-merge a PR. Called only after a #board human approves (company/realwork.MergeGate).

    A local PR ref (file://…?head=…&base=…) is squash-merged into its base on the local remote;
    a GitHub PR needs ACME_ALLOW_GITHUB_WRITES=1."""
    if acme.is_local_remote(pr_url):
        try:
            return bool(acme.merge_local(pr_url).get("merged"))
        except acme.AcmeError:
            return False
    try:
        acme.guard_write(acme.config(), "gh pr merge")
    except acme.GitHubWriteRefused:
        return False
    result = _run(
        ["gh", "pr", "merge", pr_url, "--squash", "--delete-branch"],
        timeout=60,
    )
    return result.returncode == 0


def ensure_dev_profile(base_dir, profile: str | None = None, broker: str | None = None,
                       model: str = DEV_MODEL_DEFAULT, cap: float = DEV_MAX_SESSION_COST) -> Path:
    """Write <base>/<profile>/{config,system.md,mcp.json} if missing. Never touches auth."""
    base_dir = Path(base_dir).expanduser()
    if base_dir.resolve() == Path("~/.synaps-cli").expanduser().resolve():
        raise RuntimeError("refusing to write a profile into the live Synaps dir")
    d = base_dir / (profile or _dev_profile())
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    files = {"config": DEV_PROFILE_CONFIG.format(model=model, cap=cap, broker=(broker or llm._cfg()["broker"])),
             "system.md": DEV_PROFILE_SYSTEM, "mcp.json": '{"mcpServers": {}}\n'}
    for name, body in files.items():
        p = d / name
        if not p.exists():
            p.write_text(body)
            os.chmod(p, 0o600)
    return d


if __name__ == "__main__":
    if sys.argv[1:2] == ["setup"]:
        base = llm._cfg()["base_dir"]
        if base is None:
            sys.exit("set COMPANY_SYNAPS_BASE_DIR to a private base dir first")
        print(ensure_dev_profile(base))
    else:
        sys.exit("usage: python -m company.devworker setup")
