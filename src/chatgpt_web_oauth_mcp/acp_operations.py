"""Owner-checked controller operations for durable ACP delegates.

These functions never send prompts or infer success from a native history file.
Only immutable server task metadata chooses a session binding or mailbox path.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .acp_control import AcpControlStore
from .acp_progress import progress_snapshot
from .acp_session_store import AcpSessionIdentity, AcpSessionStore, AcpStateError
from .acp_supervision import supervisor_binding
from .job_supervisor import TERMINAL_JOB_STATUSES
from .supervision import SupervisorStore, SupervisionError
from .candidate_snapshots import CandidateSnapshotStore

_TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "unknown", "interrupted"}
_ABANDONABLE = {"failed", "cancelled", "timed_out", "unknown", "interrupted"}
_MODELS = {"gemini-3.8-flash", "gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high"}


@dataclass(frozen=True)
class _Binding:
    source: dict
    identity: AcpSessionIdentity
    reference: str
    delegate_id: str
    state_root: Path

    @property
    def sessions(self):
        return AcpSessionStore(self.state_root / "sessions")

    @property
    def control(self):
        return AcpControlStore(self.state_root / "control")


def _failure(code):
    return {"success": False, "error": {"code": code, "message": code}}


def _agree(records, name, *, required=True):
    values = [item[name] for item in records if item.get(name) is not None]
    if not values:
        if required:
            raise AcpStateError("acp_control_identity_unavailable")
        return None
    if any(value != values[0] for value in values):
        raise AcpStateError("acp_control_identity_conflict")
    return values[0]


def _records(registry, source, delegate_id):
    """Metadata fallback without calling any snapshot/normalization wrapper."""
    records = [source]
    task = None
    scheduler = getattr(registry, "scheduler", None)
    if scheduler is not None:
        task = scheduler.get_task(delegate_id)
    path = None
    if task is not None:
        record = {key: getattr(task, key, None) for key in (
            "delegate_id", "execution_backend", "harness", "kind", "model",
            "reasoning_effort", "logical_session_id", "acp_resume_context",
        )}
        record["cwd"] = str(task.cwd)
        from .acp_backend import AcpHarness
        if record["execution_backend"] == "acp":
            record["acp_resume_context"] = AcpHarness.context(task)
        records.append(record)
        path = getattr(getattr(task, "log_paths", None), "metadata", None)
    if path is None:
        path = getattr(registry, "_persisted_delegate_paths", {}).get(delegate_id)
    if path is None and isinstance(source.get("logs"), dict):
        path = source["logs"].get("metadata")
    reader = getattr(registry, "_read_persisted_delegate_metadata", None)
    if path is not None and reader is not None:
        path = Path(path)
        # Paths originate in server-owned snapshots; reject malformed fallback
        # records instead of interpreting arbitrary user or native paths.
        if path.name != "metadata.json" or not path.is_absolute() or path.is_symlink():
            raise AcpStateError("acp_control_metadata_invalid")
        metadata = reader(path)
        if metadata is not None:
            if not isinstance(metadata, dict) or metadata.get("delegate_id") != delegate_id:
                raise AcpStateError("acp_control_metadata_invalid")
            records.append(metadata)
    return records


def _binding(registry, source, delegate_id, logical_session_id=None):
    if not isinstance(delegate_id, str) or not re.fullmatch(r"[a-f0-9]{12}", delegate_id):
        raise AcpStateError("acp_invalid_delegate_id")
    if not isinstance(source, dict) or source.get("delegate_id") != delegate_id:
        raise AcpStateError("delegate_not_found")
    if source.get("execution_backend") != "acp":
        raise AcpStateError("acp_backend_required")
    backend = getattr(registry, "acp_backend", None)
    if backend is None:
        raise AcpStateError("acp_backend_disabled")
    records = _records(registry, source, delegate_id)
    if _agree(records, "execution_backend") != "acp":
        raise AcpStateError("acp_backend_required")
    account = _agree(records, "harness")
    if account not in getattr(backend, "accounts", {}):
        raise AcpStateError("acp_account_not_configured")
    if _agree(records, "kind") not in {"explore", "code"}:
        raise AcpStateError("acp_task_kind_invalid")
    workspace = _agree(records, "cwd")
    model = _agree(records, "model", required=False)
    effort = _agree(records, "reasoning_effort", required=False)
    contexts = []
    details = []
    for record in records:
        for key in ("acp_resume_context", "harness_metadata"):
            value = record.get(key)
            if isinstance(value, dict):
                contexts.append(value)
                if key == "harness_metadata":
                    details.append(value)
    owner = _agree(contexts, "owner_digest")
    scope = _agree(contexts, "scope_digest")
    reference = _agree(contexts, "session_ref")
    for value, pattern in ((owner, r"[a-f0-9]{64}"), (scope, r"[a-f0-9]{64}"), (reference, r"[a-f0-9]{32}")):
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise AcpStateError("acp_control_identity_invalid")
    raw_owner = _agree(records, "logical_session_id", required=False)
    if raw_owner is not None and (not isinstance(raw_owner, str) or hashlib.sha256(raw_owner.encode()).hexdigest() != owner):
        raise AcpStateError("acp_control_identity_conflict")
    if logical_session_id is not None:
        if (not isinstance(logical_session_id, str) or not logical_session_id
                or hashlib.sha256(logical_session_id.encode()).hexdigest() != owner):
            raise AcpStateError("delegate_owned_by_another_session")
    metadata_model = _agree(details, "model", required=False)
    metadata_effort = _agree(details, "reasoning_effort", required=False)
    model = model or metadata_model
    effort = effort or metadata_effort
    if model not in _MODELS or (metadata_model is not None and metadata_model not in _MODELS):
        raise AcpStateError("acp_control_identity_invalid")
    if metadata_effort is not None and effort != metadata_effort:
        raise AcpStateError("acp_control_identity_conflict")
    try:
        identity = AcpSessionIdentity(owner=owner, account=account, workspace=workspace,
                                      scope_digest=scope, model="gemini-3.8-flash", thinking=effort)
    except (TypeError, ValueError) as exc:
        raise AcpStateError("acp_control_identity_invalid") from exc
    return _Binding(source, identity, reference, delegate_id, Path(backend.state_root))


def persisted_session_ownership_scope(registry, source, delegate_id, logical_session_id):
    """Recover an owner only from a private session and its bound execution.

    Older terminal metadata omitted the logical owner. An owner digest alone
    is insufficient: the canonical store must verify the complete identity,
    reference, and delegate turn before it can authorize result access.
    """
    if not isinstance(logical_session_id, str) or not logical_session_id:
        return "unowned"
    try:
        binding = _binding(registry, source, delegate_id)
        state = binding.sessions.load(binding.reference, binding.identity)
        if state.get("turn") != delegate_id:
            return "conflict"
        details = source.get("harness_metadata", {})
        if "turn_id" in details and details["turn_id"] != delegate_id:
            return "conflict"
        owner = hashlib.sha256(logical_session_id.encode("utf-8")).hexdigest()
    except (AcpStateError, OSError, TypeError, ValueError):
        return "conflict"
    return "owned_here" if owner == binding.identity.owner else "owned_elsewhere"


def _owned(registry, delegate_id, logical_session_id):
    if not isinstance(logical_session_id, str) or not logical_session_id:
        raise AcpStateError("acp_control_owner_required")
    source = registry._resume_source_snapshot(delegate_id)
    return _binding(registry, source, delegate_id, logical_session_id)


def _job_status(registry, binding):
    job_id = binding.source.get("durable_job_id")
    jobs = getattr(registry, "durable_job_registry", None)
    state_dir = getattr(registry, "durable_state_dir", None)
    if not isinstance(job_id, str) or not job_id or jobs is None or state_dir is None:
        raise AcpStateError("acp_control_worker_state_unavailable")
    status = jobs.job_status(job_id=job_id, state_dir=state_dir)
    if not isinstance(status, dict) or status.get("success") is False:
        raise AcpStateError("acp_control_worker_state_unavailable")
    return status.get("status")


def _checkpoint(binding):
    path = binding.state_root / "sessions" / (binding.reference + ".json")
    if not path.exists() and not path.is_symlink():
        raise AcpStateError("acp_control_session_not_started")
    checkpoint = binding.sessions.load(binding.reference, binding.identity)
    if checkpoint.get("turn") != binding.delegate_id:
        raise AcpStateError("acp_control_turn_mismatch")
    return checkpoint


def _active(registry, binding):
    source = binding.source
    if source.get("completed") is True or source.get("status") != "running":
        raise AcpStateError("acp_control_delegate_not_active")
    if _job_status(registry, binding) != "running":
        raise AcpStateError("acp_control_worker_not_active")
    checkpoint = _checkpoint(binding)
    if checkpoint.get("phase") != "running" or checkpoint.get("result") is not None:
        raise AcpStateError("acp_control_turn_not_active")
    try:
        with binding.sessions.lease(binding.reference):
            pass
    except AcpStateError as exc:
        if exc.code == "acp_session_busy":
            return checkpoint
        raise
    raise AcpStateError("acp_control_worker_not_active")


def _view(binding):
    path = binding.state_root / "control" / (binding.reference + ".json")
    if not path.exists() and not path.is_symlink():
        return {"available": False, "reason": "acp_control_not_started_or_legacy",
                "session_ref": binding.reference, "delivery_modes": {"reply": "live_selected_option", "steer": "queued_next_turn"}}
    view = binding.control.snapshot(binding.reference, binding.identity)
    view.update(available=True, session_ref=binding.reference,
                for_delegate=view["turn_id"] == binding.delegate_id,
                delivery_modes={"reply": "live_selected_option", "steer": "queued_next_turn"})
    return view


def enrich_control_snapshot(registry: Any, snapshot: dict) -> dict:
    """Add control and advisory progress without recursively obtaining a snapshot.

    The enclosing status route must perform its normal owner filtering first.
    Missing control files are expected for queued and pre-mailbox delegates.
    """
    if snapshot.get("execution_backend") != "acp":
        return snapshot
    result = dict(snapshot)
    try:
        binding = _binding(registry, snapshot, snapshot.get("delegate_id"))
        result["acp_control"] = _view(binding)
        result["supervision"] = _supervision_view(binding)
    except AcpStateError as exc:
        result["acp_control"] = {"available": False, "reason": exc.code}
    except (OSError, ValueError, TypeError):
        result["acp_control"] = {"available": False, "reason": "acp_control_unavailable"}
    result["acp_progress"] = progress_snapshot(snapshot)
    return result


def _supervision_view(binding):
    try:
        store = SupervisorStore(binding.state_root / "supervision")
        view = store.snapshot(supervisor_binding(binding.identity, binding.reference, binding.delegate_id))
        active = binding.source.get("status") == "running" and not binding.source.get("completed")
        view["worker_active"] = active
        if not active:
            view["decision_required"] = False
        return view
    except SupervisionError as exc:
        return {"available": False, "reason": exc.code}
    except (OSError, ValueError, TypeError):
        return {"available": False, "reason": "supervisor_state_unavailable"}


def supervise(registry: Any, delegate_id: str, logical_session_id: str, *, action: str = "status",
              checkpoint_id: str | None = None, expected_revision: int | None = None,
              command_id: str | None = None, instruction: str = "") -> dict:
    """ACP controller adapter. Binding/ownership are derived, never model-selected."""
    try:
        binding = _owned(registry, delegate_id, logical_session_id)
        if action == "status":
            return {"success": True, "delegate_id": delegate_id, "supervision": _supervision_view(binding)}
        _active(registry, binding)
        store = SupervisorStore(binding.state_root / "supervision")
        view = store.command(supervisor_binding(binding.identity, binding.reference, delegate_id),
                             action=action, command_id=command_id, expected_revision=expected_revision,
                             checkpoint_id=checkpoint_id, instruction=instruction)
        return {"success": True, "delegate_id": delegate_id, "supervision": view}
    except (AcpStateError, SupervisionError) as exc:
        return _failure(exc.code)
    except (OSError, ValueError, TypeError):
        return _failure("supervisor_state_unavailable")


def _draft_source_terminal(registry, binding):
    if (binding.source.get("completed") is not True
            or _job_status(registry, binding) not in TERMINAL_JOB_STATUSES):
        raise AcpStateError("draft_source_not_terminal")


def draft_operation(registry: Any, delegate_id: str, logical_session_id: str, *,
                    action: str = "status", snapshot_id: str | None = None) -> dict:
    """Inspect or explicitly discard saved artifacts, never the source worktree."""
    try:
        binding = _owned(registry, delegate_id, logical_session_id)
        store = CandidateSnapshotStore(binding.state_root / "drafts")
        identity = supervisor_binding(binding.identity, binding.reference, delegate_id)
        if action == "status":
            return {"success": True, "delegate_id": delegate_id, "drafts": store.list(identity), "accepted": False}
        if action != "discard":
            raise AcpStateError("draft_action_invalid")
        _draft_source_terminal(registry, binding)
        with binding.sessions.lease(binding.reference):
            return {"success": True, "delegate_id": delegate_id, **store.discard(identity, snapshot_id)}
    except (AcpStateError, SupervisionError) as exc:
        return _failure(exc.code)
    except (OSError, ValueError, TypeError):
        return _failure("draft_state_unavailable")


def resolve_draft_reference(registry: Any, delegate_id: str, logical_session_id: str, *,
                            snapshot_id: str, account: str, thinking: str, workspace: str) -> dict:
    """Resolve user IDs into a trusted binding before task admission."""
    binding = _owned(registry, delegate_id, logical_session_id)
    if (binding.source.get("kind") != "code" or binding.identity.account != account
            or binding.identity.thinking != thinking or binding.identity.workspace != workspace):
        raise AcpStateError("draft_execution_binding_mismatch")
    _draft_source_terminal(registry, binding)
    with binding.sessions.lease(binding.reference):
        checkpoint = binding.sessions.load(binding.reference, binding.identity)
        if checkpoint.get("phase") == "running":
            raise AcpStateError("draft_source_requires_reconciliation")
        identity = supervisor_binding(binding.identity, binding.reference, delegate_id)
        records = CandidateSnapshotStore(binding.state_root / "drafts").list(identity)
        if snapshot_id not in {r["snapshot_id"] for r in records}:
            raise AcpStateError("draft_snapshot_not_found")
    return {"source_execution_id": delegate_id, "snapshot_id": snapshot_id,
            "session_ref": binding.reference, "identity": asdict(binding.identity)}


def control_status(registry: Any, delegate_id: str, logical_session_id: str) -> dict:
    try:
        binding = _owned(registry, delegate_id, logical_session_id)
        return {"success": True, "delegate_id": delegate_id, "control": _view(binding)}
    except AcpStateError as exc:
        return _failure(exc.code)
    except (OSError, ValueError, TypeError):
        return _failure("acp_control_unavailable")


def send_message(registry: Any, delegate_id: str, logical_session_id: str, *,
                 action: str, turn_id: str | None = None, request_id: str | None = None,
                 option_id: str | None = None, message_id: str | None = None, text: str | None = None) -> dict:
    try:
        binding = _owned(registry, delegate_id, logical_session_id)
        if turn_id is not None and turn_id != delegate_id:
            raise AcpStateError("acp_control_turn_mismatch")
        if action == "reply":
            if message_id is not None or text is not None:
                raise AcpStateError("acp_control_reply_requires_selected_option")
            _active(registry, binding)
            receipt = binding.control.reply(binding.reference, binding.identity, turn_id=delegate_id,
                                            request_id=request_id, option_id=option_id)
        elif action == "steer":
            if request_id is not None or option_id is not None:
                raise AcpStateError("acp_control_steer_requires_text")
            if binding.source.get("completed") is True:
                if binding.source.get("status") not in _TERMINAL or _job_status(registry, binding) not in TERMINAL_JOB_STATUSES:
                    raise AcpStateError("acp_control_worker_not_terminal")
                checkpoint = _checkpoint(binding)
                if checkpoint.get("phase") in {"running", "abandoned"}:
                    raise AcpStateError("acp_control_session_frozen")
            else:
                _active(registry, binding)
            receipt = binding.control.enqueue(binding.reference, binding.identity, source_turn_id=delegate_id,
                                              message_id=message_id, text=text)
        else:
            raise AcpStateError("acp_control_action_invalid")
        return {"success": True, "delegate_id": delegate_id, "control": receipt}
    except AcpStateError as exc:
        return _failure(exc.code)
    except (OSError, ValueError, TypeError):
        return _failure("acp_control_unavailable")


def reconcile(registry: Any, delegate_id: str, logical_session_id: str, *,
              action: str = "abandon", reason: str) -> dict:
    try:
        binding = _owned(registry, delegate_id, logical_session_id)
        if action != "abandon":
            raise AcpStateError("acp_reconcile_action_invalid")
        if binding.source.get("completed") is not True or binding.source.get("status") not in _ABANDONABLE:
            raise AcpStateError("acp_abandon_requires_terminal_failed_delegate")
        if _job_status(registry, binding) not in TERMINAL_JOB_STATUSES:
            raise AcpStateError("acp_control_worker_not_terminal")
        _checkpoint(binding)
        receipt = binding.sessions.abandon(binding.reference, binding.identity, turn_id=delegate_id, reason=reason)
        # The frozen session checkpoint is authoritative even if the best-effort
        # mailbox annotation cannot be completed after an MCP interruption.
        path = binding.state_root / "control" / (binding.reference + ".json")
        if path.exists() or path.is_symlink():
            try:
                binding.control.finish_turn(binding.reference, binding.identity, turn_id=delegate_id, status="abandoned")
            except (AcpStateError, OSError, ValueError, TypeError):
                receipt["mailbox_closed"] = False
            else:
                receipt["mailbox_closed"] = True
        return {"success": True, "delegate_id": delegate_id, "reconciliation": receipt}
    except AcpStateError as exc:
        return _failure(exc.code)
    except (OSError, ValueError, TypeError):
        return _failure("acp_control_unavailable")
