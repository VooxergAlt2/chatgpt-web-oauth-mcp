from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os

from chatgpt_web_oauth_mcp.activity import ActivityTracker
from chatgpt_web_oauth_mcp.job_supervisor import _parse_ps_cpu_time, process_cpu_seconds


def _snapshot(job_id: str, cpu: float) -> dict[str, object]:
    return {
        "job_id": job_id,
        "status": "running",
        "pid": 100,
        "process_identity_match": True,
        "process_group_verified": True,
        "process_group_signature": [{"pid": 100, "identity": "identity"}],
        "process_group_cpu_seconds": cpu,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "last_output_at": None,
        "elapsed_seconds": 100.0,
    }


def test_parse_ps_cpu_time_portable_formats() -> None:
    assert _parse_ps_cpu_time("01:02") == 62.0
    assert _parse_ps_cpu_time("01:02:03") == 3723.0
    assert _parse_ps_cpu_time("2-01:02:03") == 176523.0
    assert _parse_ps_cpu_time("00:00.50") == 0.5
    assert _parse_ps_cpu_time("") is None
    assert _parse_ps_cpu_time("bad") is None
    assert _parse_ps_cpu_time("00:61") is None


def test_process_cpu_seconds_observes_current_process() -> None:
    value = process_cpu_seconds(os.getpid())

    assert value is not None
    assert value >= 0.0


def test_activity_tracker_is_safe_for_concurrent_clients() -> None:
    tracker = ActivityTracker(max_tracked_jobs=16)

    def assess(index: int) -> str:
        return str(tracker.assess(_snapshot(f"job_{index % 8}", float(index)), now=100.0 + index)["verdict"])

    with ThreadPoolExecutor(max_workers=8) as executor:
        verdicts = list(executor.map(assess, range(64)))

    assert len(verdicts) == 64
    assert set(verdicts) <= {"QUIET", "ACTIVE", "STALLED_SUSPECTED"}
    assert len(tracker._observations) <= 16
