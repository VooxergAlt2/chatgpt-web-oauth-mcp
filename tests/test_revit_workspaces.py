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
        self.authorization_allowed = False
        self.authorized_workspace_ids = set()
        self.authorization_workspace_override = None
        self.replace_identity_after_authorization = False
        self.owner_proofs = {}
        self.serial = 0

    async def discover(self):
        return {"instances": [deepcopy(self.instance)]}

    async def prepare(self, workspace_id, source_path, owner_proof=None):
        self.owner_proofs[workspace_id] = owner_proof
        path = self.fixed_path or rf"C:\QA\{workspace_id}\copy.rvt"
        self.paths[workspace_id] = path
        return {"workspace_id": workspace_id, "document_path": path}

    async def dispatch(self, instance, operation, payload):
        self.calls.append((operation, deepcopy(payload)))
        if operation == "hello":
            return deepcopy(self.instance)
        if "native_authorization_v1" in self.instance["capabilities"]:
            assert payload.get("owner_proof") == self.owner_proofs.get(payload.get("workspace_id")), (
                "Protected native request missing the owning chat's capability"
            )
        if operation == "authorization_check":
            authorized = payload["workspace_id"] in self.authorized_workspace_ids
            if self.replace_identity_after_authorization and authorized:
                self.instance["pid"] += 1
            return {"workspace_id": self.authorization_workspace_override or payload["workspace_id"],
                    "authorized": authorized}
        if operation in {"status", "cancel"}:
            assert payload.get("workspace_id") == self.requests[payload["request_id"]]["workspace_id"], (
                "Authenticated native status/cancel must carry exact owning workspace identity"
            )
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


def test_protected_workspace_prepare_then_user_approval_then_native_bind(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        backend.instance["capabilities"].append("native_authorization_v1")
        prepared = await create(manager)
        wid = prepared["workspace_id"]
        assert prepared["state"] == "prepared"
        assert prepared["document_path"]
        assert "owner_proof" not in prepared
        assert backend.owner_proofs[wid]
        assert backend.owner_proofs[wid] not in json.dumps(await manager.list())
        assert not [op for op, _ in backend.calls if op == "bind"]
        assert prepared["requests"] == {}

        # A prepared QA model needs desktop access BEFORE native binding,
        # but binding is disallowed while its desktop lease is held.
        lease = await manager.acquire_ui(wid)
        assert lease["workspace_id"] == wid
        with pytest.raises(ValueError, match="DESKTOP_BUSY"):
            await manager.bind_prepared(wid)
        await manager.release_ui(wid, lease["token"])
        with pytest.raises(ValueError, match="AUTHORIZATION_REQUIRED"):
            await manager.bind_prepared(wid)
        assert (await manager.list())["workspaces"][0]["state"] == "prepared"
        assert not [op for op, _ in backend.calls if op == "bind"]

        backend.authorized_workspace_ids.add(wid)  # explicit in-Revit approval delivered
        bound = await manager.bind_prepared(wid)
        assert bound["state"] == "ready"
        assert [op for op, _ in backend.calls if op == "bind"] == ["bind"]
        await manager.submit(wid, "get_project_info", {}, "qa-protected-1")
        await manager.status(wid, "qa-protected-1")
        await manager.cancel(wid, "qa-protected-1")
        for operation, payload in backend.calls:
            if operation in {"status", "cancel"}:
                assert payload["workspace_id"] == wid
        with pytest.raises(ValueError, match="WORKSPACE_NOT_PREPARED"):
            await manager.bind_prepared(wid)

    asyncio.run(check())


def test_prepared_protected_workspace_can_release_without_native_unbind(tmp_path):
    async def check():
        manager, backend, _ = setup(tmp_path)
        backend.instance["capabilities"].append("native_authorization_v1")
        prepared = await create(manager)
        assert prepared["state"] == "prepared"
        released = await manager.release(prepared["workspace_id"])
        assert released["state"] == "released"
        assert released["document_path"] == prepared["document_path"]
        assert not [op for op, _ in backend.calls if op in {"bind", "unbind"}]

    asyncio.run(check())


def test_protected_bind_refuses_foreign_grant_and_replaced_native_instance(tmp_path):
    async def check():
        manager, backend, _ = setup(tmp_path)
        backend.instance["capabilities"].append("native_authorization_v1")
        first = await create(manager)
        second = await create(manager)
        backend.authorized_workspace_ids.add(first["workspace_id"])
        with pytest.raises(ValueError, match="AUTHORIZATION_REQUIRED"):
            await manager.bind_prepared(second["workspace_id"])
        assert not any(op == "bind" for op, _ in backend.calls)

        backend.authorization_workspace_override = second["workspace_id"]
        with pytest.raises(ValueError, match="AUTHORIZATION_REQUIRED"):
            await manager.bind_prepared(first["workspace_id"])
        backend.authorization_workspace_override = None
        backend.replace_identity_after_authorization = True
        with pytest.raises(ValueError, match="INSTANCE_REPLACED"):
            await manager.bind_prepared(first["workspace_id"])
        assert (await manager.list())["workspaces"][0]["state"] == "prepared"
        assert not any(op == "bind" for op, _ in backend.calls)

    asyncio.run(check())


def test_protected_bind_lost_reply_is_reconciled_without_replay(tmp_path):
    async def check():
        manager, backend, _ = setup(tmp_path)
        backend.instance["capabilities"].append("native_authorization_v1")
        prepared = await create(manager)
        wid = prepared["workspace_id"]
        backend.authorized_workspace_ids.add(wid)
        backend.timeout_bind = True
        with pytest.raises(TimeoutError, match="bind receipt lost"):
            await manager.bind_prepared(wid)
        assert (await manager.list())["workspaces"][0]["state"] == "unknown"
        assert [op for op, _ in backend.calls if op == "bind"] == ["bind"]
        with pytest.raises(ValueError, match="WORKSPACE_NOT_PREPARED"):
            await manager.bind_prepared(wid)
        reconciled = await manager.status(wid, "bind:" + wid)
        assert reconciled["state"] == "ready"
        assert [op for op, _ in backend.calls if op == "bind"] == ["bind"]

    asyncio.run(check())


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


def test_headless_windows_reads_work_during_foreign_gui_fence(tmp_path):
    async def check():
        manager, _, owner = setup(tmp_path)
        _, token = await manager.begin_remote("windows", "windows_Click", {"x": 1})
        manager.uncertain_remote(token)
        owner[0] = "openai:other-chat"
        original = await manager.ui_status()
        assert original["state"] == "draining" and not original["owner_matches"]
        safe_calls = (
            ("windows_Process", {"mode": "list", "limit": 5}),
            ("windows_FileSystem", {"mode": "read", "path": r"C:\QA\report.txt"}),
            ("windows_FileSystem", {"mode": "list", "path": r"C:\QA"}),
            ("windows_FileSystem", {"mode": "info", "path": r"C:\QA"}),
            ("windows_DisplayInventory", {}),
            ("windows_ControlStatus", {}),
        )
        for name, args in safe_calls:
            actual, read_token = await manager.begin_remote("windows", name, args)
            assert actual == args, name
            if name == "windows_FileSystem":
                assert read_token
                manager.complete_remote(read_token)
            else:
                assert read_token is None, name
            assert (await manager.ui_status())["fence_id"] == original["fence_id"]
        unsafe_calls = (
            ("windows_Process", {"mode": "kill", "pid": 123}),
            ("windows_Process", {"mode": "list", "force": True}),
            ("windows_Process", {"mode": "list", "force": "false"}),
            ("windows_FileSystem", {"mode": "write", "path": r"C:\QA\report.txt", "content": "x"}),
            ("windows_FileSystem", {"mode": "read", "path": r"C:\QA\report.txt", "append": True}),
            ("windows_FileSystem", {"mode": "read", "path": r"C:\QA\report.txt", "destination": r"C:\secret.rvt"}),
            ("windows_FileSystem", {"mode": "search", "path": r"C:\QA"}),
            ("windows_App", {"mode": "switch", "name": "Revit"}),
            ("windows_PowerShell", {"command": "Get-Date"}),
        )
        for name, args in unsafe_calls:
            with pytest.raises(ValueError, match="DESKTOP_BUSY"):
                await manager.begin_remote("windows", name, args)
        assert await manager.ui_status() == original
    asyncio.run(check())


def test_filesystem_read_reserves_workspace_isolation_until_completion(tmp_path):
    async def check():
        manager, _, _ = setup(tmp_path)
        args = {"mode": "read", "path": r"C:\QA\trace.txt"}
        actual, read_slot = await manager.begin_remote("windows", "windows_FileSystem", args)
        assert actual == args and read_slot
        assert (await manager.ui_status())["state"] == "read_only"
        with pytest.raises(ValueError, match="DESKTOP_BUSY"):
            await create(manager)
        manager.complete_remote(read_slot)
        assert (await manager.ui_status())["state"] == "clear"
        assert (await create(manager))["state"] == "ready"
    asyncio.run(check())


def test_headless_windows_reads_are_independent_of_native_workspace(tmp_path):
    async def check():
        manager, _, owner = setup(tmp_path)
        created = await create(manager)
        owner[0] = "openai:another-chat"
        actual, token = await manager.begin_remote(
            "windows", "windows_Process", {"mode": "list", "limit": 1},
        )
        assert actual == {"mode": "list", "limit": 1} and token is None
        # Raw FileSystem can expose another owner's RVT source paths and
        # workspace manifests. Never bypass workspace ownership merely because
        # a file operation is non-mutating.
        for mode in ("read", "list", "info"):
            with pytest.raises(ValueError, match="WORKSPACE_ISOLATION"):
                await manager.begin_remote("windows", "windows_FileSystem", {
                    "mode": mode, "path": r"C:\QA\Workspaces",
                })
        with pytest.raises(ValueError, match="WORKSPACE_NOT_OWNED"):
            await manager.begin_remote("windows", "windows_Process",
                                       {"mode": "kill", "pid": 123})
        assert (await manager.list())["workspaces"] == []
        owner[0] = "openai:owner-a"
        assert (await manager.list())["workspaces"][0]["workspace_id"] == created["workspace_id"]
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


def test_parallel_revit_reads_share_a_bounded_non_gui_lane(tmp_path):
    async def check():
        manager, backend, _ = setup(tmp_path)
        clock = [1000.0]
        manager.clock = lambda: clock[0]
        other = WorkspaceManager(
            manager.path, backend, owner_provider=lambda: "openai:owner-b",
            clock=lambda: clock[0],
        )
        first_args, first = await manager.begin_remote(
            "revit", "revit_get_project_info", {"compact": True},
        )
        assert first_args == {"compact": True} and first
        second_args, second = await other.begin_remote(
            "revit", "revit_get_phases", {},
        )
        assert second_args == {} and second and second != first
        manager.require_unscoped_allowed()
        status = await manager.ui_status()
        assert status["state"] == "read_only" and status["active_reads"] == 2
        assert first not in json.dumps(status) and second not in json.dumps(status)
        assert await manager.begin_remote("windows", "windows_ControlStatus", {}) == ({}, None)
        with pytest.raises(ValueError, match="DESKTOP_BUSY"):
            await manager.begin_remote("windows", "windows_Click", {"x": 1})
        with pytest.raises(ValueError, match="DESKTOP_BUSY"):
            await create(manager)
        manager.complete_remote(first)
        assert (await manager.ui_status())["active_reads"] == 1
        with pytest.raises(ValueError, match="DESKTOP_BUSY"):
            await manager.begin_remote("windows", "windows_Click", {"x": 2})
        # Timeout or MCP error for a provably read-only handler must not
        # preserve a permanent GUI fence.
        other.uncertain_remote(second)
        assert (await manager.ui_status())["state"] == "clear"
        args, gui = await manager.begin_remote("windows", "windows_Click", {"x": 3})
        assert args == {"x": 3} and gui
        manager.complete_remote(gui)
        # Only the three audited legacy reads can enter this lane.
        with pytest.raises(ValueError, match="not approved"):
            await manager.begin_remote("revit", "revit_send_code_to_revit", {})
    asyncio.run(check())


def test_read_only_slot_expiry_never_releases_another_gui_operation(tmp_path):
    async def check():
        manager, _, _ = setup(tmp_path)
        clock = [1000.0]
        manager.clock = lambda: clock[0]
        _, read = await manager.begin_remote("revit", "revit_get_selected_elements", {})
        clock[0] += 61
        # An abandoned read cannot block the desktop indefinitely. It has
        # no model writes, unlike a remotely uncertain Windows action.
        _, gui = await manager.begin_remote("windows", "windows_Click", {"x": 1})
        manager.uncertain_remote(read)
        assert (await manager.ui_status())["state"] == "inflight"
        manager.uncertain_remote(gui)
        assert (await manager.ui_status())["state"] == "draining"
    asyncio.run(check())


def test_read_only_lane_does_not_bypass_foreign_gui_fence(tmp_path):
    async def check():
        manager, _, owner = setup(tmp_path)
        _, gui = await manager.begin_remote("windows", "windows_App", {"mode": "switch"})
        manager.uncertain_remote(gui)
        original = await manager.ui_status()
        owner[0] = "openai:owner-b"
        with pytest.raises(ValueError, match="DESKTOP_BUSY"):
            await manager.begin_remote("revit", "revit_get_project_info", {})
        assert (await manager.ui_status())["fence_id"] == original["fence_id"]
    asyncio.run(check())


def test_read_only_capacity_is_bounded_across_owners(tmp_path):
    async def check():
        manager, _, owner = setup(tmp_path)
        reads = []
        for index in range(16):
            owner[0] = f"openai:owner-{index}"
            _, token = await manager.begin_remote("revit", "revit_get_phases", {})
            reads.append(token)
        with pytest.raises(ValueError, match="capacity"):
            await manager.begin_remote("revit", "revit_get_phases", {})
        manager.uncertain_remote(reads[0])
        _, replacement = await manager.begin_remote("revit", "revit_get_phases", {})
        assert replacement not in reads
        for token in reads[1:] + [replacement]:
            manager.complete_remote(token)
        assert (await manager.ui_status())["state"] == "clear"
    asyncio.run(check())


@pytest.mark.parametrize("expiry", [True, float("nan"), float("inf"), "tomorrow", None])
def test_malformed_read_only_lease_never_becomes_immortal(tmp_path, expiry):
    async def check():
        manager, _, _ = setup(tmp_path)
        manager.path.write_text(json.dumps({
            "version": 1, "workspaces": {}, "ui": None,
            "readers": {"bad-reader": {"expires_at": expiry}},
        }))
        with pytest.raises(ValueError, match="INVALID_WORKSPACE_READER"):
            await manager.ui_status()
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


def test_local_operator_recovers_only_dead_old_unscoped_fence(tmp_path, monkeypatch):
    async def check():
        now = [10000.0]
        manager, _, owner = setup(tmp_path)
        manager.clock = lambda: now[0]
        _, token = await manager.begin_remote("windows", "windows_PowerShell",
                                              {"command": "read-only historical inspection"})
        manager.uncertain_remote(token)
        owner[0] = "openai:other-chat"
        status = await manager.ui_status()
        assert status["state"] == "draining" and status["recoverable"] is False
        assert status["operator_recovery_eligible"] is False
        monkeypatch.setattr("chatgpt_web_oauth_mcp.revit_workspaces.process_identity_matches",
                            lambda *args: False)
        now[0] += 601
        status = await manager.ui_status()
        assert status["operator_recovery_eligible"] is True
        assert status["operator_recovery_route"] == "local_only_manual"
        with pytest.raises(ValueError, match="UI_FENCE_NOT_OWNED"):
            await manager.recover_ui(status["fence_id"], True,
                                     "Another chat inspected this orphan and confirmed idleness")
        with pytest.raises(ValueError, match="OPERATOR_REMOTE_IDLE_EVIDENCE_REQUIRED"):
            manager.operator_abort_orphaned_ui(status["fence_id"], confirmed_remote_idle=False,
                evidence="Windows PowerShell child exited, desktop independently checked",
                reason="Operator confirmed request to abort stale unscoped fence")
        with pytest.raises(ValueError, match="UI_FENCE_CHANGED"):
            manager.operator_abort_orphaned_ui("f" * 24, confirmed_remote_idle=True,
                evidence="Windows ControlStatus ready, Windows process gone",
                reason="Operator confirmed request to abort stale unscoped fence")
        recovered = manager.operator_abort_orphaned_ui(status["fence_id"],
            confirmed_remote_idle=True,
            evidence="Windows ControlStatus ready; no residual remote PowerShell; native queue empty",
            reason="Explicit owner-authorized local recovery of dead unscoped controller")
        assert recovered["state"] == "clear" and recovered["mode"] == "local_operator_break_glass"
        assert (await manager.ui_status())["state"] == "clear"
        with manager._state() as state:
            audit = state["ui_recoveries"][-1]
            assert audit["fence_id"] == status["fence_id"]
            assert audit["controller_dead_confirmed"] is True
            assert audit["operation"] == "windows_PowerShell"
            assert audit["operator"] and len(audit["previous_owner_sha256"]) == 64
            assert token not in json.dumps(state["ui_recoveries"])
        monkeypatch.setattr("chatgpt_web_oauth_mcp.revit_workspaces.process_identity_matches",
                            lambda *args: True)
        _, new_token = await manager.begin_remote("windows", "windows_Click", {"x": 1})
        assert new_token != token
        manager.complete_remote(token)
        assert (await manager.ui_status())["state"] == "inflight"
        manager.complete_remote(new_token)
        assert (await manager.ui_status())["state"] == "clear"
    asyncio.run(check())


def test_orphan_operator_fails_closed_if_controller_live_young_scoped_or_unknown(tmp_path, monkeypatch):
    async def check():
        manager, _, _ = setup(tmp_path)
        now = [12000.0]
        manager.clock = lambda: now[0]
        _, token = await manager.begin_remote("windows", "windows_PowerShell", {"command": "Get-Date"})
        status = await manager.ui_status()
        monkeypatch.setattr("chatgpt_web_oauth_mcp.revit_workspaces.process_identity_matches",
                            lambda *args: False)
        assert not (await manager.ui_status())["operator_recovery_eligible"]
        manager.uncertain_remote(token)
        now[0] += 1000
        with manager._state() as state:
            state["ui"]["started_at"] = now[0] - 100
        assert not (await manager.ui_status())["operator_recovery_eligible"]
        with manager._state() as state:
            state["ui"]["started_at"] = now[0] - 700
        monkeypatch.setattr("chatgpt_web_oauth_mcp.revit_workspaces.process_identity_matches",
                            lambda *args: None)
        assert not (await manager.ui_status())["operator_recovery_eligible"]
        monkeypatch.setattr("chatgpt_web_oauth_mcp.revit_workspaces.process_identity_matches",
                            lambda *args: True)
        assert not (await manager.ui_status())["operator_recovery_eligible"]
        with pytest.raises(ValueError, match="CONTROLLER_NOT_PROVEN_DEAD"):
            manager.operator_abort_orphaned_ui(status["fence_id"], confirmed_remote_idle=True,
                evidence="Independent remote inspection found no active Windows command",
                reason="Operator wants to end this stale remote operation")
        monkeypatch.setattr("chatgpt_web_oauth_mcp.revit_workspaces.process_identity_matches",
                            lambda *args: False)
        with manager._state() as state:
            state["ui"]["workspace_id"] = "ws_foreign"
        assert not (await manager.ui_status())["operator_recovery_eligible"]
        with pytest.raises(ValueError, match="UNSCOPED_DRAINING_ONLY"):
            manager.operator_abort_orphaned_ui(status["fence_id"], confirmed_remote_idle=True,
                evidence="Independent remote inspection found no active Windows command",
                reason="Operator wants to end this stale remote operation")
        with manager._state() as state:
            state["ui"]["workspace_id"] = None
            state["ui"]["controller_identity"] = None
        assert not (await manager.ui_status())["operator_recovery_eligible"]
    asyncio.run(check())


def test_break_glass_refuses_active_native_work_and_is_not_public_mcp(tmp_path, monkeypatch):
    async def check():
        manager, _, _ = setup(tmp_path)
        now = [10000.0]
        manager.clock = lambda: now[0]
        _, token = await manager.begin_remote("windows", "windows_PowerShell",
                                              {"command": "Get-Process"})
        manager.uncertain_remote(token)
        now[0] += 900
        monkeypatch.setattr("chatgpt_web_oauth_mcp.revit_workspaces.process_identity_matches",
                            lambda *args: False)
        fence = (await manager.ui_status())["fence_id"]
        with manager._state() as state:
            state["workspaces"]["ws_live"] = {
                "workspace_id": "ws_live", "owner": "openai:owner-a",
                "state": "ready", "requests": {},
            }
        assert not (await manager.ui_status())["operator_recovery_eligible"]
        with pytest.raises(ValueError, match="CONTROLLER_NOT_PROVEN_DEAD_OR_WORK_ACTIVE"):
            manager.operator_abort_orphaned_ui(
                fence, confirmed_remote_idle=True,
                reason="Requested local operator recovery of orphaned UI fence",
                evidence="Verified Windows idle and controller already exited, but native work exists")
        with manager._state() as state:
            state["workspaces"].clear()
        assert (await manager.ui_status())["operator_recovery_eligible"]
        tool_names = [tool.name for tool in workspace_tools()]
        assert "revit_workspace_operator_abort_orphaned_ui" not in tool_names
        assert "revit_workspace_recover_ui" in tool_names
        assert (await manager.ui_status())["state"] == "draining"
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
        def prepare(workspace_id: str, source_path: str, owner_proof: str | None = None):
            seen.append((workspace_id, source_path, owner_proof))
            return {"workspace_id": workspace_id, "document_path": r"C:\QA\copy.rvt"}
        @remote.tool(name="revit_workspace_dispatch")
        def dispatch(instance_id: str, port: int, operation: str, payload: dict,
                     expected_native_identity: dict):
            seen.append((instance_id, port, operation, payload, expected_native_identity))
            return {"ok": True}
        backend = NodeWorkspaceBackend(lambda: remote)
        assert await backend.discover() == {"instances": []}
        await backend.prepare("ws", r"C:\source.rvt", "A" * 44)
        instance = {
            "instance_id": "i", "port": 123, "pid": 51,
            "process_started_utc": "2026-11-01T10:00:00Z",
            "revit_version": "2023", "plugin_build_id": "qa",
        }
        await backend.dispatch(instance, "status", {"request_id": "r", "workspace_id": "ws"})
        assert seen == [
            ("ws", r"C:\source.rvt", "A" * 44),
            ("i", 123, "status", {"request_id": "r", "workspace_id": "ws"}, {
                "pid": 51, "process_started_utc": "2026-11-01T10:00:00Z",
                "revit_version": "2023", "plugin_build_id": "qa",
            }),
        ]
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


def test_router_headless_windows_reads_keep_foreign_gui_fence(tmp_path):
    async def check():
        manager, _, owner = setup(tmp_path)
        remote, public = FastMCP("Windows"), FastMCP("public")
        seen = []

        @remote.tool(name="windows_Process")
        def processes(mode: str, pid: int | None = None):
            seen.append(("process", mode))
            return {"mode": mode}

        @remote.tool(name="windows_FileSystem")
        def filesystem(mode: str, path: str, content: str | None = None):
            seen.append(("filesystem", mode))
            return {"mode": mode, "path": path}

        @remote.tool(name="windows_DisplayInventory")
        def display_inventory():
            seen.append(("displays", None))
            return {"displays": []}

        register_tool_groups(public, transport_factory=lambda: remote, workspace_manager=manager)
        _, token = await manager.begin_remote("windows", "windows_Click", {"x": 1})
        manager.uncertain_remote(token)
        owner[0] = "openai:other-chat"
        original = await manager.ui_status()
        async with Client(public) as client:
            async def call(name: str, args: dict):
                return await client.call_tool("tool_group_call", {
                    "group": "windows", "name": name, "arguments": args,
                }, raise_on_error=False)
            for name, args in (
                ("windows_Process", {"mode": "list"}),
                ("windows_FileSystem", {"mode": "read", "path": r"C:\QA\report.txt"}),
                ("windows_DisplayInventory", {}),
            ):
                result = await call(name, args)
                assert not result.is_error, (name, result.content)
            assert seen == [("process", "list"), ("filesystem", "read"), ("displays", None)]
            for name, args in (
                ("windows_Process", {"mode": "kill", "pid": 123}),
                ("windows_FileSystem", {"mode": "write", "path": r"C:\QA\report.txt", "content": "x"}),
            ):
                assert (await call(name, args)).is_error
        assert (await manager.ui_status()) == original
        assert seen == [("process", "list"), ("filesystem", "read"), ("displays", None)]
    asyncio.run(check())


def test_two_chats_run_headless_windows_calls_concurrently(tmp_path):
    async def check():
        manager, backend, owner = setup(tmp_path)
        _, token = await manager.begin_remote("windows", "windows_Click", {"x": 1})
        manager.uncertain_remote(token)
        foreign = await manager.ui_status()
        assert foreign["state"] == "draining"

        remote = FastMCP("shared Windows")
        arrived = asyncio.Event()
        concurrent = [0]

        @remote.tool(name="windows_Process")
        async def process(mode: str, limit: int = 20):
            concurrent[0] += 1
            if concurrent[0] == 2:
                arrived.set()
            await asyncio.wait_for(arrived.wait(), timeout=2)
            return {"mode": mode}

        manager_b = WorkspaceManager(
            manager.path, backend, owner_provider=lambda: "openai:other-chat",
        )
        public_a, public_b = FastMCP("A"), FastMCP("B")
        register_tool_groups(public_a, transport_factory=lambda: remote, workspace_manager=manager)
        register_tool_groups(public_b, transport_factory=lambda: remote, workspace_manager=manager_b)
        async with Client(public_a) as first, Client(public_b) as second:
            async def call(client):
                return await client.call_tool("tool_group_call", {
                    "group": "windows", "name": "windows_Process",
                    "arguments": {"mode": "list", "limit": 2},
                }, raise_on_error=False)
            responses = await asyncio.gather(call(first), call(second))
            assert all(not result.is_error for result in responses), responses
        assert concurrent[0] == 2
        assert await manager.ui_status() == foreign
        owner[0] = "openai:other-chat"
        assert not (await manager.ui_status())["owner_matches"]
    asyncio.run(check())


def test_router_headless_failure_does_not_strand_gui(tmp_path):
    async def check():
        manager, _, owner = setup(tmp_path)
        remote, public = FastMCP("Windows"), FastMCP("public")

        @remote.tool(name="windows_Process")
        def processes(mode: str):
            raise ConnectionError("process list provider is offline")

        @remote.tool(name="windows_FileSystem")
        def filesystem(mode: str, path: str):
            raise TimeoutError("read provider unavailable")

        register_tool_groups(public, transport_factory=lambda: remote, workspace_manager=manager)
        _, token = await manager.begin_remote("windows", "windows_Click", {"x": 1})
        manager.uncertain_remote(token)
        owner[0] = "openai:other-chat"
        previous = await manager.ui_status()
        async with Client(public) as client:
            result = await client.call_tool("tool_group_call", {
                "group": "windows", "name": "windows_Process", "arguments": {"mode": "list"},
            }, raise_on_error=False)
            assert result.is_error
            filesystem_result = await client.call_tool("tool_group_call", {
                "group": "windows", "name": "windows_FileSystem",
                "arguments": {"mode": "read", "path": r"C:\QA\test.txt"},
            }, raise_on_error=False)
            assert filesystem_result.is_error
        assert await manager.ui_status() == previous
        assert json.loads(manager.path.read_text())["readers"] == {}
    asyncio.run(check())


def test_router_failed_legacy_read_does_not_strand_the_desktop(tmp_path):
    async def check():
        manager, _, _ = setup(tmp_path)
        remote, public = FastMCP("remote"), FastMCP("public")
        calls = []

        @remote.tool(name="revit_get_project_info")
        def info() -> dict:
            raise RuntimeError("QA native listener unavailable")

        @remote.tool(name="windows_Click")
        def click(x: int) -> dict:
            calls.append(x)
            return {"clicked": x}

        register_tool_groups(public, transport_factory=lambda: remote, workspace_manager=manager)
        async with Client(public) as client:
            failed = await client.call_tool("tool_group_call", {
                "group": "revit", "name": "revit_get_project_info", "arguments": {},
            }, raise_on_error=False)
            assert failed.is_error
            assert (await manager.ui_status())["state"] == "clear"
            clicked = await client.call_tool("tool_group_call", {
                "group": "windows", "name": "windows_Click", "arguments": {"x": 1},
            }, raise_on_error=False)
            assert not clicked.is_error and calls == [1]
            assert (await manager.ui_status())["state"] == "clear"
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
