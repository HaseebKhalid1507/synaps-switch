"""Unit tests for company/llm.py. No network: subprocess.run is mocked, or the
synaps binary is a local fake script. One opt-in live test (COMPANY_LLM_LIVE=1)."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import company.llm as llm

TOKEN = "test-machine-token-not-real"
SYSTEM = "You are finance."

# Real `synaps chat` 0.11.1 output shape (stdout = reply, stderr = banner + cost).
STDOUT = '```json\n{"dept": "finance", "ok": true}\n```\n'
STDERR = ("synaps 0.11.1 | claude-haiku-4-5-20251001 | session 20261106\n\n"
          "session saved: 20261106 ($0.0014)\n")


def _cp(stdout=STDOUT, stderr=STDERR, returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def env(tmp_path, monkeypatch):
    tok = tmp_path / "machine.token"
    tok.write_text(TOKEN + "\n")
    base = tmp_path / "base"
    for k in list(os.environ):
        if k.startswith(("COMPANY_", "SYNAPS_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("COMPANY_BROKER_TOKEN_FILE", str(tok))
    monkeypatch.setenv("COMPANY_SYNAPS_BASE_DIR", str(base))
    monkeypatch.setenv("COMPANY_BROKER_URL", "http://127.0.0.1:18191/")
    monkeypatch.setenv("COMPANY_SYNAPS_BIN", "synaps-fake")
    return {"token": tok, "base": base, "tmp": tmp_path}


def _run(*results):
    return patch("company.llm.subprocess.run", side_effect=list(results))


# ───────────────────────── helpers ─────────────────────────


class TestOneLine:
    def test_single_line_unchanged(self):
        assert llm._one_line("hello world") == "hello world"

    def test_newlines_become_escapes(self):
        assert llm._one_line("a\nb\r\nc\rd") == "a\\nb\\nc\\nd"
        assert "\n" not in llm._one_line("x\n\ny")

    def test_strips_outer_whitespace(self):
        assert llm._one_line("\n  hi \n") == "hi"

    def test_leading_slash_is_not_a_command(self):
        assert llm._one_line("/quit now") == "\\/quit now"
        assert llm._one_line("  /clear") == "\\/clear"

    def test_empty_prompt_raises(self):
        with pytest.raises(ValueError):
            llm._one_line(" \n ")


class TestParseCost:
    def test_session_saved_line(self):
        assert llm._parse_cost(STDERR) == pytest.approx(0.0014)

    def test_last_match_wins(self):
        assert llm._parse_cost("session saved: a ($0.0010)\nsession saved: b ($0.0023)") == pytest.approx(0.0023)

    def test_ansi_wrapped(self):
        assert llm._parse_cost("\x1b[2msession saved: abc ($0.0101)\x1b[0m") == pytest.approx(0.0101)

    def test_ignores_other_dollar_parens(self):
        assert llm._parse_cost("budget ($5000.00) approved") is None

    def test_missing(self):
        assert llm._parse_cost("nothing here") is None


class TestStripBanner:
    def test_removes_banner_and_cost_lines(self):
        mixed = STDERR + "the answer\n↳ 2 MCP servers available\n🎭 Agent: x\n"
        assert llm._strip_banner(mixed) == "the answer"

    def test_keeps_reply_lines(self):
        assert llm._strip_banner("line one\n\nline two\n") == "line one\n\nline two"

    def test_strips_ansi(self):
        assert llm._strip_banner("\x1b[0mhi\x1b[0m") == "hi"


class TestExtractJson:
    def test_bare(self):
        assert llm._extract_json('{"a": 1}') == {"a": 1}

    def test_json_fence(self):
        assert llm._extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_plain_fence(self):
        assert llm._extract_json('```\n{"a": [1, 2]}\n```') == {"a": [1, 2]}

    def test_surrounding_prose(self):
        assert llm._extract_json('Sure! Here it is: {"a": {"b": 2}} Hope that helps.') == {"a": {"b": 2}}

    def test_garbage_raises(self):
        with pytest.raises(json.JSONDecodeError):
            llm._extract_json("no json at all")


# ───────────────────────── complete ─────────────────────────


class TestComplete:
    def test_returns_text_and_cost(self, env):
        with _run(_cp(stdout="hello there\n")):
            text, cost = llm.complete(SYSTEM, "hi")
        assert text == "hello there"
        assert cost == pytest.approx(0.0014)

    def test_argv_matches_interface(self, env):
        with _run(_cp()) as run:
            llm.complete(SYSTEM, "hi")
        argv = run.call_args.args[0]
        assert argv == ["synaps-fake", "chat", "--profile", "company", "--no-extensions", "-s", SYSTEM]

    def test_prompt_on_stdin_as_one_line(self, env):
        with _run(_cp()) as run:
            llm.complete(SYSTEM, "line 1\nline 2")
        assert run.call_args.kwargs["input"] == "line 1\\nline 2\n"

    def test_env_is_private_and_brokered(self, env, monkeypatch):
        monkeypatch.setenv("SYNAPS_PROFILE", "leak")
        monkeypatch.setenv("SYNAPS_SESSION_ID", "leak")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "leak")
        with _run(_cp()) as run:
            llm.complete(SYSTEM, "hi")
        e = run.call_args.kwargs["env"]
        assert e["SYNAPS_MACHINE_TOKEN"] == TOKEN
        assert e["SYNAPS_AUTH_ENDPOINT"] == "http://127.0.0.1:18191"
        assert e["SYNAPS_BASE_DIR"] == str(env["base"])
        assert e["SYNAPS_DAEMON_AUTOSPAWN"] == "0"
        assert "SYNAPS_PROFILE" not in e and "SYNAPS_SESSION_ID" not in e
        assert "ANTHROPIC_API_KEY" not in e

    def test_token_never_in_argv(self, env):
        with _run(_cp()) as run:
            llm.complete(SYSTEM, "hi")
        assert not any(TOKEN in a for a in run.call_args.args[0])

    def test_no_base_dir_leaves_synaps_default(self, env, monkeypatch):
        monkeypatch.delenv("COMPANY_SYNAPS_BASE_DIR")
        with _run(_cp()) as run:
            llm.complete(SYSTEM, "hi")
        assert "SYNAPS_BASE_DIR" not in run.call_args.kwargs["env"]

    def test_neutral_temp_cwd(self, env):
        with _run(_cp()) as run:
            llm.complete(SYSTEM, "hi")
        cwd = run.call_args.kwargs["cwd"]
        assert "company-llm-" in cwd and not Path(cwd).exists()   # cleaned up after the call

    def test_profile_env_renames_default(self, env, monkeypatch):
        monkeypatch.setenv("COMPANY_SYNAPS_PROFILE", "company-haiku")
        with _run(_cp(), _cp()) as run:
            llm.complete(SYSTEM, "hi")
            llm.complete(SYSTEM, "hi", profile="company-dev")
        assert run.call_args_list[0].args[0][3] == "company-haiku"
        assert run.call_args_list[1].args[0][3] == "company-dev"

    def test_empty_system_uses_profile_system_md(self, env):
        with _run(_cp()) as run:
            llm.complete("  ", "hi")
        assert "-s" not in run.call_args.args[0]

    def test_huge_system_goes_through_a_file(self, env):
        big = "x" * (llm.INLINE_SYSTEM_MAX + 1)
        seen = {}

        def fake(argv, **kw):
            seen["path"] = argv[-1]
            seen["body"] = Path(argv[-1]).read_text()
            return _cp()

        with patch("company.llm.subprocess.run", side_effect=fake):
            llm.complete(big, "hi")
        assert seen["body"] == big and seen["path"].endswith("system.md")

    def test_timeout_passed_and_raised(self, env):
        with _run(subprocess.TimeoutExpired("synaps", 5)) as run:
            with pytest.raises(llm.LLMError, match="timed out"):
                llm.complete(SYSTEM, "hi", timeout=5)
        assert run.call_args.kwargs["timeout"] == 5

    def test_missing_token_raises_before_running(self, env):
        env["token"].unlink()
        with _run(_cp()) as run:
            with pytest.raises(llm.LLMError, match="no broker machine token"):
                llm.complete(SYSTEM, "hi")
        run.assert_not_called()

    def test_empty_token_raises(self, env):
        env["token"].write_text("\n")
        with _run(_cp()) as run:
            with pytest.raises(llm.LLMError, match="empty"):
                llm.complete(SYSTEM, "hi")
        run.assert_not_called()

    def test_nonzero_exit_raises_with_cost(self, env):
        err = STDERR.replace("session saved", "❌ turn failed [auth]\nsession saved")
        with _run(_cp(stdout="", stderr=err, returncode=1)):
            with pytest.raises(llm.LLMError, match="exited 1") as ei:
                llm.complete(SYSTEM, "hi")
        assert ei.value.cost_usd == pytest.approx(0.0014)

    def test_empty_reply_raises(self, env):
        with _run(_cp(stdout="\n")):
            with pytest.raises(llm.LLMError, match="no text"):
                llm.complete(SYSTEM, "hi")

    def test_missing_cost_line_books_zero(self, env):
        with _run(_cp(stdout="ok", stderr="")):
            assert llm.complete(SYSTEM, "hi") == ("ok", 0.0)

    def test_missing_binary(self, env):
        with _run(FileNotFoundError("synaps-fake")):
            with pytest.raises(llm.LLMError, match="not found"):
                llm.complete(SYSTEM, "hi")

    def test_empty_prompt_never_runs(self, env):
        with _run(_cp()) as run:
            with pytest.raises(ValueError):
                llm.complete(SYSTEM, "")
        run.assert_not_called()


# ───────────────────────── complete_json ─────────────────────────


class TestCompleteJson:
    def test_fenced_reply(self, env):
        with _run(_cp()) as run:
            obj, cost = llm.complete_json(SYSTEM, "status?")
        assert obj == {"dept": "finance", "ok": True}
        assert cost == pytest.approx(0.0014)
        system = run.call_args.args[0][-1]
        assert system.startswith(SYSTEM) and llm.JSON_SYSTEM in system

    def test_retries_once_and_sums_cost(self, env):
        bad = _cp(stdout="I think the answer is yes.", stderr="session saved: a ($0.0010)\n")
        good = _cp(stdout='{"ok": true}', stderr="session saved: b ($0.0020)\n")
        with _run(bad, good) as run:
            obj, cost = llm.complete_json(SYSTEM, "status?")
        assert obj == {"ok": True}
        assert cost == pytest.approx(0.0030)
        assert run.call_count == 2
        retry = run.call_args_list[1].kwargs["input"]
        assert retry.startswith("status?") and "not a valid JSON object" in retry

    def test_non_object_is_retried(self, env):
        with _run(_cp(stdout="[1, 2]"), _cp(stdout='{"a": 1}')) as run:
            obj, _ = llm.complete_json(SYSTEM, "x")
        assert obj == {"a": 1} and run.call_count == 2

    def test_two_failures_raise_with_total_cost(self, env):
        with _run(_cp(stdout="nope"), _cp(stdout="still nope")) as run:
            with pytest.raises(llm.LLMError, match="2 attempts") as ei:
                llm.complete_json(SYSTEM, "x")
        assert run.call_count == 2
        assert ei.value.cost_usd == pytest.approx(0.0028)

    def test_call_error_on_retry_carries_first_cost(self, env):
        with _run(_cp(stdout="nope"), _cp(stdout="", returncode=1)):
            with pytest.raises(llm.LLMError, match="exited 1") as ei:
                llm.complete_json(SYSTEM, "x")
        assert ei.value.cost_usd == pytest.approx(0.0028)

    def test_kwargs_pass_through(self, env):
        with _run(_cp()) as run:
            llm.complete_json(SYSTEM, "x", profile="company-dev", timeout=7)
        assert run.call_args.args[0][3] == "company-dev"
        assert run.call_args.kwargs["timeout"] == 7

    def test_no_system_still_asks_for_json(self, env):
        with _run(_cp()) as run:
            llm.complete_json("", "x")
        assert run.call_args.args[0][-1] == llm.JSON_SYSTEM


# ───────────────────────── fake binary (real process, no network) ─────────────────────────


FAKE = r'''#!{py}
import json, os, sys
lines = sys.stdin.read().splitlines()
sys.stderr.write("synaps 0.11.1 | claude-haiku-4-5-20251001 | session deadbeef\n\n")
print(json.dumps({{"argv": sys.argv[1:], "lines": lines, "cwd": os.getcwd(),
                  "token_ok": os.environ.get("SYNAPS_MACHINE_TOKEN") == {tok!r},
                  "endpoint": os.environ.get("SYNAPS_AUTH_ENDPOINT"),
                  "base": os.environ.get("SYNAPS_BASE_DIR")}}))
sys.stderr.write("session saved: deadbeef ($0.0007)\n")
'''


def test_fake_binary_end_to_end(env, monkeypatch):
    fake = env["tmp"] / "synaps"
    fake.write_text(FAKE.format(py=sys.executable, tok=TOKEN))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("COMPANY_SYNAPS_BIN", str(fake))
    obj, cost = llm.complete_json(SYSTEM, "first line\nsecond line")
    assert cost == pytest.approx(0.0007)
    assert obj["lines"] == ["first line\\nsecond line"]          # exactly one turn
    assert obj["argv"][:4] == ["chat", "--profile", "company", "--no-extensions"]
    assert obj["token_ok"] and obj["endpoint"] == "http://127.0.0.1:18191"
    assert obj["base"] == str(env["base"])


# ───────────────────────── profile setup ─────────────────────────


class TestEnsureProfile:
    def test_writes_profile(self, env):
        d = llm.ensure_profile(env["base"], "company", "http://127.0.0.1:18191/")
        assert d == env["base"] / "company"
        cfg = (d / "config").read_text()
        assert "model = claude-haiku-4-5" in cfg
        assert "auth.remote_endpoint = http://127.0.0.1:18191\n" in cfg
        assert "thinking = off" in cfg
        tools = [l for l in cfg.splitlines() if l.startswith("disabled_tools")][0]
        assert all(t in tools for t in ("bash", "write", "edit", "subagent", "shell_start"))
        assert json.loads((d / "mcp.json").read_text()) == {"mcpServers": {}}
        assert len((d / "system.md").read_text()) < 200
        assert stat.S_IMODE((d / "config").stat().st_mode) == 0o600
        assert not (d / "auth.json").exists()
        assert TOKEN not in cfg and "machine_token" not in cfg

    def test_idempotent_never_overwrites(self, env):
        d = llm.ensure_profile(env["base"], "company", "http://b")
        (d / "system.md").write_text("custom\n")
        llm.ensure_profile(env["base"], "company", "http://b")
        assert (d / "system.md").read_text() == "custom\n"

    def test_refuses_live_dir(self, env, monkeypatch):
        monkeypatch.setenv("HOME", str(env["tmp"]))
        (env["tmp"] / ".synaps-cli").mkdir()
        with pytest.raises(llm.LLMError, match="live Synaps dir"):
            llm.ensure_profile(env["tmp"] / ".synaps-cli", "company", "http://b")

    def test_setup_cli_needs_base_dir(self, env, monkeypatch):
        monkeypatch.delenv("COMPANY_SYNAPS_BASE_DIR")
        assert llm.main(["setup"]) == 2

    def test_setup_cli(self, env, capsys):
        assert llm.main(["setup"]) == 0
        assert (env["base"] / "company" / "config").exists()
        assert "auth.remote_endpoint = http://127.0.0.1:18191" in (env["base"] / "company" / "config").read_text()


# ───────────────────────── live (opt-in) ─────────────────────────


@pytest.mark.skipif(os.environ.get("COMPANY_LLM_LIVE") != "1", reason="live broker call: set COMPANY_LLM_LIVE=1")
def test_live_json_call_under_a_cent():
    obj, cost = llm.complete_json("You are the finance department of a small online shop.",
                                  'Week 1 ledger: $0 spent.\nReply with {"dept": "finance", "ok": true}.')
    assert obj.get("dept") == "finance" and obj.get("ok") is True
    assert 0 < cost < 0.01
