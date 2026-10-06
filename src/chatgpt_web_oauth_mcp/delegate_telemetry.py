from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import json
import math
from pathlib import Path
import threading
import time
from typing import Any, Iterator

from .delegate_failures import error_parts, is_infrastructure_failure
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
_COMPLETION_SOURCE_RANK = {
    "metadata_mtime": 1,
    "source_metadata_mtime": 1,
    "started_plus_duration": 2,
    "completed_at_epoch": 3,
    "runtime_terminal": 4,
}


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

    @staticmethod
    def _error_code(snapshot: dict[str, object]) -> str:
        return error_parts(snapshot.get("error"))[0]

    @classmethod
    def _outcome(cls, snapshot: dict[str, object]) -> str:
        status = str(snapshot.get("status") or "").strip().lower()
        if status == "succeeded":
            return "completed"
        if status == "cancelled":
            return "cancelled"
        if status == "timed_out":
            return "timed_out"
        error = snapshot.get("error")
        code = cls._error_code(snapshot)
        if code == "delegate_reported_blocked":
            return "blocked"
        if code == "delegate_reported_partial":
            return "partial"
        if is_infrastructure_failure(
            harness=str(snapshot.get("harness") or snapshot.get("executor") or ""),
            error=error,
        ):
            return "infrastructure_error"
        if status == "failed":
            return "execution_error"
        return status or "unknown"

    @staticmethod
    def _feedback_outcome(
        record: dict[str, object],
    ) -> tuple[bool, bool, bool | None, str, str]:
        status = str(record.get("status") or "").strip().lower()
        verification = record.get("code_verification")
        verification_passed: bool | None = None
        if (
            isinstance(verification, dict)
            and verification.get("enabled") is True
            and not str(verification.get("skipped_reason") or "").strip()
        ):
            verification_passed = verification.get("verified") is True
        failure_type = ""
        failure_analysis = record.get("failure_analysis")
        if isinstance(failure_analysis, dict):
            failure_type = str(failure_analysis.get("failure_type") or "").strip()
        replan_action = ""
        replan = record.get("replan")
        if isinstance(replan, dict):
            replan_action = str(replan.get("action") or "").strip()
        success = status == "succeeded"
        accepted = (
            success
            and verification_passed is not False
            and failure_type in {"", "none"}
        )
        return success, accepted, verification_passed, failure_type, replan_action

    @staticmethod
    def _sanitize_route_attempts(snapshot: dict[str, object]) -> list[dict[str, object]]:
        raw_attempts = snapshot.get("route_attempts")
        if not isinstance(raw_attempts, list):
            return []
        attempts: list[dict[str, object]] = []
        for raw in raw_attempts:
            if not isinstance(raw, dict):
                continue
            route = str(raw.get("route") or "").strip()
            if not route:
                continue
            attempt: dict[str, object] = {
                "route": route,
                "harness": str(raw.get("harness") or ""),
                "kind": str(raw.get("kind") or ""),
                "model": str(raw.get("model") or ""),
                "reasoning_effort": str(raw.get("reasoning_effort") or ""),
                "status": str(raw.get("status") or ""),
                "success": raw.get("success") is True,
                "accepted": raw.get("accepted") is True,
                "failure_type": (
                    str(raw.get("failure_type"))
                    if raw.get("failure_type") is not None
                    else None
                ),
                "replan_action": (
                    str(raw.get("replan_action"))
                    if raw.get("replan_action") is not None
                    else None
                ),
            }
            duration = raw.get("duration_seconds")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                attempt["duration_seconds"] = max(0.0, float(duration))
            total_tokens = raw.get("total_tokens")
            if isinstance(total_tokens, int) and not isinstance(total_tokens, bool):
                attempt["total_tokens"] = max(0, total_tokens)
            attempts.append(attempt)
        return attempts

    @staticmethod
    def _sanitize_route_chain(snapshot: dict[str, object]) -> dict[str, object]:
        raw = snapshot.get("route_chain")
        if not isinstance(raw, dict):
            return {}
        chain: dict[str, object] = {}
        for key in ("initial_route", "final_route"):
            value = str(raw.get(key) or "").strip()
            if value:
                chain[key] = value
        for key in ("attempt_count", "fallback_count", "total_tokens"):
            value = raw.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                chain[key] = value
        duration = raw.get("total_duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            chain["total_duration_seconds"] = max(0.0, float(duration))
        return chain

    def _terminal_record(
        self,
        snapshot: dict[str, object],
        *,
        completed_at_epoch: float | None = None,
        completion_timestamp_source: str | None = None,
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
        status = str(snapshot.get("status") or "").strip().lower()
        error_code = self._error_code(snapshot)
        record: dict[str, object] = {
            "delegate_id": delegate_id,
            "harness": str(snapshot.get("harness") or snapshot.get("executor") or ""),
            "kind": str(snapshot.get("kind") or ""),
            "model": str(snapshot.get("model") or ""),
            "reasoning_effort": str(snapshot.get("reasoning_effort") or ""),
            "status": status,
            "success": status == "succeeded",
            "outcome": self._outcome(snapshot),
            "grouped": bool(snapshot.get("group_id")),
            "resumed": bool(snapshot.get("resume_from_delegate_id")),
            "routing_mode": str(snapshot.get("routing_mode") or "unknown"),
            "routing_reason": str(snapshot.get("routing_reason") or ""),
            "completed_at_epoch": timestamp,
        }
        route_decision = snapshot.get("route_decision")
        if isinstance(route_decision, dict):
            record["route_decision"] = dict(route_decision)
        failure_analysis = snapshot.get("failure_analysis")
        if isinstance(failure_analysis, dict):
            record["failure_analysis"] = dict(failure_analysis)
        replan = snapshot.get("replan")
        if isinstance(replan, dict):
            record["replan"] = dict(replan)
        if completion_timestamp_source:
            record["completion_timestamp_source"] = str(completion_timestamp_source)
        if error_code:
            record["error_code"] = error_code
        if status == "cancelled":
            record["cancel_reason"] = error_code or "cancelled"
            cancel_source = str(snapshot.get("cancel_source") or "").strip()
            if cancel_source:
                record["cancel_source"] = cancel_source
            cancel_rationale = str(snapshot.get("cancel_rationale") or "").strip()
            if cancel_rationale:
                record["cancel_rationale"] = cancel_rationale[:500]
        duration = snapshot.get("duration_seconds")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            record["duration_seconds"] = max(0.0, float(duration))
        usage = self._usage(snapshot)
        if usage:
            record["usage"] = usage
        code_verification = snapshot.get("code_verification")
        if isinstance(code_verification, dict):
            verification_summary: dict[str, object] = {
                "enabled": bool(code_verification.get("enabled")),
                "verified": bool(code_verification.get("verified")),
            }
            for key in (
                "contract_hash",
                "changed_file_count",
                "added_lines",
                "deleted_lines",
                "skipped_reason",
            ):
                value = code_verification.get(key)
                if value is not None:
                    verification_summary[key] = value
            scope_violations = code_verification.get("scope_violations")
            if isinstance(scope_violations, list):
                verification_summary["scope_violation_count"] = len(scope_violations)
            checks = code_verification.get("verification_commands")
            if isinstance(checks, list):
                verification_summary["verification_command_count"] = len(checks)
                verification_summary["verification_failed_count"] = sum(
                    1
                    for item in checks
                    if isinstance(item, dict) and item.get("passed") is not True
                )
            record["code_verification"] = verification_summary
        route_attempts = self._sanitize_route_attempts(snapshot)
        route_chain = self._sanitize_route_chain(snapshot)
        if route_attempts:
            record["route_attempts"] = route_attempts
        if route_chain:
            record["route_chain"] = route_chain

        task_shape_hash = ""
        if isinstance(route_decision, dict):
            task_shape_hash = str(route_decision.get("task_shape_hash") or "").strip()
        if task_shape_hash:
            (
                success,
                accepted,
                verification_passed,
                failure_type,
                replan_action,
            ) = self._feedback_outcome(record)

            final_route = str(route_chain.get("final_route") or "").strip()
            if not final_route and route_attempts:
                final_route = str(route_attempts[-1].get("route") or "").strip()
            if not final_route:
                final_route = (
                    f"{record['harness']}:{record['kind']}:{record['model']}:"
                    f"{record['reasoning_effort']}"
                )

            initial_route = str(route_chain.get("initial_route") or "").strip()
            if not initial_route and isinstance(route_decision, dict):
                initial_route = str(
                    route_decision.get("initial_route")
                    or route_decision.get("current_route")
                    or ""
                ).strip()
            if not initial_route and route_attempts:
                initial_route = str(route_attempts[0].get("route") or "").strip()
            if not initial_route:
                initial_route = final_route

            if route_attempts:
                normalized_final = dict(route_attempts[-1])
                normalized_final.update(
                    {
                        "route": final_route,
                        "success": success,
                        "accepted": accepted,
                        "failure_type": failure_type or None,
                        "replan_action": replan_action or None,
                    }
                )
                route_attempts[-1] = normalized_final
                record["route_attempts"] = route_attempts
            else:
                synthesized_attempt: dict[str, object] = {
                    "route": final_route,
                    "harness": record["harness"],
                    "kind": record["kind"],
                    "model": record["model"],
                    "reasoning_effort": record["reasoning_effort"],
                    "status": status,
                    "success": success,
                    "accepted": accepted,
                    "failure_type": failure_type or None,
                    "replan_action": replan_action or None,
                }
                if isinstance(record.get("duration_seconds"), (int, float)):
                    synthesized_attempt["duration_seconds"] = record["duration_seconds"]
                usage_record = record.get("usage")
                if isinstance(usage_record, dict):
                    total_tokens = usage_record.get("total_tokens")
                    if isinstance(total_tokens, int) and not isinstance(total_tokens, bool):
                        synthesized_attempt["total_tokens"] = total_tokens
                route_attempts = [synthesized_attempt]

            fallback_count = route_chain.get("fallback_count")
            if not isinstance(fallback_count, int) or isinstance(fallback_count, bool):
                fallback_count = max(0, len(route_attempts) - 1)
                routing_fallback = snapshot.get("routing_fallback")
                if isinstance(routing_fallback, dict):
                    attempts = routing_fallback.get("attempts")
                    if isinstance(attempts, list):
                        fallback_count = max(fallback_count, len(attempts))

            attempt_count = route_chain.get("attempt_count")
            if not isinstance(attempt_count, int) or isinstance(attempt_count, bool):
                attempt_count = len(route_attempts)

            chain_duration = route_chain.get("total_duration_seconds")
            if not isinstance(chain_duration, (int, float)) or isinstance(chain_duration, bool):
                chain_duration = record.get("duration_seconds")

            chain_tokens = route_chain.get("total_tokens")
            if not isinstance(chain_tokens, int) or isinstance(chain_tokens, bool):
                attempt_tokens = [
                    int(item["total_tokens"])
                    for item in route_attempts
                    if isinstance(item.get("total_tokens"), int)
                    and not isinstance(item.get("total_tokens"), bool)
                ]
                if attempt_tokens:
                    chain_tokens = sum(attempt_tokens)
                else:
                    usage_record = record.get("usage")
                    chain_tokens = (
                        usage_record.get("total_tokens")
                        if isinstance(usage_record, dict)
                        else None
                    )

            feedback: dict[str, object] = {
                "task_shape_hash": task_shape_hash,
                "initial_route": initial_route,
                "final_route": final_route,
                "success": success,
                "accepted": accepted,
                "verification_passed": verification_passed,
                "failure_type": failure_type or None,
                "replan_action": replan_action or None,
                "attempt_count": attempt_count,
                "fallback_count": fallback_count,
                "attempts": route_attempts,
            }
            if isinstance(chain_duration, (int, float)) and not isinstance(chain_duration, bool):
                feedback["duration_seconds"] = max(0.0, float(chain_duration))
            if isinstance(chain_tokens, int) and not isinstance(chain_tokens, bool):
                feedback["total_tokens"] = max(0, chain_tokens)
            record["route_feedback"] = feedback
        return delegate_id, record

    def record_terminal(
        self,
        snapshot: dict[str, object],
        *,
        completed_at_epoch: float | None = None,
        completion_timestamp_source: str | None = None,
    ) -> None:
        prepared = self._terminal_record(
            snapshot,
            completed_at_epoch=completed_at_epoch,
            completion_timestamp_source=completion_timestamp_source,
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

    @staticmethod
    def _merge_reconciled_record(
        previous: dict[str, object],
        incoming: dict[str, object],
    ) -> dict[str, object]:
        merged = dict(previous)
        for key, value in incoming.items():
            if key in {"completed_at_epoch", "completion_timestamp_source"}:
                continue
            if value is None or value == "":
                continue
            if key == "routing_mode" and value == "unknown":
                current = str(previous.get("routing_mode") or "")
                if current and current != "unknown":
                    continue
            if key in {"grouped", "resumed"}:
                merged[key] = bool(previous.get(key)) or bool(value)
                continue
            merged[key] = value

        route_feedback = merged.get("route_feedback")
        if isinstance(route_feedback, dict):
            normalized_feedback = dict(route_feedback)
            (
                success,
                accepted,
                verification_passed,
                failure_type,
                replan_action,
            ) = DelegateTelemetryStore._feedback_outcome(merged)
            normalized_feedback.update(
                {
                    "success": success,
                    "accepted": accepted,
                    "verification_passed": verification_passed,
                    "failure_type": failure_type or None,
                    "replan_action": replan_action or None,
                }
            )
            merged_attempts = merged.get("route_attempts")
            if isinstance(merged_attempts, list) and merged_attempts:
                attempts = [
                    dict(item)
                    for item in merged_attempts
                    if isinstance(item, dict)
                ]
                if attempts:
                    final_attempt = dict(attempts[-1])
                    final_attempt.update(
                        {
                            "success": success,
                            "accepted": accepted,
                            "failure_type": failure_type or None,
                            "replan_action": replan_action or None,
                        }
                    )
                    attempts[-1] = final_attempt
                    normalized_feedback["attempts"] = attempts
                    normalized_feedback["attempt_count"] = len(attempts)
                    normalized_feedback["fallback_count"] = max(0, len(attempts) - 1)
            merged_chain = merged.get("route_chain")
            if isinstance(merged_chain, dict):
                initial_route = str(merged_chain.get("initial_route") or "").strip()
                final_route = str(merged_chain.get("final_route") or "").strip()
                if initial_route:
                    normalized_feedback["initial_route"] = initial_route
                if final_route:
                    normalized_feedback["final_route"] = final_route
                duration = merged_chain.get("total_duration_seconds")
                if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                    normalized_feedback["duration_seconds"] = max(0.0, float(duration))
                total_tokens = merged_chain.get("total_tokens")
                if isinstance(total_tokens, int) and not isinstance(total_tokens, bool):
                    normalized_feedback["total_tokens"] = max(0, total_tokens)
            merged["route_feedback"] = normalized_feedback

        incoming_timestamp = incoming.get("completed_at_epoch")
        previous_timestamp = previous.get("completed_at_epoch")
        incoming_source = str(incoming.get("completion_timestamp_source") or "")
        previous_source = str(previous.get("completion_timestamp_source") or "")
        incoming_rank = _COMPLETION_SOURCE_RANK.get(incoming_source, 0)
        previous_rank = _COMPLETION_SOURCE_RANK.get(previous_source, 0)
        previous_timestamp_valid = (
            isinstance(previous_timestamp, (int, float))
            and not isinstance(previous_timestamp, bool)
        )
        should_replace_timestamp = (
            isinstance(incoming_timestamp, (int, float))
            and not isinstance(incoming_timestamp, bool)
            and (
                not previous_timestamp_valid
                or incoming_rank > previous_rank
                or (
                    previous_rank == 0
                    and incoming_rank >= _COMPLETION_SOURCE_RANK["started_plus_duration"]
                )
            )
        )
        if should_replace_timestamp:
            merged["completed_at_epoch"] = float(incoming_timestamp)
            if incoming_source:
                merged["completion_timestamp_source"] = incoming_source
        if "consumed_at_epoch" in previous:
            merged["consumed_at_epoch"] = previous["consumed_at_epoch"]
        return merged

    def reconcile_terminals_batch(
        self,
        items: list[tuple[dict[str, object], float | None, str | None]],
    ) -> dict[str, int]:
        prepared = [
            item
            for snapshot, completed_at_epoch, source in items
            if (
                item := self._terminal_record(
                    snapshot,
                    completed_at_epoch=completed_at_epoch,
                    completion_timestamp_source=source,
                )
            )
            is not None
        ]
        if not prepared:
            return {"added": 0, "updated": 0}

        added = 0
        updated = 0
        with self._transaction():
            payload = self._read_unlocked()
            records = payload["records"]
            for delegate_id, record in prepared:
                previous = records.get(delegate_id)
                if not isinstance(previous, dict):
                    records[delegate_id] = record
                    added += 1
                    continue
                reconciled = self._merge_reconciled_record(previous, record)
                if reconciled != previous:
                    records[delegate_id] = reconciled
                    updated += 1
            pruned = self._prune_unlocked(payload, now=time.time())
            if added or updated or pruned:
                self._write_unlocked(payload)
        return {"added": added, "updated": updated}

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
        outcomes: Counter[str] = Counter()
        cancel_reasons: Counter[str] = Counter()
        cancel_sources: Counter[str] = Counter()
        verified_code_records = 0
        verified_code_passed = 0
        scope_violation_records = 0
        scope_violation_count = 0
        verification_failed_records = 0
        verification_failed_count = 0
        for item in records:
            outcome = str(item.get("outcome") or "").strip()
            if not outcome:
                status = str(item.get("status") or "").strip().lower()
                outcome = {
                    "succeeded": "completed",
                    "cancelled": "cancelled",
                    "timed_out": "timed_out",
                    "failed": "execution_error",
                }.get(status, status or "unknown")
            outcomes[outcome] += 1
            if str(item.get("status") or "").strip().lower() == "cancelled":
                cancel_reasons[
                    str(
                        item.get("cancel_reason")
                        or item.get("error_code")
                        or "unspecified"
                    ).strip()
                    or "unspecified"
                ] += 1
                cancel_sources[
                    str(item.get("cancel_source") or "unspecified").strip()
                    or "unspecified"
                ] += 1
            code_verification = item.get("code_verification")
            if isinstance(code_verification, dict) and code_verification.get("enabled") is True:
                verified_code_records += 1
                if code_verification.get("verified") is True:
                    verified_code_passed += 1
                raw_scope_violations = code_verification.get("scope_violation_count")
                if (
                    isinstance(raw_scope_violations, int)
                    and not isinstance(raw_scope_violations, bool)
                    and raw_scope_violations > 0
                ):
                    scope_violation_records += 1
                    scope_violation_count += raw_scope_violations
                raw_failed_checks = code_verification.get("verification_failed_count")
                if (
                    isinstance(raw_failed_checks, int)
                    and not isinstance(raw_failed_checks, bool)
                    and raw_failed_checks > 0
                ):
                    verification_failed_records += 1
                    verification_failed_count += raw_failed_checks
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
            "cancel_reasons": dict(sorted(cancel_reasons.items())),
            "cancel_sources": dict(sorted(cancel_sources.items())),
            "timed_out": sum(item.get("status") == "timed_out" for item in records),
            "outcomes": dict(sorted(outcomes.items())),
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
            "code_verification": {
                "terminal": verified_code_records,
                "passed": verified_code_passed,
                "rejected": verified_code_records - verified_code_passed,
                "pass_rate": (
                    round(verified_code_passed / verified_code_records, 4)
                    if verified_code_records
                    else None
                ),
                "scope_violation_records": scope_violation_records,
                "scope_violation_count": scope_violation_count,
                "verification_failed_records": verification_failed_records,
                "verification_failed_count": verification_failed_count,
            },
        }

    @staticmethod
    def _feedback_aggregate(records: list[dict[str, Any]]) -> dict[str, object]:
        feedback_rows = [
            row
            for item in records
            if isinstance((row := item.get("route_feedback")), dict)
        ]
        succeeded = sum(row.get("success") is True for row in feedback_rows)
        accepted = sum(row.get("accepted") is True for row in feedback_rows)
        verification_rows = [
            row
            for row in feedback_rows
            if isinstance(row.get("verification_passed"), bool)
        ]
        verification_passed = sum(
            row.get("verification_passed") is True for row in verification_rows
        )
        durations = [
            float(row["duration_seconds"])
            for row in feedback_rows
            if isinstance(row.get("duration_seconds"), (int, float))
            and not isinstance(row.get("duration_seconds"), bool)
        ]
        tokens = [
            float(row["total_tokens"])
            for row in feedback_rows
            if isinstance(row.get("total_tokens"), int)
            and not isinstance(row.get("total_tokens"), bool)
        ]
        fallback_counts = [
            int(row["fallback_count"])
            for row in feedback_rows
            if isinstance(row.get("fallback_count"), int)
            and not isinstance(row.get("fallback_count"), bool)
        ]
        failure_types: Counter[str] = Counter()
        replan_actions: Counter[str] = Counter()
        for row in feedback_rows:
            failure_type = str(row.get("failure_type") or "").strip()
            if failure_type and failure_type != "none":
                failure_types[failure_type] += 1
            replan_action = str(row.get("replan_action") or "").strip()
            if replan_action and replan_action != "none":
                replan_actions[replan_action] += 1
        count = len(feedback_rows)
        attempt_counts = [
            int(row["attempt_count"])
            for row in feedback_rows
            if isinstance(row.get("attempt_count"), int)
            and not isinstance(row.get("attempt_count"), bool)
        ]
        return {
            "chains": count,
            "attempts": sum(attempt_counts) if attempt_counts else count,
            "succeeded": succeeded,
            "success_rate": round(succeeded / count, 4) if count else None,
            "accepted": accepted,
            "accepted_rate": round(accepted / count, 4) if count else None,
            "verification": {
                "attempts": len(verification_rows),
                "passed": verification_passed,
                "pass_rate": (
                    round(verification_passed / len(verification_rows), 4)
                    if verification_rows
                    else None
                ),
            },
            "duration_seconds": {
                "p50": _percentile(durations, 0.50),
                "p90": _percentile(durations, 0.90),
                "max": round(max(durations), 3) if durations else None,
            },
            "total_tokens": {
                "p50": _percentile(tokens, 0.50),
                "p90": _percentile(tokens, 0.90),
                "max": int(max(tokens)) if tokens else None,
            },
            "fallback_count": {
                "average": (
                    round(sum(fallback_counts) / len(fallback_counts), 4)
                    if fallback_counts
                    else None
                ),
                "max": max(fallback_counts) if fallback_counts else None,
            },
            "failure_types": dict(sorted(failure_types.items())),
            "replan_actions": dict(sorted(replan_actions.items())),
        }

    @staticmethod
    def _attempt_feedback_aggregate(
        attempts: list[dict[str, Any]],
    ) -> dict[str, object]:
        succeeded = sum(item.get("success") is True for item in attempts)
        accepted = sum(item.get("accepted") is True for item in attempts)
        durations = [
            float(item["duration_seconds"])
            for item in attempts
            if isinstance(item.get("duration_seconds"), (int, float))
            and not isinstance(item.get("duration_seconds"), bool)
        ]
        tokens = [
            float(item["total_tokens"])
            for item in attempts
            if isinstance(item.get("total_tokens"), int)
            and not isinstance(item.get("total_tokens"), bool)
        ]
        failure_types: Counter[str] = Counter()
        replan_actions: Counter[str] = Counter()
        for item in attempts:
            failure_type = str(item.get("failure_type") or "").strip()
            if failure_type and failure_type != "none":
                failure_types[failure_type] += 1
            replan_action = str(item.get("replan_action") or "").strip()
            if replan_action and replan_action != "none":
                replan_actions[replan_action] += 1
        count = len(attempts)
        return {
            "attempts": count,
            "succeeded": succeeded,
            "success_rate": round(succeeded / count, 4) if count else None,
            "accepted": accepted,
            "accepted_rate": round(accepted / count, 4) if count else None,
            "duration_seconds": {
                "p50": _percentile(durations, 0.50),
                "p90": _percentile(durations, 0.90),
                "max": round(max(durations), 3) if durations else None,
            },
            "total_tokens": {
                "p50": _percentile(tokens, 0.50),
                "p90": _percentile(tokens, 0.90),
                "max": int(max(tokens)) if tokens else None,
            },
            "failure_types": dict(sorted(failure_types.items())),
            "replan_actions": dict(sorted(replan_actions.items())),
        }

    def feedback_for_shape(self, task_shape_hash: str) -> dict[str, object]:
        """Return route feedback for one exact TaskShape fingerprint."""

        shape_hash = str(task_shape_hash or "").strip()
        if not shape_hash:
            return {"overall": self._feedback_aggregate([]), "routes": {}}
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

        shape_records: list[dict[str, Any]] = []
        route_attempts: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            feedback = record.get("route_feedback")
            if not isinstance(feedback, dict):
                continue
            if str(feedback.get("task_shape_hash") or "").strip() != shape_hash:
                continue
            shape_records.append(record)
            attempts = feedback.get("attempts")
            if isinstance(attempts, list):
                for attempt in attempts:
                    if not isinstance(attempt, dict):
                        continue
                    route = str(attempt.get("route") or "").strip()
                    if route:
                        route_attempts.setdefault(route, []).append(attempt)
                continue
            legacy_route = str(
                feedback.get("final_route") or feedback.get("route") or ""
            ).strip()
            if legacy_route:
                route_attempts.setdefault(legacy_route, []).append(
                    {
                        "route": legacy_route,
                        "success": feedback.get("success") is True,
                        "accepted": feedback.get("accepted") is True,
                        "duration_seconds": feedback.get("duration_seconds"),
                        "total_tokens": feedback.get("total_tokens"),
                        "failure_type": feedback.get("failure_type"),
                        "replan_action": feedback.get("replan_action"),
                    }
                )
        return {
            "overall": self._feedback_aggregate(shape_records),
            "routes": {
                route: self._attempt_feedback_aggregate(attempts)
                for route, attempts in sorted(route_attempts.items())
            },
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
        by_routing_mode: dict[str, list[dict[str, Any]]] = {}
        by_route_provenance: dict[str, list[dict[str, Any]]] = {}
        feedback_by_shape: dict[str, list[dict[str, Any]]] = {}
        feedback_by_shape_route: dict[str, dict[str, list[dict[str, Any]]]] = {}
        planner_modes: Counter[str] = Counter()
        planner_authorities: Counter[str] = Counter()
        planner_plan_kinds: Counter[str] = Counter()
        planner_legacy_routes: Counter[str] = Counter()
        planner_shadow_routes: Counter[str] = Counter()
        planner_selected_routes: Counter[str] = Counter()
        planner_records = 0
        planner_disagreements = 0
        for record in records:
            harness = str(record.get("harness") or "unknown")
            kind = str(record.get("kind") or "unknown")
            model = str(record.get("model") or "default")
            effort = str(record.get("reasoning_effort") or "default")
            routing_mode = str(record.get("routing_mode") or "unknown")
            routing_reason = str(record.get("routing_reason") or "unknown")
            by_harness.setdefault(harness, []).append(record)
            by_kind.setdefault(kind, []).append(record)
            by_route.setdefault(f"{harness}:{kind}:{model}:{effort}", []).append(record)
            by_routing_mode.setdefault(routing_mode, []).append(record)
            by_route_provenance.setdefault(
                f"{routing_mode}:{routing_reason}",
                [],
            ).append(record)
            route_decision = record.get("route_decision")
            if isinstance(route_decision, dict):
                planner = route_decision.get("planner")
                if isinstance(planner, dict):
                    planner_records += 1
                    mode = str(planner.get("mode") or "unknown")
                    authority = str(planner.get("authority") or "unknown")
                    plan_kind = str(planner.get("plan_kind") or "unknown")
                    legacy_route = str(planner.get("legacy_route") or "").strip()
                    shadow_route = str(planner.get("shadow_route") or "").strip()
                    selected_route = str(planner.get("selected_route") or "").strip()
                    planner_modes[mode] += 1
                    planner_authorities[authority] += 1
                    planner_plan_kinds[plan_kind] += 1
                    if legacy_route:
                        planner_legacy_routes[legacy_route] += 1
                    if shadow_route:
                        planner_shadow_routes[shadow_route] += 1
                    if selected_route:
                        planner_selected_routes[selected_route] += 1
                    if legacy_route and shadow_route and legacy_route != shadow_route:
                        planner_disagreements += 1
            route_feedback = record.get("route_feedback")
            if isinstance(route_feedback, dict):
                task_shape_hash = str(route_feedback.get("task_shape_hash") or "").strip()
                if task_shape_hash:
                    feedback_by_shape.setdefault(task_shape_hash, []).append(record)
                    attempts = route_feedback.get("attempts")
                    if isinstance(attempts, list):
                        for attempt in attempts:
                            if not isinstance(attempt, dict):
                                continue
                            attempt_route = str(attempt.get("route") or "").strip()
                            if attempt_route:
                                feedback_by_shape_route.setdefault(
                                    task_shape_hash,
                                    {},
                                ).setdefault(attempt_route, []).append(attempt)
                    else:
                        legacy_route = str(
                            route_feedback.get("final_route")
                            or route_feedback.get("route")
                            or ""
                        ).strip()
                        if legacy_route:
                            legacy_attempt = {
                                "route": legacy_route,
                                "success": route_feedback.get("success") is True,
                                "accepted": route_feedback.get("accepted") is True,
                                "duration_seconds": route_feedback.get("duration_seconds"),
                                "total_tokens": route_feedback.get("total_tokens"),
                                "failure_type": route_feedback.get("failure_type"),
                                "replan_action": route_feedback.get("replan_action"),
                            }
                            feedback_by_shape_route.setdefault(
                                task_shape_hash,
                                {},
                            ).setdefault(legacy_route, []).append(legacy_attempt)

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
            "by_routing_mode": {
                key: self._aggregate(value)
                for key, value in sorted(by_routing_mode.items())
            },
            "by_route_provenance": {
                key: self._aggregate(value)
                for key, value in sorted(by_route_provenance.items())
            },
            "planner": {
                "records": planner_records,
                "shadow_disagreements": planner_disagreements,
                "shadow_disagreement_rate": (
                    round(planner_disagreements / planner_records, 4)
                    if planner_records
                    else None
                ),
                "modes": dict(sorted(planner_modes.items())),
                "authorities": dict(sorted(planner_authorities.items())),
                "plan_kinds": dict(sorted(planner_plan_kinds.items())),
                "legacy_routes": dict(sorted(planner_legacy_routes.items())),
                "shadow_routes": dict(sorted(planner_shadow_routes.items())),
                "selected_routes": dict(sorted(planner_selected_routes.items())),
            },
            "route_feedback": {
                shape_hash: {
                    "overall": self._feedback_aggregate(shape_records),
                    "routes": {
                        route: self._attempt_feedback_aggregate(route_records)
                        for route, route_records in sorted(
                            feedback_by_shape_route.get(shape_hash, {}).items()
                        )
                    },
                }
                for shape_hash, shape_records in sorted(feedback_by_shape.items())
            },
        }
