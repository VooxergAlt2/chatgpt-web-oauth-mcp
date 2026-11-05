from datetime import datetime
import math


def quota_delta(before, after, *, concurrent=False):
    if concurrent:
        return (None, "concurrent")

    if not isinstance(before, dict) or not isinstance(after, dict):
        return (None, "identity")

    for key in ("account", "group", "window"):
        b_val = before.get(key)
        a_val = after.get(key)
        if not isinstance(b_val, str) or not b_val.strip() or b_val != a_val:
            return (None, "identity")

    def _is_finite_num(val):
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return False
        try:
            return math.isfinite(val)
        except (OverflowError, ValueError):
            return False

    b_sampled = before.get("sampled_at")
    a_sampled = after.get("sampled_at")
    if not _is_finite_num(b_sampled) or not _is_finite_num(a_sampled):
        return (None, "stale")
    if not (a_sampled > b_sampled):
        return (None, "stale")

    b_resets = before.get("resets_at")
    a_resets = after.get("resets_at")
    if not isinstance(b_resets, str) or not b_resets.strip() or b_resets != a_resets:
        return (None, "reset")

    try:
        dt = datetime.fromisoformat(b_resets)
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            return (None, "reset")
        reset_instant = dt.timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return (None, "reset")

    if not (b_sampled < reset_instant and a_sampled < reset_instant):
        return (None, "reset")

    b_rem = before.get("remaining_percent")
    a_rem = after.get("remaining_percent")
    if not _is_finite_num(b_rem) or not (0 <= b_rem <= 100):
        return (None, "unavailable")
    if not _is_finite_num(a_rem) or not (0 <= a_rem <= 100):
        return (None, "unavailable")

    if a_rem > b_rem:
        return (None, "increased")
    if a_rem == b_rem:
        return (None, "below_resolution")

    return (round(b_rem - a_rem, 3), "observed")