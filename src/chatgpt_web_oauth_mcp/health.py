from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import time
from typing import Any

from . import session


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _project_name(raw_cwd: object) -> str | None:
    if not isinstance(raw_cwd, str) or not raw_cwd:
        return None
    return Path(raw_cwd).name or raw_cwd


class OpsHealthSnapshot:
    """Build one canonical health view for sessions, delegates, and durable jobs."""

    def __init__(
        self,
        *,
        registry: Any,
        job_registry: Any,
        activity_tracker: Any,
        state_dir: Path,
        tool_output_token_budget: int,
        session_idle_ttl_seconds: float,
        session_active_window_seconds: float,
        session_request_stall_seconds: float,
        session_orchestration_quiet_seconds: float,
        session_limit: int,
        session_ephemeral_idle_ttl_seconds: float | None = None,
    ) -> None:
        self.registry = registry
        self.job_registry = job_registry
        self.activity_tracker = activity_tracker
        self.state_dir = state_dir
        self.tool_output_token_budget = tool_output_token_budget
        self.session_idle_ttl_seconds = session_idle_ttl_seconds
        self.session_ephemeral_idle_ttl_seconds = min(
            session_idle_ttl_seconds,
            (
                session_idle_ttl_seconds
                if session_ephemeral_idle_ttl_seconds is None
                else max(0.0, session_ephemeral_idle_ttl_seconds)
            ),
        )
        self.session_active_window_seconds = session_active_window_seconds
        self.session_request_stall_seconds = session_request_stall_seconds
        self.session_orchestration_quiet_seconds = session_orchestration_quiet_seconds
        self.session_limit = session_limit
        self.started_at = time.time()

    def snapshot(self) -> dict[str, object]:
        sessions = session.registry.snapshot(
            idle_ttl_seconds=self.session_idle_ttl_seconds,
            ephemeral_idle_ttl_seconds=self.session_ephemeral_idle_ttl_seconds,
            request_stall_seconds=self.session_request_stall_seconds,
            orchestration_quiet_seconds=self.session_orchestration_quiet_seconds,
            active_window_seconds=self.session_active_window_seconds,
            limit=self.session_limit,
        )

        delegate_result = self.registry.delegate_status(
            limit=20,
            watch_seconds=0,
            max_tokens=self.tool_output_token_budget,
        )
        delegates_ok = bool(delegate_result.get("success"))
        active_delegates = (
            delegate_result.get("active_delegates", [])
            if delegates_ok and isinstance(delegate_result.get("active_delegates"), list)
            else []
        )
        delegate_rows: list[dict[str, object]] = []
        for item in active_delegates:
            if not isinstance(item, dict):
                continue
            delegate_rows.append(
                {
                    "delegate_id": item.get("delegate_id"),
                    "group_id": item.get("group_id"),
                    "harness": item.get("harness"),
                    "kind": item.get("kind"),
                    "project": _project_name(item.get("cwd")),
                    "status": item.get("status"),
                    "activity_state": item.get("activity_state"),
                    "pid": item.get("pid"),
                    "elapsed_seconds": item.get("elapsed_seconds"),
                    "last_output_seconds_ago": item.get("last_output_seconds_ago"),
                    "stdout_bytes": item.get("stdout_bytes"),
                    "stderr_bytes": item.get("stderr_bytes"),
                }
            )

        jobs_result = self.job_registry.list_active_jobs(
            state_dir=self.state_dir,
            limit=200,
        )
        jobs_ok = (
            bool(jobs_result.get("success"))
            and not bool(jobs_result.get("truncated"))
            and not bool(jobs_result.get("warnings"))
        )
        running_jobs = (
            jobs_result.get("jobs", [])
            if bool(jobs_result.get("success")) and isinstance(jobs_result.get("jobs"), list)
            else []
        )
        job_rows: list[dict[str, object]] = []
        for item in running_jobs:
            if not isinstance(item, dict):
                continue
            snapshot = self.job_registry.job_activity_snapshot(
                job_id=str(item.get("job_id") or ""),
                state_dir=self.state_dir,
            )
            assessment = self.activity_tracker.assess(snapshot)
            job_rows.append(
                {
                    "job_id": snapshot.get("job_id") or item.get("job_id"),
                    "name": snapshot.get("name") or item.get("name"),
                    "project": _project_name(snapshot.get("cwd") or item.get("cwd")),
                    "status": snapshot.get("status") or item.get("status"),
                    "pid": snapshot.get("pid"),
                    "verdict": assessment.get("verdict"),
                    "waiting_justified": assessment.get("waiting_justified"),
                    "quiet_for_seconds": assessment.get("quiet_for_seconds"),
                    "stdout_bytes": snapshot.get("stdout_bytes"),
                    "stderr_bytes": snapshot.get("stderr_bytes"),
                    "last_output_at": snapshot.get("last_output_at"),
                }
            )

        session_counts = sessions["counts"]
        delegate_states = [str(item.get("activity_state") or "") for item in delegate_rows]
        job_verdicts = [str(item.get("verdict") or "") for item in job_rows]

        summary: dict[str, object] = {
            "sessions": sessions["active_session_count"],
            "transport_sessions": sessions["transport_session_count"],
            "ephemeral_idle_sessions": sessions["ephemeral_idle_count"],
            "sessions_active": sessions["active_session_count"],
            "sessions_retained": sessions["session_count"],
            "sessions_inflight": session_counts["active"],
            "sessions_idle": session_counts["idle"],
            "sessions_orchestration_quiet": session_counts["orchestration_quiet"],
            "sessions_stalled": session_counts["stalled_request"],
            "delegates_active": sum(state == "active" for state in delegate_states),
            "delegates_quiet": sum(state == "starting_or_quiet" for state in delegate_states),
            "delegates_stalled": sum(state == "suspected_stalled" for state in delegate_states),
            "delegates_queued": sum(state == "queued" for state in delegate_states),
            "jobs_running": len(job_rows),
            "jobs_active": sum(verdict == "ACTIVE" for verdict in job_verdicts),
            "jobs_quiet": sum(verdict == "QUIET" for verdict in job_verdicts),
            "jobs_stalled": sum(verdict == "STALLED_SUSPECTED" for verdict in job_verdicts),
        }

        observation_errors: dict[str, object] = {}
        if not delegates_ok:
            observation_errors["delegates"] = delegate_result.get("error") or "unavailable"
        if not jobs_ok:
            observation_errors["jobs"] = jobs_result.get("error") or {
                "code": (
                    "job_list_truncated"
                    if jobs_result.get("truncated")
                    else "job_active_index_warning"
                    if jobs_result.get("warnings")
                    else "job_observation_unavailable"
                ),
                "warnings": jobs_result.get("warnings") or [],
            }

        hard_stall = (
            int(summary["sessions_stalled"])
            + int(summary["delegates_stalled"])
            + int(summary["jobs_stalled"])
        )
        # Quiet states are diagnostic, not faults. A process can legitimately
        # sleep or wait for I/O. Only STALLED_SUSPECTED is a hard runtime fault;
        # degraded is reserved for incomplete/unreliable observation. Likewise,
        # orchestration_quiet cannot prove a ChatGPT-side stall because the MCP
        # server cannot observe a normal user-visible final response.
        degraded = len(observation_errors)
        active = (
            int(summary["sessions_active"])
            + int(summary["delegates_active"])
            + int(summary["delegates_quiet"])
            + int(summary["delegates_queued"])
            + int(summary["jobs_running"])
        )

        if hard_stall:
            state = "stalled"
        elif degraded:
            state = "degraded"
        elif active:
            state = "active"
        else:
            state = "idle"

        return {
            "success": not observation_errors,
            "state": state,
            "timestamp": _iso_now(),
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - self.started_at, 3),
            "summary": summary,
            "sessions": sessions["sessions"],
            "sessions_truncated": sessions["truncated"],
            "delegates": delegate_rows,
            "jobs": job_rows,
            "observation_errors": observation_errors,
            "thresholds": {
                "session_idle_ttl_seconds": self.session_idle_ttl_seconds,
                "session_ephemeral_idle_ttl_seconds": self.session_ephemeral_idle_ttl_seconds,
                "session_active_window_seconds": self.session_active_window_seconds,
                "session_request_stall_seconds": self.session_request_stall_seconds,
                "session_orchestration_quiet_seconds": self.session_orchestration_quiet_seconds,
                "job_activity": self.activity_tracker.policy(),
            },
        }
