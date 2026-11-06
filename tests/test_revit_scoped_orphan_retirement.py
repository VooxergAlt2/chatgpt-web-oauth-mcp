"""Stopped native + abandoned controller recovery preserves both authority and history."""
import asyncio
from copy import deepcopy
import json

import pytest

from test_revit_workspaces import setup, create
from chatgpt_web_oauth_mcp.revit_workspace_backend import verified_identity
from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager, workspace_tools


async def fixture(tmp_path, monkeypatch):
    manager, backend, owner = setup(tmp_path)
    manager.clock = lambda: 1000.0
    wid = (await create(manager))["workspace_id"]
    with manager._state() as state:
        state["ui"] = {
            "state": "draining", "workspace_id": wid, "owner": owner[0],
            "token": "original-lease", "operation_token": "original-operation",
            "operation": "windows_PowerShell", "started_at": 100.0,
            "controller_pid": 999999, "controller_identity": "linux-start-ticks:old",
        }
    monkeypatch.setattr("chatgpt_web_oauth_mcp.revit_workspaces.process_identity_matches",
                        lambda pid, identity: False)
    observation = {
        "kind": "RevitStoppedProcessObservationV1", "observed_at": 1000.0,
        "workspace_id": wid, "native_identity": verified_identity(backend.instance),
        "windows_process_tool": "windows_Process:list:all", "process_inventory_complete": True,
        "revit_processes": [], "native_discovery_tool": "revit_workspace_instances",
        "native_instances": [],
    }
    proof = {
        "kind": "RevitOrphanedUiObservationV1", "observed_at": 1000.0,
        "fence_id": (await manager.ui_status())["fence_id"],
        "control_status_tool": "windows_ControlStatus", "control_state": "ready",
        "screenshot_tool": "windows_Screenshot",
        "evidence": "Operator inspected fresh desktop and verified no pending operation.",
    }
    return manager, backend, owner, wid, observation, proof


def retire(manager, wid, observation, proof, **kwargs):
    return manager.operator_retire_stopped_workspace(
        wid, observation=observation, orphaned_ui_observation=proof,
        confirm_process_stopped=True, confirm_unknown_outcome=True,
        confirm_remote_idle=True,
        reason="User requested recovery of this stopped disposable QA session.",
        **kwargs,
    )


def test_atomic_recovery_preserves_receipts_and_rejects_late_reply(tmp_path, monkeypatch):
    async def check():
        manager, backend, owner, wid, observation, proof = await fixture(tmp_path, monkeypatch)
        owner[0] = "openai:another-chat"
        before = json.loads(manager.path.read_text())
        assert (await manager.ui_status())["stopped_scoped_operator_candidate"]
        with pytest.raises(ValueError, match="NOT_OWNED"):
            await manager.recover_ui(proof["fence_id"], True, proof["evidence"])
        assert retire(manager, wid, observation, proof)["dry_run"]
        assert json.loads(manager.path.read_text()) == before
        calls = deepcopy(backend.calls)
        report = retire(manager, wid, observation, proof, apply=True)
        assert report["orphaned_scoped_ui_retired"]
        after = json.loads(manager.path.read_text())
        assert after["ui"] is None
        assert after["workspaces"][wid]["state"] == "retired"
        assert after["workspaces"][wid]["requests"] == before["workspaces"][wid]["requests"]
        audit = after["workspaces"][wid]["retirement"]
        assert not audit["native_completion_confirmed"]
        assert not audit["remote_completion_confirmed"]
        assert audit["stopped_scoped_ui_fence_id"] == proof["fence_id"]
        assert "token" not in audit and "operation_token" not in audit
        assert backend.calls == calls
        restarted = WorkspaceManager(manager.path, backend, owner_provider=lambda: owner[0])
        restarted.complete_remote("original-operation")
        restarted.uncertain_remote("original-operation")
        assert (await restarted.ui_status())["state"] == "clear"
        monkeypatch.setattr("chatgpt_web_oauth_mcp.revit_workspaces.process_identity_matches",
                            lambda pid, identity: True)
        with restarted._state() as state:
            state["ui"] = {"token": "new", "operation_token": "new-operation",
                           "workspace_id": None, "state": "inflight", "owner": owner[0]}
        restarted.complete_remote("original-operation")
        assert (await restarted.ui_status())["state"] == "inflight"
        assert not any("retire" in tool.name for tool in workspace_tools())
    asyncio.run(check())


@pytest.mark.parametrize("case", [
    "live_controller", "inaccessible_controller", "young", "bad_started", "inflight",
    "unscoped", "foreign_owner", "wrong_workspace", "native_pending", "reader",
    "lifecycle", "live_revit", "native_live", "stale_process", "partial_process",
    "stale_ui", "nonfinite_ui", "wrong_fence", "busy_ui", "bad_tool",
    "no_idle_confirmation", "no_evidence",
])
def test_unsafe_or_unproven_recovery_refused_without_changes(tmp_path, monkeypatch, case):
    async def check():
        manager, _, _, wid, observation, proof = await fixture(tmp_path, monkeypatch)
        if case in ("live_controller", "inaccessible_controller", "inflight"):
            monkeypatch.setattr("chatgpt_web_oauth_mcp.revit_workspaces.process_identity_matches",
                                lambda pid, identity: None if case == "inaccessible_controller" else True)
        with manager._state() as state:
            ui = state["ui"]
            if case == "young": ui["started_at"] = 900.0
            if case == "bad_started": ui["started_at"] = float("nan")
            if case == "inflight": ui["state"] = "inflight"
            if case == "unscoped": ui["workspace_id"] = None
            if case == "foreign_owner": ui["owner"] = "openai:unrelated"
            if case == "wrong_workspace": ui["workspace_id"] = "ws_other"
            if case == "native_pending":
                state["workspaces"][wid]["requests"]["unknown"] = {"status": "unknown"}
            if case == "reader":
                state["readers"] = {"r": {"expires_at": 2000.0}}
            if case == "lifecycle": state["workspaces"][wid]["state"] = "binding"
        if case == "live_revit": observation["revit_processes"] = [{"pid": 1}]
        if case == "native_live": observation["native_instances"] = [{"pid": 1}]
        if case == "stale_process": observation["observed_at"] = 800.0
        if case == "partial_process": observation["process_inventory_complete"] = False
        if case == "stale_ui": proof["observed_at"] = 800.0
        if case == "nonfinite_ui": proof["observed_at"] = float("nan")
        if case == "wrong_fence": proof["fence_id"] = "changed"
        if case == "busy_ui": proof["control_state"] = "busy"
        if case == "bad_tool": proof["screenshot_tool"] = "invented"
        if case == "no_evidence": proof["evidence"] = ""
        before = manager.path.read_bytes()
        with pytest.raises(ValueError):
            if case == "no_idle_confirmation":
                manager.operator_retire_stopped_workspace(
                    wid, observation=observation, orphaned_ui_observation=proof,
                    confirm_process_stopped=True, confirm_unknown_outcome=True,
                    reason="User requested QA recovery with missing remote idle confirmation.",
                    apply=True)
            else:
                retire(manager, wid, observation, proof, apply=True)
        assert manager.path.read_bytes() == before
    asyncio.run(check())
