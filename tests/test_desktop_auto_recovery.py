"""Automatic desktop lease/fence cleanup without erasing unknown outcomes."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager
from chatgpt_web_oauth_mcp.revit_workspace_backend import NodeWorkspaceBackend
from chatgpt_web_oauth_mcp.tool_groups import register_tool_groups
from chatgpt_web_oauth_mcp.windows_operation_client import compute_request_digest


def test_expired_foreign_idle_lease_is_released_on_next_status(tmp_path):
    manager = WorkspaceManager(tmp_path / "ledger.json", None,
                               owner_provider=lambda: "openai:new", clock=lambda: 101)
    ui = {"state": "idle", "owner": "openai:old", "workspace_id": "old",
          "token": "old-lease-token", "expires_at": 100, "activation_request_id": None}
    rows = {"old": {"state": "ready", "owner": "openai:old", "requests": {}}}
    manager.path.write_text(json.dumps({"version": 1, "ui": ui, "readers": {}, "workspaces": rows}))
    assert asyncio.run(manager.ui_status())["state"] == "clear"
    saved = json.loads(manager.path.read_text())
    assert saved["workspaces"] == rows
    assert saved["ui_recoveries"][-1]["mode"] == "automatic_idle_lease_expiry"
    assert saved["ui_recoveries"][-1]["fence_id"] == manager._ui_fence_id(ui)


@pytest.mark.parametrize("condition", ["unexpired", "operation", "native", "reader"])
def test_idle_cleanup_never_overtakes_active_work(tmp_path, condition):
    manager = WorkspaceManager(tmp_path / "ledger.json", None,
                               owner_provider=lambda: "openai:new", clock=lambda: 101)
    ui = {"state": "idle", "owner": "openai:old", "workspace_id": None,
          "token": "old-lease-token", "expires_at": 100}
    rows, readers = {}, {}
    if condition == "unexpired": ui["expires_at"] = 102
    if condition == "operation": ui["operation_token"] = "still-active"
    if condition == "native": rows["unknown"] = {"state": "unknown", "owner": "openai:old"}
    if condition == "reader": readers["reader"] = {"expires_at": 102}
    manager.path.write_text(json.dumps({"version": 1, "ui": ui, "readers": readers, "workspaces": rows}))
    assert asyncio.run(manager.ui_status())["state"] == "idle"
    assert json.loads(manager.path.read_text())["ui"] == ui


def test_unknown_without_terminal_reply_keeps_fence_across_sessions(tmp_path):
    async def check():
        remote, calls, receipts = FastMCP("unconfirmed gateway"), [], {}
        @remote.tool(name="windows_Click")
        def direct(x: int):
            pytest.fail("must use journal submit")
        @remote.tool(name="windows_operation_submit")
        def submit(operation_id: str, name: str, arguments: dict):
            calls.append(name)
            receipt = {"protocol_version": 1, "operation_id": operation_id,
                       "request_digest": compute_request_digest(name, arguments),
                       "state": "unknown", "result": None,
                       "execution_status": "unconfirmed", "error_code": "GATEWAY_RESTARTED"}
            receipts[operation_id] = receipt
            return receipt
        @remote.tool(name="windows_operation_status")
        def status(operation_id: str):
            return receipts[operation_id]
        @remote.tool(name="windows_operation_reconcile")
        def reconcile(operation_id: str, request_digest: str):
            assert receipts[operation_id]["request_digest"] == request_digest
            return receipts[operation_id]
        owner = ["openai:old"]
        manager = WorkspaceManager(tmp_path / "ledger.json", NodeWorkspaceBackend(lambda: remote),
                                   owner_provider=lambda: owner[0])
        public = FastMCP("isolated coordinator")
        register_tool_groups(public, transport_factory=lambda: remote, workspace_manager=manager)
        async with Client(public) as client:
            result = await client.call_tool("tool_group_call",
                {"group": "windows", "name": "windows_Click", "arguments": {"x": 1}},
                raise_on_error=False)
            assert result.is_error
            active = manager.get_active_durable_operation()
            owner[0] = "openai:new"
            for _ in range(2):
                state = await manager.ui_status()
                assert state["state"] == "draining" and state["owner_matches"] is False
                assert state["operation_status"]["reason"] == "unknown_state"
            blocked = await client.call_tool("tool_group_call",
                {"group": "windows", "name": "windows_Click", "arguments": {"x": 2}},
                raise_on_error=False)
            assert blocked.is_error
            assert calls == ["windows_Click"]
            assert manager.get_active_durable_operation()["operation_token"] == active["operation_token"]
    asyncio.run(check())


@pytest.mark.parametrize("execution", ["unconfirmed", "running", "invented", {}, None])
def test_no_automatic_unlock_from_unconfirmed_or_malformed_receipt(tmp_path, execution):
    async def check():
        manager = WorkspaceManager(tmp_path / "ledger.json", None, owner_provider=lambda: "openai:test")
        args, token = await manager.begin_remote("windows", "windows_Click", {"x": 1}, durable=True)
        active = manager.get_active_durable_operation()
        receipt = {"protocol_version": 1, "operation_id": token,
                   "request_digest": compute_request_digest("windows_Click", args),
                   "state": "unknown", "result": None, "execution_status": execution}
        with pytest.raises(ValueError):
            manager.reconcile_durable_receipt(active["fence_id"], receipt)
        assert manager.get_active_durable_operation()["operation_token"] == token
    asyncio.run(check())

