"""Content-free advisory progress views for owner-filtered ACP status snapshots.

The worker owns stdout; native content is never a progress source. Reads are
bounded and never change lifecycle/activity decisions or infer completion.
"""
from __future__ import annotations

import json
import math
import os
import stat
import time
from pathlib import Path

from .acp_telemetry import NATIVE_EVENTS
from .delegate_attention import normalize_tool_call_attention

_PREFIX_BYTES = 8 * 1024
_TAIL_BYTES = 64 * 1024
_PHASES = {
    "session": frozenset({"connect", "initialize", "new_session", "load_session",
                          "configure", "prompt", "cancel", "teardown"}),
    "worker": frozenset({"setup", "preflight", "native", "validation", "verification",
                         "promotion", "cleanup"}),
}
_EVENTS = {
    "session": NATIVE_EVENTS | {"phase", "input_required", "unknown"},
    "worker": frozenset({"phase", "verifying"}),
}
_TERMINAL = frozenset({"succeeded", "failed", "blocked", "cancelled", "timed_out",
                       "unknown", "interrupted"})
_BUDGET_FIELDS = (
    "total_seconds", "worker_safety_seconds", "verification_reserve_seconds",
    "native_cancel_grace_seconds", "setup_elapsed_seconds", "native_session_seconds",
    "preflight_seconds", "preflight_elapsed_seconds",
)


def _number(value, *, positive=False):
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(value) and (value > 0 if positive else value >= 0)
    except (OverflowError, ValueError):
        return False


def _count(value):
    return type(value) is int and 0 <= value <= (1 << 63) - 1


def _header(value, component=None):
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        return None
    actual = value.get("component")
    phase = value.get("phase")
    if (not isinstance(actual, str) or actual not in _PHASES
            or (component is not None and actual != component)
            or not isinstance(phase, str) or phase not in _PHASES[actual]
            or not _number(value.get("elapsed_seconds"))):
        return None
    return {"version": 1, "component": actual, "phase": phase,
            "elapsed_seconds": value["elapsed_seconds"]}


def _budget(value):
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        return None
    result = {"version": 1}
    for key in _BUDGET_FIELDS:
        if _number(value.get(key)):
            result[key] = value[key]
    return result if len(result) > 1 else None


def _progress(value):
    if not isinstance(value, dict) or value.get("event") != "acp_progress":
        return None
    data = value.get("progress")
    if not isinstance(data, dict):
        return None
    runtime = _header(data.get("runtime"))
    if runtime is None:
        return None
    observed = data["runtime"].get("observed_at_epoch")
    event = data.get("event")
    if (not _number(observed, positive=True) or not isinstance(event, str)
            or event not in _EVENTS[runtime["component"]]):
        return None
    runtime["observed_at_epoch"] = observed
    result = {"event": event, "runtime": runtime}
    for key in ("tools", "answer_bytes"):
        if _count(data.get(key)):
            result[key] = data[key]
    if (budget := _budget(data.get("execution_budget"))) is not None:
        result["execution_budget"] = budget
    if (attention := normalize_tool_call_attention(data.get("tool_call_attention"))) is not None:
        result["tool_call_attention"] = attention
    return result


def _diagnostics(value, component):
    result = _header(value, component)
    if result is None:
        return None
    durations = value.get("phase_durations_seconds")
    if isinstance(durations, dict):
        result["phase_durations_seconds"] = {
            phase: durations[phase] for phase in sorted(_PHASES[component])
            if _number(durations.get(phase))
        }
    failure = value.get("failure_phase")
    if failure is None or (isinstance(failure, str) and failure in _PHASES[component]):
        result["failure_phase"] = failure
    if _count(value.get("event_count")):
        result["event_count"] = value["event_count"]
    for key in ("first_event_seconds", "last_event_seconds"):
        if value.get(key) is None or _number(value[key]):
            result[key] = value.get(key)
    last_event = value.get("last_event")
    if last_event is None or (isinstance(last_event, str) and last_event in NATIVE_EVENTS | {"unknown"}):
        result["last_event"] = last_event
    return result


def _stdout_windows(path):
    """Read at most 72 KiB from a regular, non-symlink worker stdout.

    O_NONBLOCK prevents FIFO/device opening from hanging before fstat rejects it.
    The open descriptor, not a second pathname lookup, determines size and data.
    Prefix and tail may overlap; processing them in order still selects the last
    complete record. A cut first/last line is deliberately discarded.
    """
    if not isinstance(path, (str, Path)):
        return ()
    path = Path(path)
    if not path.is_absolute() or path.name != "stdout.log" or ".." in path.parts:
        return ()
    descriptor = None
    try:
        # Reject symlink ancestors as well as the final entry. O_NOFOLLOW closes
        # the final-entry race; these paths otherwise come from trusted snapshots.
        if any(part.is_symlink() for part in (path, *path.parents)):
            return ()
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_CLOEXEC", 0))
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return ()
        size = info.st_size
        if size <= _TAIL_BYTES:
            return (os.read(descriptor, _TAIL_BYTES),)
        prefix = os.read(descriptor, _PREFIX_BYTES)
        os.lseek(descriptor, max(0, size - _TAIL_BYTES), os.SEEK_SET)
        tail = os.read(descriptor, _TAIL_BYTES)
        # The leading tail fragment may start in UTF-8 or JSON text.
        tail = tail.partition(b"\n")[2]
        return (prefix, tail)
    except (OSError, ValueError, TypeError):
        return ()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def progress_snapshot(snapshot: dict, *, now: float | None = None) -> dict:
    """Build a fixed-schema view after the caller has enforced task ownership."""
    view = {"version": 1, "available": False, "advisory": True, "source": "unavailable"}
    if snapshot.get("completed") is True or snapshot.get("status") in _TERMINAL:
        metadata = snapshot.get("harness_metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        diagnostics = {}
        for component in ("session", "worker"):
            value = _diagnostics(metadata.get(f"acp_{component}_diagnostics"), component)
            if value is not None:
                diagnostics[component] = value
        if diagnostics:
            view.update(available=True, source="final_metadata", diagnostics=diagnostics)
            if (budget := _budget(metadata.get("execution_budget"))) is not None:
                view["execution_budget"] = budget
        else:
            view["reason"] = "acp_progress_final_metadata_unavailable"
        if (attention := normalize_tool_call_attention(metadata.get("tool_call_attention"))) is not None:
            view["tool_call_attention"] = attention
        return view

    logs = snapshot.get("logs")
    latest = None
    budget = None
    attention = None
    for window in _stdout_windows(logs.get("stdout") if isinstance(logs, dict) else None):
        # splitlines() would accept an unterminated last record: only newline
        # terminated records can have been completely emitted by the worker.
        for line in window.split(b"\n")[:-1]:
            try:
                record = _progress(json.loads(line.decode("utf-8")))
            except (ValueError, UnicodeError, RecursionError):
                continue
            if record is not None:
                latest = record
                if record.get("execution_budget") is not None:
                    budget = record["execution_budget"]
                if record.get("tool_call_attention") is not None:
                    candidate = record["tool_call_attention"]
                    if attention is None or candidate["observed_calls"] >= attention["observed_calls"]:
                        attention = candidate
    if latest is None:
        view["reason"] = "acp_progress_not_started_or_legacy"
        return view
    observed_now = time.time() if now is None else now
    if not _number(observed_now, positive=True):
        observed_now = time.time()
    view.update(available=True, source="worker_stdout", **latest)
    view["seconds_since_progress"] = round(max(0.0, observed_now - latest["runtime"]["observed_at_epoch"]), 3)
    if budget is not None:
        view["execution_budget"] = budget
    if attention is not None:
        view["tool_call_attention"] = attention
    return view
