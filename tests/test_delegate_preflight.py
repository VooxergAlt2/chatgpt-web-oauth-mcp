"""Provider preflight regression: do not spend a delegate run on known bad auth/model."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import chatgpt_web_oauth_mcp.delegate_harnesses as harnesses
import chatgpt_web_oauth_mcp.executors as executors
from chatgpt_web_oauth_mcp.delegate_harnesses import AntigravityHarness, ClaudeHarness
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def test_claude_auth_probe_caches_and_fails_closed(monkeypatch: pytest.MonkeyPatch):
    harnesses._claude_auth_probe.cache_clear()
    monkeypatch.setattr(harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(harnesses, "resolve_command_parts", lambda *_a, **_k: ["claude"])
    calls = []

    def fake_run(args, **kwargs):
        calls.append(tuple(args))
        assert args == ["claude", "auth", "status"]
        assert kwargs["timeout"] == 4
        return SimpleNamespace(returncode=1, stdout='{"loggedIn":false}', stderr="")

    monkeypatch.setattr(harnesses.subprocess, "run", fake_run)
    account = ClaudeHarness(command="claude")
    info = account.info()
    assert info["available"] is False
    assert info["explore_available"] is False
    assert info["account_authenticated"] is False
    assert info["availability_reason"] == "account_authentication_required"
    assert account.info()["available"] is False
    assert len(calls) == 1
    harnesses._claude_auth_probe.cache_clear()
    monkeypatch.setattr(
        harnesses.subprocess,
        "run",
        lambda args, **kw: SimpleNamespace(
            returncode=0, stdout='{"loggedIn":true}', stderr="",
        ),
    )
    assert account.info()["available"] is True
    assert account.info()["account_authenticated"] is True
    harnesses._claude_auth_probe.cache_clear()


def test_claude_auth_probe_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch):
    harnesses._claude_auth_probe.cache_clear()
    monkeypatch.setattr(harnesses, "resolve_command_parts", lambda *_a, **_k: ["claude"])
    monkeypatch.setattr(
        harnesses.subprocess, "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="not json", stderr=""),
    )
    ok, reason = harnesses._claude_auth_probe("claude", 10101)
    assert not ok
    assert reason == "auth_probe_invalid"
    harnesses._claude_auth_probe.cache_clear()


def test_agy_model_catalog_is_isolated_by_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    harnesses._agy_models_probe.cache_clear()
    monkeypatch.setattr(harnesses, "resolve_command_parts", lambda *_a, **_k: ["agy"])
    calls = []

    def fake_run(args, **kwargs):
        assert args == ["agy", "models"]
        assert kwargs["timeout"] == 7
        home = kwargs["env"].get("HOME")
        calls.append(home)
        model = (
            "gemini-3.8-flash-medium" if home.endswith("second")
            else "gemini-3.8-flash-high"
        )
        return SimpleNamespace(
            returncode=0,
            stdout=f"Fetching available models...\n{model}\tGemini model\n",
            stderr="",
        )

    monkeypatch.setattr(harnesses.subprocess, "run", fake_run)
    first = AntigravityHarness(command="agy", home_dir=tmp_path / "first")
    second = AntigravityHarness(command="agy", home_dir=tmp_path / "second")
    assert first.model_admission("gemini-3.8-flash-high") == (True, "model_confirmed")
    assert second.model_admission("gemini-3.8-flash-high") == (
        False, "model_not_available_in_account",
    )
    assert first.model_admission("gemini-3.8-flash-high") == (True, "model_confirmed")
    assert len(calls) == 2
    assert str(tmp_path / "first") in calls
    assert str(tmp_path / "second") in calls
    harnesses._agy_models_probe.cache_clear()


@pytest.mark.parametrize(
    ("model", "requested", "expected"),
    [
        ("gemini-3.8-flash-medium", "high", "medium"),
        ("gemini-3.8-flash-high", "low", "high"),
        ("gemini-3.8-flash-low", "high", "low"),
        ("claude-opus-4-6-thinking", "high", "none"),
    ],
)
def test_agy_model_identifiers_define_allowed_effort(model, requested, expected):
    assert AntigravityHarness.reasoning_effort_for_model(model, requested) == expected


def test_explicit_unauthenticated_claude_is_rejected_before_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    monkeypatch.setattr(executors, "_command_available", lambda _cmd: True)
    monkeypatch.setattr(harnesses, "command_available", lambda _cmd: True)
    monkeypatch.setattr(
        harnesses, "_claude_auth_probe",
        lambda command, interval: (False, "account_authentication_required"),
    )
    registry = ExecutorRegistry(
        harnesses=[ClaudeHarness(command="claude")],
        default_harness="claude",
    )
    result = registry.run_delegate(
        harness="claude", kind="explore",
        task="Read one file", cwd=tmp_path, wait_seconds=0,
    )
    assert result["error"]["code"] == "delegate_harness_unauthenticated"
    assert len(registry.scheduler.tasks) == 0


def test_unverifiable_claude_auth_is_infrastructure_not_bad_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    monkeypatch.setattr(executors, "_command_available", lambda _cmd: True)
    monkeypatch.setattr(harnesses, "command_available", lambda _cmd: True)
    monkeypatch.setattr(
        harnesses, "_claude_auth_probe",
        lambda _cmd, _interval: (False, "auth_probe_unavailable"),
    )
    registry = ExecutorRegistry(harnesses=[ClaudeHarness(command="claude")])
    result = registry.run_delegate(
        harness="claude", kind="explore",
        task="Inspect one file", cwd=tmp_path, wait_seconds=0,
    )
    assert result["error"]["code"] == "delegate_harness_preflight_unavailable"
    assert len(registry.scheduler.tasks) == 0
    assert registry.harness_info()["claude"]["account_authenticated"] is None


def test_explicit_unsupported_agy_model_is_rejected_before_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    monkeypatch.setattr(executors, "_command_available", lambda _cmd: True)
    monkeypatch.setattr(harnesses, "command_available", lambda _cmd: True)
    home = tmp_path / "agy2-home"
    credential = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    credential.parent.mkdir(parents=True)
    credential.write_text("fixture-only", encoding="utf-8")
    monkeypatch.setattr(
        AntigravityHarness, "model_admission",
        lambda self, model: (False, "model_not_available_in_account"),
    )
    registry = ExecutorRegistry(
        harnesses=[AntigravityHarness(name="antigravity2", command="agy", home_dir=home)],
    )
    result = registry.run_delegate(
        harness="antigravity2",
        model="claude-opus-4-6-thinking",
        kind="explore", task="Review an isolated file", cwd=tmp_path, wait_seconds=0,
    )
    assert result["error"]["code"] == "delegate_model_unavailable"
    assert len(registry.scheduler.tasks) == 0
