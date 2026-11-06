"""Fail-closed, bounded same-conversation recovery policy for AGY research.

The policy does not start processes, change providers, or grant extra quota.
The execution layer must verify fresh account admission before acting.
"""

from __future__ import annotations

import math
import re
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
GIT_EVIDENCE_MARKER = "Git evidence acquisition fallback:"
_SHELL_UNAVAILABLE = (
    "fork/exec", "operation not permitted", "/dev/ptmx",
    "permission denied", "cannot spawn", "pty creation",
)
MAX_AUTOMATIC_RESUMES = 1
_PROVIDER_EOF = re.compile(
    r"(?:request failed|streamgeneratecontent)[^\n]{0,1200}\beof\b",
    re.IGNORECASE,
)
MAX_RESUME_SECONDS = 120
MIN_RESUME_SECONDS = 30


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    reason: str
    conversation_id: str | None = None
    retry_timeout_seconds: int | None = None
    tool_call_threshold: int | None = None
    initial_tool_calls: int = 0

    @property
    def should_resume(self) -> bool:
        return self.action in {
            "resume_same_conversation",
            "resume_with_git_evidence",
            "repair_code_and_reverify",
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "reason": self.reason,
            "retry_timeout_seconds": self.retry_timeout_seconds,
            "tool_call_threshold": self.tool_call_threshold,
            "initial_tool_calls": self.initial_tool_calls,
        }


def plan_same_account_recovery(
    *,
    task: DelegateTask,
    result: Mapping[str, object],
    recovery_attempts: int,
) -> RecoveryDecision:
    """Allow one read-only resume after incomplete AGY output, never code.

    Tool observations and execution time are cumulative across attempts. The
    tool threshold requests attention and never forbids recovery. Location,
    quota, manual cancellation and permission failures are never eligible.
    """

    if task.kind != "explore" or task.harness not in AGY_RESEARCH_HARNESSES:
        return RecoveryDecision("stop", "non_research_or_non_agy")
    if recovery_attempts >= MAX_AUTOMATIC_RESUMES:
        return RecoveryDecision("stop", "recovery_attempt_limit")
    if task.cancel_requested or str(result.get("status") or "") != "failed":
        return RecoveryDecision("stop", "terminal_not_recoverable")
    if task.code_contract is not None:
        return RecoveryDecision("stop", "code_verification_in_scope")
    code, message = error_parts(result.get("error"))
    structured = result.get("structured_output")
    has_scoped_diff = (
        GIT_EVIDENCE_MARKER in task.prompt
        and (
            "BEGIN SCOPED DIFF" in task.prompt
            or "Tracked diff: EMPTY within the listed scope." in task.prompt
        )
    )
    blocked_by_shell = (
        code == "delegate_reported_blocked"
        and has_scoped_diff
        and isinstance(structured, Mapping)
        and str(structured.get("status") or "").lower() == "blocked"
        and isinstance(structured.get("blockers"), list)
        and 1 <= len(structured["blockers"]) <= 3
        and all(
            isinstance(blocker, str)
            and "git diff" in blocker.lower()
            and any(fragment in blocker.lower() for fragment in _SHELL_UNAVAILABLE)
            for blocker in structured["blockers"]
        )
    )
    # A structured advisory can precede an AGY streaming EOF. It is not
    # process success, but permits a single bounded same-account continuation.
    # The first artifact is retained in a private sidecar before retry.
    provider_eof_with_advisory = (
        code == "antigravity_result_error"
        and bool(_PROVIDER_EOF.search(message[:2048]))
        and isinstance(structured, Mapping)
        and str(structured.get("status") or "").lower() == "succeeded"
        and isinstance(structured.get("findings"), list)
        and len(structured["findings"]) <= 64
        and not structured.get("blockers")
    )
    if structured is not None and not (blocked_by_shell or provider_eof_with_advisory):
        # Completed advisory evidence is for the caller to review, not to
        # silently re-spend quota or overwrite.
        return RecoveryDecision("stop", "has_advisory_result")
    recoverable = (
        blocked_by_shell
        or (code == "antigravity_sandbox_exec_denied" and has_scoped_diff)
        or code in RETRYABLE_AGY_TERMINALS
        or provider_eof_with_advisory
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
        action=(
            "resume_with_git_evidence"
            if blocked_by_shell or code == "antigravity_sandbox_exec_denied"
            else "resume_same_conversation"
        ),
        reason=code,
        conversation_id=conversation_id,
        retry_timeout_seconds=min(MAX_RESUME_SECONDS, remaining_seconds),
        tool_call_threshold=task.max_tool_calls,
        initial_tool_calls=used,
    )


def plan_verified_code_repair(
    *,
    task: DelegateTask,
    result: Mapping[str, object],
    recovery_attempts: int,
) -> RecoveryDecision:
    """Permit one same-account patch correction after authoritative test failure."""

    if (
        task.kind != "code"
        or task.harness not in AGY_RESEARCH_HARNESSES
        or task.commit_mode != "forbidden"
        or not isinstance(task.code_contract, dict)
        or not isinstance(task.code_baseline, dict)
        or task.code_baseline.get("captured_clean") is not True
    ):
        return RecoveryDecision("stop", "unverified_or_non_agy_writer")
    if task.cancel_requested or str(result.get("status") or "") != "failed":
        return RecoveryDecision("stop", "terminal_not_recoverable")
    if recovery_attempts >= MAX_AUTOMATIC_RESUMES:
        return RecoveryDecision("stop", "recovery_attempt_limit")
    code, _message = error_parts(result.get("error"))
    if code != "code_verification_failed":
        return RecoveryDecision("stop", "not_a_test_failure")
    checks = task.code_contract.get("verification_commands")
    scope = task.code_contract.get("files_in_scope")
    if not (isinstance(checks, list) and 1 <= len(checks) <= 4
            and all(isinstance(c, str) and c.strip() for c in checks)):
        return RecoveryDecision("stop", "missing_bounded_server_checks")
    if not (isinstance(scope, list) and 1 <= len(scope) <= 8
            and all(isinstance(p, str) and p.strip() for p in scope)):
        return RecoveryDecision("stop", "missing_bounded_edit_scope")
    verification = result.get("code_verification")
    if not isinstance(verification, Mapping):
        return RecoveryDecision("stop", "no_verification_evidence")
    if verification.get("verification_phase") != "postflight":
        return RecoveryDecision("stop", "verification_not_postflight")
    if verification.get("contract_hash") != task.code_contract.get("contract_hash"):
        return RecoveryDecision("stop", "contract_hash_mismatch")
    if (
        verification.get("scope_violations") != []
        or verification.get("budget_violations") != []
        or verification.get("commit_violation") is not False
        or verification.get("line_budget_authoritative") is not True
        or verification.get("head_after") != task.code_baseline.get("head")
    ):
        return RecoveryDecision("stop", "unsafe_git_or_scope_state")
    failed_checks = verification.get("verification_commands")
    if (
        not isinstance(failed_checks, list)
        or len(failed_checks) != len(checks)
        or not all(
            isinstance(row, Mapping)
            and row.get("command") == expected
            and row.get("timed_out") is not True
            and row.get("not_run") is not True
            and row.get("cleanup_failed") is not True
            and isinstance(row.get("exit_code"), int)
            and not isinstance(row.get("exit_code"), bool)
            for row, expected in zip(failed_checks, checks)
        )
        or not any(row.get("passed") is False for row in failed_checks)
    ):
        return RecoveryDecision("stop", "unreliable_verification_failure")
    fingerprint = verification.get("post_verification_result_fingerprint")
    if (
        not isinstance(fingerprint, Mapping)
        or not isinstance(fingerprint.get("sha256"), str)
        or len(fingerprint["sha256"]) != 64
        or verification.get("delegated_result_fingerprint") != fingerprint
    ):
        return RecoveryDecision("stop", "unverified_repository_fingerprint")
    metadata = result.get("harness_metadata")
    conversation = (
        str(metadata.get("conversation_id") or "")
        if isinstance(metadata, Mapping) else ""
    )
    try:
        conversation_id = str(uuid.UUID(conversation))
    except (ValueError, AttributeError):
        return RecoveryDecision("stop", "no_verified_conversation_id")
    duration = result.get("duration_seconds")
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or not math.isfinite(duration)
        or duration < 0
    ):
        return RecoveryDecision("stop", "duration_not_verifiable")
    remaining = task.execution_timeout_seconds - math.ceil(duration)
    if remaining < MIN_RESUME_SECONDS:
        return RecoveryDecision("stop", "insufficient_remaining_time")
    return RecoveryDecision(
        action="repair_code_and_reverify",
        reason=code,
        conversation_id=conversation_id,
        retry_timeout_seconds=min(MAX_RESUME_SECONDS, remaining),
    )
