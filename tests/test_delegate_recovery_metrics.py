from __future__ import annotations

import json
import time
from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_telemetry import DelegateTelemetryStore


def _terminal(delegate_id: str, status: str, error: str | None = None,
              message: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "delegate_id": delegate_id,
        "harness": "antigravity",
        "kind": "explore",
        "model": "gemini-3.8-flash-medium",
        "reasoning_effort": "medium",
        "status": status, "success": status == "succeeded",
        "duration_seconds": 12,
    }
    if error:
        result["error"] = {"code": error, "message": message or error}
    return result


def test_recovery_metrics_exclude_location_and_cancellations(tmp_path: Path) -> None:
    path = tmp_path / "recovery-metrics.json"
    store = DelegateTelemetryStore(path)
    now = time.time()
    ordinary = _terminal("normal-succeeded", "succeeded")
    recovered = _terminal("recovered-once", "succeeded")
    recovered["route_chain"] = {
        "initial_route": "antigravity:explore:gemini:medium",
        "final_route": "antigravity:explore:gemini:medium",
        "attempt_count": 2, "fallback_count": 0, "recovery_count": 1,
    }
    recovered["recovery"] = {
        "attempted": True, "recovered": True, "attempt_count": 1,
        "initial_error_code": "antigravity_stream_truncated",
        "previous_metadata": "/private/this-must-not-be-recorded",
        "conversation_id": "this-must-not-be-recorded",
    }
    exhausted = _terminal(
        "recovery-failed", "failed", "antigravity_stream_truncated",
    )
    exhausted["recovery"] = {
        "attempted": True, "recovered": False,
        "attempt_count": 1, "initial_error_code": "empty_harness_result",
    }
    unrelated = _terminal("ordinary-failed", "failed", "task_error")
    location = _terminal(
        "location-refuse", "failed", "antigravity_result_error",
        "User location is not supported for the API use.",
    )
    cancelled = _terminal("user-cancelled", "cancelled", "explicit_cancel")
    for row in (ordinary, recovered, exhausted, unrelated, location, cancelled):
        store.record_terminal(row, completed_at_epoch=now)

    overall = store.snapshot()["overall"]
    assert overall["terminal"] == 6
    assert overall["failed"] == 3
    reliability = overall["reliability_excluding_location_and_cancellations"]
    assert reliability["eligible_terminal"] == 4
    assert reliability["succeeded"] == 2
    assert reliability["not_succeeded"] == 2
    assert reliability["process_success_rate"] == 0.5
    assert reliability["excluded_location"] == 1
    assert reliability["excluded_cancellations"] == 1
    assert reliability["sample_sufficient_for_monitoring"] is False
    recovery = overall["recovery"]
    assert recovery["attempted_tasks"] == 2
    assert recovery["attempts"] == 2
    assert recovery["recovered_process_successes"] == 1
    assert recovery["not_recovered"] == 1
    assert recovery["sample_sufficient_for_monitoring"] is False
    assert recovery["recovery_process_success_rate"] == 0.5
    assert recovery["initial_error_codes"] == {
        "antigravity_stream_truncated": 1, "empty_harness_result": 1,
    }
    stored = json.loads(path.read_text(encoding="utf-8"))["records"]
    assert stored["recovered-once"]["route_chain"]["recovery_count"] == 1
    assert stored["recovered-once"]["recovery"] == {
        "attempted": True, "attempt_count": 1, "recovered": True,
        "initial_error_code": "antigravity_stream_truncated",
    }
    assert stored["location-refuse"]["reliability_exclusion"] == "location_refusal"
    assert "reliability_exclusion" not in stored["ordinary-failed"]
    assert "this-must-not-be-recorded" not in path.read_text(encoding="utf-8")
    restart = DelegateTelemetryStore(path).snapshot()
    assert restart["overall"]["recovery"] == recovery
    for window in ("24h", "7d"):
        recent = restart["recent_windows"][window]
        assert recent["recovery"]["attempted_tasks"] == 2
        assert recent["reliability_excluding_location_and_cancellations"][
            "excluded_location"
        ] == 1


def test_nonlocation_eligibility_remains_in_denominator(tmp_path: Path) -> None:
    store = DelegateTelemetryStore(tmp_path / "telemetry.json")
    access = _terminal(
        "verification-required", "failed", "antigravity_result_error",
        "Account verification required before this request.",
    )
    store.record_terminal(access)
    metric = store.snapshot()["overall"]["reliability_excluding_location_and_cancellations"]
    assert metric["eligible_terminal"] == 1
    assert metric["not_succeeded"] == 1
    assert metric["excluded_location"] == 0


def test_pre_activation_records_without_raw_errors_are_unclassified(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.json"
    store = DelegateTelemetryStore(path)
    historical = _terminal(
        "historical-legacy", "failed", "antigravity_result_error",
        "User location is not supported for the API use.",
    )
    store.record_terminal(historical)
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Historical sanitized telemetry cannot reconstruct the lost error text.
    payload["records"]["historical-legacy"].pop("reliability_observed")
    payload["records"]["historical-legacy"].pop("reliability_exclusion")
    path.write_text(json.dumps(payload), encoding="utf-8")
    metric = store.snapshot()["overall"]["reliability_excluding_location_and_cancellations"]
    assert metric["eligible_terminal"] == 0
    assert metric["excluded_location"] == 0
    assert metric["pre_activation_unclassified"] == 1
    assert metric["process_success_rate"] is None


def test_daily_archive_keeps_recovery_counts_without_double_count(
    tmp_path: Path,
) -> None:
    path = tmp_path / "archive.json"
    store = DelegateTelemetryStore(path, max_records=2, retention_seconds=60)
    now = time.time()
    recovered = _terminal("recover-archived", "succeeded")
    recovered["recovery"] = {
        "attempted": True, "recovered": True, "attempt_count": 1,
        "initial_error_code": "empty_harness_result",
    }
    location = _terminal(
        "location-archived", "failed", "antigravity_result_error",
        "Not currently available in your location.",
    )
    cancelled = _terminal("cancelled-retained", "cancelled", "explicit_cancel")
    store.record_terminal(recovered, completed_at_epoch=now - 3)
    store.record_terminal(location, completed_at_epoch=now - 2)
    store.record_terminal(cancelled, completed_at_epoch=now - 1)
    first = store.snapshot()["daily_history"]["days"][-1]
    assert first["terminal"] == 3
    assert first["recovery"] == {
        "attempted_tasks": 1,
        "attempts": 1,
        "recovered_process_successes": 1,
    }
    assert first["reliability_excluding_location_and_cancellations"] == {
        "succeeded": 1,
        "excluded_location": 1,
        "excluded_cancellations": 1,
    }
    restarted = DelegateTelemetryStore(path, max_records=2, retention_seconds=60)
    restarted.record_terminal(recovered, completed_at_epoch=now + 1)
    second = restarted.snapshot()["daily_history"]["days"][-1]
    assert second == first
