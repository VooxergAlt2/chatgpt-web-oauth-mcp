from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "scripts" / "install_ha_monitor.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_ha_monitor_test", INSTALLER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_installer_generates_and_reuses_health_token(tmp_path: Path) -> None:
    installer = _load_installer()
    server_env = tmp_path / "server.env"
    server_env.write_text("CHATGPT_MCP_PORT=8770\nEXISTING=value\n", encoding="utf-8")

    token, created = installer.ensure_health_token(server_env)
    assert created is True
    assert len(token) >= 32
    assert installer.parse_env(server_env)["CHATGPT_MCP_HEALTH_TOKEN"] == token
    assert installer.parse_env(server_env)["EXISTING"] == "value"
    assert server_env.stat().st_mode & 0o777 == 0o600

    second, second_created = installer.ensure_health_token(server_env)
    assert second == token
    assert second_created is False
    assert server_env.read_text(encoding="utf-8").count("CHATGPT_MCP_HEALTH_TOKEN=") == 1


def test_installer_writes_monitor_env_without_mqtt_credentials(tmp_path: Path) -> None:
    installer = _load_installer()
    monitor_env = tmp_path / "ha-monitor.env"

    installer.write_monitor_env(
        monitor_env,
        health_token="health-secret",
        server_port=8770,
    )
    values = installer.parse_env(monitor_env)
    assert values["CHATGPT_MCP_HEALTH_TOKEN"] == "health-secret"
    assert values["OPS_MCP_HEALTH_URL"] == "http://127.0.0.1:8770/internal/health"
    assert values["OPS_MCP_MQTT_BASE_TOPIC"] == "gip-core/ops-mcp"
    assert "MQTT_PASSWORD" not in values
    assert "MQTT_USERNAME" not in values
    assert monitor_env.stat().st_mode & 0o777 == 0o600


def test_installer_systemd_unit_keeps_monitor_independent_from_mcp(tmp_path: Path) -> None:
    installer = _load_installer()
    unit = installer.build_systemd_unit(
        repo_root=tmp_path / "repo",
        monitor_python=Path("/opt/gpu-monitor/.venv/bin/python3"),
        gpu_env=Path("/etc/gpu-monitor/gpu-monitor.env"),
        monitor_env=tmp_path / "ha-monitor.env",
    )

    assert "EnvironmentFile=/etc/gpu-monitor/gpu-monitor.env" in unit
    assert f"EnvironmentFile={tmp_path / 'ha-monitor.env'}" in unit
    assert "ExecStart=/opt/gpu-monitor/.venv/bin/python3" in unit
    assert "scripts/ops_mcp_ha_monitor.py" in unit
    assert "After=network-online.target chatgpt-web-oauth-mcp.service" in unit
    assert "Requires=chatgpt-web-oauth-mcp.service" not in unit
    assert "Restart=on-failure" in unit
    assert "ProtectSystem=strict" in unit
