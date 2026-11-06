"""company/acme.py against a throwaway local bare repo (no network, no GitHub)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from company import acme
from company.market.sim import Market

IDENT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

BUGGY = 'COUPONS = {"SAVE10": 0.10}\n\ndef rate(code):\n    return COUPONS[code]\n'
FIXED = 'COUPONS = {"SAVE10": 0.10}\n\ndef rate(code):\n    if not code:\n        raise ValueError("coupon code required")\n    return COUPONS[code]\n'
REGRESSION = ('import pytest\nfrom shop import rate\n\ndef test_empty_code():\n'
              '    with pytest.raises(ValueError):\n        rate("")\n')


def g(*args, cwd):
    return acme.git(*args, cwd=cwd, env=IDENT).stdout.strip()


@pytest.fixture(autouse=True)
def _clean_run():
    acme.CREATED_THIS_RUN.clear()
    yield
    acme.CREATED_THIS_RUN.clear()


@pytest.fixture
def origin(tmp_path):
    """A bare repo with main = one buggy module + one passing test. Returns (cfg, tmp_path)."""
    src = tmp_path / "src"
    src.mkdir()
    g("init", "-q", "-b", "main", cwd=src)
    (src / "shop.py").write_text(BUGGY)
    (src / "tests").mkdir()
    (src / "tests" / "test_ok.py").write_text("from shop import rate\n\ndef test_rate():\n    assert rate('SAVE10') == 0.10\n")
    g("add", "-A", cwd=src)
    g("commit", "-qm", "initial", cwd=src)
    bare = tmp_path / "origin.git"
    acme.git("clone", "-q", "--bare", str(src), str(bare))
    return acme.config(remote=str(bare), repo="o/acme-shop"), tmp_path


def push_fix(cfg, tmp_path, base, issue=1, files=None):
    co = acme.clone(cfg.remote_url, tmp_path / f"work-{base}-{issue}", base)
    for name, body in (files or {"shop.py": FIXED, "tests/test_issue_1.py": REGRESSION}).items():
        (co / name).write_text(body)
    g("add", "-A", cwd=co)
    g("commit", "-qm", f"fix #{issue}", cwd=co)
    branch = acme.work_branch(issue, base)
    acme.push(co, branch, cfg, src="HEAD")
    return acme.local_pr_ref(cfg.remote_url, branch, base)


# ───────── config + guard ─────────

def test_config_local_and_github(monkeypatch):
    monkeypatch.delenv("ACME_REMOTE", raising=False)
    monkeypatch.delenv("ACME_ALLOW_GITHUB_WRITES", raising=False)
    c = acme.config()
    assert not c.local and c.remote_url == "git@github.com:adityapat958/acme-shop.git"
    with pytest.raises(acme.GitHubWriteRefused):
        acme.guard_write(c, "push")
    monkeypatch.setenv("ACME_REMOTE", "/tmp/x.git")
    c = acme.config()
    assert c.local and c.remote_url == "file:///tmp/x.git"
    acme.guard_write(c, "push")       # local: always allowed


def test_github_writes_need_the_flag(monkeypatch):
    monkeypatch.setenv("ACME_ALLOW_GITHUB_WRITES", "1")
    acme.guard_write(acme.config(remote="git@github.com:o/r.git"), "push")


# ───────── issues → market ─────────

GH = {
    "incident": [
        {"number": 2, "title": "Tax twice", "labels": [{"name": "incident"}, {"name": "sev2"}], "body": "x"},
        {"number": 1, "title": "Empty coupon", "labels": [{"name": "incident"}, {"name": "sev1"}], "body": ""},
        {"number": 4, "title": "Sort", "labels": [{"name": "incident"}], "body": "**Severity:** SEV-3 — meh"},
    ],
    "feature": [
        {"number": 5, "title": "Free shipping", "labels": [{"name": "feature"}], "body": "**Priority:** High — x"},
        {"number": 6, "title": "Bundle", "labels": [{"name": "feature"}], "body": "**Priority:** Medium"},
        {"number": 9, "title": "Unknown", "labels": [{"name": "feature"}], "body": ""},
    ],
}


def fake_gh(calls):
    def run(argv, **kw):
        calls.append(argv)
        label = argv[argv.index("--label") + 1]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(GH[label]), stderr="")
    return run


def test_market_issues_from_gh_json():
    m = acme.market_issues(GH["incident"], GH["feature"])
    assert m["incidents"] == [{"issue": 1, "title": "Empty coupon", "severity": 1},
                              {"issue": 2, "title": "Tax twice", "severity": 2},
                              {"issue": 4, "title": "Sort", "severity": 3}]
    assert [(f["issue"], f["demand"]) for f in m["features"]] == [(5, 0.9), (6, 0.6), (9, 0.5)]


def test_load_issues_reads_only_and_caches(tmp_path):
    calls = []
    cache = tmp_path / "issues.json"
    a = acme.load_issues("o/r", cache=cache, run=fake_gh(calls))
    b = acme.load_issues("o/r", cache=cache, run=fake_gh(calls))
    assert a == b and len(calls) == 2           # second call came from the cache
    for argv in calls:
        assert argv[:3] == ["gh", "issue", "list"] and "--state" in argv and "all" in argv


def test_build_market_uses_real_issue_numbers():
    issues = acme.market_issues(GH["incident"], GH["feature"])
    m = acme.build_market(7, {"weeks_per_quarter": 4}, issues=issues)
    assert isinstance(m, Market)
    week1 = [e for e in m.events_for_week(1) if e.kind == "incident"]
    assert sorted(e.payload["issue"] for e in week1) == [1, 2, 4]
    assert "issue #1" in week1[0].text
    assert acme.issue_kind(issues, 1) == "incident" and acme.issue_kind(issues, 5) == "feature"
    assert acme.issue_kind(issues, 99) is None


# ───────── PR refs ─────────

def test_pr_ref_round_trip():
    ref = acme.local_pr_ref("/tmp/o.git", "gen-1-fix-1", "gen-1")
    assert ref == "file:///tmp/o.git?head=gen-1-fix-1&base=gen-1"
    assert acme.parse_pr_ref(ref) == {"local": True, "remote": "file:///tmp/o.git", "head": "gen-1-fix-1",
                                      "base": "gen-1"}
    assert acme.parse_pr_ref("file:///tmp/o.git#gen-3-fix-2")["base"] == "gen-3"
    gh = acme.parse_pr_ref("https://github.com/adityapat958/acme-shop/pull/12")
    assert gh == {"local": False, "url": "https://github.com/adityapat958/acme-shop/pull/12",
                  "repo": "adityapat958/acme-shop", "number": 12}
    with pytest.raises(acme.AcmeError):
        acme.parse_pr_ref("PR #4")


# ───────── reset between trials ─────────

def test_baseline_and_generations_face_the_same_bugs(origin):
    cfg, tmp = origin
    sha = acme.ensure_baseline(cfg)
    assert acme.ensure_baseline(cfg) == sha                     # idempotent
    g1 = acme.start_generation(1, cfg)
    assert g1 == "gen-1" and (cfg.remote_url, "gen-1") in acme.CREATED_THIS_RUN
    pr = push_fix(cfg, tmp, g1)
    assert acme.merge_local(pr)["merged"]
    refs = acme.remote_refs(cfg.remote_url)
    assert refs["refs/heads/gen-1"] != sha and refs["refs/heads/main"] == sha   # main untouched
    g2 = acme.start_generation(2, cfg)
    assert acme.remote_refs(cfg.remote_url)["refs/heads/gen-2"] == sha          # the bug is back in gen-2
    co = acme.clone(cfg.remote_url, tmp / "check", g2)
    assert "if not code" not in (co / "shop.py").read_text()


def test_existing_foreign_generation_is_not_rewritten(origin):
    cfg, tmp = origin
    acme.start_generation(1, cfg)
    push_fix(cfg, tmp, "gen-1")
    acme.merge_local(acme.local_pr_ref(cfg.remote_url, "gen-1-fix-1", "gen-1"))
    acme.CREATED_THIS_RUN.clear()                                # a later run did not create gen-1
    with pytest.raises(acme.AcmeError, match="did not create"):
        acme.start_generation(1, cfg)
    assert acme.start_generation(1, cfg, prefix="r2-") == "r2-gen-1"


def test_generation_created_this_run_can_be_reset(origin):
    cfg, tmp = origin
    sha = acme.ensure_baseline(cfg)
    acme.start_generation(1, cfg)
    acme.merge_local(push_fix(cfg, tmp, "gen-1"))
    assert acme.start_generation(1, cfg) == "gen-1"              # ours: reset with a lease
    assert acme.remote_refs(cfg.remote_url)["refs/heads/gen-1"] == sha


def test_push_is_plain_for_branches_we_did_not_create(origin):
    cfg, tmp = origin
    acme.start_generation(1, cfg)
    push_fix(cfg, tmp, "gen-1")
    acme.CREATED_THIS_RUN.clear()
    co = acme.clone(cfg.remote_url, tmp / "other", "gen-1")      # diverging history, same branch name
    (co / "x.txt").write_text("x")
    g("add", "-A", cwd=co)
    g("commit", "-qm", "other", cwd=co)
    with pytest.raises(acme.AcmeError, match="not forcing"):
        acme.push(co, "gen-1-fix-1", cfg, src="HEAD")


# ───────── review inputs, tests, merge ─────────

def test_diff_files_tests_and_merge(origin):
    cfg, tmp = origin
    acme.start_generation(1, cfg)
    pr = push_fix(cfg, tmp, "gen-1")
    assert sorted(acme.pr_files(pr)) == ["shop.py", "tests/test_issue_1.py"]
    assert "+    if not code:" in acme.pr_diff(pr)
    before = acme.run_tests_at(cfg.remote_url, "gen-1")
    head = acme.run_tests_at(cfg.remote_url, "gen-1-fix-1")
    assert before["passed"] and head["passed"] and "2 passed" in head["summary"]
    m = acme.merge_local(pr, message="Merge acme-shop#1")
    assert m["merged"] and m["base"] == "gen-1"
    after = acme.tests_after_merge(pr)
    assert after["passed"] and after["sha"] == m["sha"]
    assert acme.merge_local(pr)["merged"] is False               # nothing left to merge


def test_regression_test_fails_on_the_reset_generation(origin):
    """The point of the reset: gen-k+1 still has the bug the gen-k fix removed."""
    cfg, tmp = origin
    acme.start_generation(1, cfg)
    acme.merge_local(push_fix(cfg, tmp, "gen-1"))
    acme.start_generation(2, cfg)
    co = acme.clone(cfg.remote_url, tmp / "g2", "gen-2")
    (co / "tests" / "test_issue_1.py").write_text(REGRESSION)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=co,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "1 failed" in r.stdout


def test_make_local_origin_has_no_github_remote(origin, tmp_path):
    cfg, tmp = origin
    url = acme.make_local_origin(tmp_path / "stand-in", source=cfg.remote_url)
    bare = acme.local_path(url)
    assert url.startswith("file://") and bare.exists()
    assert acme.git("remote", cwd=bare, check=False).stdout.strip() == ""
