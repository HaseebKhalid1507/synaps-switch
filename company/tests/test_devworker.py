"""Unit tests for company/devworker.py — all subprocess calls are mocked."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch, mock_open

import pytest

import company.devworker as dw


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cp(stdout="", stderr="", returncode=0) -> subprocess.CompletedProcess:
    """Build a fake CompletedProcess."""
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# _parse_cost
# ---------------------------------------------------------------------------

class TestParseCost:
    def test_parses_cost_from_session_line(self):
        text = "session saved: my-session ($0.0137)"
        assert dw._parse_cost(text) == pytest.approx(0.0137)

    def test_picks_last_match(self):
        text = "first ($0.0010)\nsecond run\nsession saved: foo ($0.0023)"
        assert dw._parse_cost(text) == pytest.approx(0.0023)

    def test_no_match_returns_zero(self):
        assert dw._parse_cost("no cost info here") == 0.0

    def test_integer_part_zero(self):
        assert dw._parse_cost("($0.5000)") == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_contains_issue_number(self):
        p = dw._build_prompt(4, "incident", "some issue text")
        assert "#4" in p

    def test_contains_kind(self):
        p = dw._build_prompt(7, "feature", "details")
        assert "feature" in p

    def test_contains_issue_text(self):
        p = dw._build_prompt(1, "incident", "NaN crash on empty coupon")
        assert "NaN crash on empty coupon" in p

    def test_instructs_no_git_push(self):
        p = dw._build_prompt(1, "incident", "x")
        assert "git push" in p.lower() or "Do NOT push" in p

    def test_names_regression_test_file(self):
        p = dw._build_prompt(4, "incident", "x")
        assert "test_issue_4.py" in p


# ---------------------------------------------------------------------------
# _fetch_issue
# ---------------------------------------------------------------------------

class TestFetchIssue:
    def test_calls_gh_issue_view_twice(self):
        ok = _cp(stdout="title\nbody")
        with patch("company.devworker._run", return_value=ok) as mock_run:
            dw._fetch_issue(3, "owner/repo")
        assert mock_run.call_count == 2

    def test_raises_on_failure(self):
        fail = _cp(stdout="", stderr="not found", returncode=1)
        with patch("company.devworker._run", return_value=fail):
            with pytest.raises(RuntimeError, match="gh issue view failed"):
                dw._fetch_issue(99, "owner/repo")

    def test_returns_tuple_of_strings(self):
        ok = _cp(stdout="the text")
        with patch("company.devworker._run", return_value=ok):
            result = dw._fetch_issue(1, "owner/repo")
        assert isinstance(result, tuple) and len(result) == 2


# ---------------------------------------------------------------------------
# _clone_or_update — repo already exists vs. fresh clone
# ---------------------------------------------------------------------------

class TestCloneOrUpdate:
    def test_clones_when_no_checkout(self, tmp_path):
        ok = _cp()
        with patch("company.devworker._run", return_value=ok) as mock_run, \
             patch("company.devworker.Path.exists", return_value=False):
            checkout = dw._clone_or_update("owner/acme-shop", str(tmp_path))
        # git clone should be called
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any(c[0] == "git" and c[1] == "clone" for c in cmds)

    def test_fetches_when_checkout_exists(self, tmp_path):
        (tmp_path / "acme-shop").mkdir()
        ok = _cp()
        with patch("company.devworker._run", return_value=ok) as mock_run:
            dw._clone_or_update("owner/acme-shop", str(tmp_path))
        cmds = [tuple(c.args[0]) for c in mock_run.call_args_list]
        assert any(c[:2] == ("git", "fetch") for c in cmds)

    def test_returns_checkout_path(self, tmp_path):
        ok = _cp()
        with patch("company.devworker._run", return_value=ok), \
             patch("company.devworker.Path.exists", return_value=False):
            result = dw._clone_or_update("owner/acme-shop", str(tmp_path))
        assert result.endswith("acme-shop")


# ---------------------------------------------------------------------------
# _make_branch
# ---------------------------------------------------------------------------

class TestMakeBranch:
    def test_creates_issue_branch(self):
        ok = _cp()
        with patch("company.devworker._run", return_value=ok) as mock_run:
            branch = dw._make_branch(7, "/some/checkout")
        assert branch == "issue-7"
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any("issue-7" in c for c in cmds)


# ---------------------------------------------------------------------------
# _run_pytest
# ---------------------------------------------------------------------------

class TestRunPytest:
    def test_passes_on_returncode_0(self):
        ok = _cp(stdout="5 passed", returncode=0)
        with patch("company.devworker._run", return_value=ok):
            passed, out = dw._run_pytest("/checkout")
        assert passed is True
        assert "5 passed" in out

    def test_fails_on_nonzero_returncode(self):
        fail = _cp(stdout="1 failed", returncode=1)
        with patch("company.devworker._run", return_value=fail):
            passed, out = dw._run_pytest("/checkout")
        assert passed is False


# ---------------------------------------------------------------------------
# _commit_and_push
# ---------------------------------------------------------------------------

class TestCommitAndPush:
    def test_adds_commits_and_pushes(self):
        ok = _cp(stdout="1 file changed")
        with patch("company.devworker._run", return_value=ok) as mock_run:
            dw._commit_and_push(4, "issue-4", "/checkout", "fixed the sort bug")
        cmds = [tuple(c.args[0]) for c in mock_run.call_args_list]
        assert any(c[:2] == ("git", "add") for c in cmds)
        assert any(c[:2] == ("git", "commit") for c in cmds)
        assert any(c[:2] == ("git", "push") for c in cmds)

    def test_raises_when_nothing_to_commit(self):
        nothing = _cp(stdout="nothing to commit", returncode=1)
        with patch("company.devworker._run", return_value=nothing):
            with pytest.raises(RuntimeError, match="nothing to commit"):
                dw._commit_and_push(1, "issue-1", "/checkout", "summary")

    def test_commit_message_contains_issue_number(self):
        ok = _cp(stdout="ok")
        with patch("company.devworker._run", return_value=ok) as mock_run:
            dw._commit_and_push(9, "issue-9", "/checkout", "some fix")
        commit_calls = [c for c in mock_run.call_args_list
                        if c.args[0][1] == "commit"]
        assert commit_calls, "git commit not called"
        msg_flag_idx = commit_calls[0].args[0].index("-m")
        commit_msg = commit_calls[0].args[0][msg_flag_idx + 1]
        assert "#9" in commit_msg


# ---------------------------------------------------------------------------
# _open_pr
# ---------------------------------------------------------------------------

class TestOpenPr:
    def test_calls_gh_pr_create(self):
        ok = _cp(stdout="https://github.com/owner/repo/pull/42")
        with patch("company.devworker._run", return_value=ok) as mock_run:
            url = dw._open_pr(4, "issue-4", "/checkout", "owner/repo",
                              "fixed sort", "5 passed")
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any(c[:3] == ["gh", "pr", "create"] for c in cmds)

    def test_returns_pr_url(self):
        ok = _cp(stdout="Some text\nhttps://github.com/owner/repo/pull/42")
        with patch("company.devworker._run", return_value=ok):
            url = dw._open_pr(4, "issue-4", "/checkout", "owner/repo",
                              "summary", "output")
        assert url == "https://github.com/owner/repo/pull/42"

    def test_raises_on_failure(self):
        fail = _cp(stderr="already exists", returncode=1)
        with patch("company.devworker._run", return_value=fail):
            with pytest.raises(RuntimeError, match="gh pr create failed"):
                dw._open_pr(4, "issue-4", "/checkout", "owner/repo", "s", "o")

    def test_body_contains_closes(self):
        captured = {}
        def capture_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _cp(stdout="https://github.com/owner/repo/pull/5")
        with patch("company.devworker._run", side_effect=capture_run):
            dw._open_pr(5, "issue-5", "/checkout", "owner/repo", "summary", "out")
        body_idx = captured["cmd"].index("--body")
        assert "Closes #5" in captured["cmd"][body_idx + 1]


# ---------------------------------------------------------------------------
# merge_pr
# ---------------------------------------------------------------------------

class TestMergePr:
    def test_calls_gh_pr_merge(self):
        ok = _cp()
        with patch("company.devworker._run", return_value=ok) as mock_run:
            result = dw.merge_pr("https://github.com/owner/repo/pull/42")
        assert result is True
        cmds = [c.args[0] for c in mock_run.call_args_list]
        assert any(c[:3] == ["gh", "pr", "merge"] for c in cmds)

    def test_squash_flag_present(self):
        ok = _cp()
        with patch("company.devworker._run", return_value=ok) as mock_run:
            dw.merge_pr("https://github.com/owner/repo/pull/42")
        cmd = mock_run.call_args.args[0]
        assert "--squash" in cmd

    def test_returns_false_on_failure(self):
        fail = _cp(returncode=1)
        with patch("company.devworker._run", return_value=fail):
            assert dw.merge_pr("https://github.com/owner/repo/pull/1") is False


# ---------------------------------------------------------------------------
# work_issue — integration of all helpers
# ---------------------------------------------------------------------------

class TestWorkIssue:
    """Mock every _helper so work_issue's orchestration logic is tested."""

    def _patch_all(self, *,
                   agent_stdout="All good\n\nsession saved: s ($0.0042)",
                   agent_returncode=0,
                   pytest_passed=True,
                   pytest_output="5 passed",
                   pr_url="https://github.com/owner/repo/pull/7"):
        patches = {
            "_clone_or_update": patch("company.devworker._clone_or_update",
                                      return_value="/tmp/acme-shop"),
            "_make_branch":     patch("company.devworker._make_branch",
                                      return_value="issue-4"),
            "_fetch_issue":     patch("company.devworker._fetch_issue",
                                      return_value=("title", "Issue body text")),
            "_read_token":      patch("company.devworker._read_token",
                                      return_value="fake-token"),
            "_run_pytest":      patch("company.devworker._run_pytest",
                                      return_value=(pytest_passed, pytest_output)),
            "_commit_and_push": patch("company.devworker._commit_and_push"),
            "_open_pr":         patch("company.devworker._open_pr",
                                      return_value=pr_url),
            "subprocess.run":   patch("company.devworker.subprocess.run",
                                      return_value=_cp(stdout=agent_stdout,
                                                       returncode=agent_returncode)),
        }
        return patches

    def test_happy_path_returns_correct_dict(self):
        patches = self._patch_all()
        with patches["_clone_or_update"], patches["_make_branch"], \
             patches["_fetch_issue"], patches["_read_token"], \
             patches["_run_pytest"], patches["_commit_and_push"], \
             patches["_open_pr"], patches["subprocess.run"]:
            result = dw.work_issue(4, "incident")

        assert result["issue"] == 4
        assert result["pr_url"] == "https://github.com/owner/repo/pull/7"
        assert result["tests_passed"] is True
        assert result["cost_usd"] == pytest.approx(0.0042)
        assert isinstance(result["summary"], str)

    def test_cost_parsed_from_agent_output(self):
        patches = self._patch_all(agent_stdout="done\n\nsession saved: x ($0.1234)")
        with patches["_clone_or_update"], patches["_make_branch"], \
             patches["_fetch_issue"], patches["_read_token"], \
             patches["_run_pytest"], patches["_commit_and_push"], \
             patches["_open_pr"], patches["subprocess.run"]:
            result = dw.work_issue(4, "incident")
        assert result["cost_usd"] == pytest.approx(0.1234)

    def test_pytest_failure_reflected_in_result(self):
        patches = self._patch_all(pytest_passed=False, pytest_output="1 failed")
        with patches["_clone_or_update"], patches["_make_branch"], \
             patches["_fetch_issue"], patches["_read_token"], \
             patches["_run_pytest"], patches["_commit_and_push"], \
             patches["_open_pr"], patches["subprocess.run"]:
            result = dw.work_issue(4, "incident")
        assert result["tests_passed"] is False

    def test_timeout_returns_failure_dict(self):
        patches = self._patch_all()
        with patches["_clone_or_update"], patches["_make_branch"], \
             patches["_fetch_issue"], patches["_read_token"]:
            with patch("company.devworker.subprocess.run",
                       side_effect=subprocess.TimeoutExpired(cmd="synaps", timeout=900)):
                result = dw.work_issue(4, "incident")
        assert result["tests_passed"] is False
        assert "Timed" in result["summary"]
        assert result["issue"] == 4

    def test_gh_failure_returns_failure_dict(self):
        patches = self._patch_all()
        with patches["_clone_or_update"], patches["_make_branch"], \
             patches["_fetch_issue"], patches["_read_token"], \
             patches["_run_pytest"], patches["_commit_and_push"], \
             patches["subprocess.run"]:
            with patch("company.devworker._open_pr",
                       side_effect=RuntimeError("gh pr create failed: already exists")):
                result = dw.work_issue(4, "incident")
        assert result["tests_passed"] is False
        assert "Error" in result["summary"]

    def test_synaps_invoked_with_machine_token_in_env(self):
        captured_env = {}
        def fake_run(cmd, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return _cp(stdout="done\nsession saved: s ($0.001)")

        patches = self._patch_all()
        with patches["_clone_or_update"], patches["_make_branch"], \
             patches["_fetch_issue"], patches["_run_pytest"], \
             patches["_commit_and_push"], patches["_open_pr"], \
             patch("company.devworker._read_token", return_value="tok-xyz"), \
             patch("company.devworker.subprocess.run", side_effect=fake_run):
            dw.work_issue(4, "incident")

        assert captured_env.get("SYNAPS_MACHINE_TOKEN") == "tok-xyz"

    def test_synaps_invoked_with_correct_flags(self):
        captured_cmd = {}
        def fake_run(cmd, **kwargs):
            captured_cmd["cmd"] = cmd
            return _cp(stdout="done\nsession saved: s ($0.001)")

        patches = self._patch_all()
        with patches["_clone_or_update"], patches["_make_branch"], \
             patches["_fetch_issue"], patches["_read_token"], \
             patches["_run_pytest"], patches["_commit_and_push"], \
             patches["_open_pr"], \
             patch("company.devworker.subprocess.run", side_effect=fake_run):
            dw.work_issue(4, "incident")

        cmd = captured_cmd["cmd"]
        assert cmd[0] == "synaps"
        assert "--profile" in cmd
        assert "company-dev" in cmd
        assert "--no-extensions" in cmd

    def test_prompt_sent_as_stdin(self):
        captured_input = {}
        def fake_run(cmd, **kwargs):
            captured_input["input"] = kwargs.get("input", "")
            return _cp(stdout="done\nsession saved: s ($0.001)")

        patches = self._patch_all()
        with patches["_clone_or_update"], patches["_make_branch"], \
             patches["_fetch_issue"], patches["_read_token"], \
             patches["_run_pytest"], patches["_commit_and_push"], \
             patches["_open_pr"], \
             patch("company.devworker.subprocess.run", side_effect=fake_run):
            dw.work_issue(4, "incident")

        assert "#4" in captured_input["input"]

    def test_cwd_is_checkout_dir(self):
        captured_cwd = {}
        def fake_run(cmd, **kwargs):
            captured_cwd["cwd"] = kwargs.get("cwd")
            return _cp(stdout="done\nsession saved: s ($0.001)")

        patches = self._patch_all()
        with patch("company.devworker._clone_or_update", return_value="/tmp/my-checkout"), \
             patches["_make_branch"], patches["_fetch_issue"], \
             patches["_read_token"], patches["_run_pytest"], \
             patches["_commit_and_push"], patches["_open_pr"], \
             patch("company.devworker.subprocess.run", side_effect=fake_run):
            dw.work_issue(4, "incident")

        assert captured_cwd["cwd"] == "/tmp/my-checkout"

    def test_result_keys_match_contract(self):
        patches = self._patch_all()
        with patches["_clone_or_update"], patches["_make_branch"], \
             patches["_fetch_issue"], patches["_read_token"], \
             patches["_run_pytest"], patches["_commit_and_push"], \
             patches["_open_pr"], patches["subprocess.run"]:
            result = dw.work_issue(4, "incident")
        assert set(result.keys()) == {"issue", "pr_url", "tests_passed",
                                       "summary", "cost_usd"}
