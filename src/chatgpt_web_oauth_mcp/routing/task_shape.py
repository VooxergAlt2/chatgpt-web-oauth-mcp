from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any


@dataclass(frozen=True)
class TaskShape:
    """Structured description of a delegate task.

    This is intentionally independent from provider/model selection. It
    describes the work required, not who should execute it.
    """

    purpose: str
    breadth: str
    depth: str
    impact: str
    verification_level: str
    parallelism: str
    risk_class: str
    context_pressure: str
    estimated_scope: str
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]


def classify_task(
    *,
    task: str | None = None,
    goal: str | None = None,
    kind: str = "code",
    complexity: str = "low",
    files_in_scope: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
) -> TaskShape:
    """Build a deterministic first-pass task shape.

    Phase 0 deliberately avoids LLM classification and routing changes.
    """

    text = " ".join(x for x in (task, goal) if x).lower()
    files = len(files_in_scope or [])
    criteria = len(acceptance_criteria or [])

    if any(
        token in text
        for token in (
            "audit",
            "analyze",
            "investigate",
            "find why",
            "аудит",
            "анализ",
            "исслед",
            "почему",
            "разбер",
            "найди прич",
        )
    ):
        purpose = "investigation"
    elif kind == "explore":
        purpose = "discovery"
    else:
        purpose = "implementation"

    breadth = "large" if files > 20 or "whole" in text else "medium" if files > 5 else "small"
    depth = "high" if complexity == "high" or purpose == "investigation" else "medium" if complexity == "medium" else "low"
    impact = (
        "high"
        if any(
            token in text
            for token in (
                "production",
                "runtime",
                "migration",
                "security",
                "прод",
                "боев",
                "рантайм",
                "миграц",
                "безопас",
            )
        )
        else "medium"
        if kind == "code"
        else "low"
    )

    return TaskShape(
        purpose=purpose,
        breadth=breadth,
        depth=depth,
        impact=impact,
        verification_level="concrete" if criteria else "basic",
        parallelism="low" if purpose == "investigation" else "medium",
        risk_class="runtime" if impact == "high" else "normal",
        context_pressure="high" if breadth == "large" else "medium" if len(text) > 500 else "low",
        estimated_scope="large" if breadth == "large" else "medium" if breadth == "medium" else "small",
        confidence=0.65,
    )
