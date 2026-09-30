from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .session_continuation import (
    observe_job_result,
    release_result_claim_capacity,
    reserve_result_claim_capacity,
)
from .tool_context import ToolContext


def start_owned_job(
    ctx: ToolContext,
    *,
    tool_name: str,
    command: str,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    name: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    """Start one durable job only when logical-session ownership can be persisted."""
    try:
        claim_reservation_id = reserve_result_claim_capacity(ctx)
    except (OSError, TypeError, ValueError) as exc:
        return {
            "success": False,
            "error": {
                "code": "session_ownership_admission_failed",
                "message": (
                    "The durable job was not started because logical-session "
                    "ownership could not be reserved safely."
                ),
            },
            "ownership_error": f"{type(exc).__name__}: {exc}",
        }

    result = ctx.job_registry.start_job(
        command=command,
        cwd=cwd,
        state_dir=ctx.state_dir,
        env=dict(env) if env is not None else None,
        name=name,
        timeout_seconds=timeout_seconds,
    )
    job_id = str(result.get("job_id") or "").strip()
    if result.get("success") is False or not job_id:
        try:
            release_result_claim_capacity(ctx, claim_reservation_id)
        except (OSError, TypeError, ValueError) as exc:
            result["resume_checkpoint_warning"] = (
                "Automatic ownership reservation cleanup failed: "
                f"{type(exc).__name__}: {exc}"
            )
        return result

    try:
        observe_job_result(
            ctx,
            tool_name=tool_name,
            result=result,
            cwd=str(cwd),
            claim=True,
            claim_reservation_id=claim_reservation_id,
        )
    except (OSError, TypeError, ValueError) as exc:
        try:
            release_result_claim_capacity(ctx, claim_reservation_id)
        except (OSError, TypeError, ValueError):
            pass
        cleanup = ctx.job_registry.kill_job(
            job_id=job_id,
            state_dir=ctx.state_dir,
            signal_name="TERM",
        )
        return {
            "success": False,
            "error": {
                "code": "session_ownership_persistence_failed",
                "message": (
                    "The durable job started, but logical-session ownership could not "
                    "be persisted. The server attempted to terminate the unowned job."
                ),
            },
            "job_id": job_id,
            "ownership_error": f"{type(exc).__name__}: {exc}",
            "cleanup": cleanup,
        }
    return result
