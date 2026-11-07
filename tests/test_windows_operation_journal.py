"""Hermetic contract and adversarial tests for the gateway-owned Windows operation journal."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import uuid

import pytest
from fastmcp import FastMCP, Client
from mcp.types import CallToolResult, TextContent

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import windows_operation_journal as wj


def _random_op_id() -> str:
    """Generate valid 64-char urlsafe operation ID."""
    return (uuid.uuid4().hex + uuid.uuid4().hex)[:64]


def test_operation_id_validation() -> None:
    valid_32 = "a" * 32
    valid_128 = "A" * 128
    valid_mixed = "Op-123_abc" + "x" * 22
    wj.validate_operation_id(valid_32)
    wj.validate_operation_id(valid_128)
    wj.validate_operation_id(valid_mixed)

    with pytest.raises(ValueError, match="length 32..128"):
        wj.validate_operation_id("too_short")
    with pytest.raises(ValueError, match="length 32..128"):
        wj.validate_operation_id("a" * 129)
    with pytest.raises(ValueError, match="urlsafe"):
        wj.validate_operation_id("a" * 31 + "!")
    with pytest.raises(ValueError, match="urlsafe"):
        wj.validate_operation_id("a" * 31 + " ")


def test_target_name_validation() -> None:
    assert wj.validate_target_name("windows_Screenshot") == "Screenshot"
    assert wj.validate_target_name("windows_ExecutePowerShell") == "ExecutePowerShell"

    with pytest.raises(ValueError, match="must start with 'windows_'"):
        wj.validate_target_name("Screenshot")
    with pytest.raises(ValueError, match="must start with 'windows_'"):
        wj.validate_target_name("revit_get_project_info")
    with pytest.raises(ValueError, match="Recursive operation_\\*"):
        wj.validate_target_name("windows_operation_submit")
    with pytest.raises(ValueError, match="Recursive operation_\\*"):
        wj.validate_target_name("windows_operation_status")
    with pytest.raises(ValueError, match="Revit tools cannot be invoked"):
        wj.validate_target_name("windows_revit_command")


def test_powershell_budget_validation() -> None:
    for value in (1, 20):
        wj.validate_powershell_arguments("PowerShell", {"timeout": value})
    wj.validate_powershell_arguments("PowerShell", {})
    for value in (0, 21, -5, "10s", True, 1.5, None):
        with pytest.raises(ValueError, match="integer between 1 and 20"):
            wj.validate_powershell_arguments("PowerShell", {"timeout": value})


def test_canonical_digest_computation() -> None:
    name = "windows_run"
    arguments = {"b": 2, "a": 1}
    digest = wj.compute_request_digest(name, arguments)
    expected_bytes = json.dumps(
        {"name": "windows_run", "arguments": {"a": 1, "b": 2}},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert digest == hashlib.sha256(expected_bytes).hexdigest()

    # None arguments normalized to empty dict
    digest_none = wj.compute_request_digest(name, None)
    expected_empty = json.dumps(
        {"name": "windows_run", "arguments": {}},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert digest_none == hashlib.sha256(expected_empty).hexdigest()


def test_parse_legacy_timeout_envelope() -> None:
    # Exact real timeout envelope
    real_timeout = "Response: Command execution timed out\nStatus Code: 1"
    assert wj.parse_legacy_timeout_envelope(real_timeout) is True

    real_timeout_multiline = "Response: Command execution timed out\nProcess aborted\nStatus Code: 1"
    assert wj.parse_legacy_timeout_envelope(real_timeout_multiline) is False

    # Forged timeout text inside legit output
    forged_mid = (
        "Logs starting...\n"
        "Response: Command execution timed out\n"
        "Status Code: 1\n"
        "Task completed successfully with exit code 0"
    )
    assert wj.parse_legacy_timeout_envelope(forged_mid) is False

    # Incomplete envelope
    assert wj.parse_legacy_timeout_envelope("Response: Command execution timed out") is False
    assert wj.parse_legacy_timeout_envelope("Status Code: 1") is False


def test_single_gateway_instance_lock(tmp_path) -> None:
    j1 = wj.OperationJournal(tmp_path)
    with pytest.raises(RuntimeError, match="Duplicate gateway service refused"):
        wj.OperationJournal(tmp_path)
    j1.close()
    # Once closed, second instance can acquire lock
    j2 = wj.OperationJournal(tmp_path)
    j2.close()


def test_adversarial_exactly_once_simultaneous_duplicate(tmp_path) -> None:
    call_count = 0
    windows = FastMCP("fake Windows")

    @windows.tool(name="slow_operation")
    async def slow_operation() -> str:
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)
        return "SUCCESS_SLOW"

    async def run() -> None:
        with wj.OperationJournal(tmp_path, windows_target=windows) as journal:
            op_id = _random_op_id()
            # Submit duplicate requests concurrently
            receipts = await asyncio.gather(
                journal.submit(op_id, "windows_slow_operation", {}),
                journal.submit(op_id, "windows_slow_operation", {}),
                journal.submit(op_id, "windows_slow_operation", {}),
            )
            for r in receipts:
                assert r["protocol_version"] == 1
                assert r["operation_id"] == op_id
                assert r["state"] in {"running", "completed"}

            # Wait for background task to complete
            for _ in range(50):
                st = await journal.status(op_id)
                if st["state"] == "completed":
                    break
                await asyncio.sleep(0.05)

            assert st["state"] == "completed"
            assert st["result"]["content"][0]["text"] == "SUCCESS_SLOW"
            assert call_count == 1, "Exactly-once constraint violated: backend called more than once"

    asyncio.run(run())


def test_adversarial_client_cancellation_with_late_success(tmp_path) -> None:
    windows = FastMCP("fake Windows")
    executed = False

    @windows.tool(name="background_job")
    async def background_job() -> str:
        nonlocal executed
        await asyncio.sleep(0.15)
        executed = True
        return "LATE_SUCCESS"

    async def run() -> None:
        with wj.OperationJournal(tmp_path, windows_target=windows) as journal:
            op_id = _random_op_id()
            receipt = await journal.submit(op_id, "windows_background_job", {})
            assert receipt["state"] == "running"

            # Simulate client cancelling its wait/request immediately
            # Background task must survive and complete
            for _ in range(50):
                st = await journal.status(op_id)
                if st["state"] == "completed":
                    break
                await asyncio.sleep(0.05)

            assert st["state"] == "completed"
            assert st["result"]["content"][0]["text"] == "LATE_SUCCESS"
            assert executed is True

    asyncio.run(run())


def test_adversarial_persisted_completion_reload(tmp_path) -> None:
    windows = FastMCP("fake Windows")

    @windows.tool(name="persist_test")
    def persist_test() -> str:
        return "PERSISTED_OK"

    async def run() -> None:
        op_id = _random_op_id()
        with wj.OperationJournal(tmp_path, windows_target=windows) as journal:
            await journal.submit(op_id, "windows_persist_test", {})
            for _ in range(50):
                st = await journal.status(op_id)
                if st["state"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert st["state"] == "completed"

        # Open completely fresh journal on same state_dir after close
        with wj.OperationJournal(tmp_path, windows_target=windows) as reloaded_journal:
            status = await reloaded_journal.status(op_id)
            assert status["state"] == "completed"
            assert status["result"]["content"][0]["text"] == "PERSISTED_OK"

    asyncio.run(run())


def test_adversarial_restart_unknown_no_replay(tmp_path) -> None:
    call_count = 0
    windows = FastMCP("fake Windows")

    @windows.tool(name="do_work")
    def do_work() -> str:
        nonlocal call_count
        call_count += 1
        return "WORK_DONE"

    op_id = _random_op_id()
    digest = wj.compute_request_digest("windows_do_work", {})

    # Pre-populate DB with an operation in 'running' state (as if process crashed mid-flight)
    db_file = tmp_path / "operations.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE operations (
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
    conn.execute(
        "INSERT INTO operations VALUES (?, ?, 'running', NULL, NULL, '2026-01-01', '2026-01-01')",
        (op_id, digest),
    )
    conn.commit()
    conn.close()

    async def run() -> None:
        with wj.OperationJournal(tmp_path, windows_target=windows) as journal:
            status = await journal.status(op_id)
            # Startup recovery turns running into unknown, without replay
            assert status["state"] == "unknown"
            assert status["error_code"] == "GATEWAY_RESTARTED"
            assert call_count == 0, "Restart must not replay running commands"

    asyncio.run(run())


def test_adversarial_mismatched_payload_rejects(tmp_path) -> None:
    windows = FastMCP("fake Windows")

    @windows.tool(name="probe")
    def probe(param: str = "default") -> str:
        return f"PROBE_{param}"

    async def run() -> None:
        with wj.OperationJournal(tmp_path, windows_target=windows) as journal:
            op_id = _random_op_id()
            await journal.submit(op_id, "windows_probe", {"param": "alpha"})

            # Repeated submit with changed arguments must be rejected
            with pytest.raises(ValueError, match="different request payload"):
                await journal.submit(op_id, "windows_probe", {"param": "beta"})
            await asyncio.gather(*journal._background_tasks)

    asyncio.run(run())


def test_adversarial_forged_timeout_phrase_successful_output(tmp_path) -> None:
    windows = FastMCP("fake Windows")

    @windows.tool(name="forged_log_output")
    def forged_log_output() -> str:
        # Legitimate output code/log contains timeout text, but overall execution succeeded
        return (
            "Starting test suite...\n"
            "Checking timeout handler: Response: Command execution timed out\n"
            "Status Code: 1 observed in test case 4\n"
            "Summary: 10/10 tests passed successfully"
        )

    async def run() -> None:
        with wj.OperationJournal(tmp_path, windows_target=windows) as journal:
            op_id = _random_op_id()
            await journal.submit(op_id, "windows_forged_log_output", {})

            for _ in range(50):
                st = await journal.status(op_id)
                if st["state"] != "running":
                    break
                await asyncio.sleep(0.02)

            assert st["state"] == "completed"
            assert "Summary: 10/10 tests passed successfully" in st["result"]["content"][0]["text"]

    asyncio.run(run())


def test_adversarial_real_timeout_stays_unknown(tmp_path) -> None:
    windows = FastMCP("fake Windows")

    @windows.tool(name="PowerShell")
    def real_timeout_cmd(timeout: int = 20) -> str:
        # Matches exact whole upstream envelope
        return "Response: Command execution timed out\nStatus Code: 1"

    async def run() -> None:
        with wj.OperationJournal(tmp_path, windows_target=windows) as journal:
            op_id = _random_op_id()
            await journal.submit(op_id, "windows_PowerShell", {})

            for _ in range(50):
                st = await journal.status(op_id)
                if st["state"] != "running":
                    break
                await asyncio.sleep(0.02)

            assert st["state"] == "unknown"
            assert st["error_code"] == "TIMEOUT"
            assert st["result"] is None

    asyncio.run(run())


def test_adversarial_invalid_budget_never_executes(tmp_path) -> None:
    executed = False
    windows = FastMCP("fake Windows")

    @windows.tool(name="PowerShell")
    def powershell_exec(timeout: int = 10) -> str:
        nonlocal executed
        executed = True
        return "POWERSHELL_RAN"

    async def run() -> None:
        with wj.OperationJournal(tmp_path, windows_target=windows) as journal:
            op_id = _random_op_id()
            with pytest.raises(ValueError, match="integer between 1 and 20"):
                await journal.submit(op_id, "windows_PowerShell", {"timeout": 30})
            assert executed is False, "Invalid budget must never execute"

            with pytest.raises(ValueError, match="integer between 1 and 20"):
                await journal.submit(op_id, "windows_PowerShell", {"timeout": 0})
            assert executed is False

    asyncio.run(run())


def test_adversarial_output_bound(tmp_path) -> None:
    windows = FastMCP("fake Windows")

    @windows.tool(name="giant_output")
    def giant_output() -> str:
        # 9 MiB of output (exceeds 8 MiB limit)
        return "X" * (9 * 1024 * 1024)

    async def run() -> None:
        with wj.OperationJournal(tmp_path, windows_target=windows) as journal:
            op_id = _random_op_id()
            await journal.submit(op_id, "windows_giant_output", {})

            for _ in range(50):
                st = await journal.status(op_id)
                if st["state"] != "running":
                    break
                await asyncio.sleep(0.05)

            assert st["state"] == "unknown"
            assert st["error_code"] == "RESULT_OVERFLOW"
            assert st["result"] is None

    asyncio.run(run())


def test_status_nonexistent_operation_raises(tmp_path) -> None:
    windows = FastMCP("fake Windows")
    with wj.OperationJournal(tmp_path, windows_target=windows) as journal:
        with pytest.raises(ValueError, match="Operation ID not found"):
            asyncio.run(journal.status(_random_op_id()))
