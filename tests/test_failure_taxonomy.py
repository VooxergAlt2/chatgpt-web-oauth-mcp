from __future__ import annotations

import pytest

from chatgpt_web_oauth_mcp.routing.failure_taxonomy import FailureType, classify_failure
from chatgpt_web_oauth_mcp.routing.replan import ReplanAction, replan_for_failure


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"status": "succeeded", "success": True}, FailureType.NONE),
        ({"status": "timed_out", "error": {"code": "timed_out"}}, FailureType.TIMEOUT),
        (
            {"status": "failed", "error": {"code": "antigravity_quota_exhausted"}},
            FailureType.RATE_LIMIT,
        ),
        (
            {"status": "failed", "error": {"code": "antigravity_permission_denied"}},
            FailureType.AUTH_ERROR,
        ),
        (
            {"status": "failed", "error": {"code": "provider_error", "message": "maximum context window exceeded"}},
            FailureType.CONTEXT_OVERFLOW,
        ),
        (
            {"status": "failed", "error": {"code": "code_change_budget_exceeded"}},
            FailureType.SCOPE_TOO_LARGE,
        ),
        (
            {
                "status": "failed",
                "error": {"code": "code_verification_failed"},
                "code_verification": {"verified": False},
            },
            FailureType.VERIFICATION_FAILED,
        ),
        (
            {"status": "partial", "error": {"code": "delegate_reported_partial"}},
            FailureType.LOW_CONFIDENCE,
        ),
    ],
)
def test_classify_failure(snapshot: dict[str, object], expected: FailureType) -> None:
    assert classify_failure(snapshot).failure_type == expected


def test_replan_distinguishes_context_shape() -> None:
    analysis = classify_failure(
        {"status": "failed", "error": {"code": "provider_error", "message": "context window exceeded"}}
    )
    small = replan_for_failure(analysis, task_shape={"breadth": "small"})
    large = replan_for_failure(analysis, task_shape={"breadth": "large"})

    assert small.action == ReplanAction.ESCALATE_CONTEXT
    assert large.action == ReplanAction.SPLIT_SCOPE
    assert small.automatic is False


def test_provider_failure_can_automatically_switch_provider() -> None:
    analysis = classify_failure(
        {
            "status": "failed",
            "harness": "local",
            "error": {"code": "local_openai_unavailable", "message": "endpoint unavailable"},
        }
    )
    directive = replan_for_failure(analysis)

    assert analysis.failure_type == FailureType.PROVIDER_ERROR
    assert directive.action == ReplanAction.SWITCH_PROVIDER
    assert directive.automatic is True


def test_success_with_low_confidence_requires_reviewer() -> None:
    analysis = classify_failure(
        {
            "status": "succeeded",
            "success": True,
            "structured_output": {"confidence": 0.3},
        }
    )
    directive = replan_for_failure(analysis)

    assert analysis.failure_type == FailureType.LOW_CONFIDENCE
    assert directive.action == ReplanAction.ADD_REVIEWER
    assert directive.automatic is False


def test_verification_error_text_does_not_become_auth_failure() -> None:
    analysis = classify_failure(
        {
            "status": "failed",
            "error": {
                "code": "code_verification_failed",
                "message": "forbidden import was detected",
            },
            "code_verification": {"enabled": True, "verified": False},
        }
    )

    assert analysis.failure_type == FailureType.VERIFICATION_FAILED


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (
            {
                "status": "cancelled",
                "structured_output": {"confidence": 0.3},
                "error": {"code": "cancelled"},
            },
            FailureType.CANCELLED,
        ),
        (
            {
                "status": "failed",
                "structured_output": {"confidence": 0.3},
                "error": {
                    "code": "antigravity_quota_exhausted",
                    "message": "quota reached",
                },
            },
            FailureType.RATE_LIMIT,
        ),
        (
            {
                "status": "failed",
                "error": {
                    "code": "code_verification_failed",
                    "message": "quota reached while running verification",
                },
                "code_verification": {"enabled": True, "verified": False},
            },
            FailureType.VERIFICATION_FAILED,
        ),
        (
            {
                "status": "failed",
                "error": {
                    "code": "antigravity_permission_denied",
                    "message": "permission denied",
                },
                "code_verification": {
                    "enabled": True,
                    "verified": False,
                    "skipped_reason": "delegate_process_not_succeeded",
                },
            },
            FailureType.AUTH_ERROR,
        ),
    ],
)
def test_failure_precedence_preserves_primary_terminal_cause(
    snapshot: dict[str, object],
    expected: FailureType,
) -> None:
    assert classify_failure(snapshot).failure_type == expected
