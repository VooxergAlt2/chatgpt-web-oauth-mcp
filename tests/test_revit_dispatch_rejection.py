"""A positive no-dispatch receipt is different from uncertain native I/O."""
import asyncio
import json

import pytest
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from test_revit_workspaces import setup, create
from chatgpt_web_oauth_mcp.revit_workspace_backend import NodeWorkspaceBackend
from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager


KIND = "RevitWorkspaceDispatchNotSentV1"


def receipt(backend, workspace_id, request_id):
    return {
        "kind": KIND, "operation": "submit", "dispatch_state": "not_sent",
        "code": "AUTHORIZATION_REQUIRED", "workspace_id": workspace_id,
        "request_id": request_id, "instance_id": backend.instance["instance_id"],
        "document_path": backend.paths[workspace_id],
        "native_identity": {key: backend.instance[key] for key in (
            "instance_id", "pid", "port", "process_started_utc", "revit_version", "plugin_build_id"
        )},
    }


async def protected(tmp_path):
    manager, backend, owner = setup(tmp_path)
    backend.instance["capabilities"].append("native_authorization_v1")
    workspace = (await create(manager))["workspace_id"]
    backend.authorized_workspace_ids.add(workspace)
    await manager.bind_prepared(workspace)
    return manager, backend, owner, workspace


def test_not_sent_rejection_is_terminal_persistent_and_reapproval_does_not_replay(tmp_path):
    async def check():
        manager, backend, owner, wid = await protected(tmp_path)
        original = backend.dispatch
        submits = []

        async def dispatch(instance, operation, payload):
            if operation == "submit":
                submits.append(payload["request_id"])
                return receipt(backend, wid, payload["request_id"])
            return await original(instance, operation, payload)

        backend.dispatch = dispatch
        denied = await manager.submit(wid, "get_phases", {}, "expired-1")
        assert denied["status"] == "rejected"
        assert denied["dispatch_state"] == "not_sent"
        assert denied["error"]["code"] == "AUTHORIZATION_REQUIRED"
        assert denied["requires_reapproval"] is True
        assert "owner_proof" not in json.dumps(denied)
        assert not backend.requests.get("expired-1")
        # An expired grant no longer invents native work and blocks the UI.
        lease = await manager.acquire_ui(wid)
        await manager.release_ui(wid, lease["token"])
        restored = WorkspaceManager(manager.path, backend, owner_provider=lambda: owner[0])
        assert (await restored.status(wid, "expired-1"))["requests"]["expired-1"] == denied
        assert await restored.cancel(wid, "expired-1") == denied
        assert await restored.submit(wid, "get_phases", {}, "expired-1") == denied
        assert submits == ["expired-1"], "a rejected request must not be silently retried"
        backend.dispatch = original
        assert (await restored.submit(wid, "get_phases", {}, "approved-new-id"))["status"] == "queued"
    asyncio.run(check())


@pytest.mark.parametrize("field,value", [
    ("request_id", "other"), ("workspace_id", "ws_other"), ("instance_id", "other"),
    ("document_path", r"C:\QA\other.rvt"), ("operation", "status"),
    ("code", "NATIVE_ERROR"), ("dispatch_state", "unknown"), ("extra", "untrusted"),
])
def test_malformed_or_mismatched_receipt_retains_unknown(tmp_path, field, value):
    async def check():
        manager, backend, _, wid = await protected(tmp_path)
        original = backend.dispatch

        async def dispatch(instance, operation, payload):
            if operation == "submit":
                return {**receipt(backend, wid, payload["request_id"]), field: value}
            return await original(instance, operation, payload)

        backend.dispatch = dispatch
        with pytest.raises(ValueError):
            await manager.submit(wid, "get_phases", {}, "uncertain")
        row = (await manager.list())["workspaces"][0]
        assert row["requests"]["uncertain"]["status"] == "unknown"
        with pytest.raises(ValueError, match="DESKTOP_BUSY"):
            await manager.acquire_ui(wid)
    asyncio.run(check())


def test_post_send_timeout_and_late_not_sent_status_cannot_clear_uncertainty(tmp_path):
    async def check():
        manager, backend, _, wid = await protected(tmp_path)
        backend.timeout_submit = True
        with pytest.raises(TimeoutError):
            await manager.submit(wid, "get_phases", {}, "lost-response")
        original = backend.dispatch

        async def dispatch(instance, operation, payload):
            if operation == "status":
                return receipt(backend, wid, payload["request_id"])
            return await original(instance, operation, payload)

        backend.dispatch = dispatch
        with pytest.raises(ValueError):
            await manager.status(wid, "lost-response")
        assert (await manager.list())["workspaces"][0]["requests"]["lost-response"]["status"] == "unknown"
        with pytest.raises(ValueError, match="BUSY"):
            await manager.release(wid)
    asyncio.run(check())


def test_legacy_text_auth_error_does_not_unlock_a_request(tmp_path):
    async def check():
        manager, backend, _, wid = await protected(tmp_path)
        original = backend.dispatch

        async def dispatch(instance, operation, payload):
            if operation == "submit":
                raise ValueError("AUTHORIZATION_REQUIRED: arbitrary text is not no-dispatch evidence")
            return await original(instance, operation, payload)

        backend.dispatch = dispatch
        with pytest.raises(ValueError, match="AUTHORIZATION_REQUIRED"):
            await manager.submit(wid, "get_phases", {}, "legacy-error")
        assert (await manager.list())["workspaces"][0]["requests"]["legacy-error"]["status"] == "unknown"
        with pytest.raises(ValueError, match="DESKTOP_BUSY"):
            await manager.acquire_ui(wid)
    asyncio.run(check())


@pytest.mark.parametrize("identity_update", [
    {"pid": 52}, {"pid": True}, {"port": 19002}, {"process_started_utc": "stale"},
    {"plugin_build_id": "other"}, {"revit_version": "2025"}, {"owner_proof": "extra"},
])
def test_wrong_native_identity_never_unlocks_a_request(tmp_path, identity_update):
    async def check():
        manager, backend, _, wid = await protected(tmp_path)
        original = backend.dispatch

        async def dispatch(instance, operation, payload):
            if operation == "submit":
                value = receipt(backend, wid, payload["request_id"])
                value["native_identity"].update(identity_update)
                return value
            return await original(instance, operation, payload)

        backend.dispatch = dispatch
        with pytest.raises(ValueError, match="INVALID_NOT_SENT_RECEIPT"):
            await manager.submit(wid, "get_phases", {}, "wrong-identity")
        assert (await manager.list())["workspaces"][0]["requests"]["wrong-identity"]["status"] == "unknown"
    asyncio.run(check())


def test_foreign_owner_cannot_inspect_cached_rejection_or_retry_it(tmp_path):
    async def check():
        manager, backend, owner, wid = await protected(tmp_path)
        original = backend.dispatch

        async def dispatch(instance, operation, payload):
            if operation == "submit":
                return receipt(backend, wid, payload["request_id"])
            return await original(instance, operation, payload)

        backend.dispatch = dispatch
        await manager.submit(wid, "get_phases", {}, "denied")
        owner[0] = "openai:foreign-owner"
        for action in (manager.status(wid, "denied"), manager.cancel(wid, "denied"),
                       manager.submit(wid, "get_phases", {}, "denied")):
            with pytest.raises(ValueError, match="WORKSPACE_NOT_OWNED"):
                await action
        assert (await manager.list())["workspaces"] == []
    asyncio.run(check())


def test_late_not_sent_receipt_cannot_replace_native_terminal_success(tmp_path):
    async def check():
        manager, backend, _, wid = await protected(tmp_path)
        original = backend.dispatch
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def dispatch(instance, operation, payload):
            if operation == "submit":
                await original(instance, operation, payload)
                entered.set()
                await proceed.wait()
                return receipt(backend, wid, payload["request_id"])
            return await original(instance, operation, payload)

        backend.dispatch = dispatch
        pending = asyncio.create_task(manager.submit(wid, "get_phases", {}, "racy"))
        await asyncio.wait_for(entered.wait(), timeout=3)
        backend.requests["racy"]["status"] = "succeeded"
        await manager.status(wid, "racy")
        proceed.set()
        with pytest.raises(ValueError, match="NOT_SENT_RECEIPT_CONFLICT"):
            await pending
        assert (await manager.list())["workspaces"][0]["requests"]["racy"]["status"] == "succeeded"
    asyncio.run(check())


@pytest.mark.parametrize("structured", [False, True])
def test_mcp_wire_receipt_through_real_node_backend_adapter(tmp_path, structured):
    async def check():
        manager, native, _, wid = await protected(tmp_path)
        remote = FastMCP("isolated Node receipt boundary")
        seen = []

        @remote.tool(name="revit_workspace_dispatch")
        async def dispatch(instance_id: str, port: int, operation: str, payload: dict,
                           expected_native_identity: dict):
            assert payload.get("owner_proof") == native.owner_proofs[wid] or operation == "hello"
            assert expected_native_identity["pid"] == native.instance["pid"]
            seen.append(operation)
            value = (receipt(native, wid, payload["request_id"]) if operation == "submit"
                     else await native.dispatch(native.instance, operation, payload))
            if structured:
                return value
            # Node rawToolResponse uses a single JSON text block, not a dict.
            return ToolResult(content=[TextContent(type="text", text=json.dumps(value))])

        manager.backend = NodeWorkspaceBackend(lambda: remote)
        result = await manager.submit(wid, "get_phases", {}, "wire-denial")
        assert result["status"] == "rejected"
        assert result["dispatch_state"] == "not_sent"
        assert seen == ["hello", "submit"]
        assert (await manager.status(wid, "wire-denial"))["requests"]["wire-denial"] == result
        assert seen == ["hello", "submit"], "local rejection must not poll nonexistent native work"
    asyncio.run(check())
