#!/usr/bin/env python3
"""Install the external Ops MCP -> Home Assistant MQTT monitor on Linux."""

from __future__ import annotations

import argparse
from pathlib import Path
import secrets
import subprocess

DEFAULT_SERVER_ENV = Path.home() / ".config/chatgpt-web-oauth-mcp/env"
DEFAULT_MONITOR_ENV = Path.home() / ".config/chatgpt-web-oauth-mcp/ha-monitor.env"
DEFAULT_GPU_ENV = Path("/etc/gpu-monitor/gpu-monitor.env")
DEFAULT_MONITOR_PYTHON = Path("/opt/gpu-monitor/.venv/bin/python3")
DEFAULT_UNIT_PATH = (
    Path.home()
    / ".config/systemd/user/chatgpt-web-oauth-mcp-ha-monitor.service"
)


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def set_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    output: list[str] = []
    replaced = False
    for raw in lines:
        if raw.startswith(key + "="):
            output.append(f"{key}={value}")
            replaced = True
        else:
            output.append(raw)
    if not replaced:
        output.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    path.chmod(0o600)


def ensure_health_token(server_env: Path) -> tuple[str, bool]:
    values = parse_env(server_env)
    existing = values.get("CHATGPT_MCP_HEALTH_TOKEN", "").strip()
    if existing:
        return existing, False
    token = secrets.token_urlsafe(32)
    set_env_value(server_env, "CHATGPT_MCP_HEALTH_TOKEN", token)
    return token, True


def write_monitor_env(
    path: Path,
    *,
    health_token: str,
    server_port: int,
) -> None:
    lines = [
        f"CHATGPT_MCP_HEALTH_TOKEN={health_token}",
        f"OPS_MCP_HEALTH_URL=http://127.0.0.1:{server_port}/internal/health",
        "OPS_MCP_MQTT_BASE_TOPIC=gip-core/ops-mcp",
        "OPS_MCP_HA_DISCOVERY_PREFIX=homeassistant",
        "OPS_MCP_HA_DISCOVERY_OBJECT_ID=gip_core_ops_mcp",
        "OPS_MCP_HA_POLL_INTERVAL=10",
        "OPS_MCP_HA_HTTP_TIMEOUT=3",
        "OPS_MCP_HA_DETAIL_SESSION_LIMIT=12",
        "LOG_LEVEL=INFO",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def build_systemd_unit(
    *,
    repo_root: Path,
    monitor_python: Path,
    gpu_env: Path,
    monitor_env: Path,
) -> str:
    monitor_script = repo_root / "scripts/ops_mcp_ha_monitor.py"
    return f"""[Unit]
Description=GIP Core Ops MCP Home Assistant MQTT Monitor
After=network-online.target chatgpt-web-oauth-mcp.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={repo_root}
EnvironmentFile={gpu_env}
EnvironmentFile={monitor_env}
ExecStart={monitor_python} {monitor_script}
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=15
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=default.target
"""


def validate_runtime(
    *,
    server_env: Path,
    gpu_env: Path,
    monitor_python: Path,
    repo_root: Path,
) -> None:
    if not server_env.exists():
        raise RuntimeError(f"Missing Ops MCP environment file: {server_env}")
    if not gpu_env.is_file():
        raise RuntimeError(f"Missing GPU MQTT environment file: {gpu_env}")
    if not monitor_python.is_file():
        raise RuntimeError(f"Missing monitor Python runtime: {monitor_python}")
    monitor_script = repo_root / "scripts/ops_mcp_ha_monitor.py"
    if not monitor_script.is_file():
        raise RuntimeError(f"Missing monitor script: {monitor_script}")
    subprocess.run(
        [
            str(monitor_python),
            "-c",
            "import paho.mqtt.client; print('PAHO_RUNTIME_OK')",
        ],
        check=True,
    )


def install(
    *,
    repo_root: Path,
    server_env: Path,
    monitor_env: Path,
    gpu_env: Path,
    monitor_python: Path,
    unit_path: Path,
    start: bool,
    restart_server: bool,
) -> dict[str, object]:
    validate_runtime(
        server_env=server_env,
        gpu_env=gpu_env,
        monitor_python=monitor_python,
        repo_root=repo_root,
    )
    health_token, created = ensure_health_token(server_env)
    server_values = parse_env(server_env)
    server_port = int(server_values.get("CHATGPT_MCP_PORT", "8766") or "8766")
    write_monitor_env(
        monitor_env,
        health_token=health_token,
        server_port=server_port,
    )

    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(
        build_systemd_unit(
            repo_root=repo_root,
            monitor_python=monitor_python,
            gpu_env=gpu_env,
            monitor_env=monitor_env,
        ),
        encoding="utf-8",
    )
    unit_path.chmod(0o600)

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    if restart_server:
        subprocess.run(
            ["systemctl", "--user", "restart", "chatgpt-web-oauth-mcp.service"],
            check=True,
        )
    if start:
        subprocess.run(
            [
                "systemctl",
                "--user",
                "enable",
                "--now",
                "chatgpt-web-oauth-mcp-ha-monitor.service",
            ],
            check=True,
        )

    return {
        "health_token_created": created,
        "monitor_env": str(monitor_env),
        "unit_path": str(unit_path),
        "started": start,
        "server_restarted": restart_server,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--server-env", type=Path, default=DEFAULT_SERVER_ENV)
    parser.add_argument("--monitor-env", type=Path, default=DEFAULT_MONITOR_ENV)
    parser.add_argument("--gpu-env", type=Path, default=DEFAULT_GPU_ENV)
    parser.add_argument("--monitor-python", type=Path, default=DEFAULT_MONITOR_PYTHON)
    parser.add_argument("--unit-path", type=Path, default=DEFAULT_UNIT_PATH)
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--no-server-restart", action="store_true")
    args = parser.parse_args()

    result = install(
        repo_root=args.repo_root.resolve(),
        server_env=args.server_env.expanduser(),
        monitor_env=args.monitor_env.expanduser(),
        gpu_env=args.gpu_env,
        monitor_python=args.monitor_python,
        unit_path=args.unit_path.expanduser(),
        start=not args.no_start,
        restart_server=not args.no_server_restart,
    )
    for key, value in result.items():
        print(f"{key}={value}")
    print("HA_MONITOR_INSTALL_OK")


if __name__ == "__main__":
    main()
