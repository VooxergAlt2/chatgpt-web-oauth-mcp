from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .task_shape import TaskShape


@dataclass(frozen=True)
class RouteDecision:
    """Shadow routing observation. Does not affect execution."""

    legacy_complexity: str
    task_shape: TaskShape
    current_route: str | None = None
    shadow_route: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "legacy_complexity": self.legacy_complexity,
            "task_shape": self.task_shape.to_dict(),
            "task_shape_hash": self.task_shape.fingerprint(),
            "current_route": self.current_route,
            "shadow_route": self.shadow_route,
        }
