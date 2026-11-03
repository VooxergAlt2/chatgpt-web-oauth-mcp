from __future__ import annotations

import json

import pytest

from chatgpt_web_oauth_mcp.acp_telemetry import AcpTelemetry


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def test_phase_totals_preserve_original_failure_through_cancel_and_cleanup():
    clock = Clock()
    telemetry = AcpTelemetry("session", clock=clock, wall_clock=lambda: 1700000000.0)
    assert telemetry.enter("connect")
    clock.value += 2
    telemetry.enter("initialize")
    clock.value += 3
    telemetry.enter("prompt")
    clock.value += 1
    telemetry.native_event("agent_thought_chunk")
    clock.value += 2
    telemetry.native_event("tool_call")
    telemetry.fail()
    telemetry.enter("cancel")
    clock.value += 4
    telemetry.fail()
    telemetry.enter("teardown")
    clock.value += 5
    snapshot = telemetry.snapshot()
    assert snapshot["failure_phase"] == "prompt"
    assert snapshot["phase_durations_seconds"] == {
        "connect": 2, "initialize": 3, "prompt": 3, "cancel": 4, "teardown": 5,
    }
    assert snapshot["elapsed_seconds"] == 17
    assert snapshot["event_count"] == 2
    assert snapshot["first_event_seconds"] == 6
    assert snapshot["last_event_seconds"] == 8
    assert snapshot["last_event"] == "tool_call"
    assert telemetry.snapshot() == snapshot  # observation never accumulates time twice


def test_progress_and_phase_changes_do_not_invent_native_activity():
    clock = Clock()
    telemetry = AcpTelemetry("worker", clock=clock, wall_clock=lambda: 1700000000.0)
    for phase in ("setup", "native", "validation", "verification", "promotion", "cleanup"):
        assert telemetry.enter(phase)
        assert not telemetry.enter(phase)
        clock.value += 1
        progress = telemetry.progress()
        assert progress["phase"] == phase
        assert progress["observed_at_epoch"] == 1700000000.0
    snapshot = telemetry.snapshot()
    assert snapshot["event_count"] == 0
    assert snapshot["first_event_seconds"] is None
    assert snapshot["last_event_seconds"] is None
    assert snapshot["last_event"] is None
    assert snapshot["failure_phase"] is None


def test_diagnostics_store_fixed_keys_and_never_store_unknown_event_text():
    clock = Clock()
    telemetry = AcpTelemetry("session", clock=clock)
    telemetry.enter("prompt")
    for _ in range(8192):
        telemetry.native_event("SECRET_PROVIDER_TEXT")
    with pytest.raises(ValueError, match="Invalid ACP telemetry phase"):
        telemetry.enter("SECRET_PHASE_TEXT")
    serialized = json.dumps(telemetry.snapshot())
    assert "SECRET" not in serialized
    assert len(serialized) < 1024
    assert telemetry.snapshot()["last_event"] == "unknown"
    assert telemetry.snapshot()["event_count"] == 8192
