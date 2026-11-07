"""Remote shell deadlines must fit the upstream transport, before taking a fence."""
import asyncio

import pytest
from fastmcp import Client, FastMCP

from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager
from chatgpt_web_oauth_mcp.tool_groups import register_tool_groups, REMOTE_CALL_TIMEOUT_SECONDS


@pytest.mark.parametrize("timeout", [None, 1, 20])
def test_effective_timeout_and_catalog_agree(tmp_path, timeout):
    async def check():
        remote, public = FastMCP("Windows"), FastMCP("Opss")
        received = []
        @remote.tool(name="windows_PowerShell")
        def powershell(command: str, timeout: int = 30):
            received.append(timeout)
            return {"timeout": timeout}
        manager = WorkspaceManager(tmp_path / "state.json", None,
                                   owner_provider=lambda: "openai:test")
        register_tool_groups(public, transport_factory=lambda: remote, workspace_manager=manager, allow_legacy_windows=True)
        args = {"command": "Get-Date"}
        if timeout is not None:
            args["timeout"] = timeout
        original = dict(args)
        async with Client(public) as client:
            catalog = await client.call_tool("tool_group_catalog", {
                "group": "windows", "search": "windows_PowerShell",
                "include_schema": True, "limit": 1})
            schema = catalog.structured_content["tools"][0]["input_schema"]["properties"]["timeout"]
            assert schema["default"] == schema["maximum"] == 20
            assert schema["minimum"] == 1
            result = await client.call_tool("tool_group_call", {
                "group": "windows", "name": "windows_PowerShell", "arguments": args})
            assert not result.is_error
        assert received == [timeout if timeout is not None else 20]
        assert received[0] < REMOTE_CALL_TIMEOUT_SECONDS
        assert args == original
        assert (await manager.ui_status())["state"] == "clear"
    asyncio.run(check())


@pytest.mark.parametrize("timeout", [0, -1, 21, 25, 30, 120, True, None, "20", 1.5])
def test_invalid_budget_never_connects_or_reserves_ui(tmp_path, timeout):
    async def check():
        public = FastMCP("Opss")
        manager = WorkspaceManager(tmp_path / "state.json", None,
                                   owner_provider=lambda: "openai:test")
        def forbidden_transport():
            raise AssertionError("Validation must precede any remote connection")
        register_tool_groups(public, transport_factory=forbidden_transport, workspace_manager=manager, allow_legacy_windows=True)
        async with Client(public) as client:
            result = await client.call_tool("tool_group_call", {
                "group": "windows", "name": "windows_PowerShell",
                "arguments": {"command": "Get-Date", "timeout": timeout}}, raise_on_error=False)
            assert result.is_error
            assert "WINDOWS_TIMEOUT_EXCEEDS_TRANSPORT_BUDGET" in str(result.content)
        assert not manager.path.exists()
    asyncio.run(check())
