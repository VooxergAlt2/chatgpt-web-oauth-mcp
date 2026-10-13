from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping

from ..delegate_failures import (
    INFRASTRUCTURE_ERROR_CODES,
    error_parts,
    model_configuration_failure,
    runtime_provider_failure,
)


class FailureType(str, Enum):
    NONE = "none"
    BLOCKED = "blocked"
    INFRASTRUCTURE_ERROR = "infrastructure_error"
    READONLY_VIOLATION = "readonly_violation"
    PROVIDER_ERROR = "provider_error"
    AUTH_ERROR = "auth_error"
    RATE_LIMIT = "rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    SCOPE_TOO_LARGE = "scope_too_large"
    LOW_CONFIDENCE = "low_confidence"
    VERIFICATION_FAILED = "verification_failed"
    NO_PROGRESS = "no_progress"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    TASK_ERROR = "task_error"
    CONFIGURATION_ERROR = "configuration_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureAnalysis:
    failure_type: FailureType
    retryable: bool
    evidence: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failure_type"] = self.failure_type.value
        return payload


_AUTH_CODES = {
    "antigravity_permission_denied",
    "authentication_failed",
    "authorization_failed",
    "permission_denied",
    "unauthorized",
}
_VERIFICATION_CODES = {
    "code_commit_mode_violation",
    "code_commit_required",
    "code_scope_violation",
    "code_verification_failed",
    "code_verification_result_mutated",
}
_SCOPE_CODES = {
    "code_change_budget_exceeded",
    "reviewer_scope_preflight_failed",
}
_PARTIAL_CODES = {
    "delegate_reported_partial",
}
_CONTEXT_FRAGMENTS = (
    "context length",
    "context limit",
    "context overflow",
    "context window",
    "maximum context",
    "too many tokens",
    "token limit",
)
_NO_PROGRESS_FRAGMENTS = (
    "no progress",
    "made no progress",
    "tool call budget",
    "tool-call budget",
    "max tool calls",
)
_SCOPE_FRAGMENTS = (
    "scope is too large",
    "scope too large",
    "complete explicit files in scope evidence",
)
_RATE_FRAGMENTS = (
    "rate limit",
    "quota exhausted",
    "quota reached",
    "too many requests",
    "error_code:429",
    "code 429",
)
_AUTH_FRAGMENTS = (
    "authentication",
    "authorization",
    "permission denied",
    "unauthorized",
    "forbidden",
)


def _structured_confidence(snapshot: Mapping[str, object]) -> float | None:
    structured = snapshot.get("structured_output")
    if not isinstance(structured, Mapping):
        return None
    value = structured.get("confidence")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _forced_finalization_reason(snapshot: Mapping[str, object]) -> str:
    metadata = snapshot.get("harness_metadata")
    if not isinstance(metadata, Mapping):
        return ""
    return str(metadata.get("forced_finalization_reason") or "").strip().lower()


def classify_failure(snapshot: Mapping[str, object]) -> FailureAnalysis:
    status = str(snapshot.get("status") or "").strip().lower()
    error = snapshot.get("error")
    code, message = error_parts(error)
    combined = f"{code} {message}".strip()
    harness = str(snapshot.get("harness") or snapshot.get("executor") or "")
    confidence = _structured_confidence(snapshot)
    forced_finalization_reason = _forced_finalization_reason(snapshot)

    if status == "cancelled":
        return FailureAnalysis(FailureType.CANCELLED, retryable=False, evidence=code or status)
    if status == "timed_out" or code == "timed_out":
        return FailureAnalysis(FailureType.TIMEOUT, retryable=True, evidence=code or status)
    if snapshot.get("success") is True or status == "succeeded":
        if confidence is not None and confidence < 0.5:
            return FailureAnalysis(
                FailureType.LOW_CONFIDENCE,
                retryable=True,
                evidence="confidence",
            )
        return FailureAnalysis(FailureType.NONE, retryable=False)

    if code in _VERIFICATION_CODES:
        return FailureAnalysis(
            FailureType.VERIFICATION_FAILED,
            retryable=True,
            evidence=code,
        )
    if code in _SCOPE_CODES:
        return FailureAnalysis(FailureType.SCOPE_TOO_LARGE, retryable=True, evidence=code)
    if code in _AUTH_CODES:
        return FailureAnalysis(FailureType.AUTH_ERROR, retryable=False, evidence=code)
    if model_configuration_failure(harness=harness, error=error):
        return FailureAnalysis(
            FailureType.CONFIGURATION_ERROR,
            retryable=False,
            evidence="unsupported_model_or_effort",
        )
    if code.endswith("quota_exhausted") or code == "delegate_quota_blocked":
        return FailureAnalysis(FailureType.RATE_LIMIT, retryable=True, evidence=code or message)
    if forced_finalization_reason in {
        "tool_call_budget_exhausted",
        "tool_calls_exhausted",
        "max_tool_calls",
    }:
        return FailureAnalysis(
            FailureType.NO_PROGRESS,
            retryable=True,
            evidence=forced_finalization_reason,
        )
    if code == "readonly_violation":
        return FailureAnalysis(FailureType.READONLY_VIOLATION, retryable=False, evidence=code)
    if code == "delegate_reported_blocked" or (
        status == "blocked" and not code
    ):
        return FailureAnalysis(FailureType.BLOCKED, retryable=False, evidence=code or status)

    if any(fragment in message for fragment in _CONTEXT_FRAGMENTS):
        return FailureAnalysis(FailureType.CONTEXT_OVERFLOW, retryable=True, evidence=code or message)
    if any(fragment in message for fragment in _SCOPE_FRAGMENTS):
        return FailureAnalysis(FailureType.SCOPE_TOO_LARGE, retryable=True, evidence=code or message)
    if any(fragment in message for fragment in _AUTH_FRAGMENTS):
        return FailureAnalysis(FailureType.AUTH_ERROR, retryable=False, evidence=code or message)
    if any(fragment in message for fragment in _RATE_FRAGMENTS):
        return FailureAnalysis(FailureType.RATE_LIMIT, retryable=True, evidence=code or message)
    code_verification = snapshot.get("code_verification")
    if (
        isinstance(code_verification, Mapping)
        and code_verification.get("verified") is False
        and not str(code_verification.get("skipped_reason") or "").strip()
    ):
        return FailureAnalysis(FailureType.VERIFICATION_FAILED, retryable=True, evidence=code or message)
    if code in _PARTIAL_CODES:
        return FailureAnalysis(FailureType.LOW_CONFIDENCE, retryable=True, evidence=code or "confidence")
    if confidence is not None and confidence < 0.5:
        return FailureAnalysis(
            FailureType.LOW_CONFIDENCE,
            retryable=True,
            evidence="confidence",
        )
    if any(fragment in combined for fragment in _NO_PROGRESS_FRAGMENTS):
        return FailureAnalysis(FailureType.NO_PROGRESS, retryable=True, evidence=code or message)

    provider_unavailable, _eligibility, provider_class = runtime_provider_failure(
        harness=harness,
        error=error,
    )
    if provider_unavailable:
        return FailureAnalysis(
            FailureType.PROVIDER_ERROR,
            retryable=True,
            evidence=provider_class or code or message,
        )
    if code in INFRASTRUCTURE_ERROR_CODES:
        return FailureAnalysis(
            FailureType.INFRASTRUCTURE_ERROR,
            retryable=True,
            evidence=code,
        )
    if code:
        return FailureAnalysis(FailureType.TASK_ERROR, retryable=False, evidence=code)
    return FailureAnalysis(FailureType.UNKNOWN, retryable=False, evidence=status or None)
