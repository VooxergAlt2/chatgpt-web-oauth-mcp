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
MODEL_PATH = Path("/mnt/rag-reference/Models/Qwen3.8-27B/Qwen3.8-27B-Q4_K_M.gguf")
REVIEWER_TOTAL_CONTEXT = 137_216
REVIEWER_PARALLEL_SLOTS = 2
LLAMA_CPP_BUILD = "050439614"


def _home(user: str) -> Path:
    return Path(pwd.getpwnam(user).pw_dir)


def _request_path(user: str) -> Path:
    return _home(user) / ".local/state/chatgpt-web-oauth-mcp/gpu-mode.request"


def _state_path(user: str) -> Path:
    return _home(user) / ".local/state/chatgpt-web-oauth-mcp/gpu-mode.state.json"


def _controller_path(user: str) -> Path:
    return _home(user) / ".local/libexec/dem-gpu-mode-control"


def _llama_server_path(user: str) -> Path:
    return _home(user) / f".local/opt/llama.cpp/{LLAMA_CPP_BUILD}/bin/llama-server"


def _unit_dir(user: str) -> Path:
    return _home(user) / ".config/systemd/user"


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


def _remove_env_keys(path: Path, keys: set[str]) -> None:
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [
        line
        for line in lines
        if not any(line.startswith(key + "=") for key in keys)
    ]
    path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def _migrate_request(path: Path) -> None:
    if not path.exists():
        path.write_text("rag\n", encoding="utf-8")
        return
    try:
        current = path.read_text(encoding="utf-8").strip().lower()
    except OSError as exc:
        raise RuntimeError(f"could not read GPU mode request: {exc}") from exc
    if current == "devstral":
        path.write_text("reviewer\n", encoding="utf-8")


def _apply_gpu_mode(controller_path: Path) -> None:
    result = subprocess.run(
        [str(controller_path), "apply"],
        text=True,
        capture_output=True,
        check=False,
        timeout=420,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:500]
        raise RuntimeError(
            f"GPU mode apply failed ({result.returncode}): {detail or 'no output'}"
        )


def _user_systemctl(user: str, *args: str) -> subprocess.CompletedProcess[str]:
    if pwd.getpwuid(os.geteuid()).pw_name != user:
        raise RuntimeError("installer_must_run_as_target_user")
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=45,
    )


def _llama_unit(user: str) -> str:
    return f"""[Unit]
Description=Qwen3.8 27B llama.cpp local reviewer
After=network-online.target
Wants=network-online.target
ConditionPathIsMountPoint=/mnt/rag-reference
ConditionPathExists={MODEL_PATH}
ConditionPathExists={_llama_server_path(user)}

[Service]
Type=simple
WorkingDirectory={_home(user)}
ExecStartPre=/usr/bin/test -r {MODEL_PATH}
ExecStart={_llama_server_path(user)} -m {MODEL_PATH} --host 127.0.0.1 --port 8083 --alias qwen38-27b -ngl 999 -c {REVIEWER_TOTAL_CONTEXT} -fa on -ctk q8_0 -ctv q4_0 --cache-ram 0 -np {REVIEWER_PARALLEL_SLOTS} -ub 128 --jinja -fit off
Restart=on-failure
RestartSec=5
TimeoutStartSec=360
TimeoutStopSec=45
KillSignal=SIGINT
LimitNOFILE=65536

[Install]
WantedBy=default.target
"""


def _control_unit(user: str) -> str:
    return f"""[Unit]
Description=DeM GPU workload mode transition
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart={_controller_path(user)} apply
TimeoutStartSec=420
"""


def _path_unit(user: str) -> str:
    return f"""[Unit]
Description=Watch DeM GPU workload mode requests
After=local-fs.target

[Path]
PathChanged={_request_path(user)}
Unit=dem-gpu-mode.service

[Install]
WantedBy=default.target
"""


def install(*, repo_root: Path, user: str, restart_user_services: bool) -> None:
    current_user = pwd.getpwuid(os.geteuid()).pw_name
    if current_user != user:
        raise SystemExit(
            f"Run this installer as target user {user!r}; current user is {current_user!r}."
        )
    controller_source = repo_root / "scripts/dem_gpu_mode_control.py"
    if not controller_source.is_file():
        raise SystemExit(f"Missing controller: {controller_source}")
    llama_server_path = _llama_server_path(user)
    if not llama_server_path.is_file():
        raise SystemExit(f"Missing {llama_server_path}")

    home = _home(user)
    request_path = _request_path(user)
    state_path = _state_path(user)
    controller_path = _controller_path(user)
    unit_dir = _unit_dir(user)

    controller_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(controller_source, controller_path)
    controller_path.chmod(0o755)

    legacy_unit = unit_dir / "llama-devstral-gip.service"
    _user_systemctl(user, "disable", "--now", legacy_unit.name)
    legacy_unit.unlink(missing_ok=True)
    _write(unit_dir / "llama-local-reviewer.service", _llama_unit(user))
    _write(unit_dir / "dem-gpu-mode.service", _control_unit(user))
    _write(unit_dir / "dem-gpu-mode.path", _path_unit(user))

    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.parent.chmod(0o700)
    _migrate_request(request_path)
    request_path.chmod(0o600)

    monitor_env = home / ".config/chatgpt-web-oauth-mcp/ha-monitor.env"
    _remove_env_keys(
        monitor_env,
        {
            "OPS_MCP_GPU_MODE_DEVSTRAL_HEALTH_URL",
            "OPS_MCP_GPU_MODE_DEVSTRAL_UNIT",
        },
    )
    for key, value in {
        "OPS_MCP_GPU_MODE_CONTROL_ENABLED": "true",
        "OPS_MCP_GPU_MODE_REQUEST_PATH": str(request_path),
        "OPS_MCP_GPU_MODE_STATE_PATH": str(state_path),
        "OPS_MCP_GPU_MODE_RAG_HEALTH_URL": "http://127.0.0.1:8091/health",
        "OPS_MCP_GPU_MODE_REVIEWER_HEALTH_URL": "http://127.0.0.1:8083/health",
        "OPS_MCP_GPU_MODE_REVIEWER_UNIT": "llama-local-reviewer.service",
    }.items():
        _set_env(monitor_env, key, value)
    monitor_env.chmod(0o600)

    ops_env = home / ".config/chatgpt-web-oauth-mcp/env"
    for key, value in {
        "CHATGPT_MCP_LOCAL2_DELEGATE_ENABLED": "true",
        "CHATGPT_MCP_LOCAL2_DELEGATE_ENDPOINT": "http://127.0.0.1:8083",
        "CHATGPT_MCP_LOCAL2_DELEGATE_MODEL": "qwen38-27b",
        "CHATGPT_MCP_LOCAL2_DELEGATE_HEALTH_TIMEOUT_MS": "1000",
        "CHATGPT_MCP_LOCAL2_DELEGATE_REQUEST_TIMEOUT_SECONDS": "300",
        "CHATGPT_MCP_LOCAL2_DELEGATE_MAX_TURNS": "8",
        "CHATGPT_MCP_LOCAL2_DELEGATE_MAX_TOKENS": "1400",
        "CHATGPT_MCP_LOCAL2_DELEGATE_MAX_CONCURRENCY": str(REVIEWER_PARALLEL_SLOTS),
        "CHATGPT_MCP_LOCAL2_DELEGATE_ENABLE_THINKING": "false",
        "CHATGPT_MCP_LOCAL2_DELEGATE_UNAVAILABLE_COOLDOWN_SECONDS": "60",
    }.items():
        _set_env(ops_env, key, value)
    _prepend_csv_env_value(
        ops_env,
        "CHATGPT_MCP_DELEGATE_FALLBACK_HARNESSES",
        "local2",
    )
    ops_env.chmod(0o600)

    result = _user_systemctl(user, "daemon-reload")
    if result.returncode != 0:
        raise RuntimeError(f"user daemon-reload failed: {result.stderr.strip()}")
    result = _user_systemctl(user, "enable", "--now", "dem-gpu-mode.path")
    if result.returncode != 0:
        raise RuntimeError(f"could not enable dem-gpu-mode.path: {result.stderr.strip()}")
    _apply_gpu_mode(controller_path)

    if restart_user_services:
        for unit in (
            "chatgpt-web-oauth-mcp.service",
            "chatgpt-web-oauth-mcp-ha-monitor.service",
        ):
            result = _user_systemctl(user, "restart", unit)
            if result.returncode != 0:
                print(f"warning: could not restart {unit}: {result.stderr.strip()}", file=sys.stderr)

    print("LOCAL_REVIEWER_GPU_MODE_INSTALL_OK")
    print(f"model_present={MODEL_PATH.is_file()}")
    print(f"llama_server={llama_server_path}")
    print(f"request_path={request_path}")
    print("reviewer switch: MQTT ON -> local reviewer, OFF -> RAG")


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
