from __future__ import annotations

import json
import shlex
import sys
import threading
import time
from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_harnesses import (
    GenericCliHarness,
    HarnessTaskDefaults,
)
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.quota_admission import DelegateQuotaAdmissionGate


def _python_command(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _usage(*, codex_5h: float = 100.0, codex_weekly: float = 100.0) -> dict:
    return {
        "status": "ok",
        "providers": {
            "codex": {
                "status": "ok",
                "windows": [
                    {
                        "window": "5h",
                        "duration_minutes": 300,
                        "remaining_percent": codex_5h,
                        "resets_at": "2026-09-28T12:00:00Z",
                    },
                    {
                        "window": "weekly",
                        "duration_minutes": 10080,
                        "remaining_percent": codex_weekly,
                        "resets_at": "2026-10-04T12:00:00Z",
                    },
                ],
            }
        },
    }


def _write_policy(path: Path, **thresholds: float) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": "2026-09-28T10:00:00Z",
                "thresholds": thresholds,
            }
        ),
        encoding="utf-8",
    )


def test_quota_gate_blocks_at_configured_nonzero_threshold(tmp_path: Path) -> None:
    policy = tmp_path / "quota.json"
    _write_policy(policy, codex_5h=10, codex_weekly=5)
    gate = DelegateQuotaAdmissionGate(
        usage_provider=lambda: _usage(codex_5h=10, codex_weekly=90),
        policy_path=policy,
    )

    decision = gate.decision(harness="codex", model="gpt-test")

    assert decision["allowed"] is False
    five_hour = next(row for row in decision["buckets"] if row["window"] == "5h")
    assert five_hour["remaining_percent"] == 10.0
    assert five_hour["threshold_percent"] == 10.0
    assert five_hour["blocked"] is True


def test_quota_gate_keeps_antigravity_accounts_independent(tmp_path: Path) -> None:
    policy = tmp_path / "quota.json"
    _write_policy(
        policy,
        antigravity_gemini_5h=20,
        antigravity_gemini_weekly=0,
        antigravity2_gemini_5h=5,
        antigravity2_gemini_weekly=0,
    )
    usage = {
        "status": "ok",
        "providers": {
            "antigravity": {
                "status": "ok",
                "windows": [
                    {"group": "Gemini Models", "window": "5h", "remaining_percent": 15},
                    {"group": "Gemini Models", "window": "weekly", "remaining_percent": 50},
                ],
            },
            "antigravity2": {
                "status": "ok",
                "windows": [
                    {"group": "Gemini Models", "window": "5h", "remaining_percent": 10},
                    {"group": "Gemini Models", "window": "weekly", "remaining_percent": 50},
                ],
            },
        },
    }
    gate = DelegateQuotaAdmissionGate(
        usage_provider=lambda: usage,
        policy_path=policy,
    )

    assert gate.decision(harness="antigravity", model="gemini-x")["allowed"] is False
    assert gate.decision(harness="antigravity2", model="gemini-x")["allowed"] is True


def test_quota_gate_blocks_new_submission_without_cancelling_running_task(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "quota.json"
    _write_policy(policy, codex_5h=10, codex_weekly=0)
    current = {"usage": _usage(codex_5h=50, codex_weekly=80)}
    gate = DelegateQuotaAdmissionGate(
        usage_provider=lambda: current["usage"],
        fresh_usage_provider=lambda _provider: current["usage"],
        policy_path=policy,
    )
    registry = ExecutorRegistry(
        codex_command=_python_command("import time; time.sleep(0.4); print('done')"),
        quota_admission_gate=gate,
    )

    running = registry.run_codex(
        task="already admitted",
        cwd=tmp_path,
        wait_seconds=0,
    )
    assert running["status"] in {"queued", "running"}
    queued = registry.run_codex(
        task="already queued",
        cwd=tmp_path,
        wait_seconds=0,
    )
    assert queued["status"] == "queued"

    current["usage"] = _usage(codex_5h=5, codex_weekly=80)
    rejected = registry.run_codex(
        task="must not be admitted",
        cwd=tmp_path,
        wait_seconds=0,
    )

    assert rejected["success"] is False
    assert rejected["error"]["code"] == "delegate_quota_blocked"
    assert registry.delegate_status(
        delegate_id=str(queued["delegate_id"])
    )["delegate"]["status"] == "queued"
    deadline = time.monotonic() + 3
    terminal = registry.delegate_status(
        delegate_id=str(running["delegate_id"])
    )["delegate"]
    while not terminal["completed"] and time.monotonic() < deadline:
        time.sleep(0.02)
        terminal = registry.delegate_status(
            delegate_id=str(running["delegate_id"])
        )["delegate"]
    assert terminal["status"] == "succeeded"
    assert terminal.get("error") is None
    deadline = time.monotonic() + 3
    queued_terminal = registry.delegate_status(
        delegate_id=str(queued["delegate_id"])
    )["delegate"]
    while not queued_terminal["completed"] and time.monotonic() < deadline:
        time.sleep(0.02)
        queued_terminal = registry.delegate_status(
            delegate_id=str(queued["delegate_id"])
        )["delegate"]
    assert queued_terminal["status"] == "succeeded"


def test_quota_gate_allows_dedupe_attach_after_threshold_is_reached(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "quota.json"
    _write_policy(policy, codex_5h=10)
    current = {"usage": _usage(codex_5h=50)}
    fresh_calls = {"count": 0}

    def fresh(provider: str) -> dict:
        assert provider == "codex"
        fresh_calls["count"] += 1
        return current["usage"]

    gate = DelegateQuotaAdmissionGate(
        usage_provider=lambda: current["usage"],
        fresh_usage_provider=fresh,
        policy_path=policy,
    )
    registry = ExecutorRegistry(
        codex_command=_python_command("import time; time.sleep(0.4); print('done')"),
        quota_admission_gate=gate,
    )

    first = registry.run_codex(
        task="same admitted work",
        cwd=tmp_path,
        wait_seconds=0,
    )
    assert fresh_calls["count"] == 1

    current["usage"] = _usage(codex_5h=5)
    attached = registry.run_codex(
        task="same admitted work",
        cwd=tmp_path,
        wait_seconds=0,
    )

    assert attached["delegate_id"] == first["delegate_id"]
    assert fresh_calls["count"] == 1


def test_batch_uses_child_model_quota_not_parent_default(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "quota.json"
    _write_policy(
        policy,
        antigravity_gemini_5h=20,
        antigravity_gemini_weekly=0,
        antigravity_claude_gpt_5h=20,
        antigravity_claude_gpt_weekly=0,
    )
    usage = {
        "status": "ok",
        "providers": {
            "antigravity": {
                "status": "ok",
                "windows": [
                    {"group": "Gemini Models", "window": "5h", "remaining_percent": 5},
                    {"group": "Gemini Models", "window": "weekly", "remaining_percent": 80},
                    {"group": "Claude and GPT models", "window": "5h", "remaining_percent": 80},
                    {"group": "Claude and GPT models", "window": "weekly", "remaining_percent": 80},
                ],
            }
        },
    }
    gate = DelegateQuotaAdmissionGate(
        usage_provider=lambda: usage,
        fresh_usage_provider=lambda provider: usage,
        policy_path=policy,
    )
    command = _python_command("print('ok')")
    harness = GenericCliHarness(
        name="antigravity",
        display_name="Test Antigravity",
        command=command,
        explore_command=command,
        explore_defaults=HarnessTaskDefaults(
            model="gemini-default",
            reasoning_effort="low",
            sandbox_mode="adapter-read-only",
        ),
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[harness],
        quota_admission_gate=gate,
    )

    result = registry.run_delegate_batch(
        tasks=[
            {"task": "third-party one", "model": "gpt-test"},
            {"task": "third-party two", "model": "claude-test"},
        ],
        cwd=tmp_path,
        harness="antigravity",
        wait_seconds=2,
    )

    assert result["success"] is True
    assert result["completed"] is True


def test_blocked_batch_creates_no_partial_delegate_records(tmp_path: Path) -> None:
    policy = tmp_path / "quota.json"
    _write_policy(
        policy,
        antigravity_gemini_5h=20,
        antigravity_gemini_weekly=0,
        antigravity_claude_gpt_5h=20,
        antigravity_claude_gpt_weekly=0,
    )
    usage = {
        "status": "ok",
        "providers": {
            "antigravity": {
                "status": "ok",
                "windows": [
                    {"group": "Gemini Models", "window": "5h", "remaining_percent": 80},
                    {"group": "Gemini Models", "window": "weekly", "remaining_percent": 80},
                    {"group": "Claude and GPT models", "window": "5h", "remaining_percent": 5},
                    {"group": "Claude and GPT models", "window": "weekly", "remaining_percent": 80},
                ],
            }
        },
    }
    gate = DelegateQuotaAdmissionGate(
        usage_provider=lambda: usage,
        fresh_usage_provider=lambda provider: usage,
        policy_path=policy,
    )
    command = _python_command("print('should-not-run')")
    harness = GenericCliHarness(
        name="antigravity",
        display_name="Test Antigravity",
        command=command,
        explore_command=command,
        explore_defaults=HarnessTaskDefaults(
            model="gemini-default",
            reasoning_effort="low",
            sandbox_mode="adapter-read-only",
        ),
    )
    state_root = tmp_path / "delegate-state"
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[harness],
        quota_admission_gate=gate,
        delegate_state_root=state_root,
    )

    result = registry.run_delegate_batch(
        tasks=[
            {"task": "allowed first", "model": "gemini-test"},
            {"task": "blocked second", "model": "gpt-test"},
        ],
        cwd=tmp_path,
        harness="antigravity",
        wait_seconds=0,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "delegate_quota_blocked"
    assert list(state_root.glob("*-delegates/*/metadata.json")) == []


def test_runtime_quota_exhaustion_forces_block_until_retry_window_expires(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy = tmp_path / "quota.json"
    _write_policy(policy, antigravity_gemini_5h=0, antigravity_gemini_weekly=0)
    now = {"value": 1000.0}
    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.quota_admission.time.time",
        lambda: now["value"],
    )
    usage = {
        "status": "ok",
        "providers": {
            "antigravity": {
                "status": "ok",
                "windows": [
                    {
                        "group": "Gemini Models",
                        "window": "5h",
                        "remaining_percent": 80,
                        "resets_at": "2030-01-01T00:00:00Z",
                    },
                    {
                        "group": "Gemini Models",
                        "window": "weekly",
                        "remaining_percent": 80,
                        "resets_at": "2030-01-02T00:00:00Z",
                    },
                ],
            }
        },
    }
    gate = DelegateQuotaAdmissionGate(
        usage_provider=lambda: usage,
        fresh_usage_provider=lambda _provider: usage,
        policy_path=policy,
    )

    gate.note_terminal(
        {
            "harness": "antigravity",
            "model": "gemini-3.8-flash",
            "harness_metadata": {
                "quota_exhausted": True,
                "retry_after": "2h 30m",
            },
            "error": {"code": "antigravity_quota_exhausted"},
        }
    )

    blocked = gate.decision(
        harness="antigravity",
        model="gemini-3.8-flash",
        fresh=True,
    )
    assert blocked["allowed"] is False
    assert blocked["reason"] == "runtime_quota_exhausted"

    now["value"] = 1000.0 + 2.5 * 3600 + 1
    recovered = gate.decision(
        harness="antigravity",
        model="gemini-3.8-flash",
        fresh=True,
    )
    assert recovered["allowed"] is True
    assert gate.snapshot()["forced_blocks"] == {}


def test_batch_fresh_quota_refresh_runs_outside_scheduler_lock(
    tmp_path: Path,
) -> None:
    policy = tmp_path / "quota.json"
    _write_policy(policy)
    observed: list[bool] = []
    registry: ExecutorRegistry

    def fresh(provider: str) -> dict:
        assert provider == "probe"

        def try_scheduler_lock() -> None:
            acquired = registry._lock.acquire(timeout=0.5)
            observed.append(acquired)
            if acquired:
                registry._lock.release()

        thread = threading.Thread(target=try_scheduler_lock)
        thread.start()
        thread.join(timeout=1)
        return {
            "status": "ok",
            "providers": {
                "probe": {
                    "status": "ok",
                    "windows": [],
                }
            },
        }

    gate = DelegateQuotaAdmissionGate(
        usage_provider=lambda: {
            "status": "ok",
            "providers": {"probe": {"status": "ok", "windows": []}},
        },
        fresh_usage_provider=fresh,
        policy_path=policy,
    )
    command = _python_command("import sys; sys.stdin.read(); print('done')")
    registry = ExecutorRegistry(
        codex_command=None,
        default_harness="probe",
        harnesses=[
            GenericCliHarness(
                name="probe",
                command=command,
                explore_command=command,
                explore_defaults=HarnessTaskDefaults(
                    model="default",
                    reasoning_effort="default",
                    sandbox_mode="adapter-enforced-read-only",
                ),
            )
        ],
        quota_admission_gate=gate,
        durable_harnesses=(),
    )

    result = registry.run_delegate_batch(
        tasks=[{"task": "one"}, {"task": "two"}],
        cwd=tmp_path,
        harness="probe",
        wait_seconds=2,
    )

    assert observed == [True]
    assert result["status"] == "succeeded"
    registry.shutdown(wait_seconds=1)


def test_quota_gate_uses_fresh_provider_for_new_admission(tmp_path: Path) -> None:
    policy = tmp_path / "quota.json"
    _write_policy(policy, codex_5h=20)
    calls = {"cached": 0, "fresh": 0}

    def cached() -> dict:
        calls["cached"] += 1
        return _usage(codex_5h=90)

    def fresh(provider: str) -> dict:
        assert provider == "codex"
        calls["fresh"] += 1
        return _usage(codex_5h=10)

    gate = DelegateQuotaAdmissionGate(
        usage_provider=cached,
        fresh_usage_provider=fresh,
        policy_path=policy,
    )

    assert gate.decision(harness="codex", model="gpt", fresh=False)["allowed"] is True
    assert gate.decision(harness="codex", model="gpt", fresh=True)["allowed"] is False
    assert calls == {"cached": 1, "fresh": 1}
