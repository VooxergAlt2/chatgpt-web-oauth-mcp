#!/usr/bin/env python3
"""Standalone Windows native smoke script for candidate durable Windows gateway."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
import uuid

FORBIDDEN_PORTS = {8000, 8840, 8841}


class McpOperationError(RuntimeError):
    pass


def get_windows_mcp_auth_key() -> str:
    """Read WINDOWS_MCP_AUTH_KEY from Astra-Revit-Bridge/WindowsMCP/secure/secrets.json."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA environment variable not set")
    secret_path = Path(local_app_data) / "Astra-Revit-Bridge/WindowsMCP/secure/secrets.json"
    data = json.loads(secret_path.read_text(encoding="utf-8-sig"))
    return str(data["WINDOWS_MCP_AUTH_KEY"])


def validate_port_available(port: int) -> None:
    """Refuse reserved ports and any existing port listener."""
    if port in FORBIDDEN_PORTS:
        raise ValueError(f"Port {port} is reserved and forbidden")
    for mode in ("connect", "bind"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if mode == "connect":
                try:
                    s.connect(("127.0.0.1", port))
                    raise RuntimeError(f"Port {port} already has an active listener")
                except OSError:
                    pass
            else:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                s.bind(("127.0.0.1", port))


def write_receipt(receipt_path: Path, success: bool, stage: str, passed_checks: list[str], error_type: str | None = None) -> None:
    """Atomically write a compact receipt without leaking auth or raw commands."""
    receipt_data = {
        "success": success,
        "stage": stage,
        "passed_checks": passed_checks,
        "native_windows": sys.platform == "win32",
        "error": error_type,
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = receipt_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(receipt_data, indent=2), encoding="utf-8")
    tmp_path.replace(receipt_path)


async def _async_call_mcp(url: str, tool_name: str, arguments: dict) -> Any:
    from fastmcp import Client
    async with Client(url, timeout=5, init_timeout=3) as client:
        result = await client.call_tool(tool_name, arguments, timeout=5, raise_on_error=False)
        if result.is_error:
            raise McpOperationError(f"MCP tool error: {tool_name}")
        return result.structured_content or {}


def call_mcp(url: str, tool_name: str, arguments: dict) -> dict:
    return asyncio.run(_async_call_mcp(url, tool_name, arguments))


def wait_for_port(host: str, port: int, timeout: float = 15.0, proc: subprocess.Popen | None = None) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc and proc.poll() is not None:
            raise RuntimeError(f"Child process exited prematurely with code {proc.returncode}")
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"Server at {host}:{port} did not become ready within {timeout}s")


def stop_process(proc: subprocess.Popen, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)


def run_serve(root: Path, port: int) -> None:
    """Run child gateway server."""
    (root / "journal").mkdir(parents=True, exist_ok=True)
    for p in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent):
        sys.path.insert(0, str(p))
    import windows_revit_gateway
    from fastmcp import FastMCP

    config = windows_revit_gateway.GatewayConfig(
        operation_state_dir=root / "journal",
        windows_url="http://127.0.0.1:8000/mcp",
        windows_token=get_windows_mcp_auth_key(),
        host="127.0.0.1",
        port=port,
        node_executable="node",
        revit_server_js=Path("unused"),
    )
    revit_target = FastMCP("no-native")
    gateway = windows_revit_gateway.build_gateway(
        config,
        windows_target=None,
        revit_target=revit_target,
        validate_files=False,
    )
    gateway.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=port,
        stateless_http=True,
        show_banner=False,
        allowed_hosts=["127.0.0.1"],
    )


def start_child_server(root: Path, port: int, run_idx: int) -> tuple[subprocess.Popen, Any]:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    log_fp = open(root / f"child_server_{run_idx}.log", "wb")
    cmd = [sys.executable, str(Path(__file__).resolve()), "--serve", "--root", str(root), "--port", str(port)]
    proc = subprocess.Popen(cmd, stdout=log_fp, stderr=subprocess.STDOUT, creationflags=flags)
    return proc, log_fp


def main() -> int:
    parser = argparse.ArgumentParser(description="QA Windows Operation Lifecycle Smoke")
    parser.add_argument("--root", type=Path, required=True, help="QA working directory")
    parser.add_argument("--receipt", type=Path, default=Path("qa_receipt.json"), help="Receipt JSON path")
    parser.add_argument("--port", type=int, default=8842, help="Gateway port (default: 8842)")
    parser.add_argument("--serve", action="store_true", help="Internal child server mode")
    args = parser.parse_args()

    root = args.root.resolve()
    port = args.port
    if port in FORBIDDEN_PORTS or not 1 <= port <= 65535:
        raise ValueError("Reserved or invalid test port")
    if args.serve:
        run_serve(root, port)
        return 0

    receipt_path = args.receipt.resolve()
    passed_checks: list[str] = []
    stage = "init"
    child_proc: subprocess.Popen | None = None
    log_fp = None

    try:
        stage = "validate_environment"
        if root.exists():
            raise RuntimeError(f"Root directory {root} already exists; exclusive QA directory required")
        root.mkdir(parents=True, exist_ok=False)
        validate_port_available(port)
        _ = get_windows_mcp_auth_key()
        passed_checks.append("validate_environment")

        stage = "start_initial_server"
        child_proc, log_fp = start_child_server(root, port, run_idx=1)
        wait_for_port("127.0.0.1", port, timeout=15.0, proc=child_proc)
        passed_checks.append("start_initial_server")

        mcp_url = f"http://127.0.0.1:{port}/mcp"
        marker_file = (root / "qa_marker.txt").resolve()
        op_id_1 = f"qa-op-{uuid.uuid4().hex}"

        stage = "submit_operation"
        ps_path_escaped = str(marker_file).replace("'", "''")
        cmd_1 = f"Add-Content -LiteralPath '{ps_path_escaped}' -Value 'qa_marker'; Start-Sleep -Seconds 2; Write-Output 'Command execution timed out'; exit 0"
        payload_1 = {"command": cmd_1, "timeout": 10}
        call_mcp(mcp_url, "windows_operation_submit", {"operation_id": op_id_1, "name": "windows_PowerShell", "arguments": payload_1})
        passed_checks.append("submit_operation")

        stage = "poll_completion"
        poll_deadline = time.time() + 25.0
        completed = False
        while time.time() < poll_deadline:
            st = call_mcp(mcp_url, "windows_operation_status", {"operation_id": op_id_1})
            if st.get("state") == "completed":
                completed = True
                break
            time.sleep(0.5)
        if not completed:
            raise TimeoutError("Initial operation did not reach completed state within 25 seconds")
        assert marker_file.is_file(), "Marker file was not created by operation"
        lines = marker_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, f"Expected 1 marker line, found {len(lines)}"
        passed_checks.append("poll_completion")

        stage = "replay_submit_idempotent"
        for _ in (1, 2):
            call_mcp(mcp_url, "windows_operation_submit", {"operation_id": op_id_1, "name": "windows_PowerShell", "arguments": payload_1})
        lines = marker_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, f"Marker lines incremented on identical replay: {len(lines)}"
        passed_checks.append("replay_submit_idempotent")

        stage = "restart_server_persistence"
        stop_process(child_proc)
        if log_fp:
            log_fp.close()
        child_proc, log_fp = start_child_server(root, port, run_idx=2)
        wait_for_port("127.0.0.1", port, timeout=15.0, proc=child_proc)

        hist_st = call_mcp(mcp_url, "windows_operation_status", {"operation_id": op_id_1})
        assert hist_st.get("state") == "completed", f"Historical status not completed: {hist_st}"

        call_mcp(mcp_url, "windows_operation_submit", {"operation_id": op_id_1, "name": "windows_PowerShell", "arguments": payload_1})
        lines = marker_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, f"Marker lines incremented after restart replay: {len(lines)}"
        passed_checks.append("restart_server_persistence")

        stage = "reject_conflicting_payload"
        mismatched_payload = {"command": "Write-Output 'conflict'", "timeout": 10}
        error_seen = False
        try:
            call_mcp(mcp_url, "windows_operation_submit", {"operation_id": op_id_1, "name": "windows_PowerShell", "arguments": mismatched_payload})
        except McpOperationError:
            error_seen = True
        assert error_seen, "Conflicting payload with same operation_id did not produce an MCP error"
        passed_checks.append("reject_conflicting_payload")

        stage = "timeout_operation_handling"
        op_id_to = f"qa-op-timeout-{uuid.uuid4().hex}"
        timeout_payload = {"command": "Start-Sleep -Seconds 5", "timeout": 1}
        call_mcp(mcp_url, "windows_operation_submit", {"operation_id": op_id_to, "name": "windows_PowerShell", "arguments": timeout_payload})
        timeout_deadline = time.monotonic() + 20
        while True:
            to_st = call_mcp(mcp_url, "windows_operation_status", {"operation_id": op_id_to})
            if to_st.get("state") != "running" or time.monotonic() >= timeout_deadline:
                break
            time.sleep(0.25)
        assert to_st.get("state") == "unknown"
        for _ in range(3):
            to_st = call_mcp(mcp_url, "windows_operation_status", {"operation_id": op_id_to})
            st_val = to_st.get("state")
            assert st_val != "completed", f"Timed out operation marked completed: {to_st}"
            assert st_val == "unknown", f"Unexpected state for timed out operation: {st_val}"
            time.sleep(0.4)

        lines = marker_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, f"Marker file corrupted during timeout test: {len(lines)}"
        passed_checks.append("timeout_operation_handling")

        stage = "cleanup_candidate"
        stop_process(child_proc)
        child_proc = None
        log_fp.close()
        log_fp = None
        passed_checks.append("cleanup_candidate")
        write_receipt(receipt_path, success=True, stage="completed", passed_checks=passed_checks)
        return 0

    except Exception as exc:
        err_type = f"{stage}:{type(exc).__name__}"
        write_receipt(receipt_path, success=False, stage=stage, passed_checks=passed_checks, error_type=err_type)
        raise
    finally:
        if child_proc:
            stop_process(child_proc)
        if log_fp:
            log_fp.close()


if __name__ == "__main__":
    sys.exit(main())
