"""Compare one named AGY quota window across two offline Opss snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from typing import Any


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
        if isinstance(val, bool):
            return False
        if isinstance(val, int):
            return True
        if isinstance(val, float):
            return math.isfinite(val)
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


def _parse_aware_iso(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise argparse.ArgumentError(None, f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentError(None, f"{label} must include a UTC offset")
    return parsed


def _read_json(path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON snapshot: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"snapshot root must be a JSON object: {path}")
    return payload


def _usage_limits(payload: dict[str, Any]) -> dict[str, Any] | None:
    nested = payload.get("usage_limits")
    if isinstance(nested, dict):
        return nested
    if isinstance(payload.get("providers"), dict):
        return payload
    return None


def _select_observation(
    payload: dict[str, Any], account: str, group: str, window: str
) -> tuple[dict[str, Any] | None, str]:
    usage = _usage_limits(payload)
    providers = usage.get("providers") if isinstance(usage, dict) else None
    if not isinstance(providers, dict):
        return None, "missing"
    provider = providers.get(account)
    if not isinstance(provider, dict):
        return None, "missing"
    windows = provider.get("windows")
    if not isinstance(windows, list):
        return None, "missing"

    selected = [
        row for row in windows if isinstance(row, dict)
        and row.get("group") == group and row.get("window") == window
    ]
    if not selected:
        return None, "missing"
    if len(selected) != 1:
        return None, "ambiguous"
    if provider.get("status") != "ok":
        return None, "unavailable"
    updated_at = provider.get("updated_at")
    try:
        sampled_at = _parse_aware_iso(updated_at, "provider.updated_at").timestamp()
    except (argparse.ArgumentError, OverflowError, OSError, ValueError):
        return None, "stale"
    if not math.isfinite(sampled_at):
        return None, "stale"
    item = selected[0]
    return {
        "account": account,
        "group": group,
        "window": window,
        "resets_at": item.get("resets_at"),
        "sampled_at": sampled_at,
        "remaining_percent": item.get("remaining_percent"),
    }, "ok"


def _result(delta: float | None, reason: str) -> dict[str, Any]:
    return {"delta_pp": delta, "reason": reason}


def _evaluate(args: argparse.Namespace) -> dict[str, Any]:
    started = _parse_aware_iso(args.started_at, "--started-at")
    finished = _parse_aware_iso(args.finished_at, "--finished-at")
    if finished <= started:
        raise argparse.ArgumentError(None, "--finished-at must be later than --started-at")
    if args.max_gap_seconds < 0 or not math.isfinite(args.max_gap_seconds):
        raise argparse.ArgumentError(None, "--max-gap-seconds must be finite and nonnegative")

    try:
        before_payload = _read_json(args.before)
        after_payload = _read_json(args.after)
    except ValueError as exc:
        raise argparse.ArgumentError(None, str(exc)) from exc

    if args.concurrent:
        return _result(None, "concurrent")

    before, before_reason = _select_observation(
        before_payload, args.account, args.group, args.window
    )
    after, after_reason = _select_observation(
        after_payload, args.account, args.group, args.window
    )
    if before is None or after is None:
        for reason in ("ambiguous", "unavailable", "stale", "missing"):
            if reason in {before_reason, after_reason}:
                return _result(None, reason)
        return _result(None, "missing")
    lead = started.timestamp() - before["sampled_at"]
    lag = after["sampled_at"] - finished.timestamp()
    if not (0 <= lead <= args.max_gap_seconds and 0 <= lag <= args.max_gap_seconds):
        return _result(None, "stale")

    delta, reason = quota_delta(before, after)
    return _result(delta, reason)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", help="JSON file containing the initial usage_limits snapshot")
    parser.add_argument("after", help="JSON file containing the final usage_limits snapshot")
    parser.add_argument("--account", required=True, help="Exact provider/account key")
    parser.add_argument("--group", required=True, help="Exact native window group")
    parser.add_argument("--window", required=True, help="Exact native AGY window (e.g. 5h or weekly)")
    parser.add_argument("--started-at", required=True, help="Aware ISO-8601 episode start")
    parser.add_argument("--finished-at", required=True, help="Aware ISO-8601 episode finish")
    parser.add_argument("--max-gap-seconds", type=float, default=60.0)
    parser.add_argument("--concurrent", action="store_true", help="Mark the episode as confounded")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = _evaluate(args)
    except argparse.ArgumentError as exc:
        parser.error(str(exc))
    print(json.dumps(result, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
