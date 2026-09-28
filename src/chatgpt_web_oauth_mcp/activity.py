from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
import time
from typing import Mapping


DEFAULT_RECENT_ACTIVITY_SECONDS = 30.0
DEFAULT_STALL_AFTER_SECONDS = 120.0
DEFAULT_STALL_MIN_OBSERVATIONS = 2
DEFAULT_MAX_TRACKED_JOBS = 256
_CPU_PROGRESS_EPSILON_SECONDS = 0.01


@dataclass(frozen=True)
class _Observation:
    observed_at: float
    cpu_seconds: float | None
    stdout_bytes: int | None
    stderr_bytes: int | None
    group_signature: tuple[tuple[int, str | None], ...]
    quiet_since: float
    quiet_observations: int


class ActivityTracker:
    """Compare durable-job activity snapshots without owning process lifecycle."""

    def __init__(
        self,
        *,
        recent_activity_seconds: float = DEFAULT_RECENT_ACTIVITY_SECONDS,
        stall_after_seconds: float = DEFAULT_STALL_AFTER_SECONDS,
        stall_min_observations: int = DEFAULT_STALL_MIN_OBSERVATIONS,
        max_tracked_jobs: int = DEFAULT_MAX_TRACKED_JOBS,
    ) -> None:
        self.recent_activity_seconds = max(0.0, float(recent_activity_seconds))
        self.stall_after_seconds = max(0.0, float(stall_after_seconds))
        self.stall_min_observations = max(2, int(stall_min_observations))
        self.max_tracked_jobs = max(1, int(max_tracked_jobs))
        self._observations: dict[str, _Observation] = {}
        self._lock = Lock()

    def policy(self) -> dict[str, object]:
        return {
            "recent_activity_seconds": self.recent_activity_seconds,
            "stall_after_seconds": self.stall_after_seconds,
            "stall_min_observations": self.stall_min_observations,
            "max_tracked_jobs": self.max_tracked_jobs,
        }

    def assess(self, snapshot: Mapping[str, object], *, now: float | None = None) -> dict[str, object]:
        with self._lock:
            return self._assess_unlocked(snapshot, now=now)

    def _assess_unlocked(self, snapshot: Mapping[str, object], *, now: float | None = None) -> dict[str, object]:
        observed_at = time.time() if now is None else float(now)
        job_id = str(snapshot.get("job_id") or "")
        pid = _as_int(snapshot.get("pid"))
        key = f"{job_id}:{pid if pid is not None else 'none'}"
        status = str(snapshot.get("status") or "")

        if status and status != "running":
            self._drop_job(job_id)
            return self._result(
                verdict="TERMINAL",
                waiting_justified=False,
                required_action="CONTINUE_AFTER_TERMINAL_JOB",
                reasons=[f"job_status={status}"],
                quiet_for_seconds=0.0,
                quiet_observations=0,
            )

        identity_match = snapshot.get("process_identity_match")
        group_verified = snapshot.get("process_group_verified")
        if identity_match is False:
            self._drop_key(key)
            return self._result(
                verdict="DEAD",
                waiting_justified=False,
                required_action="RECHECK_JOB_STATUS_OR_CONTINUE",
                reasons=["recorded_process_identity_is_gone_or_reused"],
                quiet_for_seconds=0.0,
                quiet_observations=0,
            )
        if identity_match is not True or group_verified is not True:
            return self._result(
                verdict="UNKNOWN",
                waiting_justified=False,
                required_action="INSPECT_PROCESS_IDENTITY_AND_GROUP",
                reasons=["process_identity_or_group_could_not_be_verified"],
                quiet_for_seconds=0.0,
                quiet_observations=0,
            )

        cpu_seconds = _as_float(snapshot.get("process_group_cpu_seconds"))
        stdout_bytes = _as_int(snapshot.get("stdout_bytes"))
        stderr_bytes = _as_int(snapshot.get("stderr_bytes"))
        group_signature = _group_signature(snapshot.get("process_group_signature"))
        last_output_at = _as_float(snapshot.get("last_output_at"))
        elapsed_seconds = _as_float(snapshot.get("elapsed_seconds"))
        previous = self._observations.get(key)

        reasons: list[str] = []
        if previous is not None:
            if (
                cpu_seconds is not None
                and previous.cpu_seconds is not None
                and cpu_seconds - previous.cpu_seconds >= _CPU_PROGRESS_EPSILON_SECONDS
            ):
                reasons.append("process_group_cpu_advanced")
            if _counter_advanced(stdout_bytes, previous.stdout_bytes) or _counter_advanced(
                stderr_bytes, previous.stderr_bytes
            ):
                reasons.append("job_output_grew")
            if group_signature != previous.group_signature:
                reasons.append("process_group_changed")
        else:
            if last_output_at is not None and 0.0 <= observed_at - last_output_at <= self.recent_activity_seconds:
                reasons.append("recent_job_output")
            if elapsed_seconds is not None and elapsed_seconds <= self.recent_activity_seconds:
                reasons.append("recent_job_start")

        if reasons:
            current = _Observation(
                observed_at=observed_at,
                cpu_seconds=cpu_seconds,
                stdout_bytes=stdout_bytes,
                stderr_bytes=stderr_bytes,
                group_signature=group_signature,
                quiet_since=observed_at,
                quiet_observations=0,
            )
            self._store(key, current)
            return self._result(
                verdict="ACTIVE",
                waiting_justified=True,
                required_action="POLL_OR_INSPECT_ACTIVE_PROCESS",
                reasons=reasons,
                quiet_for_seconds=0.0,
                quiet_observations=0,
            )

        quiet_since = previous.quiet_since if previous is not None else observed_at
        quiet_observations = (previous.quiet_observations + 1) if previous is not None else 1
        quiet_for = max(0.0, observed_at - quiet_since)
        current = _Observation(
            observed_at=observed_at,
            cpu_seconds=cpu_seconds,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            group_signature=group_signature,
            quiet_since=quiet_since,
            quiet_observations=quiet_observations,
        )
        self._store(key, current)

        if quiet_observations >= self.stall_min_observations and quiet_for >= self.stall_after_seconds:
            return self._result(
                verdict="STALLED_SUSPECTED",
                waiting_justified=False,
                required_action="INSPECT_STALLED_PROCESS_OR_CONTINUE_INDEPENDENT_WORK",
                reasons=["no_observable_progress_across_repeated_observations"],
                quiet_for_seconds=quiet_for,
                quiet_observations=quiet_observations,
            )

        return self._result(
            verdict="QUIET",
            waiting_justified=False,
            required_action="RECHECK_OR_INSPECT_QUIET_PROCESS",
            reasons=["no_observable_progress_in_current_window"],
            quiet_for_seconds=quiet_for,
            quiet_observations=quiet_observations,
        )

    def _drop_job(self, job_id: str) -> None:
        if not job_id:
            return
        for key in [item for item in self._observations if item.startswith(f"{job_id}:")]:
            self._observations.pop(key, None)

    def _drop_key(self, key: str) -> None:
        self._observations.pop(key, None)

    def _store(self, key: str, observation: _Observation) -> None:
        self._observations.pop(key, None)
        self._observations[key] = observation
        while len(self._observations) > self.max_tracked_jobs:
            oldest = next(iter(self._observations))
            self._observations.pop(oldest, None)

    @staticmethod
    def _result(
        *,
        verdict: str,
        waiting_justified: bool,
        required_action: str,
        reasons: list[str],
        quiet_for_seconds: float,
        quiet_observations: int,
    ) -> dict[str, object]:
        return {
            "verdict": verdict,
            "waiting_justified": waiting_justified,
            "required_action": required_action,
            "reasons": reasons,
            "quiet_for_seconds": round(max(0.0, quiet_for_seconds), 3),
            "quiet_observations": quiet_observations,
        }


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _counter_advanced(current: int | None, previous: int | None) -> bool:
    return current is not None and previous is not None and current > previous


def _group_signature(value: object) -> tuple[tuple[int, str | None], ...]:
    if not isinstance(value, list):
        return ()
    rows: list[tuple[int, str | None]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        pid = _as_int(item.get("pid"))
        identity = item.get("identity")
        if pid is None:
            continue
        rows.append((pid, identity if isinstance(identity, str) else None))
    rows.sort()
    return tuple(rows)
