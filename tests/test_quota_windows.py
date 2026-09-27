from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from chatgpt_web_oauth_mcp.quota_windows import QuotaWindowManager


NOW = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)


class FakeCollector:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.refresh_count = 0

    def snapshot(self) -> dict:
        return deepcopy(self.payload)

    def refresh_now(self) -> dict:
        self.refresh_count += 1
        return deepcopy(self.payload)


def _codex_payload(
    *,
    used_5h: float,
    remaining_5h: float,
    reset_5h: str,
    remaining_weekly: float = 90.0,
) -> dict:
    return {
        "status": "ok",
        "providers": {
            "codex": {
                "status": "ok",
                "windows": [
                    {
                        "id": "primary",
                        "window": "5h",
                        "duration_minutes": 300,
                        "used_percent": used_5h,
                        "remaining_percent": remaining_5h,
                        "resets_at": reset_5h,
                    },
                    {
                        "id": "secondary",
                        "window": "weekly",
                        "duration_minutes": 10080,
                        "used_percent": 100.0 - remaining_weekly,
                        "remaining_percent": remaining_weekly,
                        "resets_at": "2026-10-04T13:00:00Z",
                    },
                ],
            }
        },
    }


def _manager(
    tmp_path: Path,
    collector: FakeCollector,
    runner,
    *,
    post_reset_delay_seconds: float = 120,
) -> QuotaWindowManager:
    return QuotaWindowManager(
        usage_collector=collector,
        state_path=tmp_path / "quota-window-manager.json",
        antigravity_command="agy",
        claude_command="claude",
        codex_command="codex",
        check_interval_seconds=30,
        post_reset_delay_seconds=post_reset_delay_seconds,
        verification_delay_seconds=0,
        retry_seconds=300,
        command_timeout_seconds=10,
        command_runner=runner,
    )


def test_bootstrap_primes_idle_codex_once_and_persists_state(tmp_path: Path) -> None:
    collector = FakeCollector(
        _codex_payload(
            used_5h=0,
            remaining_5h=100,
            reset_5h="2026-09-27T18:00:00Z",
        )
    )
    calls: list[str] = []

    def runner(target):
        calls.append(target.key)
        collector.payload = _codex_payload(
            used_5h=0.2,
            remaining_5h=99.8,
            reset_5h="2026-09-27T18:00:05Z",
        )
        return {"success": True, "exit_code": 0}

    manager = _manager(tmp_path, collector, runner)
    first = manager.run_once(now=NOW)

    codex = first["buckets"]["codex_5h"]
    assert calls == ["codex_5h"]
    assert codex["status"] == "active"
    assert codex["last_prime_success"] is True
    assert codex["observed_used_percent"] == 0.2
    assert codex["attempts_since_activation"] == 0
    assert (tmp_path / "quota-window-manager.json").exists()

    second = manager.run_once(now=NOW)
    assert calls == ["codex_5h"]
    assert second["buckets"]["codex_5h"]["status"] == "active"


def test_active_window_arms_reset_then_primes_after_reset_delay(tmp_path: Path) -> None:
    collector = FakeCollector(
        _codex_payload(
            used_5h=8,
            remaining_5h=92,
            reset_5h="2026-09-27T14:00:00Z",
        )
    )
    calls: list[str] = []

    def runner(target):
        calls.append(target.key)
        collector.payload = _codex_payload(
            used_5h=0.1,
            remaining_5h=99.9,
            reset_5h="2026-09-27T19:03:00Z",
        )
        return {"success": True, "exit_code": 0}

    manager = _manager(tmp_path, collector, runner, post_reset_delay_seconds=120)
    armed = manager.run_once(now=NOW)
    assert calls == []
    assert armed["buckets"]["codex_5h"]["status"] == "active"
    assert armed["buckets"]["codex_5h"]["armed_reset_at"] == "2026-09-27T14:00:00Z"

    collector.payload = _codex_payload(
        used_5h=0,
        remaining_5h=100,
        reset_5h="2026-09-27T19:01:00Z",
    )
    waiting = manager.run_once(
        now=datetime(2026, 9, 27, 14, 1, 30, tzinfo=timezone.utc)
    )
    assert calls == []
    assert waiting["buckets"]["codex_5h"]["status"] == "waiting_reset"

    primed = manager.run_once(
        now=datetime(2026, 9, 27, 14, 2, 1, tzinfo=timezone.utc)
    )
    assert collector.refresh_count >= 1
    assert calls == ["codex_5h"]
    assert primed["buckets"]["codex_5h"]["status"] == "active"
    assert primed["buckets"]["codex_5h"]["armed_reset_at"] == "2026-09-27T19:03:00Z"


def test_weekly_exhaustion_blocks_priming(tmp_path: Path) -> None:
    collector = FakeCollector(
        _codex_payload(
            used_5h=0,
            remaining_5h=100,
            reset_5h="2026-09-27T18:00:00Z",
            remaining_weekly=0,
        )
    )
    calls: list[str] = []
    manager = _manager(
        tmp_path,
        collector,
        lambda target: calls.append(target.key) or {"success": True, "exit_code": 0},
    )

    result = manager.run_once(now=NOW)

    assert calls == []
    assert result["buckets"]["codex_5h"]["status"] == "weekly_exhausted"
    assert result["buckets"]["codex_5h"]["weekly_remaining_percent"] == 0.0


def test_failed_prime_is_rate_limited_and_retry_attempts_are_bounded(tmp_path: Path) -> None:
    collector = FakeCollector(
        _codex_payload(
            used_5h=0,
            remaining_5h=100,
            reset_5h="2026-09-27T18:00:00Z",
        )
    )
    calls: list[str] = []

    def runner(target):
        calls.append(target.key)
        return {"success": False, "exit_code": 1, "error": "exit_1"}

    manager = QuotaWindowManager(
        usage_collector=collector,
        state_path=tmp_path / "quota-window-manager.json",
        antigravity_command="agy",
        claude_command="claude",
        codex_command="codex",
        verification_delay_seconds=0,
        retry_seconds=300,
        command_timeout_seconds=10,
        max_attempts_per_cycle=2,
        command_runner=runner,
    )

    first = manager.run_once(now=NOW)
    assert calls == ["codex_5h"]
    assert first["buckets"]["codex_5h"]["status"] == "error"

    within_cooldown = manager.run_once(
        now=datetime(2026, 9, 27, 13, 4, 59, tzinfo=timezone.utc)
    )
    assert calls == ["codex_5h"]
    assert within_cooldown["buckets"]["codex_5h"]["status"] == "retry_wait"

    second = manager.run_once(
        now=datetime(2026, 9, 27, 13, 5, 1, tzinfo=timezone.utc)
    )
    assert calls == ["codex_5h", "codex_5h"]
    assert second["buckets"]["codex_5h"]["status"] == "error"

    exhausted = manager.run_once(
        now=datetime(2026, 9, 27, 13, 10, 2, tzinfo=timezone.utc)
    )
    assert calls == ["codex_5h", "codex_5h"]
    assert exhausted["buckets"]["codex_5h"]["status"] == "retry_exhausted"


def test_prime_commands_are_minimal_and_read_only(tmp_path: Path) -> None:
    collector = FakeCollector({"status": "warming", "providers": {}})
    manager = _manager(tmp_path, collector, lambda _target: {"success": True})

    agy_gemini = manager._prime_argv(manager._targets["antigravity_gemini_5h"])
    agy_third_party = manager._prime_argv(
        manager._targets["antigravity_claude_gpt_5h"]
    )
    claude = manager._prime_argv(manager._targets["claude_5h"])
    codex = manager._prime_argv(manager._targets["codex_5h"])

    assert "gemini-3.8-flash-low" in agy_gemini
    assert "gpt-oss-120b-medium" in agy_third_party
    assert "plan" in agy_gemini
    assert "--restricted" in claude
    assert "--safe-mode" in claude
    assert "haiku" in claude
    assert "--ephemeral" in codex
    assert "read-only" in codex
