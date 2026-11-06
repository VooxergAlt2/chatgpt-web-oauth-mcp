"""Content-free tool-count observations and owner review projection."""

from __future__ import annotations

from typing import Any


def normalize_tool_call_attention(value: object) -> dict[str, Any] | None:
    """Accept only the worker observation contract, dropping untrusted extras."""
    if not isinstance(value, dict):
        return None
    threshold, count = value.get("threshold"), value.get("observed_calls")
    if (type(value.get("version")) is not int or value["version"] != 1
            or value.get("reason") != "tool_call_threshold"
            or type(threshold) is not int or type(count) is not int
            or threshold < 1 or count < threshold
            or value.get("requires_review") is not True
            or value.get("terminating") is not False):
        return None
    return {"version": 1, "reason": "tool_call_threshold", "threshold": threshold,
            "observed_calls": count, "requires_review": True, "terminating": False}


def extract_tool_call_attention(snapshot: object) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    candidates = [snapshot]
    candidates.extend(snapshot.get(key) for key in ("acp_progress", "harness_metadata", "metadata"))
    notices = [notice for candidate in candidates if isinstance(candidate, dict)
               if (notice := normalize_tool_call_attention(candidate.get("tool_call_attention"))) is not None]
    return max(notices, key=lambda notice: notice["observed_calls"], default=None)


def project_tool_call_attention(
    notice: dict[str, Any], review: object = None, *, terminal: bool = False,
) -> dict[str, Any]:
    """Keep worker count immutable; acknowledgement rearms after N more calls."""
    reviewed = review.get("observed_calls", 0) if isinstance(review, dict) else 0
    reviewed = reviewed if type(reviewed) is int and reviewed >= 0 else 0
    next_threshold = reviewed + notice["threshold"]
    return {**notice, "reviewed_calls": reviewed, "next_threshold": next_threshold,
            "requires_review": not terminal and notice["observed_calls"] >= next_threshold}


ATTENTION_NEXT_ACTION = (
    "Inspect the delegate's progress and evidence, then acknowledge_attention with observed_calls "
    "and a command_id to continue, request an ACP checkpoint to steer, or explicitly cancel. "
    "CLI/local delegates do not support live steering. The delegate is still running."
)
