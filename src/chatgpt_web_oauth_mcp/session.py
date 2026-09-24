"""Per-MCP-session state and health classification.

The HTTP transport binds the current MCP session id into a ContextVar.
Tool code can then use the compatibility helpers in this module without passing
session ids through every call. Direct/unit invocations fall back to one local
session key so existing non-HTTP workflows remain deterministic.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
from pathlib import Path
import threading
import time
from typing import Any

_LOCAL_SESSION_KEY = "__local__"
_current_session_id: ContextVar[str | None] = ContextVar(
    "chatgpt_web_oauth_mcp_session_id",
    default=None,
)


def _now_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def _public_session_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


def _fastmcp_session_id() -> str | None:
    try:
        from fastmcp.server.dependencies import get_context

        return get_context().session_id
    except (LookupError, RuntimeError):
        return None


@dataclass
class ActiveRequest:
    request_id: str
    rpc_method: str | None
    tool: str | None
    started_at: float
    expected_deadline_at: float


@dataclass
class SessionRecord:
    session_id: str
    created_at: float
    last_seen_at: float
    default_cwd: Path | None = None
    active_requests: dict[str, ActiveRequest] = field(default_factory=dict)
    last_tool: str | None = None
    last_tool_started_at: float | None = None
    last_tool_completed_at: float | None = None
    last_error: str | None = None
    pending_required_action: str | None = None
    pending_required_action_at: float | None = None
    last_execution_state: str | None = None


class SessionRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionRecord] = {}

    def _key(self, session_id: str | None = None) -> str:
        return (
            session_id
            or _fastmcp_session_id()
            or _current_session_id.get()
            or _LOCAL_SESSION_KEY
        )

    def _get_or_create_locked(self, key: str, now: float) -> SessionRecord:
        record = self._sessions.get(key)
        if record is None:
            record = SessionRecord(session_id=key, created_at=now, last_seen_at=now)
            self._sessions[key] = record
        return record

    def touch(self, session_id: str | None = None, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        key = self._key(session_id)
        with self._lock:
            record = self._get_or_create_locked(key, timestamp)
            record.last_seen_at = timestamp

    def get_default_cwd(self, session_id: str | None = None) -> Path | None:
        key = self._key(session_id)
        with self._lock:
            record = self._sessions.get(key)
            return record.default_cwd if record is not None else None

    def set_default_cwd(
        self,
        cwd: Path | None,
        session_id: str | None = None,
    ) -> Path | None:
        now = time.time()
        key = self._key(session_id)
        with self._lock:
            record = self._get_or_create_locked(key, now)
            record.default_cwd = cwd
            record.last_seen_at = now
            return record.default_cwd

    def begin_request(
        self,
        *,
        session_id: str,
        request_id: str,
        rpc_method: str | None,
        tool: str | None,
        expected_deadline_at: float,
        started_at: float | None = None,
    ) -> None:
        now = time.time() if started_at is None else started_at
        with self._lock:
            record = self._get_or_create_locked(session_id, now)
            record.last_seen_at = now
            record.pending_required_action = None
            record.pending_required_action_at = None
            record.active_requests[request_id] = ActiveRequest(
                request_id=request_id,
                rpc_method=rpc_method,
                tool=tool,
                started_at=now,
                expected_deadline_at=expected_deadline_at,
            )
            if tool:
                record.last_tool = tool
                record.last_tool_started_at = now

    def end_request(
        self,
        *,
        session_id: str,
        request_id: str,
        error: str | None = None,
        finished_at: float | None = None,
    ) -> None:
        now = time.time() if finished_at is None else finished_at
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return
            request = record.active_requests.pop(request_id, None)
            record.last_seen_at = now
            if request is not None and request.tool:
                record.last_tool_completed_at = now
            record.last_error = error

    def note_execution_state(
        self,
        *,
        required_action: str | None,
        state: str | None,
        session_id: str | None = None,
        now: float | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        key = self._key(session_id)
        with self._lock:
            record = self._get_or_create_locked(key, timestamp)
            record.last_seen_at = timestamp
            record.last_execution_state = state
            if required_action:
                record.pending_required_action = required_action
                record.pending_required_action_at = timestamp
            else:
                record.pending_required_action = None
                record.pending_required_action_at = None

    def close(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()

    def snapshot(
        self,
        *,
        idle_ttl_seconds: float,
        request_stall_seconds: float,
        orchestration_quiet_seconds: float,
        limit: int = 20,
        now: float | None = None,
    ) -> dict[str, Any]:
        timestamp = time.time() if now is None else now
        with self._lock:
            stale = [
                key
                for key, record in self._sessions.items()
                if key != _LOCAL_SESSION_KEY
                and not record.active_requests
                and timestamp - record.last_seen_at > idle_ttl_seconds
            ]
            for key in stale:
                self._sessions.pop(key, None)

            rows: list[dict[str, Any]] = []
            for key, record in self._sessions.items():
                if key == _LOCAL_SESSION_KEY:
                    continue
                active = list(record.active_requests.values())
                oldest = min(active, key=lambda item: item.started_at) if active else None
                overdue = [
                    item
                    for item in active
                    if timestamp
                    > max(
                        item.expected_deadline_at,
                        item.started_at + request_stall_seconds,
                    )
                ]
                if overdue:
                    state = "stalled_request"
                elif active:
                    state = "active"
                elif (
                    record.pending_required_action
                    and record.pending_required_action_at is not None
                    and timestamp - record.pending_required_action_at
                    >= orchestration_quiet_seconds
                ):
                    state = "orchestration_quiet"
                else:
                    state = "idle"

                cwd = record.default_cwd
                rows.append(
                    {
                        "id": _public_session_id(key),
                        "state": state,
                        "project": cwd.name if cwd is not None else None,
                        "cwd": str(cwd) if cwd is not None else None,
                        "created_at": _now_iso(record.created_at),
                        "last_seen_at": _now_iso(record.last_seen_at),
                        "last_seen_seconds_ago": round(timestamp - record.last_seen_at, 3),
                        "active_request_count": len(active),
                        "current_tool": oldest.tool if oldest is not None else None,
                        "current_rpc_method": oldest.rpc_method if oldest is not None else None,
                        "request_age_seconds": (
                            round(timestamp - oldest.started_at, 3)
                            if oldest is not None
                            else None
                        ),
                        "request_deadline_seconds_ago": (
                            round(timestamp - oldest.expected_deadline_at, 3)
                            if oldest is not None
                            else None
                        ),
                        "last_tool": record.last_tool,
                        "last_error": record.last_error,
                        "required_action": record.pending_required_action,
                        "required_action_age_seconds": (
                            round(timestamp - record.pending_required_action_at, 3)
                            if record.pending_required_action_at is not None
                            else None
                        ),
                        "last_execution_state": record.last_execution_state,
                    }
                )

        priority = {
            "stalled_request": 0,
            "orchestration_quiet": 1,
            "active": 2,
            "idle": 3,
        }
        rows.sort(
            key=lambda item: (
                priority.get(str(item["state"]), 9),
                float(item["last_seen_seconds_ago"]),
            )
        )
        total = len(rows)
        limited = rows[: max(1, limit)]
        counts = {
            state: sum(1 for item in rows if item["state"] == state)
            for state in ("idle", "active", "orchestration_quiet", "stalled_request")
        }
        return {
            "sessions": limited,
            "session_count": total,
            "truncated": total > len(limited),
            "counts": counts,
        }


registry = SessionRegistry()


def bind_session(session_id: str | None) -> Token[str | None]:
    return _current_session_id.set(session_id)


def reset_session_binding(token: Token[str | None]) -> None:
    _current_session_id.reset(token)


def get_current_session_id() -> str | None:
    return _fastmcp_session_id() or _current_session_id.get()


def get_default_cwd() -> Path | None:
    return registry.get_default_cwd()


def set_default_cwd(cwd: Path | None) -> Path | None:
    return registry.set_default_cwd(cwd)
