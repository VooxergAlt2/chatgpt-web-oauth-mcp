"""Atomic admission reconciliation prevents late execution after automatic cleanup."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys

import pytest
from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from windows_operation_journal import OperationJournal, compute_request_digest

OP = "reconcile_" + "a" * 32
NAME, ARGS = "windows_Click", {"x": 1}
DIGEST = compute_request_digest(NAME, ARGS)


def backend():
    server, calls = FastMCP("reconcile backend"), []
    @server.tool(name="Click")
    def click(x: int):
        calls.append(x)
        return "done"
    return server, calls


def test_missing_admission_tombstone_survives_restart_and_rejects_late_submit(tmp_path):
    async def check():
        server, calls = backend()
        journal = OperationJournal(tmp_path, server)
        receipt = await journal.reconcile(OP, DIGEST)
        assert receipt["state"] == "unknown" and receipt["execution_status"] == "finished"
        assert receipt["error_code"] == "NOT_DISPATCHED"
        assert await journal.submit(OP, NAME, ARGS) == receipt
        with pytest.raises(ValueError, match="mismatch"):
            await journal.reconcile(OP, "b" * 64)
        assert not journal._background_tasks and calls == []
        journal.close()
        journal = OperationJournal(tmp_path, server)
        assert await journal.submit(OP, NAME, ARGS) == receipt
        assert calls == [] and not journal._background_tasks
        journal.close()
    asyncio.run(check())


@pytest.mark.parametrize("submit_first", [False, True])
def test_submit_and_reconcile_race_has_one_atomic_winner(tmp_path, submit_first):
    async def check():
        server, calls = backend()
        journal = OperationJournal(tmp_path, server)
        operations = [journal.submit(OP, NAME, ARGS), journal.reconcile(OP, DIGEST)]
        if not submit_first: operations.reverse()
        await asyncio.gather(*operations)
        await asyncio.gather(*list(journal._background_tasks))
        await asyncio.sleep(0)
        receipt = await journal.reconcile(OP, DIGEST)
        assert calls == ([1] if submit_first else [])
        assert receipt["state"] == ("completed" if submit_first else "unknown")
        assert receipt["execution_status"] == "finished"
        await journal.submit(OP, NAME, ARGS)
        assert len(calls) == int(submit_first)
        journal.close()
    asyncio.run(check())


def test_unavailable_backend_before_dispatch_is_terminal_without_execution(tmp_path):
    async def check():
        server, calls = backend()
        journal = OperationJournal(tmp_path, server)
        async def unavailable():
            raise OSError("private upstream connection failure")
        journal.get_available_windows_tools = unavailable
        await journal.submit(OP, NAME, ARGS)
        await asyncio.gather(*list(journal._background_tasks))
        receipt = await journal.status(OP)
        assert receipt["execution_status"] == "finished"
        assert receipt["state"] == "unknown" and receipt["error_code"] == "NOT_DISPATCHED"
        assert calls == []
        assert "private upstream" not in str(receipt)
        journal.close()
    asyncio.run(check())


def test_old_journal_migration_never_invents_unknown_execution_proof(tmp_path):
    conn = sqlite3.connect(tmp_path / "operations.db")
    conn.execute("CREATE TABLE operations (operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, state TEXT NOT NULL, response_json TEXT, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    for index, state in enumerate(["completed", "unknown", "running"]):
        conn.execute("INSERT INTO operations VALUES (?, ?, ?, ?, NULL, 'old', 'old')",
                     (str(index) * 32, DIGEST, state, json.dumps({"content": [], "isError": False}) if state == "completed" else None))
    conn.commit()
    conn.close()
    async def check():
        journal = OperationJournal(tmp_path)
        assert (await journal.status("0" * 32))["execution_status"] == "finished"
        for index in [1, 2]:
            receipt = await journal.reconcile(str(index) * 32, DIGEST)
            assert receipt["state"] == "unknown" and receipt["execution_status"] == "unconfirmed"
        journal.close()
    asyncio.run(check())

