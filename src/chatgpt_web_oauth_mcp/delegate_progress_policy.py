"""Bounded advisory checkpoint thresholds; no I/O, retries or acceptance."""
from __future__ import annotations

import math


class DelegateProgressPolicy:
    def __init__(self, max_tool_calls: int):
        if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= 64:
            raise ValueError("Invalid tool budget")
        self.hard_limit = max_tool_calls
        self.early = min(8, max_tool_calls // 4)
        self.near_limit = max(self.early + 1, max_tool_calls * 3 // 4)
        self.issued: set[str] = set()

    def due(self, tool_calls: int, remaining_seconds: float | None = None) -> str | None:
        if type(tool_calls) is not int or tool_calls < 0:
            raise ValueError("Invalid tool count")
        if remaining_seconds is not None and (
            type(remaining_seconds) not in (int, float) or not math.isfinite(remaining_seconds)
        ):
            raise ValueError("Invalid remaining time")
        if self.hard_limit < 16 or "budget_review" in self.issued:
            return None
        near_deadline = remaining_seconds is not None and 0 < remaining_seconds <= 30 and tool_calls > 0
        if tool_calls >= self.near_limit or near_deadline:
            return "budget_review"
        if tool_calls >= self.early and "early_review" not in self.issued:
            return "early_review"
        return None

    def acknowledge(self, reason: str) -> None:
        if reason not in {"early_review", "budget_review"}:
            raise ValueError("Invalid checkpoint reason")
        self.issued.add(reason)

    def snapshot(self) -> dict:
        return {"version": 1, "advisory": True, "enabled": self.hard_limit >= 16,
                "hard_limit": self.hard_limit, "early_threshold": self.early,
                "budget_threshold": self.near_limit, "issued": sorted(self.issued)}
