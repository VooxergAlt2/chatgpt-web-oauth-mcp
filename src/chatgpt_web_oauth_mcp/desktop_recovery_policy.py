"""Pure policy decisions for desktop UI lease expiration and recovery."""
from __future__ import annotations

import math
from typing import Any


def _is_finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _has_activity(value: Any) -> bool:
    if value is False or value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return not _is_finite_number(value) or value != 0
    if isinstance(value, (list, tuple, dict, set, frozenset)):
        return len(value) > 0
    return True


def expired_idle_lease_reason(
    ui: Any,
    *,
    now: Any,
    native_outstanding: Any = False,
    active_readers: Any = 0,
) -> str | None:
    """Identify safe expired idle desktop lease without OS or state side-effects."""
    if not isinstance(ui, dict):
        return None
    if ui.get("state") != "idle":
        return None
    if ui.get("operation_token") is not None:
        return None
    if ui.get("activation_request_id") is not None:
        return None
    if ui.get("durable"):
        return None
    if _has_activity(native_outstanding) or _has_activity(active_readers):
        return None
    if not _is_finite_number(now):
        return None
    expires_at = ui.get("expires_at")
    if not _is_finite_number(expires_at):
        return None
    if expires_at > now:
        return None
    return "expired_idle_lease"
