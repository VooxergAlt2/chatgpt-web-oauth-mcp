"""Fail-closed, bounded same-conversation recovery policy for AGY research.

The policy does not start processes, change providers, or grant extra quota.
The execution layer must verify fresh account admission before acting.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from typing import Mapping

from .delegate_failures import error_parts
from .delegate_models import DelegateTask


AGY_RESEARCH_HARNESSES = frozenset({"antigravity", "antigravity2", "antigravity3"})
RETRYABLE_AGY_TERMINALS = frozenset({
    "empty_harness_result",
    "antigravity_stream_truncated",
})
MAX_AUTOMATIC_RESUMES = 1
MAX_RESUME_SECONDS = 120
MIN_RESUME_SECONDS = 30


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    reason: str
    conversation_id: str | None = None
    retry_timeout_seconds: int | None = None
    remaining_tool_calls: int | None = None
    initial_tool_calls: int = 0

    @property
    def should_resume(self) -> bool:
        return self.action == "resume_same_conversation"

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "reason": self.reason,
            "retry_timeout_seconds": self.retry_timeout_seconds,
            "remaining_tool_calls": self.remaining_tool_calls,
            "initial_tool_calls": self.initial_tool_calls,
        }


def plan_same_account_recovery(
    *,
    task: DelegateTask,
    result: Mapping[str, object],
    recovery_attempts: int,
) -> RecoveryDecision:
    """Allow one read-only resume after incomplete AGY output, never code.

    Budget and execution time are cumulative across the initial attempt and
    the resumed attempt. Location, quota, manual cancellation and permission
    failures are *never* eligible for this action.
    """

    if task.kind != "explore" or task.harness not in AGY_RESEARCH_HARNESSES:
        return RecoveryDecision("stop", "non_research_or_non_agy")
    if recovery_attempts >= MAX_AUTOMATIC_RESUMES:
        return RecoveryDecision("stop", "recovery_attempt_limit")
    if task.cancel_requested or str(result.get("status") or "") != "failed":
        return RecoveryDecision("stop", "terminal_not_recoverable")
    if task.code_contract is not None:
        return RecoveryDecision("stop", "code_verification_in_scope")
    if result.get("structured_output") is not None:
        # Completed advisory evidence is for the caller to review, not to
        # silently re-spend quota or overwrite.
        return RecoveryDecision("stop", "has_advisory_result")
    code, message = error_parts(result.get("error"))
    recoverable = (
        code in RETRYABLE_AGY_TERMINALS
        or (code == "antigravity_result_error" and "stream was interrupted" in message)
    )
    if not recoverable:
        return RecoveryDecision("stop", "failure_requires_separate_action")
    if any(
        fragment in message
        for fragment in (
            "location is not supported",
            "not currently available in your location",
            "eligibility check failed",
            "quota",
            "permission denied",
        )
    ):
        return RecoveryDecision("stop", "provider_access_or_quota_block")

    metadata = result.get("harness_metadata")
    raw_conversation = (
        str(metadata.get("conversation_id") or "").strip()
        if isinstance(metadata, Mapping) else ""
    )
    try:
        conversation_id = str(uuid.UUID(raw_conversation))
    except (ValueError, AttributeError):
        return RecoveryDecision("stop", "no_verified_conversation_id")

    used = 0
    remaining: int | None = None
    if task.max_tool_calls is not None:
        budget = result.get("tool_budget")
        if not isinstance(budget, Mapping) or budget.get("stream_valid") is not True:
            return RecoveryDecision("stop", "tool_budget_not_verifiable")
        count = budget.get("used")
        limit = budget.get("limit")
        if (
            not isinstance(count, int) or isinstance(count, bool)
            or not isinstance(limit, int) or isinstance(limit, bool)
            or count < 0 or limit != task.max_tool_calls
        ):
            return RecoveryDecision("stop", "tool_budget_inconsistent")
        used = count
        remaining = limit - count
        if remaining < 2:
            return RecoveryDecision("stop", "insufficient_remaining_tool_budget",
                                    initial_tool_calls=used)

    duration_raw = result.get("duration_seconds")
    if (
        not isinstance(duration_raw, (int, float))
        or isinstance(duration_raw, bool)
        or not math.isfinite(duration_raw)
        or duration_raw < 0
    ):
        return RecoveryDecision("stop", "duration_not_verifiable")
    remaining_seconds = task.execution_timeout_seconds - math.ceil(duration_raw)
    if remaining_seconds < MIN_RESUME_SECONDS:
        return RecoveryDecision("stop", "insufficient_remaining_time",
                                initial_tool_calls=used)

    return RecoveryDecision(
        action="resume_same_conversation",
        reason=code,
        conversation_id=conversation_id,
        retry_timeout_seconds=min(MAX_RESUME_SECONDS, remaining_seconds),
        remaining_tool_calls=remaining,
        initial_tool_calls=used,
    )
