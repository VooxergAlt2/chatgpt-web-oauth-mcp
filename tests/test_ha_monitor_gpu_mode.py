from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = ROOT / "scripts" / "ops_mcp_ha_monitor.py"


def _load_monitor():
    spec = importlib.util.spec_from_file_location("ops_mcp_ha_monitor_gpu_mode_test", MONITOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _config(monkeypatch, tmp_path: Path):
    monitor = _load_monitor()
    monkeypatch.setenv("OPS_MCP_GPU_MODE_CONTROL_ENABLED", "true")
    monkeypatch.setenv("OPS_MCP_GPU_MODE_REQUEST_PATH", str(tmp_path / "gpu-mode.request"))
    monkeypatch.setenv("OPS_MCP_GPU_MODE_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("CHATGPT_MCP_HEALTH_TOKEN", "test-token")
    monkeypatch.setenv("MQTT_HOST", "mqtt.invalid")
    return monitor, monitor.Config.from_env()


def test_gpu_mode_snapshot_uses_actual_service_state(monkeypatch, tmp_path: Path) -> None:
    monitor, config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        monitor,
        "_user_unit_active",
        lambda unit: unit == config.gpu_mode_reviewer_unit,
    )
    monkeypatch.setattr(
        monitor,
        "_gpu_health",
        lambda url, **_kwargs: (
            {
                "ok": True,
                "component": "rag-gpu-inference",
                "state": "suspended",
                "maintenance_control_enabled": True,
            }
            if url == config.gpu_mode_rag_health_url
            else {"status": "ok"}
        ),
    )

    snapshot = monitor.gpu_mode_snapshot(config)

    assert snapshot["mode"] == "reviewer"
    assert snapshot["server_mode"] == "AI"
    assert snapshot["reviewer_active"] is True
    assert snapshot["reviewer_ready"] is True
    assert snapshot["rag_state"] == "suspended"
    assert snapshot["rag_maintenance_control_enabled"] is True


def test_gpu_mode_request_is_bounded_to_rag_or_reviewer(monkeypatch, tmp_path: Path) -> None:
    monitor, config = _config(monkeypatch, tmp_path)

    monitor.write_gpu_mode_request(config, "reviewer")

    assert config.gpu_mode_request_path.read_text(encoding="utf-8") == "reviewer\n"
    assert config.gpu_mode_request_path.stat().st_mode & 0o777 == 0o600

    try:
        monitor.write_gpu_mode_request(config, "shell-command")
    except ValueError:
        pass
    else:
        raise AssertionError("arbitrary GPU-mode commands must be rejected")


def test_gpu_mode_snapshot_reports_rag_server_mode(monkeypatch, tmp_path: Path) -> None:
    monitor, config = _config(monkeypatch, tmp_path)
    monkeypatch.setattr(monitor, "_user_unit_active", lambda _unit: False)
    monkeypatch.setattr(
        monitor,
        "_gpu_health",
        lambda url, **_kwargs: (
            {
                "ok": True,
                "component": "rag-gpu-inference",
                "state": "ready",
                "maintenance_control_enabled": True,
            }
            if url == config.gpu_mode_rag_health_url
            else None
        ),
    )

    snapshot = monitor.gpu_mode_snapshot(config)

    assert snapshot["mode"] == "rag"
    assert snapshot["server_mode"] == "RAG"


def test_gpu_mode_discovery_adds_switch_and_fact_sensor(monkeypatch, tmp_path: Path) -> None:
    monitor, config = _config(monkeypatch, tmp_path)

    payload = monitor.build_discovery_payload(config)
    components = payload["cmps"]
    switch = components["local_reviewer_mode"]
    sensor = components["gpu_workload"]
    server_mode = components["server_mode"]

    assert switch["p"] == "switch"
    assert switch["command_topic"].endswith("/control/gpu_mode/set")
    assert switch["state_topic"].endswith("/control/gpu_mode/state")
    assert sensor["p"] == "sensor"
    assert sensor["state_topic"].endswith("/control/gpu_mode/mode")
    assert sensor["json_attributes_topic"].endswith("/control/gpu_mode/detail")
    assert server_mode["p"] == "sensor"
    assert server_mode["state_topic"].endswith("/server/mode")


def test_gpu_mode_publish_retains_server_mode(monkeypatch, tmp_path: Path) -> None:
    monitor, config = _config(monkeypatch, tmp_path)
    published: list[tuple[str, str, int, bool]] = []

    class FakeClient:
        def publish(self, topic, payload, qos=0, retain=False):
            published.append((topic, payload, qos, retain))

    instance = monitor.OpsMcpHaMonitor.__new__(monitor.OpsMcpHaMonitor)
    instance.config = config
    instance._client = FakeClient()
    instance._previous_gpu_mode = None
    monkeypatch.setattr(
        monitor,
        "gpu_mode_snapshot",
        lambda _config: {"mode": "reviewer", "server_mode": "AI"},
    )

    instance._publish_gpu_mode_state()

    assert (
        f"{config.base_topic}/server/mode",
        "AI",
        0,
        True,
    ) in published
