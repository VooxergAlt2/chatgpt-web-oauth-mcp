from __future__ import annotations

from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore
from chatgpt_web_oauth_mcp.session_continuation import job_state_from_result


@pytest.mark.parametrize(
    ("status", "exit_code", "expected_success", "expected_terminal"),
    [
        ("running", None, None, False),
        ("succeeded", 0, True, True),
        ("failed", 7, False, True),
        ("killed", -15, False, True),
        ("interrupted", None, False, True),
        ("timed_out", None, False, True),
    ],
)
def test_job_outcome_is_not_metadata_lookup_success(
    status: str,
    exit_code: int | None,
    expected_success: bool | None,
    expected_terminal: bool,
) -> None:
    job_id, state = job_state_from_result(
        {"job_id": "job_test", "status": status, "exit_code": exit_code, "success": True}
    )
    assert job_id == "job_test"
    assert state["terminal"] is expected_terminal
    assert state["success"] is expected_success


def test_failed_job_is_unread_failure_in_session_inbox(tmp_path: Path) -> None:
    store = SessionCheckpointStore(path=tmp_path / "checkpoints.json", ttl_seconds=86400)
    job_id, state = job_state_from_result(
        {"job_id": "job_failed", "status": "failed", "exit_code": 2, "success": True}
    )
    assert job_id is not None
    store.record_runtime(session_key="openai:job-owner", last_tool="job_status", jobs={job_id: state})

    pending = store.pending_results("openai:job-owner")
    assert len(pending) == 1
    assert pending[0]["continuation_state"] == "RESULT_REQUIRES_CONSUMPTION"
    assert pending[0]["success"] is False
