"""Bounded, content-free ACP runtime diagnostics.

Only trusted phase names and native event kinds enter this module. Timings use a
monotonic clock; wall time only identifies when a progress observation was made.
No timer emits progress, so silence is never disguised as provider activity.
"""
from __future__ import annotations

import time
from collections.abc import Callable


PHASES = frozenset({
    "setup", "preflight", "native", "validation", "verification", "promotion", "cleanup",
    "connect", "initialize", "new_session", "load_session", "configure",
    "prompt", "cancel", "teardown",
})
NATIVE_EVENTS = frozenset({
    "agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update",
    "usage_update", "plan", "current_mode_update", "config_option_update",
    "available_commands_update", "user_message_chunk", "session_info_update",
})


class AcpTelemetry:
    """Keep fixed-size phase totals and the first/last accepted native event."""

    def __init__(self, component: str, *, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], float] = time.time):
        if component not in {"session", "worker"}:
            raise ValueError("Invalid ACP telemetry component")
        self.component = component
        self._clock = clock
        self._wall_clock = wall_clock
        self._started = clock()
        self._phase_started = self._started
        self.phase: str | None = None
        self.failure_phase: str | None = None
        self._durations: dict[str, float] = {}
        self._event_count = 0
        self._first_event: float | None = None
        self._last_event: float | None = None
        self._last_event_kind: str | None = None

    def enter(self, phase: str) -> bool:
        if phase not in PHASES:
            raise ValueError("Invalid ACP telemetry phase")
        if phase == self.phase:
            return False
        now = self._clock()
        if self.phase is not None:
            self._durations[self.phase] = self._durations.get(self.phase, 0.0) + max(0.0, now - self._phase_started)
        self.phase = phase
        self._phase_started = now
        return True

    def fail(self) -> None:
        # Cancellation/cleanup cannot overwrite the phase which first failed.
        if self.failure_phase is None:
            self.failure_phase = self.phase

    def native_event(self, kind: str) -> None:
        now = max(0.0, self._clock() - self._started)
        self._event_count += 1
        if self._first_event is None:
            self._first_event = now
        self._last_event = now
        self._last_event_kind = kind if kind in NATIVE_EVENTS else "unknown"

    def progress(self) -> dict:
        return {
            "version": 1, "component": self.component, "phase": self.phase,
            "elapsed_seconds": round(max(0.0, self._clock() - self._started), 3),
            "observed_at_epoch": round(self._wall_clock(), 3),
        }

    def snapshot(self) -> dict:
        now = self._clock()
        durations = dict(self._durations)
        if self.phase is not None:
            durations[self.phase] = durations.get(self.phase, 0.0) + max(0.0, now - self._phase_started)
        return {
            "version": 1, "component": self.component, "phase": self.phase,
            "elapsed_seconds": round(max(0.0, now - self._started), 3),
            "phase_durations_seconds": {key: round(value, 3) for key, value in durations.items()},
            "failure_phase": self.failure_phase,
            "event_count": self._event_count,
            "first_event_seconds": None if self._first_event is None else round(self._first_event, 3),
            "last_event_seconds": None if self._last_event is None else round(self._last_event, 3),
            "last_event": self._last_event_kind,
        }
