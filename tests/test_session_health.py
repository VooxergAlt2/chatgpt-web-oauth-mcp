from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

from chatgpt_web_oauth_mcp.session import SessionRegistry


ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = ROOT / "scripts" / "ops_mcp_ha_monitor.py"


def _load_monitor_module():
    spec = importlib.util.spec_from_file_location("ops_mcp_ha_monitor_test", MONITOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_session_registry_classifies_stalled_request_and_orchestration_quiet() -> None:
    registry = SessionRegistry()
    registry.set_default_cwd(Path("/srv/project-a"), session_id="session-a")
    registry.begin_request(
        session_id="session-a",
        request_id="request-1",
        rpc_method="tools/call",
        tool="run_command",
        expected_deadline_at=110.0,
        started_at=100.0,
    )

    stalled = registry.snapshot(
        idle_ttl_seconds=3600,
        request_stall_seconds=30,
        orchestration_quiet_seconds=60,
        now=150.0,
    )
    assert stalled["counts"]["stalled_request"] == 1
    row = stalled["sessions"][0]
    assert row["state"] == "stalled_request"
    assert row["project"] == "project-a"
    assert row["current_tool"] == "run_command"
    assert row["id"] != "session-a"

    registry.end_request(
        session_id="session-a",
        request_id="request-1",
        finished_at=151.0,
    )
    registry.note_execution_state(
        session_id="session-a",
        required_action="INVOKE_NEXT_TOOL_OR_RETURN_CHECKPOINT",
        state="NEXT_ACTION_REQUIRED",
        now=160.0,
    )
    quiet = registry.snapshot(
        idle_ttl_seconds=3600,
        request_stall_seconds=30,
        orchestration_quiet_seconds=60,
        now=221.0,
    )
    assert quiet["counts"]["orchestration_quiet"] == 1
    row = quiet["sessions"][0]
    assert row["state"] == "orchestration_quiet"
    assert row["required_action"] == "INVOKE_NEXT_TOOL_OR_RETURN_CHECKPOINT"


def test_session_registry_filters_idle_ephemeral_transport_and_attributes_active_cwd() -> None:
    registry = SessionRegistry()
    registry.touch("transport-idle", now=100.0)
    registry.begin_request(
        session_id="logical-session",
        request_id="logical-request",
        rpc_method="tools/call",
        tool="server_info",
        persistent_scope=True,
        expected_deadline_at=500.0,
        started_at=100.0,
    )
    registry.end_request(
        session_id="logical-session",
        request_id="logical-request",
        finished_at=110.0,
    )
    registry.begin_request(
        session_id="transport-active",
        request_id="transport-request",
        rpc_method="tools/call",
        tool="run_command",
        cwd=Path("/srv/active-project"),
        expected_deadline_at=500.0,
        started_at=120.0,
    )

    snapshot = registry.snapshot(
        idle_ttl_seconds=3600,
        ephemeral_idle_ttl_seconds=300,
        request_stall_seconds=180,
        orchestration_quiet_seconds=180,
        now=125.0,
    )

    assert snapshot["session_count"] == 2
    assert snapshot["transport_session_count"] == 2
    assert snapshot["ephemeral_idle_count"] == 1
    assert snapshot["counts"]["active"] == 1
    assert snapshot["counts"]["idle"] == 1
    active = next(item for item in snapshot["sessions"] if item["state"] == "active")
    assert active["project"] == "active-project"
    assert active["scope"] == "transport"
    assert active["sticky"] is False
    logical = next(item for item in snapshot["sessions"] if item["state"] == "idle")
    assert logical["scope"] == "logical"
    assert logical["sticky"] is True


def test_session_registry_prunes_ephemeral_idle_before_logical_session() -> None:
    registry = SessionRegistry()
    registry.touch("transport-idle", now=100.0)
    registry.begin_request(
        session_id="logical-session",
        request_id="logical-request",
        rpc_method="tools/call",
        tool="server_info",
        persistent_scope=True,
        expected_deadline_at=500.0,
        started_at=100.0,
    )
    registry.end_request(
        session_id="logical-session",
        request_id="logical-request",
        finished_at=110.0,
    )

    snapshot = registry.snapshot(
        idle_ttl_seconds=3600,
        ephemeral_idle_ttl_seconds=300,
        request_stall_seconds=180,
        orchestration_quiet_seconds=180,
        now=401.0,
    )

    assert snapshot["session_count"] == 1
    assert snapshot["transport_session_count"] == 0
    assert snapshot["ephemeral_idle_count"] == 0
    assert snapshot["sessions"][0]["scope"] == "logical"


def test_new_request_clears_pending_required_action() -> None:
    registry = SessionRegistry()
    registry.note_execution_state(
        session_id="session-a",
        required_action="POLL_DELEGATE_STATUS",
        state="ACTIVE_DELEGATE",
        now=100.0,
    )
    registry.begin_request(
        session_id="session-a",
        request_id="request-2",
        rpc_method="tools/call",
        tool="delegate_status",
        expected_deadline_at=300.0,
        started_at=120.0,
    )
    snapshot = registry.snapshot(
        idle_ttl_seconds=3600,
        request_stall_seconds=180,
        orchestration_quiet_seconds=10,
        now=125.0,
    )
    row = snapshot["sessions"][0]
    assert row["state"] == "active"
    assert row["required_action"] is None


def test_session_registry_active_window_does_not_count_retained_idle() -> None:
    registry = SessionRegistry()
    for session_id, timestamp in (("session-a", 99.0), ("session-b", 650.0)):
        request_id = f"request-{session_id}"
        registry.begin_request(
            session_id=session_id,
            request_id=request_id,
            rpc_method="tools/call",
            tool="server_info",
            persistent_scope=True,
            expected_deadline_at=timestamp + 100.0,
            started_at=timestamp,
        )
        registry.end_request(
            session_id=session_id,
            request_id=request_id,
            finished_at=timestamp,
        )

    snapshot = registry.snapshot(
        idle_ttl_seconds=86400,
        request_stall_seconds=180,
        orchestration_quiet_seconds=180,
        active_window_seconds=600,
        now=700.0,
    )

    assert snapshot["session_count"] == 2
    assert snapshot["active_session_count"] == 1
    rows = {row["id"]: row for row in snapshot["sessions"]}
    assert sum(bool(row["is_active"]) for row in rows.values()) == 1


def test_ha_monitor_device_discovery_and_state_payload(monkeypatch) -> None:
    monitor = _load_monitor_module()
    monkeypatch.setenv("MQTT_HOST", "broker")
    monkeypatch.setenv("CHATGPT_MCP_HEALTH_TOKEN", "health-secret")
    config = monitor.Config.from_env()

    discovery = monitor.build_discovery_payload(config)
    assert monitor.discovery_topic(config) == "homeassistant/device/gip_core_ops_mcp/config"
    assert discovery["dev"]["ids"] == ["gip_core_ops_mcp"]
    assert discovery["o"]["name"] == "chatgpt-web-oauth-mcp-ha-monitor"
    assert discovery["availability_topic"] == "gip-core/ops-mcp/monitor/status"
    assert discovery["state_topic"] == "gip-core/ops-mcp/state"
    assert discovery["cmps"]["activity_event"]["p"] == "event"
    assert "session_stalled" in discovery["cmps"]["activity_event"]["event_types"]
    assert discovery["cmps"]["problem"]["p"] == "binary_sensor"
    assert discovery["cmps"]["state"]["entity_category"] == "diagnostic"
    assert "sessions_active" not in discovery["cmps"]
    active_sessions = discovery["cmps"]["sessions"]
    assert active_sessions["name"] == "Active sessions"
    assert active_sessions["state_class"] == "measurement"
    assert active_sessions["unit_of_measurement"] == "sessions"
    assert active_sessions["suggested_display_precision"] == 0
    assert discovery["cmps"]["sessions_retained"]["entity_category"] == "diagnostic"
    assert discovery["cmps"]["claude_5h"]["unit_of_measurement"] == "%"
    assert discovery["cmps"]["codex_5h"]["entity_category"] == "diagnostic"
    assert discovery["cmps"]["codex_weekly"]["unit_of_measurement"] == "%"
    assert discovery["cmps"]["claude_sonnet_weekly"]["entity_category"] == "diagnostic"
    assert discovery["cmps"]["quota_priming"]["p"] == "sensor"
    removal = monitor.build_discovery_component_removal_payload(
        config,
        component_id="sessions_active",
        platform="sensor",
    )
    assert removal["cmps"] == {"sessions_active": {"p": "sensor"}}
    assert removal["dev"]["ids"] == ["gip_core_ops_mcp"]

    state = monitor.state_payload(
        {
            "state": "degraded",
            "timestamp": "2026-09-24T12:00:00+03:00",
            "pid": 123,
            "uptime_seconds": 42,
            "usage_limits": {
                "status": "ok",
                "providers": {
                    "antigravity": {
                        "status": "ok",
                        "windows": [
                            {"group": "Gemini Models", "window": "5h", "remaining_percent": 80.0},
                            {"group": "Gemini Models", "window": "weekly", "remaining_percent": 55.0},
                            {"group": "Claude and GPT models", "window": "5h", "remaining_percent": 70.0},
                            {"group": "Claude and GPT models", "window": "weekly", "remaining_percent": 45.0},
                        ],
                    },
                    "claude": {
                        "status": "ok",
                        "windows": [
                            {"id": "five_hour", "remaining_percent": 60.0},
                            {"id": "seven_day", "remaining_percent": 50.0},
                            {"id": "seven_day_sonnet", "remaining_percent": 40.0},
                        ],
                    },
                    "codex": {
                        "status": "ok",
                        "windows": [
                            {"duration_minutes": 300, "remaining_percent": 50.0},
                            {"duration_minutes": 10080, "remaining_percent": 35.0},
                        ],
                    },
                },
            },
            "quota_window_manager": {"status": "ok", "buckets": {}},
            "summary": {
                "sessions": 1,
                "transport_sessions": 4,
                "ephemeral_idle_sessions": 2,
                "sessions_active": 1,
                "sessions_retained": 3,
                "sessions_inflight": 1,
                "sessions_orchestration_quiet": 1,
                "sessions_stalled": 0,
                "delegates_active": 1,
                "delegates_stalled": 0,
                "jobs_running": 0,
                "jobs_stalled": 0,
            },
        }
    )
    assert state["state"] == "degraded"
    assert state["summary"]["sessions"] == 1
    assert state["summary"]["transport_sessions"] == 4
    assert state["summary"]["ephemeral_idle_sessions"] == 2
    assert state["summary"]["sessions_retained"] == 3
    assert state["summary"]["sessions_orchestration_quiet"] == 1
    assert state["limits"] == {
        "antigravity_gemini_5h": 80.0,
        "antigravity_claude_gpt_5h": 70.0,
        "claude_5h": 60.0,
        "codex_5h": 50.0,
        "antigravity_gemini_weekly": 55.0,
        "antigravity_claude_gpt_weekly": 45.0,
        "claude_weekly": 50.0,
        "claude_sonnet_weekly": 40.0,
        "codex_weekly": 35.0,
    }
    assert state["quota_priming_status"] == "ok"
    assert state["data_stale"] is False


def test_ha_monitor_detail_omits_full_cwd_and_offline_keeps_last_summary() -> None:
    monitor = _load_monitor_module()
    health = {
        "state": "active",
        "summary": {
            "sessions": 1,
            "sessions_active": 1,
            "sessions_retained": 2,
        },
        "sessions": [
            {
                "id": "abc123",
                "state": "active",
                "project": "rag-project",
                "cwd": "/home/user/secret/path/rag-project",
                "current_tool": "delegate_status",
                "last_seen_seconds_ago": 1,
                "is_active": True,
            }
        ],
        "delegates": [],
        "jobs": [],
        "usage_limits": {"status": "ok", "providers": {"codex": {"status": "ok"}}},
        "quota_window_manager": {"status": "active", "buckets": {"codex_5h": {"status": "active"}}},
    }
    detail = monitor.detail_payload(health, session_limit=12)
    assert detail["sessions"][0]["project"] == "rag-project"
    assert "cwd" not in detail["sessions"][0]
    assert detail["usage_limits"]["status"] == "ok"
    assert detail["quota_window_manager"]["status"] == "active"

    offline = monitor.offline_payload(last_state=health, error="connection refused")
    assert offline["state"] == "offline"
    assert offline["summary"]["sessions"] == 1
    assert offline["data_stale"] is True
    assert offline["error"] == "connection refused"


def test_ops_health_snapshot_keeps_quiet_active_until_stalled(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import session
    from chatgpt_web_oauth_mcp.health import OpsHealthSnapshot

    class FakeDelegateRegistry:
        def __init__(self, activity_state: str) -> None:
            self.activity_state = activity_state

        def delegate_status(self, **_kwargs):
            return {
                "success": True,
                "active_delegates": [
                    {
                        "delegate_id": "d1",
                        "harness": "antigravity",
                        "kind": "explore",
                        "cwd": str(tmp_path / "project"),
                        "status": "running",
                        "activity_state": self.activity_state,
                        "pid": 10,
                        "elapsed_seconds": 30,
                        "last_output_seconds_ago": 20,
                    }
                ],
            }

    class FakeJobRegistry:
        def list_active_jobs(self, **_kwargs):
            return {
                "success": True,
                "jobs": [],
                "total": 0,
                "truncated": False,
            }

    class FakeActivityTracker:
        def policy(self):
            return {"stall_after_seconds": 120}

    session.registry.reset()
    try:
        quiet = OpsHealthSnapshot(
            registry=FakeDelegateRegistry("starting_or_quiet"),
            job_registry=FakeJobRegistry(),
            activity_tracker=FakeActivityTracker(),
            state_dir=tmp_path,
            tool_output_token_budget=8500,
            session_idle_ttl_seconds=3600,
            session_active_window_seconds=600,
            session_request_stall_seconds=180,
            session_orchestration_quiet_seconds=180,
            session_limit=20,
        ).snapshot()
        assert quiet["state"] == "active"
        assert quiet["summary"]["delegates_quiet"] == 1

        stalled = OpsHealthSnapshot(
            registry=FakeDelegateRegistry("suspected_stalled"),
            job_registry=FakeJobRegistry(),
            activity_tracker=FakeActivityTracker(),
            state_dir=tmp_path,
            tool_output_token_budget=8500,
            session_idle_ttl_seconds=3600,
            session_active_window_seconds=600,
            session_request_stall_seconds=180,
            session_orchestration_quiet_seconds=180,
            session_limit=20,
        ).snapshot()
        assert stalled["state"] == "stalled"
        assert stalled["summary"]["delegates_stalled"] == 1
    finally:
        session.registry.reset()


def test_ops_health_snapshot_reports_active_session_without_false_degraded(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import session
    from chatgpt_web_oauth_mcp.health import OpsHealthSnapshot

    class FakeDelegateRegistry:
        def delegate_status(self, **_kwargs):
            return {"success": True, "active_delegates": []}

    class FakeJobRegistry:
        def list_active_jobs(self, **_kwargs):
            return {
                "success": True,
                "jobs": [],
                "total": 0,
                "truncated": False,
            }

    class FakeActivityTracker:
        def policy(self):
            return {"stall_after_seconds": 120}

    session.registry.reset()
    try:
        session.registry.begin_request(
            session_id="session-active",
            request_id="request-active",
            rpc_method="tools/call",
            tool="run_command",
            expected_deadline_at=10_000_000_000.0,
        )
        snapshot = OpsHealthSnapshot(
            registry=FakeDelegateRegistry(),
            job_registry=FakeJobRegistry(),
            activity_tracker=FakeActivityTracker(),
            state_dir=tmp_path,
            tool_output_token_budget=8500,
            session_idle_ttl_seconds=3600,
            session_active_window_seconds=600,
            session_request_stall_seconds=180,
            session_orchestration_quiet_seconds=180,
            session_limit=20,
        ).snapshot()
        assert snapshot["state"] == "active"
        assert snapshot["summary"]["sessions_active"] == 1
        assert snapshot["summary"]["sessions_stalled"] == 0
    finally:
        session.registry.reset()



def test_orchestration_quiet_is_diagnostic_not_overall_degraded(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import session
    from chatgpt_web_oauth_mcp.health import OpsHealthSnapshot

    class FakeDelegateRegistry:
        def delegate_status(self, **_kwargs):
            return {"success": True, "active_delegates": []}

    class FakeJobRegistry:
        def list_active_jobs(self, **_kwargs):
            return {
                "success": True,
                "jobs": [],
                "total": 0,
                "truncated": False,
            }

    class FakeActivityTracker:
        def policy(self):
            return {"stall_after_seconds": 120}

    session.registry.reset()
    try:
        session.registry.note_execution_state(
            session_id="quiet-session",
            required_action="INVOKE_NEXT_TOOL_OR_RETURN_CHECKPOINT",
            state="NEXT_ACTION_REQUIRED",
            now=100.0,
        )
        original = session.registry.snapshot

        def fixed_snapshot(**kwargs):
            return original(now=400.0, **kwargs)

        session.registry.snapshot = fixed_snapshot  # type: ignore[method-assign]
        try:
            snapshot = OpsHealthSnapshot(
                registry=FakeDelegateRegistry(),
                job_registry=FakeJobRegistry(),
                activity_tracker=FakeActivityTracker(),
                state_dir=tmp_path,
                tool_output_token_budget=8500,
                session_idle_ttl_seconds=3600,
                session_active_window_seconds=600,
                session_request_stall_seconds=180,
                session_orchestration_quiet_seconds=180,
                session_limit=20,
            ).snapshot()
        finally:
            session.registry.snapshot = original  # type: ignore[method-assign]

        assert snapshot["summary"]["sessions_orchestration_quiet"] == 1
        assert snapshot["state"] == "active"
    finally:
        session.registry.reset()
