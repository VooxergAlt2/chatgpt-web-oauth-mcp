from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .task_shape import TaskShape


@dataclass(frozen=True)
class RouteDecision:
    """Routing observation shared by legacy and Planner execution."""

    legacy_complexity: str
    task_shape: TaskShape
    initial_route: str | None = None
    shadow_route: str | None = None
    planner: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "legacy_complexity": self.legacy_complexity,
            "task_shape": self.task_shape.to_dict(),
            "task_shape_hash": self.task_shape.fingerprint(),
            "initial_route": self.initial_route,
            "shadow_route": self.shadow_route,
            "planner": self.planner,
        }
