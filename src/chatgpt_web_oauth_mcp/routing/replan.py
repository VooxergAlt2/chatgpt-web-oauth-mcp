from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping

from .failure_taxonomy import FailureAnalysis, FailureType


class ReplanAction(str, Enum):
    NONE = "none"
    SWITCH_PROVIDER = "switch_provider"
    SPLIT_SCOPE = "split_scope"
    ESCALATE_CONTEXT = "escalate_context"
    ADD_REVIEWER = "add_reviewer"
    IMPLEMENTATION_LOOP = "implementation_loop"
    REASSESS_TASK = "reassess_task"
    FIX_CONFIGURATION = "fix_configuration"


@dataclass(frozen=True)
class ReplanDirective:
    action: ReplanAction
    reason: FailureType
    automatic: bool
    preserve_evidence: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action"] = self.action.value
        payload["reason"] = self.reason.value
        return payload


def replan_for_failure(
    analysis: FailureAnalysis,
    *,
    task_shape: Mapping[str, object] | None = None,
) -> ReplanDirective:
    failure_type = analysis.failure_type
    if failure_type in {FailureType.NONE, FailureType.CANCELLED, FailureType.BLOCKED}:
        return ReplanDirective(ReplanAction.NONE, failure_type, automatic=False)
    if failure_type == FailureType.CONFIGURATION_ERROR:
        return ReplanDirective(ReplanAction.FIX_CONFIGURATION, failure_type, automatic=False)
    if failure_type in {FailureType.PROVIDER_ERROR, FailureType.RATE_LIMIT, FailureType.TIMEOUT}:
        return ReplanDirective(ReplanAction.SWITCH_PROVIDER, failure_type, automatic=True)
    if failure_type == FailureType.CONTEXT_OVERFLOW:
        breadth = str((task_shape or {}).get("breadth") or "")
        action = ReplanAction.SPLIT_SCOPE if breadth == "large" else ReplanAction.ESCALATE_CONTEXT
        return ReplanDirective(action, failure_type, automatic=False)
    if failure_type == FailureType.SCOPE_TOO_LARGE:
        return ReplanDirective(ReplanAction.SPLIT_SCOPE, failure_type, automatic=False)
    if failure_type == FailureType.LOW_CONFIDENCE:
        return ReplanDirective(ReplanAction.ADD_REVIEWER, failure_type, automatic=False)
    if failure_type == FailureType.VERIFICATION_FAILED:
        return ReplanDirective(ReplanAction.IMPLEMENTATION_LOOP, failure_type, automatic=False)
    if failure_type == FailureType.NO_PROGRESS:
        return ReplanDirective(ReplanAction.REASSESS_TASK, failure_type, automatic=False)
    return ReplanDirective(ReplanAction.REASSESS_TASK, failure_type, automatic=False)
