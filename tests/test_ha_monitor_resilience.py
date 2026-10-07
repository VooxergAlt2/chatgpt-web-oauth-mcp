from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
MONITOR_PATH = ROOT / "scripts" / "ops_mcp_ha_monitor.py"


def _load_monitor():
    spec = importlib.util.spec_from_file_location(
        "ops_mcp_ha_monitor_resilience_test",
        MONITOR_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _config(monkeypatch):
    monitor = _load_monitor()
    monkeypatch.setenv("CHATGPT_MCP_HEALTH_TOKEN", "test-token")
    monkeypatch.setenv("MQTT_HOST", "mqtt.invalid")
    monkeypatch.delenv("OPS_MCP_HA_FAILURE_THRESHOLD", raising=False)
    return monitor, monitor.Config.from_env()


def test_health_failure_threshold_defaults_to_three(monkeypatch) -> None:
    _monitor, config = _config(monkeypatch)

    assert config.health_failure_threshold == 3


def test_transient_health_failure_does_not_immediately_publish_offline(monkeypatch) -> None:
    monitor, config = _config(monkeypatch)
    instance = monitor.OpsMcpHaMonitor.__new__(monitor.OpsMcpHaMonitor)
    instance.config = config
    instance._consecutive_health_failures = 0
    offline: list[str] = []
    instance._publish_offline = offline.append

    instance._handle_health_failure("timeout-1")
    instance._handle_health_failure("timeout-2")

    assert offline == []
    assert instance._consecutive_health_failures == 2

    instance._handle_health_failure("timeout-3")

    assert offline == ["timeout-3"]
    assert instance._consecutive_health_failures == 3


def test_success_resets_failure_streak(monkeypatch) -> None:
    monitor, config = _config(monkeypatch)
    instance = monitor.OpsMcpHaMonitor.__new__(monitor.OpsMcpHaMonitor)
    instance.config = config
    instance._consecutive_health_failures = 2
    instance._previous_state = "active"
    published: list[dict[str, object]] = []
    instance._publish_event = lambda *_args, **_kwargs: None
    instance._publish_health = published.append
    health = {"state": "idle"}

    instance._handle_health_success(health)

    assert instance._consecutive_health_failures == 0
    assert published == [health]
