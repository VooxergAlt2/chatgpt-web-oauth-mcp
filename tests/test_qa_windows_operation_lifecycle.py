"""Portable helper checks; these do not constitute native Windows acceptance."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import qa_windows_operation_lifecycle as qa


@pytest.mark.parametrize("port", [8000, 8840, 8841])
def test_native_harness_never_reuses_live_service_port(port):
    with pytest.raises(ValueError, match="reserved"):
        qa.validate_port_available(port)


def test_native_main_refuses_linux_before_creating_work(tmp_path, monkeypatch):
    root = tmp_path / "must-not-exist"
    monkeypatch.setattr(qa.sys, "platform", "linux")
    monkeypatch.setattr(qa.sys, "argv", ["qa", "--root", str(root)])
    with pytest.raises(RuntimeError, match="requires Windows"):
        qa.main()
    assert not root.exists()


def test_receipt_binds_candidate_bytes_and_cleanup(tmp_path, monkeypatch):
    names = ("qa_windows_operation_lifecycle.py", "windows_revit_gateway.py", "windows_operation_journal.py")
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
    monkeypatch.setattr(qa, "__file__", str(tmp_path / names[0]))
    target = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="cleanup"):
        qa.write_receipt(target, True, "completed", [])
    qa.write_receipt(target, True, "completed", ["cleanup_candidate"], evidence={"executions": 1})
    data = json.loads(target.read_text())
    assert data["cleanup_completed"] is True
    assert data["receipt_version"] == 2
    assert data["candidate_hashes"] == {n: hashlib.sha256(n.encode()).hexdigest() for n in names}
    assert data["evidence"] == {"executions": 1}
    assert data["coverage"]["lost_http_response"] == "not_run"
    assert data["coverage"]["opss_coordinator_restart"] == "not_run"
    assert not target.with_suffix(".tmp").exists()


@pytest.mark.parametrize("timeout", [False, True])
def test_stop_process_only_uses_supplied_owned_handle(timeout):
    class Owned:
        def __init__(self): self.actions = []
        def poll(self): return None
        def terminate(self): self.actions.append("terminate")
        def kill(self): self.actions.append("kill")
        def wait(self, timeout):
            self.actions.append("wait")
            if len(self.actions) == 2 and should_timeout:
                raise subprocess.TimeoutExpired("owned", timeout)
    should_timeout = timeout
    proc = Owned()
    qa.stop_process(proc)
    assert proc.actions == (["terminate", "wait", "kill", "wait"] if timeout else ["terminate", "wait"])


def test_marker_wait_is_bounded_by_monotonic_deadline(tmp_path, monkeypatch):
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(qa.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="marker"):
        qa.wait_for_marker(tmp_path / "absent", 1.0)

