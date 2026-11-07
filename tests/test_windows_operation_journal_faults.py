"""Regression cases for journal persistence and worker supervision failures."""
import asyncio
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys

import pytest
from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from windows_operation_journal import OperationJournal

OP = "fault_case_" + "a" * 32
OTHER = "fault_case_" + "b" * 32


@pytest.mark.parametrize("boundary", ["update", "commit"])
def test_terminal_write_failure_is_unknown_and_admission_is_latched(tmp_path, boundary):
    async def check():
        backend, calls = FastMCP("fault backend"), []
        @backend.tool(name="Click")
        def click(x: int):
            calls.append(x)
            return {"private": "unpersisted success"}
        journal = OperationJournal(tmp_path, backend)
        original = journal._get_connection
        class Connection:
            updated = False
            def __init__(self, conn):
                self.conn = conn
            def execute(self, query, *args):
                if query.lstrip().startswith("UPDATE operations"):
                    self.updated = True
                    if boundary == "update":
                        raise sqlite3.OperationalError("private disk failure")
                return self.conn.execute(query, *args)
            def commit(self):
                if self.updated and boundary == "commit":
                    raise sqlite3.OperationalError("private commit failure")
                return self.conn.commit()
            def __getattr__(self, key):
                return getattr(self.conn, key)
        @contextmanager
        def fail_terminal():
            with original() as conn:
                yield Connection(conn)
        journal._get_connection = fail_terminal
        await journal.submit(OP, "windows_Click", {"x": 7})
        await asyncio.gather(*list(journal._background_tasks))
        await asyncio.sleep(0)  # observe done callbacks
        receipt = await journal.status(OP)
        assert receipt["state"] == "unknown"
        assert receipt["error_code"] == "JOURNAL_WRITE_FAILED"
        assert receipt["result"] is None
        assert not journal._background_tasks
        assert calls == [7]
        # Status/dedup must remain usable if storage subsequently becomes unreadable.
        @contextmanager
        def unavailable():
            raise sqlite3.OperationalError("private unavailable path")
            yield
        journal._get_connection = unavailable
        assert await journal.status(OP) == receipt
        assert await journal.submit(OP, "windows_Click", {"x": 7}) == receipt
        with pytest.raises(ValueError, match="different request payload"):
            await journal.submit(OP, "windows_Click", {"x": 8})
        with pytest.raises(RuntimeError, match="JOURNAL_UNHEALTHY"):
            await journal.submit(OTHER, "windows_Click", {"x": 8})
        assert calls == [7]
        journal._get_connection = original
        journal.close()
        # Durable pre-execution identity survives failed terminal persistence.
        restarted = OperationJournal(tmp_path, backend)
        assert (await restarted.status(OP))["state"] == "unknown"
        assert (await restarted.submit(OP, "windows_Click", {"x": 7}))["state"] == "unknown"
        assert calls == [7]
        restarted.close()
    asyncio.run(check())


def test_unexpected_worker_exception_is_observed_without_false_running(tmp_path):
    async def check():
        backend = FastMCP("worker fault")
        @backend.tool(name="Click")
        def click(x: int):
            pytest.fail("replacement worker must not dispatch")
        journal = OperationJournal(tmp_path, backend)
        unhandled = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, event: unhandled.append(event))
        async def broken(*args):
            raise RuntimeError("private worker failure")
        journal._execute_operation = broken
        await journal.submit(OP, "windows_Click", {"x": 1})
        # Do not gather/read the task exception: supervision must consume it itself.
        while journal._background_tasks:
            await asyncio.sleep(0.01)
        receipt = await journal.status(OP)
        assert receipt["state"] == "unknown"
        assert receipt["error_code"] == "JOURNAL_WORKER_FAILED"
        assert "private worker failure" not in str(receipt)
        with pytest.raises(RuntimeError, match="JOURNAL_UNHEALTHY"):
            await journal.submit(OTHER, "windows_Click", {"x": 2})
        assert not unhandled
        journal.close()
    asyncio.run(check())


def test_cancel_before_worker_start_never_leaves_unexplained_running(tmp_path):
    async def check():
        backend, calls = FastMCP("cancel before dispatch"), []
        @backend.tool(name="Click")
        def click(x: int):
            calls.append(x)
            return "done"
        journal = OperationJournal(tmp_path, backend)
        await journal.submit(OP, "windows_Click", {"x": 1})
        task = next(iter(journal._background_tasks))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        receipt = await journal.status(OP)
        assert receipt["state"] == "unknown"
        assert receipt["error_code"] == "TASK_CANCELLED"
        assert await journal.submit(OP, "windows_Click", {"x": 1}) == receipt
        assert calls == []
        assert not journal._background_tasks
        journal.close()
    asyncio.run(check())

