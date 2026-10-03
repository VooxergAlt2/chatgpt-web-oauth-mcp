#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

DEFAULT_USER = "vooxerg"
MODEL_PATH = Path("/mnt/rag-reference/Models/Devstral-Small-2-24B-Instruct-2512/mistralai_Devstral-Small-2-24B-Instruct-2512-Q5_K_M.gguf")
REQUEST_PATH = Path("/home/vooxerg/.local/state/chatgpt-web-oauth-mcp/gpu-mode.request")
STATE_PATH = Path("/run/dem-gpu-mode/state.json")
CONTROLLER_PATH = Path("/usr/local/libexec/dem-gpu-mode-control")


def _write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _set_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(key + "="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _prepend_csv_env_value(path: Path, key: str, item: str) -> None:
    current = ""
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                current = line.split("=", 1)[1].strip()
                break
    values = [value.strip() for value in current.split(",") if value.strip()]
    values = [value for value in values if value != item]
    _set_env(path, key, ",".join([item, *values]))


def _user_systemctl(user: str, *args: str) -> subprocess.CompletedProcess[str]:
    entry = pwd.getpwnam(user)
    return subprocess.run(
        [
            "runuser", "-u", user, "--",
            "env", f"XDG_RUNTIME_DIR=/run/user/{entry.pw_uid}",
            "systemctl", "--user", *args,
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )


def _llama_unit(user: str) -> str:
    return f"""[Unit]
Description=Devstral Small 2 24B llama.cpp reviewer
After=network-online.target
Wants=network-online.target
RequiresMountsFor=/mnt/rag-reference
Conflicts=rag-gpu-inference.service llama-qwen35-gip.service

[Service]
Type=simple
User={user}
Group={user}
WorkingDirectory=/home/{user}
ExecStart=/usr/local/bin/llama-server -m {MODEL_PATH} --host 127.0.0.1 --port 8083 --alias devstral-gip -ngl 999 -c 32768 -fa on -ctk q8_0 -ctv q8_0 --no-mmap --cache-ram 0 -np 1 --jinja
Restart=on-failure
RestartSec=5
TimeoutStartSec=360
TimeoutStopSec=45
KillSignal=SIGINT
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
"""


def _control_unit() -> str:
    return f"""[Unit]
Description=DeM GPU workload mode transition
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=-/etc/rag-safe-wrapper-storage.env
ExecStart={CONTROLLER_PATH} apply
TimeoutStartSec=420
"""


def _path_unit() -> str:
    return f"""[Unit]
Description=Watch DeM GPU workload mode requests
After=local-fs.target

[Path]
PathChanged={REQUEST_PATH}
Unit=dem-gpu-mode.service

[Install]
WantedBy=multi-user.target
"""


def install(*, repo_root: Path, user: str, restart_user_services: bool) -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this installer as root.")
    entry = pwd.getpwnam(user)
    controller_source = repo_root / "scripts/dem_gpu_mode_control.py"
    if not controller_source.is_file():
        raise SystemExit(f"Missing controller: {controller_source}")
    if not Path("/usr/local/bin/llama-server").is_file():
        raise SystemExit("Missing /usr/local/bin/llama-server")

    CONTROLLER_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(controller_source, CONTROLLER_PATH)
    CONTROLLER_PATH.chmod(0o755)

    _write(Path("/etc/systemd/system/llama-devstral-gip.service"), _llama_unit(user))
    _write(Path("/etc/systemd/system/dem-gpu-mode.service"), _control_unit())
    _write(Path("/etc/systemd/system/dem-gpu-mode.path"), _path_unit())

    REQUEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chown(REQUEST_PATH.parent, entry.pw_uid, entry.pw_gid)
    REQUEST_PATH.parent.chmod(0o700)
    if not REQUEST_PATH.exists():
        REQUEST_PATH.write_text("rag\n", encoding="utf-8")
    os.chown(REQUEST_PATH, entry.pw_uid, entry.pw_gid)
    REQUEST_PATH.chmod(0o600)

    monitor_env = Path(f"/home/{user}/.config/chatgpt-web-oauth-mcp/ha-monitor.env")
    for key, value in {
        "OPS_MCP_GPU_MODE_CONTROL_ENABLED": "true",
        "OPS_MCP_GPU_MODE_REQUEST_PATH": str(REQUEST_PATH),
        "OPS_MCP_GPU_MODE_STATE_PATH": str(STATE_PATH),
        "OPS_MCP_GPU_MODE_RAG_UNIT": "rag-gpu-inference.service",
        "OPS_MCP_GPU_MODE_DEVSTRAL_UNIT": "llama-devstral-gip.service",
    }.items():
        _set_env(monitor_env, key, value)
    os.chown(monitor_env, entry.pw_uid, entry.pw_gid)
    monitor_env.chmod(0o600)

    ops_env = Path(f"/home/{user}/.config/chatgpt-web-oauth-mcp/env")
    for key, value in {
        "CHATGPT_MCP_LOCAL2_DELEGATE_ENABLED": "true",
        "CHATGPT_MCP_LOCAL2_DELEGATE_ENDPOINT": "http://127.0.0.1:8083",
        "CHATGPT_MCP_LOCAL2_DELEGATE_MODEL": "devstral-gip",
        "CHATGPT_MCP_LOCAL2_DELEGATE_HEALTH_TIMEOUT_MS": "1000",
        "CHATGPT_MCP_LOCAL2_DELEGATE_REQUEST_TIMEOUT_SECONDS": "300",
        "CHATGPT_MCP_LOCAL2_DELEGATE_MAX_TURNS": "8",
        "CHATGPT_MCP_LOCAL2_DELEGATE_MAX_TOKENS": "1400",
        "CHATGPT_MCP_LOCAL2_DELEGATE_MAX_CONCURRENCY": "1",
        "CHATGPT_MCP_LOCAL2_DELEGATE_ENABLE_THINKING": "false",
        "CHATGPT_MCP_LOCAL2_DELEGATE_UNAVAILABLE_COOLDOWN_SECONDS": "60",
    }.items():
        _set_env(ops_env, key, value)
    _prepend_csv_env_value(
        ops_env,
        "CHATGPT_MCP_DELEGATE_FALLBACK_HARNESSES",
        "local2",
    )
    os.chown(ops_env, entry.pw_uid, entry.pw_gid)
    ops_env.chmod(0o600)

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "dem-gpu-mode.path"], check=True)
    subprocess.run([str(CONTROLLER_PATH), "apply"], check=False)

    if restart_user_services:
        for unit in (
            "chatgpt-web-oauth-mcp.service",
            "chatgpt-web-oauth-mcp-ha-monitor.service",
        ):
            result = _user_systemctl(user, "restart", unit)
            if result.returncode != 0:
                print(f"warning: could not restart {unit}: {result.stderr.strip()}", file=sys.stderr)

    print("DEVSTRAL_GPU_MODE_INSTALL_OK")
    print(f"model_present={MODEL_PATH.is_file()}")
    print(f"request_path={REQUEST_PATH}")
    print("weekend switch: MQTT ON -> Devstral, OFF -> RAG")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--no-restart-user-services", action="store_true")
    args = parser.parse_args()
    install(
        repo_root=args.repo_root.resolve(),
        user=args.user,
        restart_user_services=not args.no_restart_user_services,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
