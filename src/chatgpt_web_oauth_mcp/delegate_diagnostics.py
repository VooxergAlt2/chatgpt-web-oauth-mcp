"""Bounded, stdlib-only accumulator for delegate repetition and error diagnostics.

Maintains fixed-size, HMAC-keyed observation history without persisting raw
arguments, raw file content, query strings, paths, or HMAC secret keys.
All diagnostics are advisory only: no cancellation, no mutation, no repair loop.
"""
from __future__ import annotations

import collections
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable, Sequence
from typing import Any

DIAGNOSTICS_VERSION = 1
DEFAULT_MAX_WINDOW = 64
DEFAULT_LIMITATIONS: tuple[str, ...] = (
    "native_visibility_incomplete",
    "advisory_only",
    "no_cancellation",
    "reasoning_unobserved",
)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    except Exception:
        return str(value).encode("utf-8", errors="replace")


def _op_digest(secret: bytes, name: str, arguments: Any) -> str:
    norm_args = arguments if isinstance(arguments, (dict, list)) else {}
    payload = {"arguments": norm_args, "name": str(name)}
    raw = _canonical_bytes(payload)
    return hmac.new(secret, b"op:" + raw, hashlib.sha256).hexdigest()


def _res_digest(secret: bytes, is_error: bool, value: Any) -> str:
    tag = b"res:err:" if is_error else b"res:ok:"
    raw = _canonical_bytes(value)
    return hmac.new(secret, tag + raw, hashlib.sha256).hexdigest()


class DelegateDiagnosticsAccumulator:
    """Bounded, content-free diagnostics accumulator for tool operations."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_window: int = DEFAULT_MAX_WINDOW,
        limitations: Sequence[str] = DEFAULT_LIMITATIONS,
    ) -> None:
        self._clock = clock
        self._started = clock()
        self._secret = secrets.token_bytes(32)
        self._max_window = min(DEFAULT_MAX_WINDOW, max(1, int(max_window)))
        self._limitations = tuple(limitations)
        self.total_calls = 0
        self.same_request_response_repeats = 0
        self.repeated_identical_errors = 0
        self.repeat_streak = 0
        self.max_repeat_streak = 0
        self.error_streak = 0
        self._last_novel_elapsed: float | None = None
        self._prev_op_digest: str | None = None
        self._prev_res_digest: str | None = None
        self._prev_error_digest: str | None = None
        self._window: collections.deque[tuple[str, str, bool, float]] = collections.deque(
            maxlen=self._max_window
        )

    def __repr__(self) -> str:
        return (
            f"<DelegateDiagnosticsAccumulator calls={self.total_calls} "
            f"repeats={self.same_request_response_repeats} streak={self.repeat_streak}>"
        )

    def record_call(
        self,
        name: str,
        arguments: Any = None,
        *,
        result: Any = None,
        error: Any = None,
    ) -> None:
        """Record an observed tool call execution (advisory only, fail-safe)."""
        try:
            now = self._clock()
            elapsed = max(0.0, now - self._started)
            is_error = error is not None
            op_digest = _op_digest(self._secret, name, arguments)
            res_digest = _res_digest(self._secret, is_error, error if is_error else result)

            self.total_calls += 1

            if self._prev_op_digest is None:
                self.repeat_streak = 0
                self._last_novel_elapsed = elapsed
                if is_error:
                    self.error_streak = 1
                    self._prev_error_digest = res_digest
                else:
                    self.error_streak = 0
                    self._prev_error_digest = None
            else:
                is_same_op = (op_digest == self._prev_op_digest)
                is_same_res = (res_digest == self._prev_res_digest)

                if is_same_op and is_same_res:
                    self.same_request_response_repeats += 1
                    self.repeat_streak += 1
                    if self.repeat_streak > self.max_repeat_streak:
                        self.max_repeat_streak = self.repeat_streak
                else:
                    # Operation or response changed.
                    # Changed response breaks the repeat streak.
                    self.repeat_streak = 0
                    self._last_novel_elapsed = elapsed

                if is_error:
                    if self._prev_error_digest is not None and res_digest == self._prev_error_digest:
                        self.repeated_identical_errors += 1
                        self.error_streak += 1
                    else:
                        self.error_streak = 1
                    self._prev_error_digest = res_digest
                else:
                    self.error_streak = 0
                    self._prev_error_digest = None

            self._prev_op_digest = op_digest
            self._prev_res_digest = res_digest
            self._window.append((op_digest, res_digest, is_error, elapsed))
        except Exception:
            # Diagnostics are strictly advisory and must never fail or propagate.
            pass

    def record_success(self, name: str, arguments: Any = None, result: Any = None) -> None:
        self.record_call(name, arguments, result=result, error=None)

    def record_error(self, name: str, arguments: Any = None, error: Any = None) -> None:
        self.record_call(name, arguments, result=None, error=error)

    def summary(self) -> dict[str, Any]:
        """Stable schema version 1 summary."""
        return {
            "version": DIAGNOSTICS_VERSION,
            "advisory": True,
            "observation_scope": "gateway",
            "completeness": "incomplete",
            "native_visibility": "incomplete",
            "limitations": list(self._limitations),
            "total_calls": self.total_calls,
            "same_request_response_repeats": self.same_request_response_repeats,
            "repeated_identical_errors": self.repeated_identical_errors,
            "repeat_streak": self.repeat_streak,
            "max_repeat_streak": self.max_repeat_streak,
            "error_streak": self.error_streak,
            "last_novel_observation_elapsed": (
                round(self._last_novel_elapsed, 3)
                if self._last_novel_elapsed is not None
                else None
            ),
            "window_size": len(self._window),
        }

