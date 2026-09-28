from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable


POLICY_SCHEMA_VERSION = 1
DEFAULT_THRESHOLD_PERCENT = 0.0
DEFAULT_FORCED_BLOCK_COOLDOWN_SECONDS = 300.0


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clamp_percent(value: object, default: float = DEFAULT_THRESHOLD_PERCENT) -> float:
    number = _number(value)
    if number is None:
        return default
    return round(min(100.0, max(0.0, number)), 4)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _retry_after_seconds(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", value.lower()):
        matched = True
        factor = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}[unit]
        total += float(amount) * factor
    return total if matched and total > 0 else None


def _antigravity_family(model: str | None) -> str:
    normalized_model = (model or "").strip().lower()
    return "gemini" if "gemini" in normalized_model or not normalized_model else "claude_gpt"


def _forced_block_key(harness: str, model: str | None) -> str:
    normalized_harness = harness.strip().lower()
    if normalized_harness in {"antigravity", "antigravity2"}:
        return f"{normalized_harness}:{_antigravity_family(model)}"
    return normalized_harness


def quota_bucket_keys(harness: str, model: str | None) -> tuple[str, str]:
    normalized_harness = harness.strip().lower()
    if normalized_harness in {"antigravity", "antigravity2"}:
        family = _antigravity_family(model)
        prefix = normalized_harness
        return (
            f"{prefix}_{family}_5h",
            f"{prefix}_{family}_weekly",
        )
    return (f"{normalized_harness}_5h", f"{normalized_harness}_weekly")


def _provider_name(harness: str) -> str:
    return harness.strip().lower()


def _matching_window(
    provider: dict[str, Any],
    *,
    harness: str,
    model: str | None,
    window_name: str,
) -> dict[str, Any] | None:
    windows = provider.get("windows")
    if not isinstance(windows, list):
        return None
    normalized_harness = harness.strip().lower()
    normalized_model = (model or "").strip().lower()
    for item in windows:
        if not isinstance(item, dict):
            continue
        if normalized_harness in {"antigravity", "antigravity2"}:
            if item.get("window") != window_name:
                continue
            group = str(item.get("group") or "").lower()
            if "gemini" in normalized_model or not normalized_model:
                if "gemini" in group:
                    return item
            elif "claude" in group or "gpt" in group:
                return item
            continue
        if normalized_harness == "claude":
            expected_id = "five_hour" if window_name == "5h" else "seven_day"
            if item.get("id") == expected_id:
                return item
            continue
        if normalized_harness == "codex":
            duration = 300 if window_name == "5h" else 10080
            if item.get("duration_minutes") == duration:
                return item
            continue
    return None


@dataclass(frozen=True)
class QuotaDecision:
    allowed: bool
    harness: str
    model: str | None
    provider_status: str
    checked_at: str
    buckets: tuple[dict[str, object], ...]
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "harness": self.harness,
            "model": self.model,
            "provider_status": self.provider_status,
            "checked_at": self.checked_at,
            "reason": self.reason,
            "buckets": [dict(item) for item in self.buckets],
        }


class DelegateQuotaAdmissionGate:
    """Admission-only quota gate driven by MQTT-persisted threshold policy."""

    def __init__(
        self,
        *,
        usage_provider: Callable[[], dict[str, Any]],
        policy_path: Path,
        fresh_usage_provider: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.usage_provider = usage_provider
        self.fresh_usage_provider = fresh_usage_provider
        self.policy_path = Path(policy_path)
        self._lock = threading.RLock()
        self._forced_blocks: dict[str, dict[str, object]] = {}

    def policy_snapshot(self) -> dict[str, object]:
        payload = self._read_policy()
        thresholds = payload.get("thresholds")
        thresholds = thresholds if isinstance(thresholds, dict) else {}
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "updated_at": payload.get("updated_at"),
            "thresholds": {
                str(key): _clamp_percent(value)
                for key, value in sorted(thresholds.items())
                if isinstance(key, str)
            },
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            forced = deepcopy(self._forced_blocks)
        return {
            "policy": self.policy_snapshot(),
            "forced_blocks": forced,
        }

    def decision(
        self,
        *,
        harness: str,
        model: str | None,
        fresh: bool = False,
    ) -> dict[str, object]:
        normalized = harness.strip().lower()
        now = _iso_now()
        now_epoch = time.time()
        usage = (
            self.fresh_usage_provider(normalized)
            if fresh and self.fresh_usage_provider is not None
            else self.usage_provider()
        )
        providers = usage.get("providers") if isinstance(usage, dict) else None
        provider = (
            providers.get(_provider_name(normalized))
            if isinstance(providers, dict)
            else None
        )
        provider = provider if isinstance(provider, dict) else {}
        provider_status = str(provider.get("status") or "unavailable")
        thresholds = self.policy_snapshot()["thresholds"]
        assert isinstance(thresholds, dict)
        bucket_keys = quota_bucket_keys(normalized, model)
        bucket_rows: list[dict[str, object]] = []
        blocked = False
        for key, window_name in zip(bucket_keys, ("5h", "weekly"), strict=True):
            window = _matching_window(
                provider,
                harness=normalized,
                model=model,
                window_name=window_name,
            )
            threshold = _clamp_percent(thresholds.get(key))
            remaining = (
                _number(window.get("remaining_percent"))
                if isinstance(window, dict)
                else None
            )
            resets_at = window.get("resets_at") if isinstance(window, dict) else None
            reset_epoch = _parse_iso_epoch(resets_at)
            window_expired = reset_epoch is not None and reset_epoch <= now_epoch
            row: dict[str, object] = {
                "key": key,
                "window": window_name,
                "threshold_percent": threshold,
                "remaining_percent": remaining,
                "resets_at": resets_at,
                "window_expired": window_expired,
                "blocked": (
                    remaining is not None
                    and not window_expired
                    and remaining <= threshold
                ),
            }
            if row["blocked"]:
                blocked = True
            bucket_rows.append(row)

        decision_reason: str | None = "quota_threshold_reached" if blocked else None
        forced_block_key = _forced_block_key(normalized, model)
        with self._lock:
            forced = deepcopy(self._forced_blocks.get(forced_block_key))
        if forced is not None:
            forced_keys = {
                str(item)
                for item in forced.get("bucket_keys", [])
                if isinstance(item, str)
            }
            requested_keys = set(bucket_keys)
            applies = not forced_keys or bool(forced_keys & requested_keys)
            forced_until = _number(forced.get("blocked_until_epoch"))
            if applies and forced_until is None:
                reset_candidates = [
                    epoch
                    for row in bucket_rows
                    if row.get("key") in forced_keys
                    for epoch in [_parse_iso_epoch(row.get("resets_at"))]
                    if epoch is not None and epoch > now_epoch
                ]
                if reset_candidates:
                    forced_until = min(reset_candidates)
                else:
                    observed_epoch = _number(forced.get("observed_at_epoch"))
                    if observed_epoch is None:
                        observed_epoch = _parse_iso_epoch(forced.get("observed_at"))
                    forced_until = (
                        observed_epoch
                        if observed_epoch is not None
                        else now_epoch
                    ) + DEFAULT_FORCED_BLOCK_COOLDOWN_SECONDS
                forced["blocked_until_epoch"] = forced_until
                with self._lock:
                    current = self._forced_blocks.get(forced_block_key)
                    if current is not None:
                        current["blocked_until_epoch"] = forced_until
            recovered = (
                applies
                and forced_until is not None
                and now_epoch >= forced_until
            )
            if recovered:
                with self._lock:
                    self._forced_blocks.pop(forced_block_key, None)
                forced = None
            elif applies:
                blocked = True
                decision_reason = "runtime_quota_exhausted"
                bucket_rows.append(
                    {"key": "runtime_quota_exhausted", **forced, "blocked": True}
                )

        decision = QuotaDecision(
            allowed=not blocked,
            harness=normalized,
            model=model,
            provider_status=provider_status,
            checked_at=now,
            buckets=tuple(bucket_rows),
            reason=decision_reason if blocked else None,
        )
        return decision.as_dict()

    def note_terminal(self, snapshot: dict[str, object]) -> None:
        harness = str(snapshot.get("harness") or "").strip().lower()
        metadata = snapshot.get("harness_metadata")
        error = snapshot.get("error")
        quota_exhausted = (
            isinstance(metadata, dict) and metadata.get("quota_exhausted") is True
        ) or (
            isinstance(error, dict)
            and str(error.get("code") or "").endswith("quota_exhausted")
        )
        if not harness or not quota_exhausted:
            return
        retry_after = metadata.get("retry_after") if isinstance(metadata, dict) else None
        model = str(snapshot.get("model") or "").strip() or None
        retry_seconds = _retry_after_seconds(retry_after)
        observed_at_epoch = time.time()
        forced_block_key = _forced_block_key(harness, model)
        with self._lock:
            self._forced_blocks[forced_block_key] = {
                "reason": "runtime_quota_exhausted",
                "retry_after": retry_after,
                "observed_at": _iso_now(),
                "observed_at_epoch": observed_at_epoch,
                "blocked_until_epoch": (
                    observed_at_epoch + retry_seconds
                    if retry_seconds is not None
                    else None
                ),
                "model": model,
                "bucket_keys": list(quota_bucket_keys(harness, model)),
            }

    def clear_forced_block(self, harness: str, model: str | None = None) -> None:
        normalized = harness.strip().lower()
        with self._lock:
            if normalized in {"antigravity", "antigravity2"} and model is None:
                prefix = f"{normalized}:"
                for key in tuple(self._forced_blocks):
                    if key.startswith(prefix):
                        self._forced_blocks.pop(key, None)
                return
            self._forced_blocks.pop(_forced_block_key(normalized, model), None)

    def _read_policy(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.policy_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {"schema_version": POLICY_SCHEMA_VERSION, "thresholds": {}}
        if not isinstance(payload, dict):
            return {"schema_version": POLICY_SCHEMA_VERSION, "thresholds": {}}
        if payload.get("schema_version") != POLICY_SCHEMA_VERSION:
            return {"schema_version": POLICY_SCHEMA_VERSION, "thresholds": {}}
        return payload
