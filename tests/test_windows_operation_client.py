"""Behavioral regression tests of the complete coordinator/journal boundary."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools import ToolResult

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from windows_revit_gateway import GatewayConfig, build_gateway
from chatgpt_web_oauth_mcp.revit_workspace_backend import NodeWorkspaceBackend
from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager
from chatgpt_web_oauth_mcp.tool_groups import register_tool_groups
from chatgpt_web_oauth_mcp.windows_operation_client import compute_request_digest


def fixture(tmp_path, windows):
    gateway = build_gateway(
        GatewayConfig(windows_url="http://127.0.0.1:8000/mcp",
                      node_executable="unused", revit_server_js=Path("unused"),
                      operation_state_dir=tmp_path / "journal"),
        windows_target=windows, revit_target=FastMCP("no Revit"), validate_files=False)
    owners = ["openai:original"]
    manager = WorkspaceManager(tmp_path / "ledger.json", NodeWorkspaceBackend(lambda: gateway),
                               owner_provider=lambda: owners[0])
    public = FastMCP("Opss candidate")
    register_tool_groups(public, transport_factory=lambda: gateway, workspace_manager=manager)
    return gateway, manager, owners, public


async def call(client, name, arguments=None, group="windows"):
    return await client.call_tool("tool_group_call",
                                 {"group": group, "name": name, "arguments": arguments or {}},
                                 raise_on_error=False)


async def terminal(journal, token):
    async with asyncio.timeout(5):
        while True:
            result = await journal.status(token)
            if result["state"] != "running":
                return result
            await asyncio.sleep(0.01)


def test_complete_result_and_output_text_survive_journal(tmp_path):
    async def check():
        remote = FastMCP("Windows")
        calls = []
        @remote.tool(name="PowerShell")
        def shell(command: str, timeout: int = 30):
            calls.append(timeout)
            return ToolResult(structured_content={"value": "Command execution timed out",
                                                   "ok": True})
        gateway, manager, owners, public = fixture(tmp_path, remote)
        async with Client(public) as client:
            result = await call(client, "windows_PowerShell", {"command": "source search"})
            assert not result.is_error
            assert result.structured_content == {"value": "Command execution timed out", "ok": True}
            assert calls == [20]
            assert (await manager.ui_status())["state"] == "clear"
            catalog = await client.call_tool("tool_group_catalog", {"group": "windows", "limit": 20})
            names = {row["name"] for row in catalog.structured_content["tools"]}
            assert "windows_operation_result" in names
            assert "windows_operation_submit" not in names
            assert "windows_operation_status" not in names
            denied = await call(client, "windows_operation_submit", {})
            assert denied.is_error
        gateway.operation_journal.close()
    asyncio.run(check())


def test_lost_reply_reconciles_after_controller_restart_without_replay(tmp_path):
    async def check():
        remote = FastMCP("Windows")
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []
        @remote.tool(name="Click")
        async def click(x: int):
            calls.append(x)
            started.set()
            await finish.wait()
            return {"private_result": "owner-only"}
        gateway, manager, owners, public = fixture(tmp_path, remote)
        async with Client(public) as client:
            task = asyncio.create_task(call(client, "windows_Click", {"x": 42}))
            await asyncio.wait_for(started.wait(), 5)
            active = manager.get_active_durable_operation()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert manager.get_active_durable_operation() is not None
            finish.set()
            receipt = await terminal(gateway.operation_journal, active["operation_token"])
            assert receipt["state"] == "completed"
            owners[0] = "openai:other"
            restarted = WorkspaceManager(manager.path, NodeWorkspaceBackend(lambda: gateway),
                                         owner_provider=lambda: owners[0])
            status = await restarted.ui_status()
            assert status == {"state": "clear", "scope": None, "recoverable": False}
            assert "owner-only" not in str(status)
            denied = await call(client, "windows_operation_result", {"fence_id": active["fence_id"]})
            assert denied.is_error and "owner-only" not in str(denied)
            owners[0] = "openai:original"
            recovered = await call(client, "windows_operation_result", {"fence_id": active["fence_id"]})
            assert recovered.structured_content == {"private_result": "owner-only"}
            assert calls == [42]
        gateway.operation_journal.close()
    asyncio.run(check())


def test_unknown_upstream_never_replays_or_unlocks(tmp_path):
    async def check():
        remote = FastMCP("Windows")
        calls = []
        @remote.tool(name="PowerShell")
        def shell(command: str, timeout: int = 20):
            calls.append(command)
            return "Response: Command execution timed out\n\nStatus Code: 1"
        gateway, manager, owners, public = fixture(tmp_path, remote)
        async with Client(public) as client:
            result = await call(client, "windows_PowerShell", {"command": "timed operation"})
            assert result.is_error
            active = manager.get_active_durable_operation()
            for _ in range(2):
                status = await manager.ui_status()
                assert status["state"] == "draining"
                assert status["operation_status"]["reason"] == "unknown_state"
                assert status["operation_status"]["error_code"] == "TIMEOUT"
                assert status["operation_status"]["next_action"] == "operator_inspection_required"
                again = await call(client, "windows_operation_result", {"fence_id": active["fence_id"]})
                assert again.is_error
                assert "TIMEOUT" in str(again)
            blocked = await call(client, "windows_PowerShell", {"command": "must not start"})
            assert blocked.is_error
            assert calls == ["timed operation"]
        gateway.operation_journal.close()
    asyncio.run(check())


@pytest.mark.parametrize("mutation", [
    {"protocol_version": True}, {"protocol_version": 2},
    {"operation_id": "another"}, {"request_digest": "another"},
    {"state": "unknown"}, {"result": {}},
    {"result": {"content": [], "isError": True}},
    {"result": {"content": [], "isError": 0}},
    {"result": {"content": [], "isError": False, "resultType": "input_required"}},
    {"result": {"content": [{"type": "not-mcp"}], "isError": False}},
])
def test_invalid_receipt_cannot_release_desktop(tmp_path, mutation):
    async def check():
        manager = WorkspaceManager(tmp_path / "state.json", None, owner_provider=lambda: "openai:test")
        args, token = await manager.begin_remote("windows", "windows_Click", {"x": 1}, durable=True)
        active = manager.get_active_durable_operation()
        good = {"protocol_version": 1, "operation_id": token,
                "request_digest": compute_request_digest("windows_Click", args),
                "state": "completed", "result": {"content": [], "isError": False}}
        with pytest.raises(ValueError):
            manager.complete_durable_operation(active["fence_id"], good | mutation)
        assert manager.get_active_durable_operation()["operation_token"] == token
    asyncio.run(check())


def test_old_receipt_cannot_unlock_new_operation(tmp_path):
    async def check():
        manager = WorkspaceManager(tmp_path / "state.json", None, owner_provider=lambda: "openai:test")
        args, old_token = await manager.begin_remote("windows", "windows_Click", {"x": 1}, durable=True)
        old = manager.get_active_durable_operation()
        receipt = {"protocol_version": 1, "operation_id": old_token,
                   "request_digest": compute_request_digest("windows_Click", args),
                   "state": "completed", "result": {"content": [], "isError": False}}
        assert manager.complete_durable_operation(old["fence_id"], receipt)
        _, new_token = await manager.begin_remote("windows", "windows_Click", {"x": 2}, durable=True)
        assert not manager.complete_durable_operation(old["fence_id"], receipt)
        assert manager.get_active_durable_operation()["operation_token"] == new_token
    asyncio.run(check())


@pytest.mark.parametrize("code, expected", [
    ("JOURNAL_WRITE_FAILED", "JOURNAL_WRITE_FAILED"),
    ("GATEWAY_RESTARTED", "GATEWAY_RESTARTED"),
    ("private upstream error text", "UNKNOWN_OUTCOME"),
    (None, "UNKNOWN_OUTCOME"),
    ({"secret": "value"}, "UNKNOWN_OUTCOME"),
])
def test_unknown_diagnostics_do_not_expose_upstream_text(code, expected):
    from chatgpt_web_oauth_mcp.windows_operation_client import unknown_operation_diagnostic
    assert unknown_operation_diagnostic({"error_code": code}) == {
        "error_code": expected, "next_action": "operator_inspection_required"}


def test_old_gateway_is_rejected_before_desktop_reservation(tmp_path):
    async def check():
        remote, public = FastMCP("old gateway"), FastMCP("Opss")
        @remote.tool(name="windows_Click")
        def click(x: int):
            pytest.fail("must not dispatch")
        manager = WorkspaceManager(tmp_path / "state.json", None, owner_provider=lambda: "openai:test")
        register_tool_groups(public, transport_factory=lambda: remote, workspace_manager=manager)
        async with Client(public) as client:
            result = await call(client, "windows_Click", {"x": 1})
            assert result.is_error and "DURABLE_WINDOWS_ENDPOINTS_REQUIRED" in str(result)
        assert (await manager.ui_status())["state"] == "clear"
    asyncio.run(check())


def test_invalid_arguments_are_rejected_before_reserving(tmp_path):
    async def check():
        remote = FastMCP("Windows")
        @remote.tool(name="Click")
        def click(x: int):
            pytest.fail("must not dispatch")
        gateway, manager, owners, public = fixture(tmp_path, remote)
        async with Client(public) as client:
            result = await call(client, "windows_Click", {"x": "not an integer"})
            assert result.is_error
            assert manager.get_active_durable_operation() is None
            assert (await manager.ui_status())["state"] == "clear"
        gateway.operation_journal.close()
    asyncio.run(check())
