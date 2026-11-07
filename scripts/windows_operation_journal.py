"""Gateway-owned Windows execution journal.

Durable SQLite-backed operation journal providing windows_operation_submit
and windows_operation_status endpoints for the Opss coordinator.

Single gateway instance assumption:
Only one live gateway process may own an operation state directory at a time.
Enforced via a process-lifetime exclusive file lock (`gateway.lock`). Live first
process holds this lock; any duplicate second process is refused with RuntimeError.
Command arguments and credentials are never logged; private state resides
under the local user profile and never on network shares (NAS).
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from typing import Any

from fastmcp import Client
from mcp.types import CallToolResult


PROTOCOL_VERSION = 1
MAX_RESULT_BYTES = 8 * 1024 * 1024  # 8 MiB limit on serialized CallToolResult
OPERATION_ID_REGEX = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def compute_request_digest(name: str, arguments: dict | None) -> str:
    """Compute deterministic SHA256 digest over canonical JSON representation."""
    # Notice: keys must be sorted and compact (no whitespace after separators)
    serialized = json.dumps(
        {"name": name, "arguments": arguments if arguments is not None else {}},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def parse_legacy_timeout_envelope(text: str) -> bool:
    """Check if text matches the exact whole upstream PowerShell timeout envelope.

    Matches whole envelope starting with 'Response: Command execution timed out'
    and ending with 'Status Code: 1'. Arbitrary substrings in legit output
    do NOT match.
    """
    if not text or not isinstance(text, str):
        return False
    stripped = text.strip()
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    if len(lines) != 2:
        return False
    return (
        lines[0] == "Response: Command execution timed out"
        and lines[-1] == "Status Code: 1"
    )


def validate_operation_id(operation_id: str) -> None:
    """Validate operation ID is URL-safe and between 32 and 128 characters."""
    if not isinstance(operation_id, str) or not OPERATION_ID_REGEX.fullmatch(operation_id):
        raise ValueError(
            f"Invalid operation_id: must be urlsafe characters [A-Za-z0-9_-], "
            "length 32..128"
        )


def validate_target_name(name: str) -> str:
    """Validate operation name and return stripped target name.

    Only accepts names starting with 'windows_' that do not recursively
    invoke Revit or operation_* tools.
    """
    if not isinstance(name, str) or not name.startswith("windows_"):
        raise ValueError(f"Operation name must start with 'windows_', got {name!r}")
    target_name = name[len("windows_"):]
    if not target_name:
        raise ValueError("Operation name cannot be empty after stripping 'windows_'")
    if target_name.startswith("operation_"):
        raise ValueError("Recursive operation_* calls are forbidden")
    if "revit" in target_name.lower():
        raise ValueError("Revit tools cannot be invoked via the Windows operation journal")
    return target_name


def validate_powershell_arguments(target_name: str, arguments: dict) -> None:
    """Validate PowerShell timeout/budget parameters are between 1 and 20 seconds."""
    if target_name == "PowerShell":
        value = arguments.get("timeout", 20)
        if type(value) is not int or not 1 <= value <= 20:
            raise ValueError("PowerShell timeout must be an integer between 1 and 20")


def make_envelope(
    operation_id: str,
    request_digest: str,
    state: str,
    result: dict | None = None,
    error_code: str | None = None,
    execution_status: str | None = None,
) -> dict:
    """Construct standard journal response envelope."""
    envelope: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "operation_id": operation_id,
        "request_digest": request_digest,
        "state": state,
        "result": result,
        "execution_status": execution_status or (
            "finished" if state == "completed" else "running" if state == "running" else "unconfirmed"),
    }
    if error_code is not None:
        envelope["error_code"] = error_code
    return envelope


class OperationJournal:
    """Durable SQLite-backed execution journal for Windows operations."""

    def __init__(self, state_dir: Path | str, windows_target: Any = None) -> None:
        if Path(state_dir).is_symlink():
            raise ValueError("Operation state dir cannot be a symlink")
        self.state_dir = Path(state_dir).resolve()
        if str(self.state_dir).startswith(("//", "\\\\")):
            raise ValueError("Operation state dir cannot be a network share (NAS)")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.windows_target = windows_target

        self.db_path = self.state_dir / "operations.db"
        self.lock_path = self.state_dir / "gateway.lock"

        self._lock_file: Any = None
        self._db_lock = threading.Lock()
        self._background_tasks: set[asyncio.Task] = set()
        self._operation_failures: dict[str, dict] = {}
        self._journal_failure: str | None = None
        self._available_tools: set[str] | None = None

        self._acquire_file_lock()
        self._init_database()

    def _acquire_file_lock(self) -> None:
        """Acquire process-lifetime exclusive file lock or refuse duplicate service."""
        if self.lock_path.is_symlink() or self.db_path.is_symlink():
            raise ValueError("Operation state files cannot be symlinks")
        self._lock_file = open(self.lock_path, "a+b")
        if os.fstat(self._lock_file.fileno()).st_size == 0:
            self._lock_file.write(b"\0")
            self._lock_file.flush()
        if sys.platform == "win32":
            import msvcrt
            try:
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except (BlockingIOError, OSError) as exc:
                self._lock_file.close()
                self._lock_file = None
                raise RuntimeError(
                    f"Duplicate gateway service refused: another process holds lock on {self.state_dir}"
                ) from exc
        else:
            import fcntl
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                self._lock_file.close()
                self._lock_file = None
                raise RuntimeError(
                    f"Duplicate gateway service refused: another process holds lock on {self.state_dir}"
                ) from exc

    def _release_file_lock(self) -> None:
        """Release process-lifetime file lock."""
        if self._lock_file is not None and not self._lock_file.closed:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._lock_file.seek(0)
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                self._lock_file.close()
            except Exception:
                pass
            self._lock_file = None

    @contextmanager
    def _get_connection(self):
        """Return a configured SQLite connection."""
        conn = sqlite3.connect(str(self.db_path), timeout=2.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA busy_timeout = 2000;")
            conn.execute("PRAGMA synchronous = FULL;")
            yield conn
        finally:
            conn.close()

    def _init_database(self) -> None:
        """Initialize database schema and perform startup recovery."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    response_json TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(operations)")}
            if "execution_status" not in columns:
                conn.execute("ALTER TABLE operations ADD COLUMN execution_status TEXT NOT NULL DEFAULT 'unconfirmed'")
                conn.execute("UPDATE operations SET execution_status = 'finished' WHERE state = 'completed'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_operations_state ON operations(state)")
            # Startup recovery: any 'running' rows become 'unknown', no replay.
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE operations
                SET state = 'unknown', execution_status = 'unconfirmed', error_code = 'GATEWAY_RESTARTED', updated_at = ?
                WHERE state = 'running'
                """,
                (now,),
            )
            conn.commit()

    async def get_available_windows_tools(self) -> set[str]:
        """Fetch and cache available tools from the Windows upstream target."""
        if self._available_tools is None:
            if self.windows_target is None:
                self._available_tools = set()
            else:
                async with asyncio.timeout(4), Client(self.windows_target, timeout=3, init_timeout=2) as client:
                    tools = await client.list_tools()
                    self._available_tools = {tool.name for tool in tools}
        return self._available_tools

    def _record_worker_failure(self, operation_id: str, digest: str, code: str) -> None:
        # A failed persistence boundary cannot report success. Keep a process-local
        # uncertainty receipt even when storage is unreadable. Admission is latched
        # closed, so this map can grow only with workers already admitted.
        self._journal_failure = self._journal_failure or code
        self._operation_failures.setdefault(operation_id, make_envelope(
            operation_id, digest, "unknown", error_code=code))

    def _worker_done(self, task: asyncio.Task, operation_id: str, digest: str) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            self._record_worker_failure(operation_id, digest, "TASK_CANCELLED")
        elif task.exception() is not None:
            # Retrieve every exception so an unexpected worker failure is observed
            # and diagnosed, rather than becoming an unhandled task warning.
            self._record_worker_failure(operation_id, digest, "JOURNAL_WORKER_FAILED")

    def _require_healthy_journal(self) -> None:
        if self._journal_failure:
            raise RuntimeError("JOURNAL_UNHEALTHY: inspect operation/storage before restarting gateway; do not replay unknown operations")

    async def submit(
        self,
        operation_id: str,
        name: str,
        arguments: dict | None = None,
    ) -> dict:
        """Submit an operation for durable asynchronous Windows execution."""
        validate_operation_id(operation_id)
        target_name = validate_target_name(name)
        args = dict(arguments) if arguments is not None else {}
        validate_powershell_arguments(target_name, args)
        request_digest = compute_request_digest(name, args)

        failed = self._operation_failures.get(operation_id)
        if failed is not None:
            if failed["request_digest"] != request_digest:
                raise ValueError("Operation ID already exists with different request payload")
            return dict(failed)
        self._require_healthy_journal()

        now = datetime.now(timezone.utc).isoformat()
        should_launch = False

        with self._db_lock:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT operation_id, request_digest, state, response_json, error_code, execution_status FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()

                if row is not None:
                    # Existing operation
                    if row["request_digest"] != request_digest:
                        raise ValueError(
                            f"Operation ID {operation_id} already exists with different request payload"
                        )
                    res_dict = (
                        json.loads(row["response_json"])
                        if row["state"] == "completed" and row["response_json"]
                        else None
                    )
                    return make_envelope(
                        operation_id=operation_id,
                        request_digest=row["request_digest"],
                        state=row["state"],
                        result=res_dict,
                        error_code=row["error_code"],
                        execution_status=row["execution_status"],
                    )

                # Admission may have become unhealthy during upstream discovery.
                self._require_healthy_journal()
                # Atomically insert unique row
                try:
                    conn.execute(
                        """
                        INSERT INTO operations (operation_id, request_digest, state, response_json, error_code, created_at, updated_at, execution_status)
                        VALUES (?, ?, 'running', NULL, NULL, ?, ?, 'running')
                        """,
                        (operation_id, request_digest, now, now),
                    )
                    conn.commit()
                    should_launch = True
                except sqlite3.IntegrityError:
                    # Handle concurrent race
                    row = conn.execute(
                        "SELECT operation_id, request_digest, state, response_json, error_code, execution_status FROM operations WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                    if row is not None:
                        if row["request_digest"] != request_digest:
                            raise ValueError(
                                f"Operation ID {operation_id} already exists with different request payload"
                            )
                        res_dict = (
                            json.loads(row["response_json"])
                            if row["state"] == "completed" and row["response_json"]
                            else None
                        )
                        return make_envelope(
                            operation_id=operation_id,
                            request_digest=row["request_digest"],
                            state=row["state"],
                            result=res_dict,
                            error_code=row["error_code"],
                            execution_status=row["execution_status"],
                        )
                    raise

        if should_launch:
            # Retain background task so caller cancellation does not abort execution
            task = asyncio.create_task(
                self._execute_operation(operation_id, target_name, args, request_digest)
            )
            self._background_tasks.add(task)
            task.add_done_callback(lambda done: self._worker_done(done, operation_id, request_digest))

        return make_envelope(
            operation_id=operation_id,
            request_digest=request_digest,
            state="running",
            result=None,
        )

    async def _execute_operation(
        self,
        operation_id: str,
        target_name: str,
        arguments: dict,
        request_digest: str,
    ) -> None:
        """Execute upstream tool and persist terminal result before reporting completion."""
        state = "unknown"
        error_code = None
        response_json = None
        execution_status = "unconfirmed"
        dispatched = False

        try:
            available = await self.get_available_windows_tools()
            if target_name not in available:
                raise ValueError("Windows tool unavailable before dispatch")
            effective_args = dict(arguments)
            if target_name == "PowerShell":
                effective_args.setdefault("timeout", 20)
            async with asyncio.timeout(70), Client(self.windows_target, timeout=60, init_timeout=5) as client:
                dispatched = True
                call_res = await client.call_tool(target_name, effective_args, timeout=60, raise_on_error=False)
                # A real MCP tool reply proves the handler returned, even if
                # its result/outcome is an error. A transport exception does not.
                execution_status = "finished"

            text_content = ""
            if hasattr(call_res, "content"):
                for item in call_res.content:
                    if hasattr(item, "text") and item.text:
                        text_content += item.text + "\n"

            is_timeout = target_name == "PowerShell" and parse_legacy_timeout_envelope(text_content)
            is_error = getattr(call_res, "is_error", False)

            if is_error or is_timeout:
                # Engineering outcome is unknown; request execution has ended.
                state = "unknown"
                error_code = "TIMEOUT" if is_timeout else "UPSTREAM_ERROR"
            else:
                if isinstance(call_res, CallToolResult):
                    mcp_result = call_res
                else:
                    mcp_result = CallToolResult(
                        content=list(getattr(call_res, "content", [])),
                        isError=False,
                        structuredContent=getattr(call_res, "structured_content", None),
                        _meta=getattr(call_res, "meta", None),
                    )
                if hasattr(mcp_result, "model_dump"):
                    result_dict = mcp_result.model_dump(by_alias=True, mode="json")
                else:
                    result_dict = mcp_result.dict(by_alias=True)

                serialized = json.dumps(result_dict, ensure_ascii=False)
                if len(serialized.encode("utf-8")) > MAX_RESULT_BYTES:
                    state = "unknown"
                    error_code = "RESULT_OVERFLOW"
                else:
                    state = "completed"
                    response_json = serialized
                    error_code = None

        except asyncio.CancelledError:
            state = "unknown"
            error_code = "TASK_CANCELLED"
            raise
        except Exception as exc:
            state = "unknown"
            error_code = f"EXCEPTION_{type(exc).__name__}" if dispatched else "NOT_DISPATCHED"
            if not dispatched:
                execution_status = "finished"
        finally:
            # Persist terminal result before reporting completion. A persistence
            # failure is an explicit service fault, not a still-running action.
            now = datetime.now(timezone.utc).isoformat()
            try:
                with self._db_lock:
                    with self._get_connection() as conn:
                        conn.execute(
                            """
                            UPDATE operations
                            SET state = ?, response_json = ?, error_code = ?, updated_at = ?, execution_status = ?
                            WHERE operation_id = ?
                            """,
                            (state, response_json, error_code, now, execution_status, operation_id),
                        )
                        conn.commit()
            except Exception:
                self._record_worker_failure(operation_id, request_digest, "JOURNAL_WRITE_FAILED")

    async def reconcile(self, operation_id: str, request_digest: str) -> dict:
        """Atomically reconcile admission; a missing ID becomes a no-dispatch tombstone.

        A delayed submit must observe this same identity and cannot execute after
        the coordinator releases its fence. Existing accepted work is untouched.
        """
        validate_operation_id(operation_id)
        if not isinstance(request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", request_digest):
            raise ValueError("Invalid request digest")
        failed = self._operation_failures.get(operation_id)
        if failed is not None:
            if failed["request_digest"] != request_digest:
                raise ValueError("Request digest mismatch")
            return dict(failed)
        now = datetime.now(timezone.utc).isoformat()
        with self._db_lock:
            with self._get_connection() as conn:
                row = conn.execute("SELECT request_digest FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
                if row is not None:
                    if row["request_digest"] != request_digest:
                        raise ValueError("Request digest mismatch")
                else:
                    conn.execute(
                        "INSERT INTO operations (operation_id, request_digest, state, response_json, error_code, created_at, updated_at, execution_status) "
                        "VALUES (?, ?, 'unknown', NULL, 'NOT_DISPATCHED', ?, ?, 'finished')",
                        (operation_id, request_digest, now, now))
                    conn.commit()
        return await self.status(operation_id)

    async def status(self, operation_id: str) -> dict:
        """Check status of an operation. Never launches work."""
        validate_operation_id(operation_id)
        failed = self._operation_failures.get(operation_id)
        if failed is not None:
            return dict(failed)
        with self._db_lock:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT operation_id, request_digest, state, response_json, error_code, execution_status FROM operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()

        if row is None:
            raise ValueError(f"Operation ID not found: {operation_id}")

        res_dict = None
        if row["state"] == "completed" and row["response_json"]:
            res_dict = json.loads(row["response_json"])

        return make_envelope(
            operation_id=row["operation_id"],
            request_digest=row["request_digest"],
            state=row["state"],
            result=res_dict,
            error_code=row["error_code"],
            execution_status=row["execution_status"],
        )

    def close(self) -> None:
        """Close journal resources and release file lock."""
        if any(not task.done() for task in self._background_tasks):
            raise RuntimeError("Cannot close journal while operations are running")
        self._release_file_lock()

    def __enter__(self) -> OperationJournal:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
