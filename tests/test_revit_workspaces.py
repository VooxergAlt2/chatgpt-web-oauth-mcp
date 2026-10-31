"""Linux-only workspace coordination checks; no Windows/network dependencies."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path

import pytest
from fastmcp import Client, FastMCP

from chatgpt_web_oauth_mcp.revit_workspace_backend import NodeWorkspaceBackend
from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager, document_key, workspace_tools
from chatgpt_web_oauth_mcp.session import bind_session, reset_session_binding
from chatgpt_web_oauth_mcp.tool_groups import register_tool_groups


class FakeBackend:
    def __init__(self):
        self.instance = dict(instance_id="native-1", pid=51, process_started_utc="2026-10-31T10:00:00Z",
                             revit_version="2023", plugin_build_id="build-1", port=19001,
                             protocol_version=1, capabilities=["document_context_v1", "workspace_queue_v1"])
        self.paths = {}
        self.requests = {}
        self.calls = []
        self.bind_status = "succeeded"
        self.submit_status = "queued"
        self.unbind_status = "succeeded"
        self.activate_status = "succeeded"
        self.timeout_bind = False
        self.timeout_submit = False
        self.fixed_path = None
        self.serial = 0

    async def discover(self):
        return {"instances": [deepcopy(self.instance)]}

    async def prepare(self, workspace_id, source_path):
        path = self.fixed_path or rf"C:\QA\{workspace_id}\copy.rvt"
        self.paths[workspace_id] = path
        return {"workspace_id": workspace_id, "document_path": path}

    async def dispatch(self, instance, operation, payload):
        self.calls.append((operation, deepcopy(payload)))
        if operation == "hello":
            return deepcopy(self.instance)
        if operation in {"bind", "activate", "submit", "unbind"}:
            self.serial += 1
            ws = payload["workspace_id"]
            rid = payload.get("request_id", f"{operation}:{ws}")
            result = dict(request_id=rid, workspace_id=ws, instance_id=self.instance["instance_id"],
                          document_path=self.paths[ws], status=getattr(self, operation + "_status"))
            self.requests[rid] = result
            if operation == "submit" and self.timeout_submit:
                raise TimeoutError("response lost; native keeps running")
            if operation == "bind" and self.timeout_bind:
                raise TimeoutError("bind receipt lost")
            return deepcopy(result)
        result = self.requests[payload["request_id"]]
        # cancel is an acknowledgment, NOT a guarantee of cancellation.
        return deepcopy(result)


def setup(tmp_path):
    backend = FakeBackend()
    owner = ["openai:owner-a"]
    manager = WorkspaceManager(tmp_path / "workspaces.json", backend, owner_provider=lambda: owner[0])
    return manager, backend, owner


async def create(manager):
    return await manager.create(r"C:\Sources\immutable.rvt", "native-1")


def test_two_owners_two_documents_persistence_and_wrong_owner(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        first = await create(manager)
        owner[0] = "openai:owner-b"
        second = await create(manager)
        assert first["document_path"] != second["document_path"]
        assert first["workspace_id"] != second["workspace_id"]
        assert first["state"] == second["state"] == "ready"
        assert [r["workspace_id"] for r in (await manager.list())["workspaces"]] == [second["workspace_id"]]
        for action in [lambda: manager.submit(first["workspace_id"], "send_code_to_revit", {}),
                       lambda: manager.status(first["workspace_id"]),
                       lambda: manager.cancel(first["workspace_id"], "r"),
                       lambda: manager.release(first["workspace_id"]),
                       lambda: manager.acquire_ui(first["workspace_id"])]:
            with pytest.raises(ValueError, match="NOT_OWNED"):
                await action()
        restarted = WorkspaceManager(manager.path, backend, owner_provider=lambda: owner[0])
        assert (await restarted.list())["workspaces"][0]["workspace_id"] == second["workspace_id"]
        await restarted.submit(second["workspace_id"], "send_code_to_revit", {"code": "debug"}, "b-1")
        owner[0] = "openai:owner-a"
        await restarted.submit(first["workspace_id"], "send_code_to_revit", {"code": "test"}, "a-1")
        assert [p["workspace_id"] for op, p in backend.calls if op == "submit"] == [second["workspace_id"], first["workspace_id"]]
        with pytest.raises(ValueError, match="REQUEST_NOT_FOUND"):
            await restarted.cancel(first["workspace_id"], "b-1")
        raw = json.loads(manager.path.read_text())
        assert len(raw["workspaces"]) == 2
    asyncio.run(check())


def test_concurrent_status_does_not_get_overwritten_by_late_create_timeout(tmp_path):
    async def check():
        manager, backend, _ = setup(tmp_path)
        entered = asyncio.Event()
        release = asyncio.Event()
        original = backend.dispatch

        async def gated_dispatch(instance, operation, payload):
            result = await original(instance, operation, payload)
            if operation == "bind":
                entered.set()
                await release.wait()
                raise TimeoutError("original bind response lost after concurrent status")
            return result

        backend.dispatch = gated_dispatch
        creating = asyncio.create_task(create(manager))
        await asyncio.wait_for(entered.wait(), timeout=3)
        workspace = (await manager.list())["workspaces"][0]
        wid = workspace["workspace_id"]
        reconciled = await manager.status(wid, "bind:" + wid)
        assert reconciled["state"] == "ready"
        assert reconciled["requests"]["bind:" + wid]["status"] == "succeeded"
        release.set()
        with pytest.raises(TimeoutError, match="original bind response lost"):
            await creating
        verified = (await manager.list())["workspaces"][0]
        assert verified["state"] == "ready", "Confirmed native bind regressed to unknown"
        assert (await manager.status(wid, "bind:" + wid))["state"] == "ready"

    asyncio.run(check())


def test_reserved_lifecycle_request_ids_cannot_override_submit_or_release(tmp_path):
    async def check():
        manager, _, _ = setup(tmp_path)
        wid = (await create(manager))["workspace_id"]
        for request_id in ("bind:" + wid, "unbind:" + wid, "activate:" + wid,
                           "unbind:other-id", "activate:arbitrary"):
            with pytest.raises(ValueError, match="RESERVED_REQUEST_ID"):
                await manager.submit(wid, "get_project_info", {}, request_id)
        valid = await manager.submit(wid, "get_project_info", {}, "client:req-1")
        assert valid["request_id"] == "client:req-1"
        # Native reports the operation complete before cleanup.
        backend = manager.backend
        backend.requests["client:req-1"]["status"] = "succeeded"
        await manager.status(wid, "client:req-1")
        released = await manager.release(wid)
        assert released["state"] == "released"
        assert released["requests"]["bind:" + wid]["operation"] == "bind"
        assert released["requests"]["unbind:" + wid]["operation"] == "unbind"
    asyncio.run(check())


def test_lost_bind_receipt_recovers_by_deterministic_native_id(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        backend.timeout_bind = True
        with pytest.raises(TimeoutError):
            await create(manager)
        row = (await manager.list())["workspaces"][0]
        assert row["state"] == "unknown"
        assert row["lifecycle_request_id"] == "bind:" + row["workspace_id"]
        restarted = WorkspaceManager(manager.path, backend, owner_provider=lambda: owner[0])
        assert (await restarted.status(row["workspace_id"]))["state"] == "ready"
        assert len([op for op, _ in backend.calls if op == "bind"]) == 1
    asyncio.run(check())


def test_ui_lease_allows_gui_selection_before_native_document_verification(tmp_path):
    async def check():
        manager, backend, _ = setup(tmp_path)
        wid = (await create(manager))["workspace_id"]
        backend.activate_status = "queued"
        lease = await manager.acquire_ui(wid)
        assert lease["state"] == "idle" and lease["verified"] is False
        assert lease["activation_request_id"] is None
        assert [op for op, _ in backend.calls if op == "activate"] == []
        # Reserve the Windows desktop first, then allow the GUI to select RVT.
        args, token = await manager.begin_remote("windows", "windows_Click", {
            "_workspace_id": wid, "_lease_token": lease["token"],
        })
        assert args == {} and token != lease["token"]
        manager.complete_remote(token)
        pending = await manager.verify_active(wid, lease["token"])
        assert pending["state"] == "verifying" and pending["verified"] is False
        with pytest.raises(ValueError, match="UI_LEASE_REQUIRED"):
            await manager.begin_remote("windows", "windows_Click", {
                "_workspace_id": wid, "_lease_token": lease["token"],
            })
        backend.requests[pending["activation_request_id"]]["status"] = "succeeded"
        status = await manager.status(wid, pending["activation_request_id"])
        assert status["ui_lease"]["state"] == "idle"
        assert status["ui_lease"]["verified"] is True
        assert status["ui_lease"]["token"] == lease["token"]
        args, token = await manager.begin_remote("windows", "windows_Click", {
            "_workspace_id": wid, "_lease_token": lease["token"],
        })
        manager.complete_remote(token)
        assert (await manager.status(wid))["ui_lease"]["verified"] is False
        await manager.release_ui(wid, lease["token"])
    asyncio.run(check())


def test_wrong_document_verification_does_not_grant_or_leak_another_chat_ui(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        a = (await create(manager))["workspace_id"]
        owner[0] = "openai:owner-b"
        b = (await create(manager))["workspace_id"]
        owner[0] = "openai:owner-a"

        lease = await manager.acquire_ui(a)
        backend.activate_status = "failed"
        failed = await manager.verify_active(a, lease["token"])
        assert failed["state"] == "activation_failed"
        assert failed["verified"] is False
        with pytest.raises(ValueError, match="UI_LEASE_REQUIRED"):
            await manager.begin_remote("windows", "windows_Click", {
                "_workspace_id": a, "_lease_token": lease["token"],
            })
        owner[0] = "openai:owner-b"
        with pytest.raises(ValueError, match="BUSY"):
            await manager.acquire_ui(b)
        with pytest.raises(ValueError, match="NOT_OWNED"):
            await manager.release_ui(a, lease["token"])

        owner[0] = "openai:owner-a"
        await manager.release_ui(a, lease["token"])
        owner[0] = "openai:owner-b"
        backend.activate_status = "succeeded"
        lease_b = await manager.acquire_ui(b)
        verified = await manager.verify_active(b, lease_b["token"])
        assert verified["state"] == "idle" and verified["verified"] is True
        await manager.release_ui(b, lease_b["token"])

    asyncio.run(check())


def test_bind_is_async_and_release_is_async_without_saving(tmp_path):
    async def check():
        manager, backend, _ = setup(tmp_path)
        backend.bind_status = "queued"
        ws = await create(manager)
        wid = ws["workspace_id"]
        assert ws["state"] == "binding"
        with pytest.raises(ValueError, match="NOT_READY"):
            await manager.submit(wid, "debug", {})
        with pytest.raises(ValueError, match="BUSY"):
            await manager.release(wid)
        backend.requests[ws["lifecycle_request_id"]]["status"] = "succeeded"
        assert (await manager.status(wid))["state"] == "ready"
        backend.unbind_status = "running"
        released = await manager.release(wid)
        assert released["state"] == "releasing"
        backend.requests[released["lifecycle_request_id"]]["status"] = "succeeded"
        assert (await manager.status(wid))["state"] == "released"
        assert [op for op, _ in backend.calls if op not in {"hello", "status"}] == ["bind", "unbind"]
    asyncio.run(check())


def test_windows_path_alias_rejected_across_owners(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        backend.fixed_path = r"C:\QA\Folder\copy.rvt"
        await create(manager)
        owner[0] = "openai:owner-b"
        backend.fixed_path = "c:/qa/folder/other/../COPY.rvt"
        with pytest.raises(ValueError, match="ALREADY_REGISTERED"):
            await create(manager)
        assert len([op for op, _ in backend.calls if op == "bind"]) == 1
        assert (await manager.list())["workspaces"][0]["state"] == "unknown"
        assert document_key(r"C:\QA\Folder\copy.rvt") == document_key(backend.fixed_path)
    asyncio.run(check())


@pytest.mark.parametrize("path", [r"relative.rvt", r"C:relative.rvt", r"C:\a.rvt:stream", r"C:\dir.\a.rvt", r"\\?\C:\a.rvt", r"\\server\share\x.rvt"])
def test_ambiguous_paths_fail_closed(path):
    with pytest.raises(ValueError):
        document_key(path)


def test_idempotency_unknown_and_late_native_terminal_after_restart(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        wid = (await create(manager))["workspace_id"]
        backend.timeout_submit = True
        with pytest.raises(TimeoutError):
            await manager.submit(wid, "send_code_to_revit", {"code": "x"}, "r1")
        manager = WorkspaceManager(manager.path, backend, owner_provider=lambda: owner[0])
        assert (await manager.submit(wid, "send_code_to_revit", {"code": "x"}, "r1"))["status"] == "unknown"
        with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
            await manager.submit(wid, "send_code_to_revit", {"code": "y"}, "r1")
        with pytest.raises(ValueError, match="UNCERTAIN"):
            await manager.submit(wid, "send_code_to_revit", {"code": "x"}, "new-id")
        with pytest.raises(ValueError, match="BUSY"):
            await manager.release(wid)
        with pytest.raises(ValueError, match="BUSY"):
            await manager.acquire_ui(wid)
        assert len([op for op, _ in backend.calls if op == "submit"]) == 1
        backend.requests["r1"]["status"] = "running"
        await manager.status(wid, "r1")
        assert (await manager.cancel(wid, "r1"))["status"] == "running"
        with pytest.raises(ValueError, match="BUSY"):
            await manager.release(wid)
        backend.requests["r1"]["status"] = "succeeded"
        assert (await manager.status(wid, "r1"))["requests"]["r1"]["status"] == "succeeded"
        assert (await manager.release(wid))["state"] == "released"
    asyncio.run(check())


def test_multiple_known_requests_queue_but_desktop_lease_excludes_native_work(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        a = (await create(manager))["workspace_id"]
        owner[0] = "openai:owner-b"
        b = (await create(manager))["workspace_id"]
        await manager.submit(b, "debug", {}, "b")
        owner[0] = "openai:owner-a"
        await manager.submit(a, "debug", {}, "a")
        with pytest.raises(ValueError, match="BUSY"):
            await manager.acquire_ui(a)
        for rid in ["a", "b"]:
            backend.requests[rid]["status"] = "succeeded"
        await manager.status(a, "a")
        owner[0] = "openai:owner-b"
        await manager.status(b, "b")
        lease = await manager.acquire_ui(b)
        owner[0] = "openai:owner-a"
        with pytest.raises(ValueError, match="BUSY"):
            await manager.submit(a, "debug", {}, "a2")
        with pytest.raises(ValueError, match="BUSY"):
            await create(manager)
        with pytest.raises(ValueError, match="NOT_OWNED"):
            await manager.begin_remote("windows", "windows_Click", {"_workspace_id": b, "_lease_token": lease["token"]})
        owner[0] = "openai:owner-b"
        args, token = await manager.begin_remote("windows", "windows_Click", {
            "x": 1, "_workspace_id": b, "_lease_token": lease["token"],
        })
        assert args == {"x": 1}
        with pytest.raises(ValueError, match="UI_LEASE_REQUIRED"):
            await manager.begin_remote("windows", "windows_Click", {"_workspace_id": b, "_lease_token": token})
        manager.complete_remote(token)
        await manager.release_ui(b, lease["token"])
        await manager.submit(b, "debug", {}, "b2")
    asyncio.run(check())


def test_generic_ui_timeout_keeps_draining_across_restart_and_expiry(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        wid = (await create(manager))["workspace_id"]
        lease = await manager.acquire_ui(wid)
        _, fence = await manager.begin_remote("windows", "windows_PowerShell", {
            "command": "anything", "_workspace_id": wid, "_lease_token": lease["token"],
        })
        manager.uncertain_remote(fence)
        restarted = WorkspaceManager(manager.path, backend, owner_provider=lambda: owner[0], clock=lambda: 1e20)
        with pytest.raises(ValueError, match="DRAINING"):
            await restarted.release_ui(wid, lease["token"])
        with pytest.raises(ValueError, match="BUSY"):
            await restarted.submit(wid, "debug", {}, "r")
        assert await restarted.begin_remote("windows", "windows_Screenshot", {}) == ({}, None)
        assert await restarted.begin_remote("windows", "windows_ControlStatus", {}) == ({}, None)
    asyncio.run(check())


def test_ui_recovery_catalog_marks_status_as_readonly_only():
    tools = {tool.name: tool for tool in workspace_tools()}
    status = tools["revit_workspace_ui_status"].annotations.model_dump(by_alias=True)
    recovery = tools["revit_workspace_recover_ui"].annotations.model_dump(by_alias=True)
    assert status["readOnlyHint"] is True and status["destructiveHint"] is False
    assert recovery["readOnlyHint"] is False and recovery["destructiveHint"] is True


def test_operator_reconciles_only_the_exact_unscoped_ui_fence(tmp_path):
    async def check():
        manager, _, owner = setup(tmp_path)
        args, token = await manager.begin_remote("windows", "windows_App", {
            "mode": "switch", "name": "QA Revit",
        })
        assert args == {"mode": "switch", "name": "QA Revit"} and token
        running = await manager.ui_status()
        assert running["state"] == "inflight" and not running["recoverable"]
        with pytest.raises(ValueError, match="UI_NOT_DRAINING"):
            await manager.recover_ui(running["fence_id"], True, "Windows inspected but transport still waiting")
        manager.uncertain_remote(token)
        snapshot = await manager.ui_status()
        assert snapshot["state"] == "draining" and snapshot["scope"] == "unscoped"
        assert snapshot["recoverable"] and snapshot["operation"] == "windows_App"
        assert snapshot["owner_matches"] and len(snapshot["fence_id"]) == 24
        assert token not in json.dumps(snapshot)
        with pytest.raises(ValueError, match="OPERATOR_CONFIRMATION_REQUIRED"):
            await manager.recover_ui(snapshot["fence_id"], False, "Windows screenshot checked")
        with pytest.raises(ValueError, match="UI_FENCE_CHANGED"):
            await manager.recover_ui("not-the-current-fence", True, "Windows ControlStatus and screenshot checked")
        owner[0] = "openai:other-chat"
        assert (await manager.ui_status())["recoverable"] is False
        with pytest.raises(ValueError, match="UI_FENCE_NOT_OWNED"):
            await manager.recover_ui(snapshot["fence_id"], True, "Another chat looked at desktop")
        owner[0] = "openai:owner-a"
        with pytest.raises(ValueError, match="BUSY"):
            manager.require_unscoped_allowed()
        recovered = await manager.recover_ui(
            snapshot["fence_id"], True,
            "Windows ControlStatus reports ready, screenshot shows QA model and no modal dialogs",
        )
        assert recovered == {"recovered": True, "state": "clear", "audit_entries": 1}
        assert (await manager.ui_status())["state"] == "clear"
        manager.require_unscoped_allowed()
        with manager._state() as state:
            assert len(state["ui_recoveries"]) == 1
            assert state["ui_recoveries"][0]["operation"] == "windows_App"
        # Legacy records with owner=None still require an exact fence ID and
        # acknowledgement, even if the originating MCP session has expired.
        _, token2 = await manager.begin_remote("windows", "windows_PowerShell", {
            "command": "Get-Date",
        })
        manager.uncertain_remote(token2)
        with manager._state() as state:
            state["ui"]["owner"] = None
        legacy = await manager.ui_status()
        assert legacy["scope"] == "unscoped" and legacy["recoverable"]
        assert (await manager.recover_ui(
            legacy["fence_id"], True,
            "Legacy unscoped call ended, desktop visually checked by operator",
        ))["recovered"]
        assert token2 not in json.dumps(await manager.ui_status())
    asyncio.run(check())


def test_scoped_recovery_preserves_owner_lease_and_requires_native_verification(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        workspace = (await create(manager))["workspace_id"]
        lease = await manager.acquire_ui(workspace)
        _, fence = await manager.begin_remote("windows", "windows_Click", {
            "_workspace_id": workspace, "_lease_token": lease["token"], "x": 500,
        })
        manager.uncertain_remote(fence)
        snapshot = await manager.ui_status()
        assert snapshot["state"] == "draining" and snapshot["scope"] == "workspace"
        assert snapshot["recoverable"] is True
        owner[0] = "openai:other-chat"
        assert (await manager.ui_status())["recoverable"] is False
        with pytest.raises(ValueError, match="UI_FENCE_NOT_OWNED"):
            await manager.recover_ui(
                snapshot["fence_id"], True,
                "Cannot reconcile another chat's model with GUI heuristics",
            )
        owner[0] = "openai:owner-a"
        with pytest.raises(ValueError, match="UI_DRAINING"):
            await manager.release_ui(workspace, lease["token"])
        recovered = await manager.recover_ui(snapshot["fence_id"], True,
            "ControlStatus ready; screenshot inspected; GUI action ended without a modal dialog")
        assert recovered["state"] == "idle" and recovered["verified"] is False
        assert recovered["workspace_id"] == workspace
        assert backend.calls[-1][0] == "hello"
        with pytest.raises(ValueError, match="BUSY"):
            await manager.submit(workspace, "debug", {}, "still-fenced")
        verified = await manager.verify_active(workspace, lease["token"])
        assert verified["verified"] is True
        await manager.release_ui(workspace, lease["token"])
        assert (await manager.release(workspace))["state"] == "released"
    asyncio.run(check())


def test_scoped_recovery_rejects_replaced_native_instance_and_preserves_fence(tmp_path):
    async def check():
        manager, backend, _ = setup(tmp_path)
        wid = (await create(manager))["workspace_id"]
        lease = await manager.acquire_ui(wid)
        _, fence = await manager.begin_remote("windows", "windows_Click", {
            "_workspace_id": wid, "_lease_token": lease["token"],
        })
        manager.uncertain_remote(fence)
        snapshot = await manager.ui_status()
        backend.instance["instance_id"] = "replacement"
        with pytest.raises(ValueError, match="INSTANCE_REPLACED"):
            await manager.recover_ui(snapshot["fence_id"], True, "Desktop inspection cannot prove native identity")
        assert (await manager.ui_status())["state"] == "draining"
    asyncio.run(check())


def test_late_remote_completion_cannot_clear_a_new_call_under_same_lease(tmp_path):
    async def check():
        manager, _, _ = setup(tmp_path)
        wid = (await create(manager))["workspace_id"]
        lease = await manager.acquire_ui(wid)
        args = {"_workspace_id": wid, "_lease_token": lease["token"]}
        _, old_call = await manager.begin_remote("windows", "windows_Click", args)
        manager.uncertain_remote(old_call)
        snapshot = await manager.ui_status()
        await manager.recover_ui(snapshot["fence_id"], True, "Old GUI call ended; ControlStatus and screenshot inspected")
        _, new_call = await manager.begin_remote("windows", "windows_Click", args)
        assert new_call != old_call and new_call != lease["token"]
        manager.complete_remote(old_call)
        manager.uncertain_remote(old_call)
        assert (await manager.ui_status())["state"] == "inflight"
        manager.complete_remote(new_call)
        await manager.release_ui(wid, lease["token"])
    asyncio.run(check())


def test_dead_controller_only_marks_inflight_uncertain_and_never_clears_it(tmp_path, monkeypatch):
    async def check():
        manager, _, _ = setup(tmp_path)
        _, fence = await manager.begin_remote("windows", "windows_Click", {})
        import chatgpt_web_oauth_mcp.revit_workspaces as module
        monkeypatch.setattr(module, "process_identity_matches", lambda *args: None)
        assert (await manager.ui_status())["state"] == "inflight"
        monkeypatch.setattr(module, "process_identity_matches", lambda *args: False)
        snapshot = await manager.ui_status()
        assert snapshot["state"] == "draining" and snapshot["recoverable"]
        with pytest.raises(ValueError, match="BUSY"):
            manager.require_unscoped_allowed()
        await manager.recover_ui(snapshot["fence_id"], True, "Controller process gone; Windows desktop independently inspected")
        assert (await manager.ui_status())["state"] == "clear"
    asyncio.run(check())


@pytest.mark.parametrize("field,value", [("pid", 52), ("plugin_build_id", "build-2"),
                                         ("process_started_utc", "new-start"), ("instance_id", "replacement"),
                                         ("capabilities", []), ("protocol_version", 2)])
def test_replaced_native_identity_or_capabilities_blocks_submit(tmp_path, field, value):
    async def check():
        manager, backend, _ = setup(tmp_path)
        wid = (await create(manager))["workspace_id"]
        backend.instance[field] = value
        with pytest.raises(ValueError, match="REPLACED|PROTOCOL_REQUIRED"):
            await manager.submit(wid, "debug", {}, "r")
        assert not [op for op, _ in backend.calls if op == "submit"]
        assert (await manager.list())["workspaces"][0]["state"] == "fenced"
        with pytest.raises(ValueError, match="BUSY"):
            await manager.release(wid)
    asyncio.run(check())


def test_bad_capabilities_prevent_copy_and_transient_owner_is_labelled(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        backend.instance["capabilities"] = []
        with pytest.raises(ValueError, match="PROTOCOL_REQUIRED"):
            await create(manager)
        assert not backend.paths
        backend.instance["capabilities"] = ["document_context_v1", "workspace_queue_v1"]
        owner[0] = "__local__"
        with pytest.raises(ValueError, match="CHAT_OWNER_REQUIRED"):
            await create(manager)
        owner[0] = "mcp:transient"
        row = await create(manager)
        assert row["owner_scope"] == "transport" and "reconnect_limitation" in row
        owner[0] = "mcp:reconnected"
        assert (await manager.list())["workspaces"] == []
    asyncio.run(check())


def test_node_backend_exact_contract_and_json_result(tmp_path):
    async def check():
        remote = FastMCP("fake Node")
        seen = []
        @remote.tool(name="revit_workspace_discover")
        def discover():
            return {"instances": []}
        @remote.tool(name="revit_workspace_prepare")
        def prepare(workspace_id: str, source_path: str):
            seen.append((workspace_id, source_path))
            return {"workspace_id": workspace_id, "document_path": r"C:\QA\copy.rvt"}
        @remote.tool(name="revit_workspace_dispatch")
        def dispatch(instance_id: str, port: int, operation: str, payload: dict):
            seen.append((instance_id, port, operation, payload))
            return {"ok": True}
        backend = NodeWorkspaceBackend(lambda: remote)
        assert await backend.discover() == {"instances": []}
        await backend.prepare("ws", r"C:\source.rvt")
        await backend.dispatch({"instance_id": "i", "port": 123}, "status", {"request_id": "r"})
        assert seen == [("ws", r"C:\source.rvt"), ("i", 123, "status", {"request_id": "r"})]
    asyncio.run(check())


def test_private_router_virtual_schemas_and_crossgroup_fence(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        remote, public = FastMCP("Windows"), FastMCP("public")
        seen = []
        @remote.tool(name="windows_Click")
        def click(x: int):
            seen.append(x)
            return {"clicked": x}
        @remote.tool(name="revit_get_project_info")
        def info():
            return {"unsafe_active_document": True}
        register_tool_groups(public, transport_factory=lambda: remote, workspace_manager=manager)
        async with Client(public) as client:
            assert {t.name for t in await client.list_tools()} == {"tool_group_select", "tool_group_catalog", "tool_group_call"}
            catalog = await client.call_tool("tool_group_catalog", {"group": "revit", "limit": 20})
            assert {t["name"] for t in catalog.structured_content["tools"]} == (
                {t.name for t in workspace_tools()} |
                {"revit_get_project_info", "revit_get_phases", "revit_get_selected_elements"}
            )
            async def call(name, arguments=None, group="revit"):
                return await client.call_tool("tool_group_call", {"group": group, "name": name, "arguments": arguments or {}}, raise_on_error=False)
            created = await call("revit_workspace_create", {"source_path": r"C:\source.rvt", "instance_id": "native-1"})
            assert not created.is_error, created
            wid = created.structured_content["workspace_id"]
            for name in ["revit_get_project_info", "revit_send_code_to_revit"]:
                result = await call(name)
                assert result.is_error and "WORKSPACE_REQUIRED" in result.content[0].text
            assert (await call("windows_Click", {"x": 1}, "windows")).is_error
            lease = (await call("revit_workspace_acquire_ui", {"workspace_id": wid})).structured_content
            clicked = await call("windows_Click", {"x": 2, "_workspace_id": wid, "_lease_token": lease["token"]}, "windows")
            assert not clicked.is_error and seen == [2]
            await call("revit_workspace_release_ui", {"workspace_id": wid, "token": lease["token"]})
            submitted = await call("revit_workspace_submit", {"workspace_id": wid, "command": "send_code_to_revit", "arguments": {"code": "debug"}, "request_id": "r"})
            assert not submitted.is_error
            assert (await call("revit_workspace_acquire_ui", {"workspace_id": wid})).is_error
            extra = await call("revit_workspace_status", {"workspace_id": wid, "oops": True})
            assert extra.is_error
    asyncio.run(check())


def test_unknown_native_status_and_cancel_cannot_unlock_or_trigger_resubmit(tmp_path):
    async def check():
        manager, backend, _ = setup(tmp_path)
        wid = (await create(manager))["workspace_id"]
        await manager.submit(wid, "debug", {}, "r")
        backend.requests["r"]["status"] = "unknown"
        with pytest.raises(ValueError, match="INVALID_NATIVE_ENVELOPE"):
            await manager.status(wid, "r")
        with pytest.raises(ValueError, match="UNCERTAIN"):
            await manager.submit(wid, "debug", {}, "r-new")
        with pytest.raises(ValueError, match="BUSY"):
            await manager.release(wid)
        backend.requests["r"]["status"] = "cancelled"
        await manager.status(wid, "r")
        assert (await manager.release(wid))["state"] == "released"
    asyncio.run(check())


def test_router_windows_error_retains_drain_and_schema_strips_reserved_fields(tmp_path):
    async def check():
        manager, backend, _ = setup(tmp_path)
        wid = (await create(manager))["workspace_id"]
        lease = await manager.acquire_ui(wid)
        remote, public = FastMCP("remote"), FastMCP("public")
        @remote.tool(name="windows_Click")
        def click(x: int):
            raise ConnectionError("UI completion unknown")
        register_tool_groups(public, transport_factory=lambda: remote, workspace_manager=manager)
        async with Client(public) as client:
            result = await client.call_tool("tool_group_call", {
                "group": "windows", "name": "windows_Click", "arguments": {
                    "x": 1, "_workspace_id": wid, "_lease_token": lease["token"],
                }}, raise_on_error=False)
            assert result.is_error
        with pytest.raises(ValueError, match="DRAINING"):
            await manager.release_ui(wid, lease["token"])
        with pytest.raises(ValueError, match="BUSY"):
            await manager.submit(wid, "debug", {})
    asyncio.run(check())


def test_windows_powershell_timeout_in_success_envelope_retains_gui_fence(tmp_path):
    async def check():
        manager, _, _ = setup(tmp_path)
        remote, public = FastMCP("mock Windows"), FastMCP("public")
        @remote.tool(name="windows_PowerShell")
        def powershell(command: str):
            return {"result": "Response: Command execution timed out\nStatus Code: 1"}

        register_tool_groups(public, transport_factory=lambda: remote, workspace_manager=manager)
        async with Client(public) as client:
            result = await client.call_tool(
                "tool_group_call", {
                    "group": "windows", "name": "windows_PowerShell",
                    "arguments": {"command": "Read-OnlyQAOperation"},
                }, raise_on_error=False,
            )
            assert result.is_error
            assert "Command execution timed out" in str(result.content)
            fence = await client.call_tool(
                "tool_group_call", {
                    "group": "revit", "name": "revit_workspace_ui_status",
                    "arguments": {},
                },
            )
            assert fence.structured_content["state"] == "draining"
            assert fence.structured_content["recoverable"]
            with pytest.raises(ValueError, match="BUSY"):
                manager.require_unscoped_allowed()
            another = await client.call_tool(
                "tool_group_call", {
                    "group": "windows", "name": "windows_PowerShell",
                    "arguments": {"command": "AnotherOperation"},
                }, raise_on_error=False,
            )
            assert another.is_error
        snapshot = await manager.ui_status()
        await manager.recover_ui(
            snapshot["fence_id"], True,
            "Windows ControlStatus ready and screenshot proves desktop idle after timeout",
        )
        manager.require_unscoped_allowed()

    asyncio.run(check())


def test_canonical_session_binding_and_no_local_fallback(tmp_path):
    async def check():
        backend = FakeBackend()
        manager = WorkspaceManager(tmp_path / "state.json", backend)
        token = bind_session(None)
        try:
            with pytest.raises(ValueError, match="CHAT_OWNER_REQUIRED"):
                await manager.list()
        finally:
            reset_session_binding(token)
        token = bind_session("openai:stable")
        try:
            row = await create(manager)
        finally:
            reset_session_binding(token)
        token = bind_session("openai:another")
        try:
            with pytest.raises(ValueError, match="NOT_OWNED"):
                await manager.status(row["workspace_id"])
        finally:
            reset_session_binding(token)
        token = bind_session("openai:stable")
        try:
            assert len((await manager.list())["workspaces"]) == 1
        finally:
            reset_session_binding(token)
    asyncio.run(check())
