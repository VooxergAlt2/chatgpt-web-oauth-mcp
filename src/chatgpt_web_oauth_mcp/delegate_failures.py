from __future__ import annotations

from typing import Mapping


INFRASTRUCTURE_ERROR_CODES = frozenset(
    {
        "antigravity_unavailable",
        "antigravity_stream_truncated",
        "claude_result_error",
        "delegate_harness_unavailable",
        "delegate_harness_preflight_unavailable",
        "durable_job_start_failed",
        "durable_job_status_failed",
        "durable_job_interrupted",
        "empty_harness_result",
        "harness_output_invalid",
        "readonly_audit_unavailable",
    }
)

ELIGIBILITY_ERROR_FRAGMENTS = (
    "eligibility check failed",
    "not eligible",
    "not currently available in your location",
    "user location is not supported",
    "location is not supported for the api use",
)

TRANSIENT_PROVIDER_ERROR_FRAGMENTS = (
    "service is currently unavailable",
    "unavailable (code 503)",
    "internal error encountered",
    "internal (code 500)",
    "stream was interrupted",
)


def error_parts(error: object) -> tuple[str, str]:
    if not isinstance(error, Mapping):
        return "", ""
    return (
        str(error.get("code") or "").strip().lower(),
        str(error.get("message") or "").strip().lower(),
    )


def model_configuration_failure(*, harness: str, error: object) -> bool:
    """Recognize AGY CLI model/effort validation, not task or provider failures."""
    if str(harness or "").strip().lower() not in {"antigravity", "antigravity2", "antigravity3"}:
        return False
    code, message = error_parts(error)
    return (
        code == "antigravity_result_error"
        and message.startswith("invalid model selection (")
        and (
            "not recognized as a known model or custom model" in message
            or "--effort is not supported for model" in message
        )
    )


def runtime_provider_failure(
    *,
    harness: str,
    error: object,
) -> tuple[bool, bool, str | None]:
    """Classify provider/runtime failures that are safe to route around.

    Configuration and task-level errors intentionally remain visible rather than
    being hidden by fallback. The second return value marks eligibility/location
    failures because those may trigger the Antigravity recovery watchdog.
    """

    normalized_harness = str(harness or "").strip().lower()
    code, message = error_parts(error)
    if code in {"delegate_harness_unavailable", "antigravity_unavailable"}:
        return True, False, "harness_unavailable"
    if normalized_harness in {"local", "local2"} and code == "local_openai_unavailable":
        return True, False, "local_endpoint_unavailable"
    if normalized_harness not in {"antigravity", "antigravity2", "antigravity3"}:
        return False, False, None
    if code != "antigravity_result_error":
        return False, False, None
    if any(fragment in message for fragment in ELIGIBILITY_ERROR_FRAGMENTS):
        return True, True, "eligibility_or_location"
    if any(fragment in message for fragment in TRANSIENT_PROVIDER_ERROR_FRAGMENTS):
        return True, False, "transient_provider_failure"
    return False, False, None


def is_infrastructure_failure(*, harness: str, error: object) -> bool:
    code, _message = error_parts(error)
    if code in INFRASTRUCTURE_ERROR_CODES:
        return True
    unavailable, _eligibility, _reason = runtime_provider_failure(
        harness=harness,
        error=error,
    )
    return unavailable
