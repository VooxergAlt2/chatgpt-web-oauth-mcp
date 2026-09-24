from __future__ import annotations

from chatgpt_web_oauth_mcp.activity import ActivityTracker


def _snapshot(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "success": True,
        "job_id": "job_test",
        "status": "running",
        "pid": 101,
        "process_identity_match": True,
        "process_group_verified": True,
        "process_group_signature": [{"pid": 101, "identity": "linux-start-ticks:1"}],
        "process_group_cpu_seconds": 1.0,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "last_output_at": None,
        "elapsed_seconds": 100.0,
    }
    value.update(overrides)
    return value


def test_recent_job_start_is_active() -> None:
    tracker = ActivityTracker(recent_activity_seconds=30)
    result = tracker.assess(_snapshot(elapsed_seconds=5.0), now=100.0)

    assert result["verdict"] == "ACTIVE"
    assert result["waiting_justified"] is True
    assert result["reasons"] == ["recent_job_start"]


def test_recent_job_output_is_active() -> None:
    tracker = ActivityTracker(recent_activity_seconds=30)
    result = tracker.assess(_snapshot(last_output_at=90.0), now=100.0)

    assert result["verdict"] == "ACTIVE"
    assert result["reasons"] == ["recent_job_output"]


def test_quiet_job_becomes_stalled_only_after_repeated_old_observations() -> None:
    tracker = ActivityTracker(stall_after_seconds=120, stall_min_observations=2)

    first = tracker.assess(_snapshot(), now=100.0)
    second = tracker.assess(_snapshot(), now=221.0)

    assert first["verdict"] == "QUIET"
    assert first["waiting_justified"] is False
    assert first["quiet_observations"] == 1
    assert second["verdict"] == "STALLED_SUSPECTED"
    assert second["waiting_justified"] is False
    assert second["quiet_observations"] == 2
    assert second["quiet_for_seconds"] == 121.0


def test_cpu_progress_resets_quiet_window() -> None:
    tracker = ActivityTracker(stall_after_seconds=120)

    assert tracker.assess(_snapshot(), now=100.0)["verdict"] == "QUIET"
    progressed = tracker.assess(_snapshot(process_group_cpu_seconds=1.05), now=150.0)

    assert progressed["verdict"] == "ACTIVE"
    assert progressed["reasons"] == ["process_group_cpu_advanced"]
    assert progressed["quiet_observations"] == 0


def test_output_growth_is_activity_even_without_cpu_progress() -> None:
    tracker = ActivityTracker()

    tracker.assess(_snapshot(), now=100.0)
    progressed = tracker.assess(_snapshot(stdout_bytes=10), now=101.0)

    assert progressed["verdict"] == "ACTIVE"
    assert progressed["reasons"] == ["job_output_grew"]


def test_process_group_change_is_activity() -> None:
    tracker = ActivityTracker()

    tracker.assess(_snapshot(), now=100.0)
    progressed = tracker.assess(
        _snapshot(
            process_group_signature=[
                {"pid": 101, "identity": "linux-start-ticks:1"},
                {"pid": 202, "identity": "linux-start-ticks:2"},
            ]
        ),
        now=101.0,
    )

    assert progressed["verdict"] == "ACTIVE"
    assert progressed["reasons"] == ["process_group_changed"]


def test_missing_recorded_process_is_dead_not_waitable() -> None:
    tracker = ActivityTracker()
    result = tracker.assess(_snapshot(process_identity_match=False, process_group_verified=False), now=100.0)

    assert result["verdict"] == "DEAD"
    assert result["waiting_justified"] is False
    assert result["required_action"] == "RECHECK_JOB_STATUS_OR_CONTINUE"


def test_unverifiable_process_group_is_unknown_not_waitable() -> None:
    tracker = ActivityTracker()
    result = tracker.assess(_snapshot(process_group_verified=False), now=100.0)

    assert result["verdict"] == "UNKNOWN"
    assert result["waiting_justified"] is False


def test_terminal_job_clears_previous_observation() -> None:
    tracker = ActivityTracker(stall_after_seconds=0)

    assert tracker.assess(_snapshot(), now=100.0)["verdict"] == "QUIET"
    terminal = tracker.assess(_snapshot(status="succeeded"), now=101.0)
    restarted = tracker.assess(_snapshot(), now=102.0)

    assert terminal["verdict"] == "TERMINAL"
    assert restarted["verdict"] == "QUIET"
    assert restarted["quiet_observations"] == 1
