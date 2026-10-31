"""Live in-memory FastMCP acceptance for exactly three lazy groups."""
from __future__ import annotations

import asyncio
import base64
import pytest

from fastmcp import Client, FastMCP
from fastmcp.tools import ToolResult
from mcp.types import ImageContent

from chatgpt_web_oauth_mcp.tool_groups import (
    MAX_PAGE_SIZE,
    MAX_SCHEMA_PAGE_SIZE,
    READONLY_REVIT_TOOLS,
    REVIT_NATIVE_QA_GUIDE,
    _enforce_revit_launch_policy,
    register_tool_groups,
)
from chatgpt_web_oauth_mcp.revit_workspaces import workspace_tools


@pytest.fixture(autouse=True)
def private_workspace_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATGPT_MCP_STATE_DIR", str(tmp_path / "state"))


@pytest.mark.parametrize("tool_name,tool_args", [
    ("windows_App", {"mode": "launch", "name": "Revit 2023"}),
    ("windows_App", {"mode": "launch_executable",
                     "executable": r"C:\Program Files\Autodesk\Revit 2023\Revit.exe"}),
    ("windows_App", {"name": "Autodesk Revit"}),
    ("windows_PowerShell", {"command": r'Start-Process "C:\Program Files\Autodesk\Revit 2023\Revit.exe"'}),
    ("windows_PowerShell", {"command": r"Invoke-Item 'C:\Users\Public\Desktop\Revit 2023.lnk'"}),
    ("windows_PowerShell", {"command": r'& "C:\Program Files\Autodesk\Revit 2023\Revit.exe"'}),
])
def test_known_unsafe_revit_process_launch_routes_are_denied(tool_name, tool_args) -> None:
    with pytest.raises(ValueError, match="DIRECT_REVIT_LAUNCH_FORBIDDEN"):
        _enforce_revit_launch_policy(tool_name, tool_args)


@pytest.mark.parametrize("tool_name,tool_args", [
    ("windows_App", {"mode": "switch", "name": "Revit 2023"}),
    ("windows_App", {"mode": "resize", "name": "Revit 2023"}),
    ("windows_PowerShell", {"command": "Get-Process -Name Revit"}),
    ("windows_PowerShell", {"command": r'Get-Item "C:\Program Files\Autodesk\Revit 2023\Revit.exe"'}),
    ("windows_PowerShell", {"command": r'& powershell.exe -NoProfile -File "C:\Users\Vlad\AppData\Local\Astra-Revit-Bridge\QA\Invoke-VerifiedExplorerRevit2023.ps1" -Mode ValidateOnly'}),
])
def test_readonly_or_verified_explorer_routes_are_not_denied(tool_name, tool_args) -> None:
    _enforce_revit_launch_policy(tool_name, tool_args)


def make_servers(*, many_windows: bool = False):
    opss = FastMCP("Opss")
    remote = FastMCP("Windows and Revit")

    @opss.tool(name="run_command")
    def linux_command() -> str:
        return "GIP_CORE"

    @remote.tool(name="windows_Screenshot")
    def screenshot() -> ToolResult:
        return ToolResult(
            content=[
                ImageContent(
                    type="image",
                    mimeType="image/png",
                    data=base64.b64encode(b"test-png-data").decode("ascii"),
                )
            ]
        )

    @remote.tool(name="windows_Click")
    def click(x: int, y: int) -> dict[str, int]:
        return {"x": x, "y": y}

    @remote.tool(name="windows_PowerShell")
    def shell(command: str) -> str:
        return "shell-not-used:" + command

    @remote.tool(name="revit_get_project_info")
    def project_info() -> dict[str, str]:
        return {"version": "2023", "document": "QA.rvt"}

    @remote.tool(name="revit_get_phases")
    def phases() -> list[str]:
        return ["existing", "new"]

    @remote.tool(name="revit_get_selected_elements")
    def selected(limit: int = 100) -> dict[str, int]:
        return {"count": min(limit, 4)}

    @remote.tool(name="revit_send_code_to_revit")
    def unsafe_csharp(code: str) -> str:
        return "MUST_NOT_RUN:" + code

    @remote.tool(name="revit_delete_element")
    def unsafe_delete(element_id: int) -> str:
        return "MUST_NOT_RUN"

    @remote.tool(name="windows_HugeText")
    def huge_text() -> str:
        return "x" * 135000

    if many_windows:
        for number in range(140):
            @remote.tool(name=f"windows_fake_{number:03d}")
            def dummy(value: int = 0) -> int:
                return value

    register_tool_groups(opss, transport_factory=lambda: remote)
    return opss, remote


def call(client, name, args):
    return client.call_tool(name, args, raise_on_error=False)


def test_only_three_dispatcher_tools_and_native_opss_despite_large_remote_catalog() -> None:
    opss, _ = make_servers(many_windows=True)

    async def check() -> None:
        async with Client(opss) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == {
                "run_command", "tool_group_select", "tool_group_catalog", "tool_group_call"
            }
            result = await client.call_tool("run_command")
            assert result.content[0].text == "GIP_CORE"
            selected = await client.call_tool("tool_group_select", {"group": "windows"})
            assert selected.structured_content["total"] == 144
            assert selected.structured_content["next_offset"] == 10
            assert selected.structured_content["selected"] == "windows"

    asyncio.run(check())


def test_revit_launch_policy_is_visible_and_enforced_before_windows_backend() -> None:
    opss, remote = make_servers()
    app_calls = []

    @remote.tool(name="windows_App")
    def app(mode: str = "launch", name: str | None = None,
            executable: str | None = None) -> str:
        app_calls.append((mode, name, executable))
        return "APP_EXECUTED"

    async def check() -> None:
        async with Client(opss) as client:
            selected = await client.call_tool(
                "tool_group_select", {"group": "windows", "limit": 2}
            )
            assert REVIT_NATIVE_QA_GUIDE in selected.structured_content[
                "revit_native_qa_launch_policy"
            ]
            unsafe = await client.call_tool(
                "tool_group_call",
                {"group": "windows", "name": "windows_App",
                 "arguments": {"mode": "launch", "name": "Revit 2023"}},
                raise_on_error=False,
            )
            assert unsafe.is_error
            assert not app_calls
            accepted = await client.call_tool(
                "tool_group_call",
                {"group": "windows", "name": "windows_App",
                 "arguments": {"mode": "launch", "name": "Notepad"}},
            )
            assert accepted.content[0].text == "APP_EXECUTED"
            assert app_calls == [("launch", "Notepad", None)]

    asyncio.run(check())


def test_opss_group_is_local_and_requires_no_remote_connection() -> None:
    opss = FastMCP("Opss")
    invoked = []

    @opss.tool(name="run_command")
    def local_command() -> str:
        return "LOCAL_OK"

    def unavailable():
        invoked.append("unexpected")
        raise ConnectionError("Windows VM unreachable")

    register_tool_groups(opss, transport_factory=unavailable)

    async def check():
        async with Client(opss) as client:
            response = await client.call_tool("tool_group_select", {"group": "opss"})
            assert response.structured_content["selected"] == "opss"
            catalog = await client.call_tool("tool_group_catalog", {"group": "opss"})
            assert catalog.structured_content["native_tools"] is True
            local = await client.call_tool("run_command")
            assert local.content[0].text == "LOCAL_OK"
            assert not invoked
            missing = await call(client, "tool_group_select", {"group": "windows"})
            assert missing.is_error
            still_local = await client.call_tool("run_command")
            assert still_local.content[0].text == "LOCAL_OK"

    asyncio.run(check())


def test_revit_group_lists_private_workspaces_and_legacy_readonly_tools() -> None:
    opss, _ = make_servers()

    async def check() -> None:
        async with Client(opss) as client:
            selected = await client.call_tool("tool_group_select", {"group": "revit", "limit": 20})
            assert {item["name"] for item in selected.structured_content["tools"]} == (
                {tool.name for tool in workspace_tools()} | READONLY_REVIT_TOOLS
            )
            wrong = await call(
                client, "tool_group_call", {
                    "group": "revit", "name": "revit_send_code_to_revit",
                    "arguments": {"code": "System.IO.File.Delete(...)"},
                }
            )
            assert wrong.is_error
            wrong2 = await call(
                client, "tool_group_call", {
                    "group": "revit", "name": "revit_delete_element",
                    "arguments": {"element_id": 123},
                }
            )
            assert wrong2.is_error
            info = await client.call_tool(
                "tool_group_call", {"group": "revit", "name": "revit_get_project_info"}
            )
            assert info.structured_content["version"] == "2023"

    asyncio.run(check())


def test_select_catalog_search_pagination_and_exact_schema() -> None:
    opss, _ = make_servers(many_windows=True)

    async def check() -> None:
        async with Client(opss) as client:
            names = await client.call_tool(
                "tool_group_catalog",
                {"group": "windows", "search": "fake", "offset": 130, "limit": 5},
            )
            data = names.structured_content
            assert data["group"] == "windows"
            assert data["total"] == 140
            assert len(data["tools"]) == 5
            assert data["next_offset"] == 135

            schema = await client.call_tool(
                "tool_group_catalog",
                {"group": "windows", "search": "Click", "limit": 1, "include_schema": True},
            )
            tool = schema.structured_content["tools"][0]
            assert tool["name"] == "windows_Click"
            assert set(tool["required_arguments"]) == {"x", "y"}
            assert set(tool["input_schema"]["properties"]) == {"x", "y", "_workspace_id", "_lease_token"}

            click = await client.call_tool(
                "tool_group_call",
                {"group": "windows", "name": "windows_Click", "arguments": {"x": 3, "y": 7}},
            )
            assert click.structured_content == {"x": 3, "y": 7}

    asyncio.run(check())


def test_screen_image_preserved_in_generic_group_dispatch() -> None:
    opss, _ = make_servers()

    async def check() -> None:
        async with Client(opss) as client:
            response = await client.call_tool(
                "tool_group_call", {"group": "windows", "name": "windows_Screenshot"}
            )
            assert response.content[0].type == "image"
            assert base64.b64decode(response.content[0].data) == b"test-png-data"

    asyncio.run(check())


def test_explicit_group_survives_reconnect_and_separate_clients() -> None:
    opss, _ = make_servers()

    async def check() -> None:
        async with Client(opss) as first:
            w = await first.call_tool("tool_group_select", {"group": "windows"})
            assert w.structured_content["selected"] == "windows"
        # A new client/transport session still calls the intended group.
        async with Client(opss) as second:
            r = await second.call_tool("tool_group_select", {"group": "revit"})
            assert r.structured_content["selected"] == "revit"
            # Desktop mutations are now serialized against native calls, even
            # for the compatibility path before any workspace is registered.
            call_a = await second.call_tool(
                "tool_group_call", {"group": "revit", "name": "revit_get_phases"}
            )
            call_b = await second.call_tool(
                "tool_group_call",
                {"group": "windows", "name": "windows_Click", "arguments": {"x": 1, "y": 2}},
            )
            assert "existing" in call_a.content[0].text
            assert call_b.structured_content["x"] == 1

    asyncio.run(check())


def test_invalid_calls_never_reach_remote_provider() -> None:
    opss, _ = make_servers()

    async def check() -> None:
        async with Client(opss) as client:
            bad = [
                {"group": "opss", "name": "run_command"},
                {"group": "windows", "name": "revit_get_phases"},
                {"group": "revit", "name": "windows_PowerShell"},
                {"group": "windows", "name": "windows_no_such_tool"},
                {"group": "windows", "name": "windows_Click", "arguments": {"text": "x" * 70000}},
            ]
            for arguments in bad:
                result = await call(client, "tool_group_call", arguments)
                assert result.is_error, arguments

    asyncio.run(check())


def test_tool_result_size_and_catalog_page_limits_are_bounded() -> None:
    opss, _ = make_servers()

    async def check() -> None:
        async with Client(opss) as client:
            huge = await call(
                client, "tool_group_call", {"group": "windows", "name": "windows_HugeText"}
            )
            assert huge.is_error
            wrong_page = await call(
                client,
                "tool_group_catalog",
                {"group": "windows", "limit": MAX_SCHEMA_PAGE_SIZE + 1, "include_schema": True},
            )
            assert wrong_page.is_error
            wrong_limit = await call(
                client, "tool_group_catalog", {"group": "revit", "limit": MAX_PAGE_SIZE + 1}
            )
            assert wrong_limit.is_error

    asyncio.run(check())
