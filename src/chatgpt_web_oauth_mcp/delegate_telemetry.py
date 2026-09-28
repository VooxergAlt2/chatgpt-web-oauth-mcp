from __future__ import annotations

from contextlib import contextmanager
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Iterator

from .state_io import atomic_write_bytes, ensure_private_directory, interprocess_file_lock


SCHEMA_VERSION = 1
DEFAULT_MAX_RECORDS = 2000
DEFAULT_RETENTION_SECONDS = 30 * 86400
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_read_tokens",
    "total_tokens",
)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


class DelegateTelemetryStore:
    """Small durable store for delegate lifecycle/consumption aggregates."""

    def __init__(
        self,
        path: Path,
        *,
        max_records: int = DEFAULT_MAX_RECORDS,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.max_records = max(1, int(max_records))
        self.retention_seconds = max(60.0, float(retention_seconds))
        self._lock = threading.RLock()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        ensure_private_directory(self.path.parent)
        with self._lock:
            with interprocess_file_lock(self.lock_path):
                yield

    def _empty(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "records": {},
            "order": [],
        }

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._empty()
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            return self._empty()
        records = payload.get("records")
        order = payload.get("order")
        if not isinstance(records, dict) or not isinstance(order, list):
            return self._empty()
        return {
            "schema_version": SCHEMA_VERSION,
            "records": records,
            "order": [str(item) for item in order if str(item) in records],
        }

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        atomic_write_bytes(self.path, encoded, mode=0o600, sync_directory=True)

    def _prune_unlocked(self, payload: dict[str, Any], *, now: float) -> bool:
        records = payload["records"]
        cutoff = now - self.retention_seconds
        changed = False
        retained: list[tuple[float, str]] = []
        for delegate_id, record in list(records.items()):
            if not isinstance(record, dict):
                records.pop(delegate_id, None)
                changed = True
                continue
            timestamp = record.get("completed_at_epoch")
            if isinstance(timestamp, (int, float)) and float(timestamp) < cutoff:
                records.pop(delegate_id, None)
                changed = True
                continue
            retained.append(
                (
                    float(timestamp)
                    if isinstance(timestamp, (int, float))
                    else 0.0,
                    str(delegate_id),
                )
            )
        retained.sort()
        if len(retained) > self.max_records:
            for _timestamp, delegate_id in retained[
                : len(retained) - self.max_records
            ]:
                records.pop(delegate_id, None)
            retained = retained[-self.max_records :]
            changed = True
        canonical_order = [delegate_id for _timestamp, delegate_id in retained]
        if canonical_order != payload["order"]:
            payload["order"] = canonical_order
            changed = True
        return changed

    @staticmethod
    def _usage(snapshot: dict[str, object]) -> dict[str, int]:
        metadata = snapshot.get("harness_metadata")
        usage = metadata.get("usage") if isinstance(metadata, dict) else None
        if not isinstance(usage, dict):
            return {}
        result: dict[str, int] = {}
        for field in _USAGE_FIELDS:
            value = usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[field] = value
        return result

    def _terminal_record(
        self,
        snapshot: dict[str, object],
        *,
        completed_at_epoch: float | None = None,
    ) -> tuple[str, dict[str, object]] | None:
        delegate_id = str(snapshot.get("delegate_id") or "").strip()
        if not delegate_id:
            return None
        timestamp = (
            float(completed_at_epoch)
            if isinstance(completed_at_epoch, (int, float))
            and not isinstance(completed_at_epoch, bool)
            else time.time()
        )
        record: dict[str, object] = {
            "delegate_id": delegate_id,
            "harness": str(snapshot.get("harness") or snapshot.get("executor") or ""),
            "kind": str(snapshot.get("kind") or ""),
            "model": str(snapshot.get("model") or ""),
            "reasoning_effort": str(snapshot.get("reasoning_effort") or ""),
            "status": str(snapshot.get("status") or ""),
            "success": bool(snapshot.get("success")),
            "grouped": bool(snapshot.get("group_id")),
            "resumed": bool(snapshot.get("resume_from_delegate_id")),
            "completed_at_epoch": timestamp,
        }
        duration = snapshot.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            record["duration_seconds"] = max(0.0, float(duration))
        usage = self._usage(snapshot)
        if usage:
            record["usage"] = usage
        return delegate_id, record

    def record_terminal(
        self,
        snapshot: dict[str, object],
        *,
        completed_at_epoch: float | None = None,
    ) -> None:
        prepared = self._terminal_record(
            snapshot,
            completed_at_epoch=completed_at_epoch,
        )
        if prepared is None:
            return
        delegate_id, record = prepared
        with self._transaction():
            payload = self._read_unlocked()
            records = payload["records"]
            previous = records.get(delegate_id)
            if isinstance(previous, dict):
                if "consumed_at_epoch" in previous:
                    record["consumed_at_epoch"] = previous["consumed_at_epoch"]
            records[delegate_id] = record
            self._prune_unlocked(payload, now=time.time())
            self._write_unlocked(payload)

    def record_terminals_batch(
        self,
        items: list[tuple[dict[str, object], float | None]],
        *,
        skip_existing: bool = False,
    ) -> int:
        prepared = [
            item
            for snapshot, completed_at_epoch in items
            if (
                item := self._terminal_record(
                    snapshot,
                    completed_at_epoch=completed_at_epoch,
                )
            )
            is not None
        ]
        if not prepared:
            return 0

        added = 0
        with self._transaction():
            payload = self._read_unlocked()
            records = payload["records"]
            for delegate_id, record in prepared:
                previous = records.get(delegate_id)
                if skip_existing and isinstance(previous, dict):
                    continue
                if isinstance(previous, dict) and "consumed_at_epoch" in previous:
                    record["consumed_at_epoch"] = previous["consumed_at_epoch"]
                records[delegate_id] = record
                added += 1
            pruned = self._prune_unlocked(payload, now=time.time())
            if added or pruned:
                self._write_unlocked(payload)
        return added

    def mark_consumed(
        self,
        delegate_id: str,
        *,
        consumed_at_epoch: float | None = None,
    ) -> bool:
        normalized = str(delegate_id or "").strip()
        if not normalized:
            return False
        timestamp = time.time() if consumed_at_epoch is None else float(consumed_at_epoch)
        with self._transaction():
            payload = self._read_unlocked()
            record = payload["records"].get(normalized)
            if not isinstance(record, dict):
                return False
            if isinstance(record.get("consumed_at_epoch"), (int, float)):
                return True
            record["consumed_at_epoch"] = timestamp
            self._prune_unlocked(payload, now=time.time())
            self._write_unlocked(payload)
            return True

    @staticmethod
    def _aggregate(records: list[dict[str, Any]]) -> dict[str, object]:
        durations = [
            float(item["duration_seconds"])
            for item in records
            if isinstance(item.get("duration_seconds"), (int, float))
        ]
        successful = [item for item in records if item.get("status") == "succeeded"]
        consumed = [
            item
            for item in records
            if isinstance(item.get("consumed_at_epoch"), (int, float))
        ]
        successful_consumed = [
            item
            for item in successful
            if isinstance(item.get("consumed_at_epoch"), (int, float))
        ]
        usage_totals = {field: 0 for field in _USAGE_FIELDS}
        usage_records = 0
        for item in records:
            usage = item.get("usage")
            if not isinstance(usage, dict):
                continue
            usage_records += 1
            for field in _USAGE_FIELDS:
                value = usage.get(field)
                if isinstance(value, int) and not isinstance(value, bool):
                    usage_totals[field] += value
        return {
            "terminal": len(records),
            "succeeded": len(successful),
            "failed": sum(item.get("status") == "failed" for item in records),
            "cancelled": sum(item.get("status") == "cancelled" for item in records),
            "timed_out": sum(item.get("status") == "timed_out" for item in records),
            "consumed": len(consumed),
            "successful_consumed": len(successful_consumed),
            "successful_consume_rate": (
                round(len(successful_consumed) / len(successful), 4)
                if successful
                else None
            ),
            "duration_seconds": {
                "p50": _percentile(durations, 0.50),
                "p90": _percentile(durations, 0.90),
                "max": round(max(durations), 3) if durations else None,
            },
            "usage_records": usage_records,
            "usage_totals": usage_totals if usage_records else {},
        }

    def snapshot(self) -> dict[str, object]:
        now = time.time()
        with self._transaction():
            payload = self._read_unlocked()
            if self._prune_unlocked(payload, now=now):
                self._write_unlocked(payload)
            records = [
                payload["records"][delegate_id]
                for delegate_id in payload["order"]
                if isinstance(payload["records"].get(delegate_id), dict)
            ]

        by_harness: dict[str, list[dict[str, Any]]] = {}
        by_kind: dict[str, list[dict[str, Any]]] = {}
        by_route: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            harness = str(record.get("harness") or "unknown")
            kind = str(record.get("kind") or "unknown")
            model = str(record.get("model") or "default")
            effort = str(record.get("reasoning_effort") or "default")
            by_harness.setdefault(harness, []).append(record)
            by_kind.setdefault(kind, []).append(record)
            by_route.setdefault(f"{harness}:{kind}:{model}:{effort}", []).append(record)

        return {
            "enabled": True,
            "path": str(self.path),
            "records": len(records),
            "retention_seconds": self.retention_seconds,
            "max_records": self.max_records,
            "overall": self._aggregate(records),
            "by_harness": {
                key: self._aggregate(value)
                for key, value in sorted(by_harness.items())
            },
            "by_kind": {
                key: self._aggregate(value)
                for key, value in sorted(by_kind.items())
            },
            "by_route": {
                key: self._aggregate(value)
                for key, value in sorted(by_route.items())
            },
        }
