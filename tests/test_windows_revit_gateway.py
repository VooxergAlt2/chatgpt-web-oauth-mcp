"""Hermetic contract tests for the Windows/Revit FastMCP composition."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from starlette.testclient import TestClient

from chatgpt_web_oauth_mcp import upstream_gateway


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "windows_revit_gateway.py"
_SPEC = importlib.util.spec_from_file_location("_windows_revit_gateway_test", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
gateway_mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gateway_mod
_SPEC.loader.exec_module(gateway_mod)


def test_windows_and_revit_tools_are_mounted_independently_and_revit_is_allowlisted() -> None:
    windows = FastMCP("fake Windows")
    revit = FastMCP("fake Revit")

    @windows.tool(name="Screenshot")
    def screenshot() -> str:
        return "GUI_CAPTURE_OK"

    @revit.tool(name="get_project_info")
    def project_info() -> str:
        return "REVIT_API_OK"

    @revit.tool(name="get_phases")
    def get_phases() -> str:
        return "PHASES_OK"

    @revit.tool(name="send_code_to_revit")
    def execute_code() -> str:
        return "UNSAFE"

    cfg = gateway_mod.GatewayConfig(
        windows_url="http://127.0.0.1:8000/mcp",
        node_executable="node",
        revit_server_js=Path("C:/RevitMCP/server/build/index.js"),
    )
    gateway = gateway_mod.build_gateway(
        cfg, windows_target=windows, revit_target=revit, validate_files=False
    )
    async def check() -> None:
        async with Client(gateway) as client:
            names = {item.name for item in await client.list_tools()}
            assert names == {"windows_Screenshot", "revit_get_project_info", "revit_get_phases"}
            screen = await client.call_tool("windows_Screenshot", {})
            revit_result = await client.call_tool("revit_get_project_info", {})
            assert screen.content[0].text == "GUI_CAPTURE_OK"
            assert revit_result.content[0].text == "REVIT_API_OK"
            with pytest.raises(Exception, match="Unknown tool"):
                await client.call_tool("revit_send_code_to_revit", {})

    asyncio.run(check())


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"windows_url": "http://192.168.0.216:8000/mcp"}, "loopback"),
        ({"windows_url": "http://127.0.0.1:8000/wrong"}, "/mcp"),
        ({"host": "0.0.0.0"}, "requires"),
        ({"host": "192.168.0.216", "auth_token": "a" * 40}, "requires"),
        ({"revit_tools": frozenset({"send_code_to_revit"})}, "pre-approved"),
        ({"port": 65536}, "port"),
    ],
)
def test_gateway_validation_rejects_unsafe_config(kwargs, reason) -> None:
    base = {
        "windows_url": "http://127.0.0.1:8000/mcp",
        "node_executable": "node",
        "revit_server_js": Path("C:/QA/server/build/index.js"),
    }
    with pytest.raises(ValueError, match=reason):
        gateway_mod.GatewayConfig(**(base | kwargs)).validate(validate_files=False)


def test_gateway_check_only_does_not_spawn_subprocesses(tmp_path, monkeypatch, capsys) -> None:
    js = tmp_path / "index.js"
    js.write_text("process.exit(0)", encoding="utf-8")

    def forbid_proxy(*args, **kwargs):
        raise AssertionError("A check-only request must not launch a backend")

    monkeypatch.setattr(gateway_mod, "create_proxy", forbid_proxy)
    assert gateway_mod.main(["--check-only", "--revit-server-js", str(js)]) == 0
    assert "GATEWAY_CONFIGURATION_PASS" in capsys.readouterr().out


def test_http_gateway_requires_valid_bearer_token() -> None:
    windows = FastMCP("fake Windows")
    revit = FastMCP("fake Revit")

    @windows.tool(name="Screenshot")
    def screenshot() -> str:
        return "ok"

    @revit.tool(name="get_project_info")
    def project_info() -> str:
        return "ok"

    token = "QA" * 24
    cfg = gateway_mod.GatewayConfig(
        windows_url="http://127.0.0.1:8000/mcp",
        node_executable="node",
        revit_server_js=Path("C:/QA/server/build/index.js"),
        auth_token=token,
    )
    app = gateway_mod.build_gateway(
        cfg, windows_target=windows, revit_target=revit, validate_files=False
    ).http_app(path="/mcp", transport="streamable-http", stateless_http=True)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "qa", "version": "1"},
        },
    }
    headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    with TestClient(app) as client:
        assert client.post("/mcp", json=initialize, headers=headers).status_code == 401
        assert client.post(
            "/mcp", json=initialize,
            headers=headers | {"authorization": "Bearer incorrect"},
        ).status_code == 401
        assert client.post(
            "/mcp", json=initialize,
            headers=headers | {"authorization": f"Bearer {token}"},
        ).status_code == 200


def test_opss_upstream_groups_not_registered_without_environment() -> None:
    mcp = SimpleNamespace(mount=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError))
    assert upstream_gateway.register_gateway_groups_from_env(mcp, environ={}) is False


def test_opss_upstream_transport_never_allows_plaintext_remote() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        upstream_gateway.gateway_transport(
            url="http://192.168.0.216:8840/mcp",
            token="x" * 40,
        )
    with pytest.raises(ValueError, match="bearer"):
        upstream_gateway.gateway_transport(
            url="https://192.168.0.216:8840/mcp",
            token="short",
        )


def test_opss_upstream_registers_three_group_tools_without_connecting(monkeypatch) -> None:
    calls = []
    remote = FastMCP("not started by import")

    def transport_factory(**kwargs):
        calls.append(kwargs)
        return remote

    monkeypatch.setattr(upstream_gateway, "gateway_transport", transport_factory)
    server = FastMCP("Opss local tools")
    assert upstream_gateway.register_gateway_groups_from_env(
        server,
        environ={
            "OPSS_WINVM_GATEWAY_URL": "https://192.168.0.216:8840/mcp",
            "OPSS_WINVM_GATEWAY_TOKEN": "A" * 40,
        },
    ) is True
    assert len(calls) == 1  # configuration validation only, no MCP connection

    async def check() -> None:
        async with Client(server) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == {"tool_group_select", "tool_group_catalog", "tool_group_call"}
            result = await client.call_tool("tool_group_select", {"group": "opss"})
            assert not result.is_error
            assert len(calls) == 1  # Opss selection stays local

    asyncio.run(check())
