"""Durable semantic checkpoints for resumable logical ChatGPT sessions."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import threading
import time
from typing import Any, Iterator

from .state_io import atomic_write_bytes, interprocess_file_lock


MAX_CONSUMED_RUNTIME_REFERENCES = 8
MAX_UNCONSUMED_RUNTIME_REFERENCES = 64
MAX_OWNED_RUNTIME_REFERENCES = 256
POLL_REQUIRED = "POLL_REQUIRED"
RESULT_REQUIRES_CONSUMPTION = "RESULT_REQUIRES_CONSUMPTION"
RESULT_CONSUMED = "RESULT_CONSUMED"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _owned_state(
    previous: object,
    incoming: dict[str, Any],
    *,
    timestamp: float,
) -> dict[str, Any]:
    prior = previous if isinstance(previous, dict) else {}
    state = deepcopy(incoming)
    owned_at = prior.get("owned_at")
    if not isinstance(owned_at, (int, float)):
        owned_at = timestamp
    state["owned_at"] = float(owned_at)
    state["owned_at_iso"] = _iso(float(owned_at))

    terminal = bool(state.get("terminal"))
    if terminal:
        consumed_at = prior.get("result_consumed_at")
        ready_at = prior.get("result_ready_at")
        if not isinstance(ready_at, (int, float)):
            ready_at = timestamp
        state["result_ready_at"] = float(ready_at)
        state["result_ready_at_iso"] = _iso(float(ready_at))
        if isinstance(consumed_at, (int, float)):
            state["continuation_state"] = RESULT_CONSUMED
            state["result_consumed_at"] = float(consumed_at)
            state["result_consumed_at_iso"] = _iso(float(consumed_at))
        else:
            state["continuation_state"] = RESULT_REQUIRES_CONSUMPTION
            state.pop("result_consumed_at", None)
            state.pop("result_consumed_at_iso", None)
    else:
        state["continuation_state"] = POLL_REQUIRED
        state.pop("result_ready_at", None)
        state.pop("result_ready_at_iso", None)
        state.pop("result_consumed_at", None)
        state.pop("result_consumed_at_iso", None)
    return state


def _prune_consumed_history(
    collection: dict[str, Any],
    order: list[str],
) -> list[str]:
    consumed_ids = [
        result_id
        for result_id in order
        if isinstance(collection.get(result_id), dict)
        and collection[result_id].get("continuation_state") == RESULT_CONSUMED
    ]
    excess = max(0, len(consumed_ids) - MAX_CONSUMED_RUNTIME_REFERENCES)
    for result_id in consumed_ids[:excess]:
        state = collection.get(result_id)
        if not isinstance(state, dict):
            continue
        collection[result_id] = {
            key: deepcopy(state[key])
            for key in (
                "status",
                "success",
                "terminal",
                "continuation_state",
                "owned_at",
                "owned_at_iso",
                "result_ready_at",
                "result_ready_at_iso",
                "result_consumed_at",
                "result_consumed_at_iso",
                "group_id",
            )
            if key in state
        }
    return [result_id for result_id in order if result_id in collection]


def _unconsumed_reference_count(*collections: object) -> int:
    return sum(
        1
        for collection in collections
        if isinstance(collection, dict)
        for state in collection.values()
        if isinstance(state, dict)
        and state.get("continuation_state") != RESULT_CONSUMED
    )


def _owned_reference_count(*collections: object) -> int:
    return sum(
        len(collection)
        for collection in collections
        if isinstance(collection, dict)
    )


def _result_owned_elsewhere(
    payload: dict[str, Any],
    *,
    session_key: str,
    collection_key: str,
    result_id: str,
) -> bool:
    for owner_key, item in payload["sessions"].items():
        if owner_key == session_key or not isinstance(item, dict):
            continue
        runtime = item.get("runtime")
        if not isinstance(runtime, dict):
            continue
        collection = runtime.get(collection_key)
        if isinstance(collection, dict) and isinstance(
            collection.get(result_id),
            dict,
        ):
            return True
    return False


class SessionCheckpointStore:
    """Small atomic JSON store keyed by the privacy-safe logical session key."""

    def __init__(self, *, path: Path, ttl_seconds: float) -> None:
        self.path = path
        self.lock_path = path.with_name(f"{path.name}.lock")
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            with interprocess_file_lock(self.lock_path):
                yield

    def _load_locked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "sessions": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {"version": 1, "sessions": {}}
        if not isinstance(payload, dict):
            return {"version": 1, "sessions": {}}
        sessions = payload.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        return {"version": 1, "sessions": sessions}

    def _write_locked(self, payload: dict[str, Any]) -> None:
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        atomic_write_bytes(self.path, encoded, mode=0o600)

    def _prune_locked(self, payload: dict[str, Any], now: float) -> int:
        sessions = payload["sessions"]
        expired = [
            key
            for key, item in sessions.items()
            if not isinstance(item, dict)
            or float(item.get("expires_at", 0.0) or 0.0) <= now
        ]
        for key in expired:
            sessions.pop(key, None)
        return len(expired)

    def put(
        self,
        *,
        session_key: str,
        checkpoint: dict[str, Any],
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        with self._transaction():
            payload = self._load_locked()
            self._prune_locked(payload, timestamp)
            item = deepcopy(checkpoint)
            existing = payload["sessions"].get(session_key)
            if (
                isinstance(existing, dict)
                and "runtime" not in item
                and isinstance(existing.get("runtime"), dict)
            ):
                item["runtime"] = deepcopy(existing["runtime"])
            item.update(
                {
                    "updated_at": timestamp,
                    "updated_at_iso": _iso(timestamp),
                    "expires_at": timestamp + self.ttl_seconds,
                    "expires_at_iso": _iso(timestamp + self.ttl_seconds),
                }
            )
            payload["sessions"][session_key] = item
            self._write_locked(payload)
        return deepcopy(item)

    def record_runtime(
        self,
        *,
        session_key: str,
        last_tool: str | None,
        cwd: str | None = None,
        jobs: dict[str, dict[str, Any]] | None = None,
        delegates: dict[str, dict[str, Any]] | None = None,
        next_action: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        with self._transaction():
            payload = self._load_locked()
            self._prune_locked(payload, timestamp)
            current = payload["sessions"].get(session_key)
            item = deepcopy(current) if isinstance(current, dict) else {}
            runtime = item.get("runtime")
            if not isinstance(runtime, dict):
                runtime = {}

            if last_tool:
                runtime["last_tool"] = last_tool
            if cwd:
                runtime["cwd"] = cwd
            if next_action:
                runtime["next_action"] = next_action
            runtime["updated_at"] = timestamp
            runtime["updated_at_iso"] = _iso(timestamp)

            runtime_jobs = runtime.get("jobs")
            if not isinstance(runtime_jobs, dict):
                runtime_jobs = {}
            runtime_delegates = runtime.get("delegates")
            if not isinstance(runtime_delegates, dict):
                runtime_delegates = {}
            job_order = runtime.get("job_order")
            if not isinstance(job_order, list):
                job_order = [str(job_id) for job_id in runtime_jobs]
            job_order = [
                str(job_id)
                for job_id in job_order
                if str(job_id) in runtime_jobs
            ]
            for job_id, job_state in (jobs or {}).items():
                normalized_job_id = str(job_id)
                previous_job_state = runtime_jobs.get(normalized_job_id)
                if (
                    not isinstance(previous_job_state, dict)
                    and _result_owned_elsewhere(
                        payload,
                        session_key=session_key,
                        collection_key="jobs",
                        result_id=normalized_job_id,
                    )
                ):
                    raise ValueError(
                        "durable job is already owned by another logical session"
                    )
                if (
                    not isinstance(previous_job_state, dict)
                    and _owned_reference_count(
                        runtime_jobs,
                        runtime_delegates,
                    )
                    >= MAX_OWNED_RUNTIME_REFERENCES
                ):
                    raise ValueError(
                        "session total ownership capacity exceeded; close the logical "
                        "session before starting more background work"
                    )
                next_job_state = _owned_state(
                    previous_job_state,
                    job_state,
                    timestamp=timestamp,
                )
                previous_unconsumed = (
                    isinstance(previous_job_state, dict)
                    and previous_job_state.get("continuation_state")
                    != RESULT_CONSUMED
                )
                next_unconsumed = (
                    next_job_state.get("continuation_state") != RESULT_CONSUMED
                )
                if (
                    next_unconsumed
                    and not previous_unconsumed
                    and _unconsumed_reference_count(
                        runtime_jobs,
                        runtime_delegates,
                    )
                    >= MAX_UNCONSUMED_RUNTIME_REFERENCES
                ):
                    raise ValueError(
                        "session ownership capacity exceeded; consume or close existing "
                        "owned results before starting more background work"
                    )
                runtime_jobs[normalized_job_id] = next_job_state
                job_order = [
                    existing
                    for existing in job_order
                    if existing != normalized_job_id
                ]
                job_order.append(normalized_job_id)
            job_order = _prune_consumed_history(runtime_jobs, job_order)
            runtime["jobs"] = runtime_jobs
            runtime["job_order"] = job_order

            delegate_order = runtime.get("delegate_order")
            if not isinstance(delegate_order, list):
                delegate_order = [
                    str(delegate_id) for delegate_id in runtime_delegates
                ]
            delegate_order = [
                str(delegate_id)
                for delegate_id in delegate_order
                if str(delegate_id) in runtime_delegates
            ]
            for delegate_id, delegate_state in (delegates or {}).items():
                normalized_delegate_id = str(delegate_id)
                previous_delegate_state = runtime_delegates.get(
                    normalized_delegate_id
                )
                if (
                    not isinstance(previous_delegate_state, dict)
                    and _result_owned_elsewhere(
                        payload,
                        session_key=session_key,
                        collection_key="delegates",
                        result_id=normalized_delegate_id,
                    )
                ):
                    raise ValueError(
                        "delegate is already owned by another logical session"
                    )
                if (
                    not isinstance(previous_delegate_state, dict)
                    and _owned_reference_count(
                        runtime_jobs,
                        runtime_delegates,
                    )
                    >= MAX_OWNED_RUNTIME_REFERENCES
                ):
                    raise ValueError(
                        "session total ownership capacity exceeded; close the logical "
                        "session before starting more background work"
                    )
                next_delegate_state = _owned_state(
                    previous_delegate_state,
                    delegate_state,
                    timestamp=timestamp,
                )
                previous_unconsumed = (
                    isinstance(previous_delegate_state, dict)
                    and previous_delegate_state.get("continuation_state")
                    != RESULT_CONSUMED
                )
                next_unconsumed = (
                    next_delegate_state.get("continuation_state")
                    != RESULT_CONSUMED
                )
                if (
                    next_unconsumed
                    and not previous_unconsumed
                    and _unconsumed_reference_count(
                        runtime_jobs,
                        runtime_delegates,
                    )
                    >= MAX_UNCONSUMED_RUNTIME_REFERENCES
                ):
                    raise ValueError(
                        "session ownership capacity exceeded; consume or close existing "
                        "owned results before starting more background work"
                    )
                runtime_delegates[normalized_delegate_id] = next_delegate_state
                delegate_order = [
                    existing
                    for existing in delegate_order
                    if existing != normalized_delegate_id
                ]
                delegate_order.append(normalized_delegate_id)
            delegate_order = _prune_consumed_history(
                runtime_delegates,
                delegate_order,
            )
            runtime["delegates"] = runtime_delegates
            runtime["delegate_order"] = delegate_order

            item["runtime"] = runtime
            item["updated_at"] = timestamp
            item["updated_at_iso"] = _iso(timestamp)
            item["expires_at"] = timestamp + self.ttl_seconds
            item["expires_at_iso"] = _iso(timestamp + self.ttl_seconds)
            payload["sessions"][session_key] = item
            self._write_locked(payload)
            return deepcopy(item)

    def pending_results(
        self,
        session_key: str,
        *,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        checkpoint = self.get(session_key, now=now)
        if checkpoint is None:
            return []
        runtime = checkpoint.get("runtime")
        if not isinstance(runtime, dict):
            return []

        pending: list[dict[str, Any]] = []
        for kind, collection_key, order_key in (
            ("job", "jobs", "job_order"),
            ("delegate", "delegates", "delegate_order"),
        ):
            collection = runtime.get(collection_key)
            if not isinstance(collection, dict):
                continue
            order = runtime.get(order_key)
            if not isinstance(order, list):
                order = list(collection)
            ordered_ids = [
                str(item)
                for item in order
                if str(item) in collection
            ]
            ordered_ids.extend(
                str(item)
                for item in collection
                if str(item) not in ordered_ids
            )
            for result_id in ordered_ids:
                state = collection.get(result_id)
                if not isinstance(state, dict):
                    continue
                if state.get("continuation_state") != RESULT_REQUIRES_CONSUMPTION:
                    continue
                pending.append(
                    {
                        "kind": kind,
                        "id": result_id,
                        **deepcopy(state),
                    }
                )
        return pending

    def ensure_claim_capacity(
        self,
        session_key: str,
        *,
        slots: int = 1,
        now: float | None = None,
    ) -> None:
        if isinstance(slots, bool) or slots < 1:
            raise ValueError("slots must be a positive integer")
        timestamp = time.time() if now is None else now
        with self._transaction():
            payload = self._load_locked()
            pruned = self._prune_locked(payload, timestamp)
            item = payload["sessions"].get(session_key)
            runtime = item.get("runtime") if isinstance(item, dict) else None
            if not isinstance(runtime, dict):
                runtime = {}
            jobs = runtime.get("jobs")
            delegates = runtime.get("delegates")
            owned_count = _owned_reference_count(jobs, delegates)
            unconsumed_count = _unconsumed_reference_count(jobs, delegates)
            if owned_count + slots > MAX_OWNED_RUNTIME_REFERENCES:
                raise ValueError(
                    "session total ownership capacity exceeded; close the logical "
                    "session before starting more background work"
                )
            if unconsumed_count + slots > MAX_UNCONSUMED_RUNTIME_REFERENCES:
                raise ValueError(
                    "session ownership capacity exceeded; consume or close existing "
                    "owned results before starting more background work"
                )
            if pruned:
                self._write_locked(payload)

    def owned_result(
        self,
        session_key: str,
        *,
        kind: str,
        result_id: str,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        checkpoint = self.get(session_key, now=now)
        if checkpoint is None:
            return None
        runtime = checkpoint.get("runtime")
        if not isinstance(runtime, dict):
            return None
        collection_key = {
            "job": "jobs",
            "delegate": "delegates",
        }.get(kind)
        if collection_key is None:
            raise ValueError(f"Unsupported result kind: {kind}")
        collection = runtime.get(collection_key)
        if not isinstance(collection, dict):
            return None
        state = collection.get(result_id)
        return deepcopy(state) if isinstance(state, dict) else None

    def result_ownership_scope(
        self,
        session_key: str,
        *,
        kind: str,
        result_id: str,
        now: float | None = None,
    ) -> str:
        collection_key = {
            "job": "jobs",
            "delegate": "delegates",
        }.get(kind)
        if collection_key is None:
            raise ValueError(f"Unsupported result kind: {kind}")
        timestamp = time.time() if now is None else now
        with self._transaction():
            payload = self._load_locked()
            pruned = self._prune_locked(payload, timestamp)
            owned_here = False
            owned_elsewhere = False
            for owner_key, item in payload["sessions"].items():
                if not isinstance(item, dict):
                    continue
                runtime = item.get("runtime")
                if not isinstance(runtime, dict):
                    continue
                collection = runtime.get(collection_key)
                if not isinstance(collection, dict):
                    continue
                state = collection.get(result_id)
                if not isinstance(state, dict):
                    continue
                if owner_key == session_key:
                    owned_here = True
                else:
                    owned_elsewhere = True
            if pruned:
                self._write_locked(payload)
        if owned_here and owned_elsewhere:
            return "conflict"
        if owned_here:
            return "owned_here"
        if owned_elsewhere:
            return "owned_elsewhere"
        return "unowned"

    def foreign_owned_result_ids(
        self,
        session_key: str,
        *,
        kind: str,
        now: float | None = None,
    ) -> set[str]:
        collection_key = {
            "job": "jobs",
            "delegate": "delegates",
        }.get(kind)
        if collection_key is None:
            raise ValueError(f"Unsupported result kind: {kind}")
        timestamp = time.time() if now is None else now
        with self._transaction():
            payload = self._load_locked()
            pruned = self._prune_locked(payload, timestamp)
            foreign: set[str] = set()
            for owner_key, item in payload["sessions"].items():
                if owner_key == session_key or not isinstance(item, dict):
                    continue
                runtime = item.get("runtime")
                if not isinstance(runtime, dict):
                    continue
                collection = runtime.get(collection_key)
                if not isinstance(collection, dict):
                    continue
                foreign.update(
                    str(result_id)
                    for result_id, state in collection.items()
                    if isinstance(state, dict)
                )
            if pruned:
                self._write_locked(payload)
            return foreign

    def delegate_group_ownership_scope(
        self,
        session_key: str,
        *,
        group_id: str,
        now: float | None = None,
    ) -> str:
        timestamp = time.time() if now is None else now
        with self._transaction():
            payload = self._load_locked()
            pruned = self._prune_locked(payload, timestamp)
            owned_here = False
            owned_elsewhere = False
            for owner_key, item in payload["sessions"].items():
                if not isinstance(item, dict):
                    continue
                runtime = item.get("runtime")
                if not isinstance(runtime, dict):
                    continue
                collection = runtime.get("delegates")
                if not isinstance(collection, dict):
                    continue
                if not any(
                    isinstance(state, dict) and state.get("group_id") == group_id
                    for state in collection.values()
                ):
                    continue
                if owner_key == session_key:
                    owned_here = True
                else:
                    owned_elsewhere = True
            if pruned:
                self._write_locked(payload)
        if owned_here and owned_elsewhere:
            return "conflict"
        if owned_here:
            return "owned_here"
        if owned_elsewhere:
            return "owned_elsewhere"
        return "unowned"

    def mark_result_consumed(
        self,
        session_key: str,
        *,
        kind: str,
        result_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        collection_key = {
            "job": "jobs",
            "delegate": "delegates",
        }.get(kind)
        if collection_key is None:
            raise ValueError(f"Unsupported result kind: {kind}")
        timestamp = time.time() if now is None else now
        with self._transaction():
            payload = self._load_locked()
            self._prune_locked(payload, timestamp)
            item = payload["sessions"].get(session_key)
            if not isinstance(item, dict):
                return {"found": False, "consumed": False}
            runtime = item.get("runtime")
            if not isinstance(runtime, dict):
                return {"found": False, "consumed": False}
            collection = runtime.get(collection_key)
            if not isinstance(collection, dict):
                return {"found": False, "consumed": False}
            state = collection.get(result_id)
            if not isinstance(state, dict):
                return {"found": False, "consumed": False}
            if not bool(state.get("terminal")):
                return {
                    "found": True,
                    "consumed": False,
                    "reason": "result_not_terminal",
                    "state": deepcopy(state),
                }
            consumed_at = state.get("result_consumed_at")
            already_consumed = isinstance(consumed_at, (int, float))
            if not already_consumed:
                consumed_at = timestamp
                state["result_consumed_at"] = timestamp
                state["result_consumed_at_iso"] = _iso(timestamp)
                state["continuation_state"] = RESULT_CONSUMED
                order_key = "job_order" if kind == "job" else "delegate_order"
                order = runtime.get(order_key)
                if not isinstance(order, list):
                    order = [str(item) for item in collection]
                runtime[order_key] = _prune_consumed_history(
                    collection,
                    [
                        str(item)
                        for item in order
                        if str(item) in collection
                    ],
                )
                item["updated_at"] = timestamp
                item["updated_at_iso"] = _iso(timestamp)
                item["expires_at"] = timestamp + self.ttl_seconds
                item["expires_at_iso"] = _iso(timestamp + self.ttl_seconds)
                self._write_locked(payload)
            return {
                "found": True,
                "consumed": True,
                "already_consumed": already_consumed,
                "state": deepcopy(state),
            }

    def get(
        self,
        session_key: str,
        *,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        timestamp = time.time() if now is None else now
        with self._transaction():
            payload = self._load_locked()
            pruned = self._prune_locked(payload, timestamp)
            item = payload["sessions"].get(session_key)
            if pruned:
                self._write_locked(payload)
            return deepcopy(item) if isinstance(item, dict) else None

    def pop(self, session_key: str) -> dict[str, Any] | None:
        with self._transaction():
            payload = self._load_locked()
            removed = payload["sessions"].pop(session_key, None)
            if isinstance(removed, dict):
                self._write_locked(payload)
                return deepcopy(removed)
            return None

    def close(self, session_key: str) -> bool:
        return self.pop(session_key) is not None

    def count(self, *, now: float | None = None) -> int:
        timestamp = time.time() if now is None else now
        with self._transaction():
            payload = self._load_locked()
            pruned = self._prune_locked(payload, timestamp)
            if pruned:
                self._write_locked(payload)
            return len(payload["sessions"])
