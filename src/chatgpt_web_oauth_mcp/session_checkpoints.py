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


MAX_RUNTIME_REFERENCES = 8


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


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
        last_tool: str,
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
                runtime_jobs[normalized_job_id] = deepcopy(job_state)
                job_order = [
                    existing
                    for existing in job_order
                    if existing != normalized_job_id
                ]
                job_order.append(normalized_job_id)
            while len(job_order) > MAX_RUNTIME_REFERENCES:
                expired_job_id = job_order.pop(0)
                runtime_jobs.pop(expired_job_id, None)
            runtime["jobs"] = runtime_jobs
            runtime["job_order"] = job_order

            runtime_delegates = runtime.get("delegates")
            if not isinstance(runtime_delegates, dict):
                runtime_delegates = {}
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
                runtime_delegates[normalized_delegate_id] = deepcopy(delegate_state)
                delegate_order = [
                    existing
                    for existing in delegate_order
                    if existing != normalized_delegate_id
                ]
                delegate_order.append(normalized_delegate_id)
            while len(delegate_order) > MAX_RUNTIME_REFERENCES:
                expired_delegate_id = delegate_order.pop(0)
                runtime_delegates.pop(expired_delegate_id, None)
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

    def close(self, session_key: str) -> bool:
        with self._transaction():
            payload = self._load_locked()
            existed = session_key in payload["sessions"]
            payload["sessions"].pop(session_key, None)
            if existed:
                self._write_locked(payload)
            return existed

    def count(self, *, now: float | None = None) -> int:
        timestamp = time.time() if now is None else now
        with self._transaction():
            payload = self._load_locked()
            pruned = self._prune_locked(payload, timestamp)
            if pruned:
                self._write_locked(payload)
            return len(payload["sessions"])
