"""Durable semantic checkpoints for resumable logical ChatGPT sessions."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import os
from pathlib import Path
import threading
import time
from typing import Any


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


class SessionCheckpointStore:
    """Small atomic JSON store keyed by the privacy-safe logical session key."""

    def __init__(self, *, path: Path, ttl_seconds: float) -> None:
        self.path = path
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temp.chmod(0o600)
        os.replace(temp, self.path)
        self.path.chmod(0o600)

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
        item = deepcopy(checkpoint)
        item.update(
            {
                "updated_at": timestamp,
                "updated_at_iso": _iso(timestamp),
                "expires_at": timestamp + self.ttl_seconds,
                "expires_at_iso": _iso(timestamp + self.ttl_seconds),
            }
        )
        with self._lock:
            payload = self._load_locked()
            self._prune_locked(payload, timestamp)
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
        with self._lock:
            payload = self._load_locked()
            pruned = self._prune_locked(payload, timestamp)
            item = payload["sessions"].get(session_key)
            if pruned:
                self._write_locked(payload)
            return deepcopy(item) if isinstance(item, dict) else None

    def close(self, session_key: str) -> bool:
        with self._lock:
            payload = self._load_locked()
            existed = session_key in payload["sessions"]
            payload["sessions"].pop(session_key, None)
            if existed:
                self._write_locked(payload)
            return existed

    def count(self, *, now: float | None = None) -> int:
        timestamp = time.time() if now is None else now
        with self._lock:
            payload = self._load_locked()
            pruned = self._prune_locked(payload, timestamp)
            if pruned:
                self._write_locked(payload)
            return len(payload["sessions"])
