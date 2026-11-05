"""Isolated real HTTP acceptance for exclusive three-tool Opss gateway.

Starts an ephemeral *separate* Python/ASGI process on 127.0.0.1, with
private temporary state and token. Never restarts production Opss.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from chatgpt_web_oauth_mcp.revit_workspaces import workspace_tools


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_exclusive_http_mcp_real_transport_auth_and_local_ops(tmp_path: Path) -> None:
    port = _free_local_port()
    token = secrets.token_urlsafe(36)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    env = os.environ.copy()
    # This test explicitly verifies the offline-Windows contract. The real
    # developer host may now have the Windows gateway configured; do not
    # inherit that unrelated production connectivity into an isolated pilot.
    for name in tuple(env):
        if name.startswith("OPSS_WINVM_GATEWAY_"):
            env.pop(name)
    env.update({
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "CHATGPT_MCP_HOST": "127.0.0.1",
        "CHATGPT_MCP_PORT": str(port),
        "CHATGPT_MCP_AUTH_MODE": "shared_token",
        "CHATGPT_MCP_AUTH_TOKEN": token,
        "CHATGPT_MCP_TOOL_GROUP_MODE": "exclusive",
        "CHATGPT_MCP_TOOL_PROFILE": "lean",
        "CHATGPT_MCP_STATE_DIR": str(state),
        "CHATGPT_MCP_DELEGATE_STATE_DIR": str(state / "delegates"),
        "CHATGPT_MCP_WORKSPACE_ROOT": str(tmp_path),
        "CHATGPT_MCP_HEALTH_USAGE_LIMITS_ENABLED": "false",
        "CHATGPT_MCP_QUOTA_PRIMING_ENABLED": "false",
        "CHATGPT_MCP_CODE_GRAPH_ENABLED": "false",
    })
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "chatgpt_web_oauth_mcp.server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=3)
                raise AssertionError(
                    f"Pilot MCP exited {proc.returncode}: "
                    f"{(out + err).decode('utf-8', errors='replace')[-1600:]}"
                )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
                check.settimeout(0.2)
                if check.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
            raise AssertionError("Pilot MCP did not start within 18 seconds")

        url = f"http://127.0.0.1:{port}/mcp"
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18",
                       "capabilities": {},
                       "clientInfo": {"name": "pilot", "version": "1"}},
        }
        with httpx.Client(timeout=5.0) as raw:
            unauthorized = raw.post(url, json=request, headers=headers)
            assert unauthorized.status_code == 401
            unauthorized_bad = raw.post(
                url, json=request, headers=headers | {"Authorization": "Bearer wrong"}
            )
            assert unauthorized_bad.status_code == 401

        async def check_gateway() -> None:
            transport = StreamableHttpTransport(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-openai-session": "opss-three-groups-http-pilot",
                },
            )
            async with Client(transport, timeout=15, init_timeout=10) as client:
                visible = await client.list_tools()
                assert {tool.name for tool in visible} == {
                    "tool_group_select", "tool_group_catalog", "tool_group_call"
                }
                select = await client.call_tool("tool_group_select", {"group": "opss"})
                assert select.structured_content["selected"] == "opss"
                assert select.structured_content["total"] == 60
                catalog = await client.call_tool(
                    "tool_group_catalog",
                    {"group": "opss", "search": "run_command", "limit": 1,
                     "include_schema": True},
                )
                tool = catalog.structured_content["tools"][0]
                assert tool["name"] == "run_command"
                assert "command" in tool["input_schema"]["properties"]

                response = await client.call_tool(
                    "tool_group_call",
                    {"group": "opss", "name": "run_command",
                     "arguments": {"command": "printf HTTP_OPSS_PILOT_PASS", "timeout": 5}},
                    timeout=15,
                )
                assert response.structured_content["success"] is True
                assert response.structured_content["stdout"] == "HTTP_OPSS_PILOT_PASS"

                # Offline Windows must not block the Opss branch.
                missing = await client.call_tool(
                    "tool_group_select", {"group": "windows"}, raise_on_error=False,
                )
                assert missing.is_error
                # Offline native catalog is still discoverable, without adding
                # public tools or dialing Windows during startup/catalog calls.
                workspaces = await client.call_tool(
                    "tool_group_catalog",
                    {"group": "revit", "search": "workspace", "limit": 20},
                )
                assert workspaces.structured_content["total"] == len(workspace_tools())
                assert any(tool["name"] == "revit_workspace_bind_prepared"
                           for tool in workspaces.structured_content["tools"])
                unavailable = await client.call_tool(
                    "tool_group_call",
                    {"group": "revit", "name": "revit_workspace_instances"},
                    raise_on_error=False,
                )
                assert unavailable.is_error
                back = await client.call_tool(
                    "tool_group_call",
                    {"group": "opss", "name": "get_default_cwd", "arguments": {}},
                )
                assert back.structured_content["effective_cwd"] == str(tmp_path)

                # Preserve ownership of durable jobs across HTTP tool calls.
                started = await client.call_tool(
                    "tool_group_call",
                    {"group": "opss", "name": "job_start",
                     "arguments": {
                         "command": "printf HTTP_DURABLE_PASS",
                         "cwd": str(tmp_path), "name": "http-pilot",
                     }},
                )
                job_id = started.structured_content["job_id"]
                done = await client.call_tool(
                    "tool_group_call",
                    {"group": "opss", "name": "await_job",
                     "arguments": {"job_id": job_id, "wait_seconds": 10}},
                )
                assert done.structured_content["success"] is True
                output = await client.call_tool(
                    "tool_group_call",
                    {"group": "opss", "name": "job_output",
                     "arguments": {"job_id": job_id, "stream": "stdout", "max_bytes": 1024}},
                )
                assert "HTTP_DURABLE_PASS" in output.structured_content["content"]
                consumed = await client.call_tool(
                    "tool_group_call",
                    {"group": "opss", "name": "mark_result_consumed",
                     "arguments": {"kind": "job", "result_id": job_id}},
                )
                assert consumed.structured_content["success"] is True

        asyncio.run(check_gateway())
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
