"""Logical-session ownership and durable result-inbox orchestration."""

from __future__ import annotations

from copy import deepcopy
import json
import time
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult

from . import session
from .job_supervisor import TERMINAL_JOB_STATUSES
from .session_checkpoints import POLL_REQUIRED, RESULT_REQUIRES_CONSUMPTION


TERMINAL_DELEGATE_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "timed_out"}
)
_TERMINAL_SUMMARY_MAX_CHARS = 4096
_TERMINAL_STRUCTURED_OUTPUT_MAX_BYTES = 16384
_MIDDLEWARE_PENDING_LIMIT = 8


def _bounded_structured_output(value: object) -> tuple[object | None, bool]:
    if value is None:
        return None, False
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None, True
    if len(encoded) > _TERMINAL_STRUCTURED_OUTPUT_MAX_BYTES:
        return None, True
    return value, False


def job_state_from_result(
    result: dict[str, object],
) -> tuple[str | None, dict[str, Any]]:
    job_id = str(result.get("job_id") or "").strip() or None
    status = str(result.get("status") or "").strip().lower() or "unknown"
    terminal = status in TERMINAL_JOB_STATUSES or result.get("exit_code") is not None
    state: dict[str, Any] = {
        "status": status,
        "name": result.get("name"),
        "exit_code": result.get("exit_code"),
        "success": result.get("success"),
        "terminal": terminal,
    }
    for key in (
        "termination_reason",
        "log_limit_exceeded",
        "stdout_bytes",
        "stderr_bytes",
        "log_bytes",
        "last_output_at",
        "cwd",
    ):
        value = result.get(key)
        if value is not None:
            state[key] = value
    return job_id, state


def delegate_state_from_result(
    result: dict[str, object],
    *,
    delegate_id: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    snapshot = result.get("delegate")
    if not isinstance(snapshot, dict):
        snapshot = result
    resolved_delegate_id = (
        delegate_id or str(snapshot.get("delegate_id") or "")
    ).strip() or None
    status = str(snapshot.get("status") or "").strip().lower() or "unknown"
    terminal = bool(snapshot.get("completed")) or status in TERMINAL_DELEGATE_STATUSES
    state: dict[str, Any] = {
        "status": status,
        "completed": bool(snapshot.get("completed")),
        "success": snapshot.get("success"),
        "harness": snapshot.get("harness"),
        "activity_state": snapshot.get("activity_state"),
        "terminal": terminal,
    }
    if terminal:
        summary = str(snapshot.get("summary") or "")
        if summary:
            state["summary"] = summary[:_TERMINAL_SUMMARY_MAX_CHARS]
            state["summary_truncated"] = (
                len(summary) > _TERMINAL_SUMMARY_MAX_CHARS
            )
        for key in (
            "error",
            "logs",
            "exit_code",
            "model",
            "reasoning_effort",
            "timed_out",
            "sandbox_mode",
            "recovered_from_disk",
        ):
            value = snapshot.get(key)
            if value is not None:
                state[key] = value
        structured_output, omitted = _bounded_structured_output(
            snapshot.get("structured_output")
        )
        if structured_output is not None:
            state["structured_output"] = structured_output
        if omitted:
            state["structured_output_omitted"] = True
    cwd = snapshot.get("cwd")
    if cwd is not None:
        state["cwd"] = cwd
    group_id = snapshot.get("group_id")
    if group_id is not None:
        state["group_id"] = group_id
    return resolved_delegate_id, state


def result_access_scope(
    ctx: Any,
    *,
    kind: str,
    result_id: str,
) -> str:
    session_key = session.get_current_session_id()
    if not session_key:
        return "unscoped"
    checkpoint_scope = ctx.checkpoint_store.result_ownership_scope(
        session_key,
        kind=kind,
        result_id=result_id,
    )
    if kind != "delegate":
        return checkpoint_scope
    registry_scope = "unowned"
    ownership_scope = getattr(ctx.registry, "delegate_ownership_scope", None)
    if callable(ownership_scope):
        registry_scope = ownership_scope(
            logical_session_id=session_key,
            delegate_id=result_id,
        )
    return _merge_ownership_scopes(checkpoint_scope, registry_scope)


def delegate_group_access_scope(
    ctx: Any,
    *,
    group_id: str,
) -> str:
    session_key = session.get_current_session_id()
    if not session_key:
        return "unscoped"
    checkpoint_scope = ctx.checkpoint_store.delegate_group_ownership_scope(
        session_key,
        group_id=group_id,
    )
    registry_scope = "unowned"
    ownership_scope = getattr(ctx.registry, "delegate_ownership_scope", None)
    if callable(ownership_scope):
        registry_scope = ownership_scope(
            logical_session_id=session_key,
            group_id=group_id,
        )
    return _merge_ownership_scopes(checkpoint_scope, registry_scope)


def reserve_result_claim_capacity(ctx: Any, *, slots: int = 1) -> str | None:
    session_key = session.get_current_session_id()
    if not session_key:
        return None
    return ctx.checkpoint_store.reserve_claim_capacity(
        session_key,
        slots=slots,
    )


def release_result_claim_capacity(
    ctx: Any,
    reservation_id: str | None,
) -> bool:
    session_key = session.get_current_session_id()
    if not session_key or not reservation_id:
        return False
    return ctx.checkpoint_store.release_claim_reservation(
        session_key,
        reservation_id,
    )


def foreign_owned_result_ids(ctx: Any, *, kind: str) -> set[str]:
    session_key = session.get_current_session_id()
    if not session_key:
        return set()
    result = ctx.checkpoint_store.foreign_owned_result_ids(
        session_key,
        kind=kind,
    )
    if kind == "delegate":
        foreign_delegate_ids = getattr(ctx.registry, "foreign_delegate_ids", None)
        if callable(foreign_delegate_ids):
            result.update(
                foreign_delegate_ids(logical_session_id=session_key)
            )
    return result


def _merge_ownership_scopes(left: str, right: str) -> str:
    if "conflict" in {left, right}:
        return "conflict"
    concrete = {scope for scope in (left, right) if scope != "unowned"}
    if not concrete:
        return "unowned"
    if concrete == {"owned_here"}:
        return "owned_here"
    if concrete == {"owned_elsewhere"}:
        return "owned_elsewhere"
    return "conflict"


def _owned_ids(checkpoint: dict[str, Any], *, kind: str) -> list[str]:
    runtime = checkpoint.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    if kind == "job":
        collection_key = "jobs"
        order_key = "job_order"
    elif kind == "delegate":
        collection_key = "delegates"
        order_key = "delegate_order"
    else:
        raise ValueError(f"Unsupported owned-work kind: {kind}")

    collection = runtime.get(collection_key)
    if not isinstance(collection, dict):
        collection = {}
    order = runtime.get(order_key)
    if not isinstance(order, list):
        order = list(collection)

    ids: list[str] = []
    for value in [*order, *collection]:
        if not isinstance(value, str) or not value or value in ids:
            continue
        ids.append(value)
    return ids


def owns_result(
    checkpoint: dict[str, Any],
    *,
    kind: str,
    result_id: str,
) -> bool:
    return result_id in _owned_ids(checkpoint, kind=kind)


def _previous_state(
    checkpoint: dict[str, Any],
    *,
    kind: str,
    result_id: str,
) -> dict[str, Any]:
    runtime = checkpoint.get("runtime")
    if not isinstance(runtime, dict):
        return {}
    collection = runtime.get("jobs" if kind == "job" else "delegates")
    if not isinstance(collection, dict):
        return {}
    state = collection.get(result_id)
    return deepcopy(state) if isinstance(state, dict) else {}


def _record_job(
    ctx: Any,
    session_key: str,
    result: dict[str, object],
    *,
    tool_name: str | None,
    claim: bool = False,
    claim_reservation_id: str | None = None,
) -> None:
    job_id, state = job_state_from_result(result)
    if not job_id:
        return
    if (
        not claim
        and ctx.checkpoint_store.result_ownership_scope(
            session_key,
            kind="job",
            result_id=job_id,
        )
        != "owned_here"
    ):
        return
    terminal = bool(state.get("terminal"))
    ctx.checkpoint_store.record_runtime(
        session_key=session_key,
        last_tool=tool_name,
        cwd=str(result.get("cwd") or "") or None,
        jobs={job_id: state},
        claim_reservation_id=claim_reservation_id,
        next_action=(
            (
                "Consume the terminal durable-job result and continue the current conversation plan."
                if terminal
                else "Poll or inspect the owned durable job until terminal, then continue the current conversation plan."
            )
            if tool_name is not None
            else None
        ),
    )


def _record_delegate(
    ctx: Any,
    session_key: str,
    result: dict[str, object],
    *,
    tool_name: str | None,
    delegate_id: str | None = None,
    cwd: str | None = None,
    claim: bool = False,
    claim_reservation_id: str | None = None,
) -> None:
    resolved_delegate_id, state = delegate_state_from_result(
        result,
        delegate_id=delegate_id,
    )
    if not resolved_delegate_id:
        return
    if (
        not claim
        and ctx.checkpoint_store.result_ownership_scope(
            session_key,
            kind="delegate",
            result_id=resolved_delegate_id,
        )
        != "owned_here"
    ):
        return
    terminal = bool(state.get("terminal"))
    resolved_cwd = str(state.get("cwd") or cwd or "") or None
    ctx.checkpoint_store.record_runtime(
        session_key=session_key,
        last_tool=tool_name,
        cwd=resolved_cwd,
        delegates={resolved_delegate_id: state},
        claim_reservation_id=claim_reservation_id,
        next_action=(
            (
                "Consume and independently verify the terminal delegate result, then continue the current conversation plan."
                if terminal
                else "Poll or inspect the owned delegate until terminal, then continue the current conversation plan."
            )
            if tool_name is not None
            else None
        ),
    )


def observe_job_result(
    ctx: Any,
    *,
    tool_name: str,
    result: dict[str, object],
    job_id: str | None = None,
    cwd: str | None = None,
    claim: bool = False,
    claim_reservation_id: str | None = None,
) -> None:
    session_key = session.get_current_session_id()
    if not session_key:
        return
    normalized = dict(result)
    if job_id and not normalized.get("job_id"):
        normalized["job_id"] = job_id
    if cwd and not normalized.get("cwd"):
        normalized["cwd"] = cwd
    _record_job(
        ctx,
        session_key,
        normalized,
        tool_name=tool_name,
        claim=claim,
        claim_reservation_id=claim_reservation_id,
    )


def observe_delegate_result(
    ctx: Any,
    *,
    tool_name: str,
    result: dict[str, object],
    delegate_id: str | None = None,
    cwd: str | None = None,
    claim: bool = False,
    claim_reservation_id: str | None = None,
) -> None:
    session_key = session.get_current_session_id()
    if not session_key:
        return
    _record_delegate(
        ctx,
        session_key,
        result,
        tool_name=tool_name,
        delegate_id=delegate_id,
        cwd=cwd,
        claim=claim,
        claim_reservation_id=claim_reservation_id,
    )


def observe_delegate_group_result(
    ctx: Any,
    *,
    tool_name: str,
    result: dict[str, object],
    cwd: str | None = None,
    claim: bool = False,
    claim_reservation_id: str | None = None,
) -> None:
    session_key = session.get_current_session_id()
    if not session_key:
        return
    group = result.get("group")
    if not isinstance(group, dict):
        group = result
    children = group.get("children")
    if not isinstance(children, list):
        return
    harness = group.get("harness") or group.get("executor")
    group_id = str(group.get("group_id") or result.get("group_id") or "").strip()
    states: dict[str, dict[str, Any]] = {}
    resolved_cwd: str | None = None
    any_terminal = False
    for child in children:
        if not isinstance(child, dict):
            continue
        delegate_id = str(child.get("delegate_id") or "").strip()
        if not delegate_id:
            continue
        if (
            not claim
            and ctx.checkpoint_store.result_ownership_scope(
                session_key,
                kind="delegate",
                result_id=delegate_id,
            )
            != "owned_here"
        ):
            continue

        child_result = child.get("result")
        if isinstance(child_result, dict):
            delegate_snapshot = dict(child_result)
            if group_id and not delegate_snapshot.get("group_id"):
                delegate_snapshot["group_id"] = group_id
            snapshot: dict[str, object] = {
                "success": True,
                "delegate": delegate_snapshot,
            }
        else:
            child_status = str(child.get("status") or "").strip().lower()
            terminal_hint = bool(child.get("completed")) or (
                child_status in TERMINAL_DELEGATE_STATUSES
            )
            if terminal_hint and tool_name == "delegate_status":
                refreshed = ctx.registry.delegate_status(
                    delegate_id=delegate_id,
                    watch_seconds=0.0,
                    poll_seconds=0.1,
                    max_tokens=ctx.tool_output_token_budget,
                )
                delegate_snapshot = refreshed.get("delegate")
                if not (
                    refreshed.get("success") is True
                    and isinstance(delegate_snapshot, dict)
                ):
                    continue
                delegate_snapshot = dict(delegate_snapshot)
                if group_id and not delegate_snapshot.get("group_id"):
                    delegate_snapshot["group_id"] = group_id
                snapshot = {
                    "success": True,
                    "delegate": delegate_snapshot,
                }
            elif terminal_hint:
                delegate_snapshot = dict(child)
                if group_id and not delegate_snapshot.get("group_id"):
                    delegate_snapshot["group_id"] = group_id
                snapshot = {
                    "success": True,
                    "delegate": delegate_snapshot,
                }
            else:
                snapshot = {
                    "success": True,
                    "delegate": {
                        "delegate_id": delegate_id,
                        "status": child.get("status"),
                        "completed": False,
                        "harness": harness,
                        "cwd": cwd,
                        "group_id": group_id or None,
                    },
                }

        resolved_delegate_id, state = delegate_state_from_result(
            snapshot,
            delegate_id=delegate_id,
        )
        if not resolved_delegate_id:
            continue
        states[resolved_delegate_id] = state
        any_terminal = any_terminal or bool(state.get("terminal"))
        if resolved_cwd is None:
            resolved_cwd = str(state.get("cwd") or cwd or "") or None

    if not states:
        return
    ctx.checkpoint_store.record_runtime(
        session_key=session_key,
        last_tool=tool_name,
        cwd=resolved_cwd,
        delegates=states,
        claim_reservation_id=claim_reservation_id,
        next_action=(
            "Consume and independently verify terminal delegate results, then continue the current conversation plan."
            if any_terminal
            else "Poll or inspect the owned delegates until terminal, then continue the current conversation plan."
        ),
    )


def _refresh_job(
    ctx: Any,
    *,
    session_key: str,
    job_id: str,
    wait_seconds: float = 0.0,
) -> dict[str, object]:
    checkpoint = ctx.checkpoint_store.get(session_key) or {}
    previous = _previous_state(
        checkpoint,
        kind="job",
        result_id=job_id,
    )
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    while True:
        result = ctx.job_registry.job_status(
            job_id=job_id,
            state_dir=ctx.state_dir,
        )
        if result.get("success") is False and bool(previous.get("terminal")):
            return {
                "success": True,
                "job_id": job_id,
                **previous,
            }
        _record_job(
            ctx,
            session_key,
            result,
            tool_name=None,
        )
        status = str(result.get("status") or "").lower()
        if status in TERMINAL_JOB_STATUSES or result.get("exit_code") is not None:
            return result
        if time.monotonic() >= deadline:
            return result
        time.sleep(min(0.1, max(0.01, deadline - time.monotonic())))


def _refresh_delegate(
    ctx: Any,
    *,
    session_key: str,
    delegate_id: str,
    wait_seconds: float = 0.0,
) -> dict[str, object]:
    checkpoint = ctx.checkpoint_store.get(session_key) or {}
    previous = _previous_state(
        checkpoint,
        kind="delegate",
        result_id=delegate_id,
    )
    result = ctx.registry.delegate_status(
        delegate_id=delegate_id,
        watch_seconds=max(0.0, float(wait_seconds)),
        poll_seconds=min(5.0, max(0.1, float(wait_seconds) or 0.1)),
        max_tokens=ctx.tool_output_token_budget,
    )
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict) and error.get("code") == "delegate_not_found":
        previous_status = str(previous.get("status") or "").lower()
        previous_terminal = bool(
            previous.get("terminal")
            or previous.get("completed")
            or previous_status in TERMINAL_DELEGATE_STATUSES
        )
        if previous_terminal:
            result = {
                "success": True,
                "delegate": {
                    **previous,
                    "delegate_id": delegate_id,
                    "completed": True,
                    "in_progress": False,
                    "terminal": True,
                },
                "complete": True,
            }
        else:
            result = {
                "success": True,
                "delegate": {
                    **previous,
                    "success": False,
                    "delegate_id": delegate_id,
                    "status": "cancelled",
                    "completed": True,
                    "in_progress": False,
                    "terminal": True,
                    "error": {
                        "code": "server_restart",
                        "message": (
                            "The non-durable delegate was interrupted by an MCP server "
                            "restart before a terminal result was recorded."
                        ),
                    },
                },
                "complete": True,
            }
    _record_delegate(
        ctx,
        session_key,
        result,
        tool_name=None,
        delegate_id=delegate_id,
    )
    return result


def refresh_session_owned_work(
    ctx: Any,
    *,
    session_key: str,
    kind: str | None = None,
    result_id: str | None = None,
    wait_seconds: float = 0.0,
) -> dict[str, object]:
    checkpoint = ctx.checkpoint_store.get(session_key)
    if checkpoint is None:
        return {
            "success": True,
            "resumable": False,
            "jobs": [],
            "delegates": [],
            "pending_results": [],
        }

    jobs: list[dict[str, object]] = []
    delegates: list[dict[str, object]] = []
    if kind in {None, "job"}:
        job_ids = _owned_ids(checkpoint, kind="job")
        if result_id is not None and kind == "job":
            job_ids = [result_id] if result_id in job_ids else []
        for job_id in job_ids:
            previous = _previous_state(
                checkpoint,
                kind="job",
                result_id=job_id,
            )
            if result_id is None and bool(previous.get("terminal")):
                jobs.append({"job_id": job_id, **previous})
                continue
            jobs.append(
                _refresh_job(
                    ctx,
                    session_key=session_key,
                    job_id=job_id,
                    wait_seconds=(
                        wait_seconds
                        if kind == "job" and result_id == job_id
                        else 0.0
                    ),
                )
            )

    checkpoint = ctx.checkpoint_store.get(session_key) or checkpoint
    if kind in {None, "delegate"}:
        delegate_ids = _owned_ids(checkpoint, kind="delegate")
        if result_id is not None and kind == "delegate":
            delegate_ids = [result_id] if result_id in delegate_ids else []
        for delegate_id in delegate_ids:
            previous = _previous_state(
                checkpoint,
                kind="delegate",
                result_id=delegate_id,
            )
            if result_id is None and bool(previous.get("terminal")):
                delegates.append(
                    {
                        "success": True,
                        "delegate": {
                            "delegate_id": delegate_id,
                            **previous,
                        },
                        "complete": True,
                    }
                )
                continue
            delegates.append(
                _refresh_delegate(
                    ctx,
                    session_key=session_key,
                    delegate_id=delegate_id,
                    wait_seconds=(
                        wait_seconds
                        if kind == "delegate" and result_id == delegate_id
                        else 0.0
                    ),
                )
            )

    return {
        "success": True,
        "resumable": True,
        "jobs": jobs,
        "delegates": delegates,
        "pending_results": ctx.checkpoint_store.pending_results(session_key),
    }


def _pending_meta(pending: list[dict[str, Any]]) -> dict[str, object]:
    compact = []
    for item in pending[:_MIDDLEWARE_PENDING_LIMIT]:
        compact.append(
            {
                "kind": item.get("kind"),
                "id": item.get("id"),
                "status": item.get("status"),
                "success": item.get("success"),
                "result_ready_at": item.get("result_ready_at"),
            }
        )
    return {
        "state": "RESULT_UNCONSUMED",
        "required_action": RESULT_REQUIRES_CONSUMPTION,
        "pending_count": len(pending),
        "results": compact,
        "truncated": len(pending) > len(compact),
    }


def summarize_abandoned_checkpoint(
    checkpoint: dict[str, Any] | None,
) -> dict[str, object]:
    if not isinstance(checkpoint, dict):
        return {
            "continuation_abandoned": False,
            "semantic_checkpoint_abandoned": False,
            "pending_result_count": 0,
            "owned_in_progress_count": 0,
            "pending_results": [],
            "owned_in_progress": [],
            "truncated": False,
        }

    runtime = checkpoint.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    pending: list[dict[str, object]] = []
    in_progress: list[dict[str, object]] = []
    for kind, collection_key in (("job", "jobs"), ("delegate", "delegates")):
        collection = runtime.get(collection_key)
        if not isinstance(collection, dict):
            continue
        for result_id, state in collection.items():
            if not isinstance(state, dict):
                continue
            compact = {
                "kind": kind,
                "id": str(result_id),
                "status": state.get("status"),
            }
            continuation_state = state.get("continuation_state")
            if continuation_state == RESULT_REQUIRES_CONSUMPTION:
                pending.append(compact)
            elif continuation_state == POLL_REQUIRED:
                in_progress.append(compact)

    semantic_abandoned = any(
        bool(checkpoint.get(key))
        for key in ("goal", "current_slice", "next_action", "notes")
    )
    return {
        "continuation_abandoned": bool(
            semantic_abandoned or pending or in_progress
        ),
        "semantic_checkpoint_abandoned": semantic_abandoned,
        "pending_result_count": len(pending),
        "owned_in_progress_count": len(in_progress),
        "pending_results": pending[:_MIDDLEWARE_PENDING_LIMIT],
        "owned_in_progress": in_progress[:_MIDDLEWARE_PENDING_LIMIT],
        "truncated": (
            len(pending) > _MIDDLEWARE_PENDING_LIMIT
            or len(in_progress) > _MIDDLEWARE_PENDING_LIMIT
        ),
    }


class SessionContinuationMiddleware(Middleware):
    """Surface newly terminal owned work on the next MCP tool interaction."""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Any,
    ) -> ToolResult:
        session_key = session.get_current_session_id()
        warning: str | None = None
        if session_key:
            try:
                refresh_session_owned_work(
                    self.ctx,
                    session_key=session_key,
                )
            except (OSError, TypeError, ValueError, RuntimeError) as exc:
                warning = f"{type(exc).__name__}: {exc}"

        result = await call_next(context)

        if not session_key:
            return result
        try:
            pending = self.ctx.checkpoint_store.pending_results(session_key)
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            pending = []
            warning = warning or f"{type(exc).__name__}: {exc}"

        meta = dict(result.meta or {})
        if pending:
            meta["session_continuation"] = _pending_meta(pending)
            session.registry.note_execution_state(
                required_action=RESULT_REQUIRES_CONSUMPTION,
                state="RESULT_UNCONSUMED",
                session_id=session_key,
            )
        elif warning:
            meta["session_continuation_warning"] = warning
        result.meta = meta or None
        return result
