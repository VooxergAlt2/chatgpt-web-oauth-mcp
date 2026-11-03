"""Durable-job child for an operator-configured ACP backend. No MCP server loop."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import resource
import signal
import sys
import tempfile
import time
from pathlib import Path

from .acp_backend import AcpBackend
from .acp_control import AcpControlStore
from .acp_gateway import AcpReadonlyGateway
from .acp_result import parse_acp_worker_output
from .acp_sandbox import AcpSandbox, verification_command
from .acp_session import AcpRunLimits, AcpSessionRunner
from .acp_session_store import AcpSessionIdentity, AcpSessionStore, AcpStateError
from .acp_workspace import AcpWorkspace
from .delegate_code_verification import _run_verification_commands, _scope_matches


def emit(event: str, value: dict):
    print(json.dumps({"event": event, **value}, ensure_ascii=False), flush=True)


def _failure(result: dict, code: str, *, uncertain: bool = False) -> dict:
    return {**result, "success": False,
            "status": "cancelled" if code.startswith("acp_cancelled") else "failed",
            "error": {"code": code}, "outcome_uncertain": uncertain}


def _checkpoint_result(result: dict) -> dict:
    """Keep checkpoint bounds independent of declared command output volume."""
    saved = copy.deepcopy(result)
    receipt = saved.get("harness_metadata", {}).get("acp_code_receipt")
    if isinstance(receipt, dict):
        # Full bounded command output remains in the durable job result/logs.
        for row in receipt.get("verification_commands", []):
            if isinstance(row, dict):
                row.pop("stdout", None)
                row.pop("stderr", None)
    return saved


def _validate_request(request: dict) -> None:
    required = {"delegate_id", "account", "owner", "workspace", "thinking",
                "scope_digest", "session_ref", "turn_id", "prompt", "timeout",
                "max_tool_calls", "kind", "scope", "code_contract", "code_baseline"}
    if not isinstance(request, dict) or set(request) - {"resume_from_turn"} != required:
        raise AcpStateError("acp_worker_request_invalid")
    if request["turn_id"] != request["delegate_id"]:
        raise AcpStateError("acp_worker_turn_mismatch")
    scope = request["scope"]
    if (not isinstance(scope, dict) or set(scope) != {"files_in_scope", "out_of_scope"}
            or any(not isinstance(scope[k], list) or len(scope[k]) > 512
                   or any(not isinstance(v, str) or len(v.encode("utf-8")) > 4096
                          for v in scope[k]) for k in scope)):
        raise AcpStateError("acp_worker_scope_invalid")
    if request["kind"] == "code":
        contract = request["code_contract"]
        baseline = request["code_baseline"]
        if (not isinstance(contract, dict) or not isinstance(baseline, dict)
                or not baseline.get("captured_clean")
                or contract.get("files_in_scope") != scope["files_in_scope"]
                or not isinstance(contract.get("verification_commands"), list)
                or not contract["verification_commands"]
                or len(contract["verification_commands"]) > 16
                or any(not isinstance(v, str) or not v.strip() or len(v.encode("utf-8")) > 8192
                       for v in contract["verification_commands"])):
            raise AcpStateError("acp_writer_contract_invalid")
        if request["timeout"] < 30:
            raise AcpStateError("acp_writer_timeout_too_short")
    elif request["kind"] != "explore" or request["code_contract"] is not None:
        raise AcpStateError("acp_worker_request_invalid")


async def _finish_writer(*, result: dict, request: dict, workspace: AcpWorkspace,
                         store: AcpSessionStore, identity: AcpSessionIdentity,
                         control: AcpControlStore, cancel: asyncio.Event,
                         cancel_path: Path, deadline: float) -> dict:
    from .acp_code_verification import build_verification_receipt, inspect_candidate

    reference, turn_id = request["session_ref"], request["turn_id"]
    def cancelled():
        return cancel.is_set() or cancel_path.exists()
    # Hold the same session lease through validation and export. A dead worker
    # leaves an uncertain checkpoint; neither a retry nor a reply can replay it.
    with store.lease(reference):
        state = store.load(reference, identity)
        if state.get("turn") != turn_id:
            raise AcpStateError("acp_worker_turn_mismatch")
        state.update(phase="running", result=_checkpoint_result(
            _failure(result, "acp_writer_postprocessing_incomplete", uncertain=True)))
        store.save(reference, state)
        promotion_started = False
        try:
            parsed = parse_acp_worker_output(json.dumps({"event": "acp_result", "result": result}), "")
            if parsed.error is not None or not isinstance(parsed.structured_output, dict):
                raise AcpStateError("acp_result_manifest_invalid")
            if parsed.structured_output.get("status") != "succeeded":
                raise AcpStateError("acp_writer_incomplete_manifest")
            if cancelled():
                raise AcpStateError("acp_cancelled_before_verification")
            inspect_candidate(workspace, request["code_contract"])
            for path in workspace.validate_changes():
                if any(_scope_matches(path, excluded.rstrip("/")) for excluded in request["scope"]["out_of_scope"]):
                    raise AcpStateError("acp_writer_excluded_path")
            digest = workspace.snapshot_digest()
            emit("acp_progress", {"progress": {"event": "verifying", "changed_files": len(workspace.validate_changes())}})
            remaining = min(float(request["code_contract"].get("verification_timeout_seconds", 300)),
                            deadline - time.monotonic() - 2)
            if remaining <= 0:
                raise AcpStateError("acp_writer_verification_deadline")
            checks = await asyncio.to_thread(
                _run_verification_commands, workspace.root,
                request["code_contract"]["verification_commands"],
                timeout_seconds=remaining,
                argv_factory=lambda command: verification_command(workspace.root, workspace.source, command),
                cancel_check=cancelled,
            )
            if cancelled():
                raise AcpStateError("acp_cancelled_before_promotion")
            if time.monotonic() >= deadline - 1:
                raise AcpStateError("acp_writer_verification_deadline")
            receipt = build_verification_receipt(
                delegate_id=request["delegate_id"], contract=request["code_contract"],
                baseline=request["code_baseline"], workspace=workspace,
                verification_results=checks, expected_digest=digest,
            )
            prepared = {**result, "harness_metadata": {
                **result.get("harness_metadata", {}), "acp_code_receipt": receipt,
                "promoted_paths": list(workspace.validate_changes()),
                "writer_boundary": "private-copy-verified-promotion",
            }}
            checkpoint = {**state, "phase": "completed", "result": _checkpoint_result(prepared)}
            if len(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True).encode("utf-8")) > 250000:
                raise AcpStateError("acp_code_receipt_too_large")
            if cancelled():
                raise AcpStateError("acp_cancelled_before_promotion")
            promotion_started = True
            workspace.promote(expected_digest=digest)
            result = prepared
        except AcpStateError as exc:
            result = _failure(result, exc.code,
                              uncertain=exc.code == "acp_writer_rollback_failed")
            # The detached copy is deleted at worker exit. Keep bounded file
            # metadata so a rejected edit remains diagnosable without retaining
            # file contents or weakening the promotion boundary.
            result.setdefault("harness_metadata", {})["writer_rejection"] = (
                workspace.inspect_changes_unchecked())
        except asyncio.CancelledError:
            # The already-synced running checkpoint is intentional.
            raise
        except Exception:  # noqa: BLE001
            result = _failure(result, "acp_writer_finalization_failed", uncertain=promotion_started)
        state.update(
            phase="running" if result.get("outcome_uncertain") else
                  "completed" if result.get("success") else
                  "cancelled" if result.get("status") == "cancelled" else "failed",
            result=_checkpoint_result(result),
        )
        store.save(reference, state)
        try:
            control.finish_turn(reference, identity, turn_id=turn_id,
                                status="uncertain" if result.get("outcome_uncertain") else state["phase"])
        except (AcpStateError, OSError, ValueError):
            # The saved session checkpoint owns finality. A mailbox annotation
            # failure cannot discard an independently verified export receipt.
            result.setdefault("harness_metadata", {})["control_warning"] = "acp_control_close_failed"
        return result


async def execute(config_path: Path, request: dict) -> dict:
    backend = AcpBackend.load(config_path)
    _validate_request(request)
    delegate_id = request["delegate_id"]
    cancel_path = backend.cancel_path(delegate_id)
    reference = request["session_ref"]
    AcpSessionStore.validate_reference(reference)
    workspace = Path(request["workspace"]).resolve(strict=True)
    backend.validate(account=request["account"], workspace=workspace, kind=request["kind"],
                     model=None, timeout=request["timeout"])
    identity = AcpSessionIdentity(owner=request["owner"], account=request["account"], workspace=str(workspace),
                                  scope_digest=request["scope_digest"], thinking=request["thinking"])
    store = AcpSessionStore(backend.state_root / "sessions")
    control_store = AcpControlStore(backend.state_root / "control")
    deadline = time.monotonic() + request["timeout"] - 3
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, cancel.set)

    async def watch_cancel():
        while not cancel.is_set():
            if cancel_path.exists():
                cancel.set()
                return
            await asyncio.sleep(0.1)

    watcher = asyncio.create_task(watch_cancel())
    writer = None
    gateway = None
    try:
        if request["kind"] == "code":
            writer = AcpWorkspace.prepare(workspace, request["scope"]["files_in_scope"],
                                          backend.state_root / "workspaces")
            if writer.baseline_head != request["code_baseline"].get("head"):
                raise AcpStateError("acp_writer_baseline_changed")
        if request["scope"]["files_in_scope"]:
            gateway = AcpReadonlyGateway(
                workspace=writer.root if writer else workspace, owner=request["owner"], task_id=delegate_id,
                files_in_scope=request["scope"]["files_in_scope"],
                out_of_scope=request["scope"]["out_of_scope"],
                workspace_alias=workspace, baseline_root=writer.baseline_root if writer else None,
                max_calls=request["max_tool_calls"],
            )
        with tempfile.TemporaryDirectory(prefix="opss-acp-helper-") as temporary:
            control = Path(temporary)
            control.chmod(0o700)
            box_kwargs = ({"workspace_view": writer.root, "writable_workspace": True} if writer else {})
            box = AcpSandbox(
                backend.server, backend.helper, backend.accounts[request["account"]]["profile"],
                workspace, control, backend.state_root / "conversations" / reference, **box_kwargs,
            )
            runner = AcpSessionRunner(store, box.transport(gateway=gateway))
            reserve = min(300, max(8, request["timeout"] / 4)) if writer else 0
            native_budget = deadline - time.monotonic() - reserve
            if native_budget <= 6:
                raise AcpStateError("acp_start_deadline_exhausted")
            result = await runner.run(
                reference=reference, identity=identity, turn_id=request["turn_id"], prompt=request["prompt"],
                limits=AcpRunLimits(timeout_seconds=native_budget, cancel_grace_seconds=5,
                                    max_tool_calls=request["max_tool_calls"]),
                cancel=cancel, progress=lambda data: emit("acp_progress", {"progress": data}),
                control=control_store, allow_native_tools=writer is not None,
                defer_completion=writer is not None,
                expected_previous_turn=request.get("resume_from_turn"),
                mcp_servers=[gateway.mcp_server()] if gateway else [],
            )
            # runner.run has torn down all native processes before mirror inspection.
            if gateway is not None:
                result.setdefault("harness_metadata", {})["scoped_gateway"] = gateway.summary()
            if writer is not None and result.get("success") is True:
                result = await _finish_writer(result=result, request=request, workspace=writer,
                                              store=store, identity=identity, control=control_store,
                                              cancel=cancel, cancel_path=cancel_path, deadline=deadline)
            return result
    finally:
        if gateway is not None:
            gateway.revoke()
        if writer is not None:
            writer.close()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        cancel_path.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(196609)
        if len(raw) > 196608:
            raise AcpStateError("acp_request_too_large")
        result = asyncio.run(execute(args.config, json.loads(raw)))
    except AcpStateError as exc:
        result = _failure({}, exc.code)
    except Exception:  # noqa: BLE001
        result = {"success": False, "status": "failed", "error": {"code": "acp_worker_failed"}}
    emit("acp_result", {"result": result})


if __name__ == "__main__":
    main()
