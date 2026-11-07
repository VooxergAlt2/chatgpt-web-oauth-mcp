"""Bounded pipe capture with real subprocesses and a mocked Windows adapter."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp import delegate_code_verification as verification


def run_python(tmp_path, program, *, timeout=4, cancel_check=None):
    return verification._run_verification_commands(
        tmp_path, ["python fixture"], timeout_seconds=timeout,
        argv_factory=lambda _: [sys.executable, "-I", "-u", "-c", program],
        cancel_check=cancel_check,
    )[0]


def test_both_streams_are_captured_without_output_files(tmp_path, monkeypatch):
    import tempfile
    def no_file(*args, **kwargs):
        pytest.fail("verification capture must not create a temporary output file")
    monkeypatch.setattr(tempfile, "TemporaryFile", no_file)
    row = run_python(tmp_path, "import os; os.write(1, b'x'*20000); os.write(2, b'y'*20000)")
    assert row["passed"] and row["exit_code"] == 0
    assert row["stdout"] == "x" * 12000 and row["stderr"] == "y" * 12000
    assert row["stdout_truncated"] and row["stderr_truncated"]


def test_exact_aggregate_budget_remains_accepted(tmp_path):
    half = verification.MAX_VERIFICATION_CAPTURE_BYTES // 2
    row = run_python(tmp_path, f"import sys; sys.stdout.buffer.write(b'x'*{half}); sys.stderr.buffer.write(b'y'*{half})")
    assert row["passed"] and row["exit_code"] == 0
    assert row["stdout_truncated"] and row["stderr_truncated"]
    assert not row.get("output_limit_exceeded")


@pytest.mark.parametrize("target", [1, 2])
def test_zero_exit_oversized_stream_is_rejected(tmp_path, target):
    size = verification.MAX_VERIFICATION_CAPTURE_BYTES + 1
    row = run_python(tmp_path, f"import os; os.write({target}, b'x'*{size})")
    assert row["passed"] is False and row["output_limit_exceeded"] is True
    assert len(row["stdout" if target == 1 else "stderr"]) == 12000
    assert not row.get("cleanup_failed")


def test_dual_flood_uses_bounded_memory_and_skips_later_commands(tmp_path, monkeypatch):
    observations = []
    original = verification._VerificationOutputCapture
    class ObservedCapture(original):
        def record(self, stream, chunk):
            super().record(stream, chunk)
            observations.append((self.total_bytes, sum(len(buf) for buf in self._buffers.values())))
    monkeypatch.setattr(verification, "_VerificationOutputCapture", ObservedCapture)
    marker = tmp_path / "should-not-run"
    program = (
        "import os, threading; os.write(1,b'O'); os.write(2,b'E'); "
        "exec('def flood(fd):\\n while True: os.write(fd,b\"x\"*65536)'); "
        "t=threading.Thread(target=flood,args=(2,)); t.start(); flood(1)"
    )
    def argv(command):
        return [sys.executable, "-I", "-u", "-c", program if command == "flood" else
                f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    started = time.monotonic()
    rows = verification._run_verification_commands(
        tmp_path, ["flood", "later"], timeout_seconds=5, argv_factory=argv)
    assert time.monotonic() - started < 4
    assert rows[0]["output_limit_exceeded"] and not rows[0]["passed"]
    assert rows[0]["stdout"].startswith("O") and rows[0]["stderr"].startswith("E")
    assert rows[1]["not_run"] and rows[1]["output_limit_exceeded"]
    assert not marker.exists() and not any(row.get("cleanup_failed") for row in rows)
    assert observations[-1][0] == verification.MAX_VERIFICATION_CAPTURE_BYTES + 1
    assert max(retained for _, retained in observations) <= 2 * 12000


def test_invalid_utf8_prefix_is_bounded_and_preserved_with_replacement(tmp_path):
    row = run_python(tmp_path, "import os; os.write(1, b'\\xff'*12001)")
    assert row["passed"] and row["stdout"] == "\ufffd" * 12000
    assert row["stdout_truncated"] is True


def test_closed_output_pipes_do_not_bypass_deadline(tmp_path):
    started = time.monotonic()
    row = run_python(tmp_path, "import os,time; os.close(1); os.close(2); time.sleep(20)", timeout=0.15)
    assert time.monotonic() - started < 3
    assert row["timed_out"] and not row["passed"] and not row.get("cleanup_failed")


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_inherited_background_pipe_does_not_hold_capture_open(tmp_path):
    pidfile = tmp_path / "background-pid"
    late = tmp_path / "late"
    child = f"import time,pathlib; time.sleep(1); pathlib.Path({str(late)!r}).touch()"
    program = (
        "import subprocess,sys,pathlib; "
        f"p=subprocess.Popen([sys.executable,'-I','-c',{child!r}]); "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(p.pid)); print('done')"
    )
    started = time.monotonic()
    row = run_python(tmp_path, program, timeout=4)
    assert time.monotonic() - started < 2
    assert row["passed"] and row["stdout"] == "done\n" and not row.get("cleanup_failed")
    time.sleep(1.05)
    assert not late.exists()


def test_nonzero_check_does_not_skip_unrelated_later_check(tmp_path):
    rows = verification._run_verification_commands(
        tmp_path, ["first", "second"], timeout_seconds=3,
        argv_factory=lambda command: [sys.executable, "-I", "-c", "raise SystemExit(3)" if command == "first" else "print('ran')"],
    )
    assert rows[0]["exit_code"] == 3 and not rows[0]["passed"]
    assert rows[1]["passed"] and rows[1]["stdout"] == "ran\n"


@pytest.mark.skipif(os.name != "posix", reason="POSIX selector capture")
def test_capture_io_failure_closes_pipes_and_reaps_process(tmp_path, monkeypatch):
    original_popen = subprocess.Popen
    processes = []
    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process
    class BrokenSelector:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def register(self, *args): raise OSError("forced selector failure")
    monkeypatch.setattr(verification.subprocess, "Popen", popen)
    monkeypatch.setattr(verification.selectors, "DefaultSelector", BrokenSelector)
    row = run_python(tmp_path, "import time; time.sleep(20)")
    assert not row["passed"] and row["capture_failed"] is True
    assert "forced selector failure" in row["error"] and not row.get("cleanup_failed")
    assert len(processes) == 1 and processes[0].poll() is not None
    assert processes[0].stdout.closed and processes[0].stderr.closed


@pytest.mark.parametrize("interruption", ["deadline", "cancel"])
def test_launcher_validation_cannot_launch_after_deadline_or_cancellation(tmp_path, monkeypatch, interruption):
    clock = [0.0]
    cancelled = [False]
    monkeypatch.setattr(verification, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    def argv(command):
        clock[0] = 2.0 if interruption == "deadline" else 0.0
        cancelled[0] = interruption == "cancel"
        return ["never-launch"]
    def forbidden(*args, **kwargs):
        pytest.fail("expired/cancelled launcher must not spawn a process")
    monkeypatch.setattr(verification.subprocess, "Popen", forbidden)
    rows = verification._run_verification_commands(
        tmp_path, ["first", "second"], timeout_seconds=1, argv_factory=argv,
        cancel_check=lambda: cancelled[0])
    assert len(rows) == 2 and all(row["not_run"] and not row["passed"] for row in rows)
    assert all(row["timed_out" if interruption == "deadline" else "cancelled"] for row in rows)


@pytest.mark.parametrize("available,error,expected", [(17, 0, 17), (0, 0, 0), (None, 109, None), (None, 232, None), (None, 233, None)])
def test_windows_peek_uses_pointer_sized_handle_and_distinguishes_eof(monkeypatch, available, error, expected):
    calls = []
    def peek(handle, buffer, count, read, total, remaining):
        calls.append(handle.value)
        if available is None:
            return 0
        total._obj.value = available
        return 1
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: (1 << 40) + fd))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: SimpleNamespace(PeekNamedPipe=peek), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    result = verification._windows_verification_peek()(SimpleNamespace(fileno=lambda: 12))
    assert result == expected and calls == [(1 << 40) + 12]


def test_windows_peek_unexpected_failure_does_not_mean_eof(monkeypatch):
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: fd))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: SimpleNamespace(PeekNamedPipe=lambda *args: 0), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda value: OSError(value, "denied"), raising=False)
    with pytest.raises(OSError, match="denied"):
        verification._windows_verification_peek()(SimpleNamespace(fileno=lambda: 12))


def test_windows_capture_reads_only_available_bytes_and_does_not_wait_inherited_eof(monkeypatch):
    class Pipe:
        def __init__(self, fd): self.fd, self.closed = fd, False
        def fileno(self): return self.fd
        def __enter__(self): return self
        def __exit__(self, *args): self.closed = True
    stdout, stderr = Pipe(10), Pipe(11)
    polls = iter([None, 0])
    process = SimpleNamespace(stdout=stdout, stderr=stderr, poll=lambda: next(polls))
    counts = {stdout: iter([2, 0]), stderr: iter([None])}
    monkeypatch.setattr(verification, "_windows_verification_peek", lambda: lambda pipe: next(counts[pipe]))
    reads = []
    def read(fd, count):
        reads.append((fd, count)); return b"ok"
    monkeypatch.setattr(verification, "os", SimpleNamespace(name="nt", read=read))
    capture = verification._VerificationOutputCapture(1024, 100)
    timed_out, cancelled = verification._capture_verification_process(
        process, capture, deadline=time.monotonic() + 1, cancel_check=None)
    assert (timed_out, cancelled) == (False, False)
    assert reads == [(10, 2)] and capture.output("stdout") == ("ok", False)
    assert stdout.closed and stderr.closed
