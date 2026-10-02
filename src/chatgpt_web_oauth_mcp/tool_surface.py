from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Iterator

import tiktoken
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult

from . import session
from .state_io import atomic_write_bytes, ensure_private_directory, interprocess_file_lock


SCHEMA_VERSION = 1
DEFAULT_USAGE_FILENAME = "tool-usage.json"
DEFAULT_ENCODING = "o200k_base"
DEFAULT_FLUSH_EVERY = 20
DEFAULT_FLUSH_INTERVAL_SECONDS = 5.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_tool_payload(tool: Any) -> dict[str, object]:
    mcp_tool = tool.to_mcp_tool()
    payload = mcp_tool.model_dump(by_alias=True, exclude_none=True)
    if not isinstance(payload, dict):
        raise TypeError("MCP tool schema must serialize to an object.")
    return payload


def tool_schema_footprint(
    tools: Iterable[Any],
    *,
    encoding_name: str = DEFAULT_ENCODING,
) -> dict[str, object]:
    """Measure the exact serialized MCP tool catalog without changing it."""

    encoding = tiktoken.get_encoding(encoding_name)
    rows: list[dict[str, object]] = []
    total_tokens = 0
    total_bytes = 0
    for tool in tools:
        payload = _canonical_tool_payload(tool)
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        token_count = len(encoding.encode(rendered))
        byte_count = len(rendered.encode("utf-8"))
        total_tokens += token_count
        total_bytes += byte_count
        rows.append(
            {
                "name": str(payload.get("name") or ""),
                "tokens": token_count,
                "bytes": byte_count,
            }
        )
    rows.sort(key=lambda item: (-int(item["tokens"]), str(item["name"])))
    return {
        "encoding": encoding_name,
        "tool_count": len(rows),
        "total_tokens": total_tokens,
        "total_bytes": total_bytes,
        "tools": rows,
    }


class ToolUsageStore:
    """Durable aggregate telemetry with no tool arguments, payloads, or session ids."""

    def __init__(
        self,
        path_provider: Callable[[], Path],
        *,
        flush_every: int = DEFAULT_FLUSH_EVERY,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
    ) -> None:
        self._path_provider = path_provider
        self._lock = threading.RLock()
        self._flush_every = max(1, int(flush_every))
        self._flush_interval_seconds = max(0.1, float(flush_interval_seconds))
        self._last_flush_monotonic = time.monotonic()
        self._pending_calls = 0
        self._pending_errors = 0
        self._pending_tools: dict[str, dict[str, object]] = {}
        self._pending_transitions: dict[str, int] = {}

    @property
    def path(self) -> Path:
        return Path(self._path_provider()).expanduser().resolve()

    @property
    def lock_path(self) -> Path:
        path = self.path
        return path.with_name(f".{path.name}.lock")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        path = self.path
        ensure_private_directory(path.parent)
        with self._lock:
            with interprocess_file_lock(self.lock_path):
                yield

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "total_calls": 0,
            "total_errors": 0,
            "tools": {},
            "transitions": {},
        }

    def _read_unlocked(self) -> dict[str, Any]:
        path = self.path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._empty()
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            return self._empty()
        tools = payload.get("tools")
        transitions = payload.get("transitions")
        if not isinstance(tools, dict) or not isinstance(transitions, dict):
            return self._empty()
        payload["total_calls"] = int(payload.get("total_calls") or 0)
        payload["total_errors"] = int(payload.get("total_errors") or 0)
        return payload

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        atomic_write_bytes(self.path, encoded, mode=0o600, sync_directory=True)

    def _merge_pending_unlocked(self, payload: dict[str, Any]) -> None:
        tools = payload["tools"]
        transitions = payload["transitions"]
        assert isinstance(tools, dict)
        assert isinstance(transitions, dict)
        for name, pending in self._pending_tools.items():
            row = tools.get(name)
            if not isinstance(row, dict):
                row = {
                    "calls": 0,
                    "errors": 0,
                    "total_latency_ms": 0.0,
                    "max_latency_ms": 0.0,
                    "last_used_at": None,
                }
                tools[name] = row
            row["calls"] = int(row.get("calls") or 0) + int(pending["calls"])
            row["errors"] = int(row.get("errors") or 0) + int(pending["errors"])
            row["total_latency_ms"] = round(
                float(row.get("total_latency_ms") or 0.0)
                + float(pending["total_latency_ms"]),
                3,
            )
            row["max_latency_ms"] = round(
                max(
                    float(row.get("max_latency_ms") or 0.0),
                    float(pending["max_latency_ms"]),
                ),
                3,
            )
            row["last_used_at"] = pending["last_used_at"]
        for key, count in self._pending_transitions.items():
            transitions[key] = int(transitions.get(key) or 0) + int(count)
        payload["total_calls"] = int(payload.get("total_calls") or 0) + self._pending_calls
        payload["total_errors"] = int(payload.get("total_errors") or 0) + self._pending_errors
        if self._pending_calls:
            payload["updated_at"] = _utc_now()

    def _clear_pending_locked(self) -> None:
        self._pending_calls = 0
        self._pending_errors = 0
        self._pending_tools.clear()
        self._pending_transitions.clear()
        self._last_flush_monotonic = time.monotonic()

    def _flush_locked(self) -> None:
        if self._pending_calls <= 0:
            self._last_flush_monotonic = time.monotonic()
            return
        path = self.path
        ensure_private_directory(path.parent)
        with interprocess_file_lock(self.lock_path):
            payload = self._read_unlocked()
            self._merge_pending_unlocked(payload)
            self._write_unlocked(payload)
        self._clear_pending_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def record(
        self,
        *,
        tool_name: str,
        success: bool,
        latency_ms: float,
        previous_tool: str | None = None,
    ) -> None:
        name = str(tool_name).strip()
        if not name:
            return
        with self._lock:
            row = self._pending_tools.get(name)
            if row is None:
                row = {
                    "calls": 0,
                    "errors": 0,
                    "total_latency_ms": 0.0,
                    "max_latency_ms": 0.0,
                    "last_used_at": None,
                }
                self._pending_tools[name] = row
            row["calls"] = int(row["calls"]) + 1
            if not success:
                row["errors"] = int(row["errors"]) + 1
            bounded_latency = max(0.0, float(latency_ms))
            row["total_latency_ms"] = round(
                float(row["total_latency_ms"]) + bounded_latency,
                3,
            )
            row["max_latency_ms"] = round(
                max(float(row["max_latency_ms"]), bounded_latency),
                3,
            )
            row["last_used_at"] = _utc_now()
            self._pending_calls += 1
            if not success:
                self._pending_errors += 1
            previous = str(previous_tool or "").strip()
            if previous and previous != name:
                key = f"{previous}->{name}"
                self._pending_transitions[key] = self._pending_transitions.get(key, 0) + 1
            if (
                self._pending_calls >= self._flush_every
                or time.monotonic() - self._last_flush_monotonic
                >= self._flush_interval_seconds
            ):
                self._flush_locked()

    def snapshot(self) -> dict[str, object]:
        self.flush()
        with self._transaction():
            payload = self._read_unlocked()
        tools = payload.get("tools")
        rows: list[dict[str, object]] = []
        if isinstance(tools, dict):
            for name, raw in tools.items():
                if not isinstance(raw, dict):
                    continue
                calls = max(0, int(raw.get("calls") or 0))
                errors = max(0, int(raw.get("errors") or 0))
                total_latency = max(0.0, float(raw.get("total_latency_ms") or 0.0))
                rows.append(
                    {
                        "name": str(name),
                        "calls": calls,
                        "errors": errors,
                        "error_rate": round(errors / calls, 4) if calls else 0.0,
                        "avg_latency_ms": round(total_latency / calls, 3) if calls else 0.0,
                        "max_latency_ms": round(
                            max(0.0, float(raw.get("max_latency_ms") or 0.0)),
                            3,
                        ),
                        "last_used_at": raw.get("last_used_at"),
                    }
                )
        rows.sort(key=lambda item: (-int(item["calls"]), str(item["name"])))
        transitions = payload.get("transitions")
        transition_rows: list[dict[str, object]] = []
        if isinstance(transitions, dict):
            for key, count in transitions.items():
                if not isinstance(key, str) or "->" not in key:
                    continue
                source, target = key.split("->", 1)
                transition_rows.append(
                    {
                        "from": source,
                        "to": target,
                        "count": max(0, int(count or 0)),
                    }
                )
        transition_rows.sort(
            key=lambda item: (
                -int(item["count"]),
                str(item["from"]),
                str(item["to"]),
            )
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "path": str(self.path),
            "updated_at": payload.get("updated_at"),
            "total_calls": max(0, int(payload.get("total_calls") or 0)),
            "total_errors": max(0, int(payload.get("total_errors") or 0)),
            "tools": rows,
            "transitions": transition_rows,
        }


def _tool_result_success(result: ToolResult) -> bool:
    if result.is_error:
        return False
    payload = result.structured_content
    if isinstance(payload, dict) and payload.get("success") is False:
        return False
    return True


class ToolUsageTelemetryMiddleware(Middleware):
    """Record aggregate tool selection/latency data without recording inputs or outputs."""

    def __init__(self, store: ToolUsageStore) -> None:
        self.store = store
        self._previous_by_session: dict[str, str] = {}
        self._lock = threading.RLock()

    def _previous(self, session_key: str | None, tool_name: str) -> str | None:
        if not session_key:
            return None
        with self._lock:
            previous = self._previous_by_session.get(session_key)
            self._previous_by_session[session_key] = tool_name
            return previous

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: Any,
    ) -> ToolResult:
        message = context.message
        tool_name = str(getattr(message, "name", "") or "")
        session_key = session.get_current_session_id()
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except BaseException:
            try:
                self.store.record(
                    tool_name=tool_name,
                    success=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    previous_tool=self._previous(session_key, tool_name),
                )
            except (OSError, TypeError, ValueError, RuntimeError):
                pass
            raise
        try:
            self.store.record(
                tool_name=tool_name,
                success=_tool_result_success(result),
                latency_ms=(time.perf_counter() - started) * 1000,
                previous_tool=self._previous(session_key, tool_name),
            )
        except (OSError, TypeError, ValueError, RuntimeError):
            pass
        return result
