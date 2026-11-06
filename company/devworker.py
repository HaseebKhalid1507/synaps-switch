"""company/devworker.py — engineering agent worker.

Clones/fetches adityapat958/acme-shop, creates a branch, invokes
`synaps chat --profile company-dev --no-extensions` with a prompt to fix or
implement a GitHub issue, then commits, pushes, and opens a PR via `gh pr create`.

Contracts (from INTERFACES.md):
    work_issue(issue, kind, *, repo, workdir) -> dict
        {issue, pr_url, tests_passed, summary, cost_usd}
    merge_pr(pr_url) -> bool
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_DEFAULT = "adityapat958/acme-shop"
WORKDIR_DEFAULT = "company/state/work"
TIMEOUT_SECONDS = 15 * 60          # 15 min per issue
TOKEN_PATH = Path.home() / ".synaps-cli" / "company" / "broker" / "token"
_COST_RE = re.compile(r"\(\$([0-9]+\.[0-9]+)\)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_token() -> str:
    return TOKEN_PATH.read_text().strip()


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
    """Return (title, body) of a GitHub issue via gh."""
    result = _run(["gh", "issue", "view", str(issue),
                   "--repo", repo,
                   "--json", "title,body",
                   "-q", ".title,.body"],
                  timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"gh issue view failed: {result.stderr}")
    # gh puts title and body on separate sections when -q selects multiple
    # We just return raw output; the prompt will get the full context.
    out = result.stdout.strip()
    # Also grab a nicer combined view for the prompt
    full = _run(["gh", "issue", "view", str(issue), "--repo", repo], timeout=30)
    return out, full.stdout.strip()


def _clone_or_update(repo: str, workdir: str) -> str:
    """Clone repo into workdir/<repo_name> if not present, else fetch.
    Returns the local checkout path."""
    repo_name = repo.split("/")[-1]
    checkout = str(Path(workdir) / repo_name)
    Path(workdir).mkdir(parents=True, exist_ok=True)

    if Path(checkout).exists():
        _run(["git", "fetch", "origin"], cwd=checkout, timeout=60)
        _run(["git", "checkout", "main"], cwd=checkout, timeout=30)
        _run(["git", "reset", "--hard", "origin/main"], cwd=checkout, timeout=30)
    else:
        _run(["git", "clone", f"git@github.com:{repo}.git", checkout], timeout=120)

    return checkout


def _make_branch(issue: int, checkout: str) -> str:
    branch = f"issue-{issue}"
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


def _commit_and_push(issue: int, branch: str, checkout: str, summary: str) -> None:
    _run(["git", "add", "-A"], cwd=checkout, timeout=30)
    msg = f"fix: resolve issue #{issue}\n\n{summary[:500]}"
    result = _run(["git", "commit", "-m", msg], cwd=checkout, timeout=30)
    if result.returncode != 0 and "nothing to commit" in result.stdout + result.stderr:
        raise RuntimeError("Agent made no changes — nothing to commit")
    _run(["git", "push", "-u", "origin", branch, "--force"], cwd=checkout, timeout=60)


def _open_pr(issue: int, branch: str, checkout: str, repo: str, summary: str,
             pytest_tail: str) -> str:
    body = (
        f"Closes #{issue}\n\n"
        f"## Summary\n{summary}\n\n"
        f"## Test output\n```\n{pytest_tail[-1500:]}\n```"
    )
    result = _run(
        ["gh", "pr", "create",
         "--repo", repo,
         "--head", branch,
         "--base", "main",
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
        ["python3", "-m", "pytest", "-q"],
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
) -> dict:
    """Clone/fetch acme-shop, branch, invoke coding agent, commit, push, open PR.

    Returns:
        {issue, pr_url, tests_passed, summary, cost_usd}
    """
    t0 = time.time()
    pr_url = ""
    cost_usd = 0.0

    try:
        # 1. Prepare checkout
        checkout = _clone_or_update(repo, workdir)
        branch = _make_branch(issue, checkout)

        # 2. Fetch issue text
        _, issue_text = _fetch_issue(issue, repo)
        prompt = _build_prompt(issue, kind, issue_text)

        # 3. Build env for synaps chat
        machine_token = _read_token()
        env = {**os.environ, "SYNAPS_MACHINE_TOKEN": machine_token}

        # 4. Run the coding agent
        agent_result = subprocess.run(
            ["synaps", "chat", "--profile", "company-dev", "--no-extensions"],
            cwd=checkout,
            env=env,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
        agent_output = agent_result.stdout + agent_result.stderr
        cost_usd = _parse_cost(agent_output)

        # Extract last paragraph as summary (after agent finishes)
        paragraphs = [p.strip() for p in agent_output.split("\n\n") if p.strip()]
        summary = paragraphs[-1][:800] if paragraphs else "Agent produced no summary."

        # 5. Run pytest ourselves to verify
        tests_passed, pytest_output = _run_pytest(checkout)

        # 6. Commit + push + open PR
        _commit_and_push(issue, branch, checkout, summary)
        pr_url = _open_pr(issue, branch, checkout, repo, summary, pytest_output)

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
    """Squash-merge a PR and delete its branch. Called after board approval."""
    result = _run(
        ["gh", "pr", "merge", pr_url, "--squash", "--delete-branch", "--yes"],
        timeout=60,
    )
    return result.returncode == 0
