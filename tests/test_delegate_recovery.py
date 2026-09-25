from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.job_supervisor import (
    process_group_exists,
    process_identity,
)


def _python_command(code: str) -> str:
    return f"{sys.executable} -c {code!r}"


def _write_metadata(root: Path, delegate_id: str, payload: dict[str, object]) -> Path:
    log_dir = root / f"20260925T000000Z-{delegate_id}"
    log_dir.mkdir(parents=True)
    metadata = log_dir / "metadata.json"
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    return metadata


def test_running_delegate_persists_process_identity(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command=_python_command("import time; time.sleep(30)"),
        cancel_grace_seconds=0.1,
    )
    running = registry.run_codex(
        task="persist identity",
        cwd=tmp_path,
        wait_seconds=0,
    )
    metadata_path = Path(running["logs"]["metadata"])
    deadline = time.monotonic() + 3
    payload: dict[str, object] = {}
    while time.monotonic() < deadline:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if payload.get("pid") and payload.get("process_identity"):
            break
        time.sleep(0.02)

    try:
        assert payload["status"] == "running"
        assert isinstance(payload["pid"], int)
        assert isinstance(payload["pgid"], int)
        assert payload["pgid"] == payload["pid"]
        assert isinstance(payload["process_identity"], str)
        assert payload["owner_pid"] == os.getpid()
        assert isinstance(payload["owner_process_identity"], str)
        assert payload["logs"]["metadata"] == str(metadata_path)
    finally:
        registry.delegate_cancel(delegate_id=str(running["delegate_id"]))


def test_recovery_loads_terminal_delegate_for_direct_status(tmp_path: Path) -> None:
    delegate_id = "a1b2c3d4e5f6"
    root = tmp_path / "antigravity-delegates"
    _write_metadata(
        root,
        delegate_id,
        {
            "delegate_id": delegate_id,
            "harness": "antigravity",
            "executor": "antigravity",
            "status": "succeeded",
            "success": True,
            "completed": True,
            "in_progress": False,
            "summary": "done",
        },
    )
    registry = ExecutorRegistry(codex_command="true")

    recovered = registry.recover_persisted_delegates(roots=[root])
    status = registry.delegate_status(delegate_id=delegate_id, watch_seconds=0)

    assert recovered["terminal_loaded"] == 1
    assert status["success"] is True
    assert status["delegate"]["status"] == "succeeded"
    assert status["delegate"]["recovered_from_disk"] is True
    assert status["delegate"]["logs"]["metadata"].endswith("metadata.json")


def test_recovery_never_kills_reused_or_mismatched_pid(tmp_path: Path) -> None:
    delegate_id = "111111111111"
    root = tmp_path / "antigravity-delegates"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        pgid = os.getpgid(process.pid)
        metadata = _write_metadata(
            root,
            delegate_id,
            {
                "delegate_id": delegate_id,
                "harness": "antigravity",
                "status": "running",
                "completed": False,
                "in_progress": True,
                "pid": process.pid,
                "pgid": pgid,
                "process_identity": "definitely-not-the-current-process",
                "owner_pid": 99999999,
                "owner_process_identity": "dead-owner",
            },
        )
        registry = ExecutorRegistry(codex_command="true", cancel_grace_seconds=0.1)

        recovered = registry.recover_persisted_delegates(roots=[root])

        assert recovered["interrupted"] == 1
        assert recovered["orphan_groups_signalled"] == 0
        assert process.poll() is None
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        assert payload["status"] == "cancelled"
        assert payload["error"]["code"] == "server_restart"
        assert payload["recovery"]["process_identity_match"] is False
        assert payload["recovery"]["termination_signalled"] is False
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait(timeout=3)


def test_recovery_refuses_nonleader_recorded_process_group(tmp_path: Path) -> None:
    delegate_id = "121212121212"
    root = tmp_path / "antigravity-delegates"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        metadata = _write_metadata(
            root,
            delegate_id,
            {
                "delegate_id": delegate_id,
                "harness": "antigravity",
                "status": "running",
                "completed": False,
                "in_progress": True,
                "pid": process.pid,
                "pgid": process.pid + 1,
                "process_identity": identity,
                "owner_pid": 99999999,
                "owner_process_identity": "dead-owner",
            },
        )
        registry = ExecutorRegistry(codex_command="true", cancel_grace_seconds=0.1)

        recovered = registry.recover_persisted_delegates(roots=[root])

        assert recovered["interrupted"] == 1
        assert recovered["orphan_groups_signalled"] == 0
        assert process.poll() is None
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        assert payload["recovery"]["action"] == "process_group_leader_mismatch"
        assert payload["recovery"]["termination_signalled"] is False
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait(timeout=3)


def test_recovery_reaps_verified_orphan_process_group(tmp_path: Path) -> None:
    delegate_id = "222222222222"
    root = tmp_path / "antigravity-delegates"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    pgid = os.getpgid(process.pid)
    identity = process_identity(process.pid)
    assert identity is not None
    metadata = _write_metadata(
        root,
        delegate_id,
        {
            "delegate_id": delegate_id,
            "harness": "antigravity",
            "status": "running",
            "completed": False,
            "in_progress": True,
            "pid": process.pid,
            "pgid": pgid,
            "process_identity": identity,
            "owner_pid": 99999999,
            "owner_process_identity": "dead-owner",
        },
    )
    registry = ExecutorRegistry(codex_command="true", cancel_grace_seconds=0.1)

    recovered = registry.recover_persisted_delegates(roots=[root])
    process.wait(timeout=3)

    assert recovered["interrupted"] == 1
    assert recovered["orphan_groups_signalled"] == 1
    assert not process_group_exists(pgid)
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert payload["status"] == "cancelled"
    assert payload["error"]["code"] == "server_restart"
    assert payload["recovery"]["termination_signalled"] is True
    status = registry.delegate_status(delegate_id=delegate_id, watch_seconds=0)
    assert status["delegate"]["recovered_from_disk"] is True


def test_recovery_does_not_touch_delegate_owned_by_live_server(tmp_path: Path) -> None:
    delegate_id = "333333333333"
    root = tmp_path / "antigravity-delegates"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        _write_metadata(
            root,
            delegate_id,
            {
                "delegate_id": delegate_id,
                "harness": "antigravity",
                "status": "running",
                "completed": False,
                "in_progress": True,
                "pid": process.pid,
                "pgid": os.getpgid(process.pid),
                "process_identity": process_identity(process.pid),
                "owner_pid": os.getpid(),
                "owner_process_identity": process_identity(os.getpid()),
            },
        )
        registry = ExecutorRegistry(codex_command="true", cancel_grace_seconds=0.1)

        recovered = registry.recover_persisted_delegates(roots=[root])

        assert recovered["interrupted"] == 0
        assert recovered["orphan_groups_signalled"] == 0
        assert recovered["live_owner_skipped"] == 1
        assert process.poll() is None
    finally:
        if process.poll() is None:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait(timeout=3)


def test_recovery_tracks_live_owner_and_observes_later_terminal_metadata(
    tmp_path: Path,
) -> None:
    delegate_id = "444444444444"
    root = tmp_path / "antigravity-delegates"
    metadata = _write_metadata(
        root,
        delegate_id,
        {
            "delegate_id": delegate_id,
            "harness": "antigravity",
            "status": "running",
            "completed": False,
            "in_progress": True,
            "owner_pid": os.getpid(),
            "owner_process_identity": process_identity(os.getpid()),
        },
    )
    registry = ExecutorRegistry(codex_command="true")

    recovered = registry.recover_persisted_delegates(roots=[root])
    running = registry.delegate_status(delegate_id=delegate_id, watch_seconds=0)

    assert recovered["live_owner_skipped"] == 1
    assert running["delegate"]["status"] == "running"
    assert running["delegate"]["completed"] is False
    assert running["delegate"]["in_progress"] is True
    assert running["delegate"]["recovered_from_disk"] is True
    assert running["delegate"]["detached_from_scheduler"] is True

    metadata.write_text(
        json.dumps(
            {
                "delegate_id": delegate_id,
                "harness": "antigravity",
                "status": "succeeded",
                "success": True,
                "completed": True,
                "in_progress": False,
                "summary": "finished by drained server",
            }
        ),
        encoding="utf-8",
    )
    terminal = registry.delegate_status(delegate_id=delegate_id, watch_seconds=0)

    assert terminal["delegate"]["status"] == "succeeded"
    assert terminal["delegate"]["completed"] is True
    assert terminal["delegate"]["summary"] == "finished by drained server"


def test_live_owner_that_dies_after_startup_scan_is_reconciled_on_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    delegate_id = "555555555555"
    root = tmp_path / "antigravity-delegates"
    _write_metadata(
        root,
        delegate_id,
        {
            "delegate_id": delegate_id,
            "harness": "antigravity",
            "status": "running",
            "completed": False,
            "in_progress": True,
            "owner_pid": 12345,
            "owner_process_identity": "owner-generation-a",
        },
    )
    matches = iter([True, False, False])
    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.executors.process_identity_matches",
        lambda _pid, _identity: next(matches),
    )
    registry = ExecutorRegistry(codex_command="true")

    recovered = registry.recover_persisted_delegates(roots=[root])
    status = registry.delegate_status(delegate_id=delegate_id, watch_seconds=0)

    assert recovered["live_owner_skipped"] == 1
    assert status["delegate"]["status"] == "cancelled"
    assert status["delegate"]["completed"] is True
    assert status["delegate"]["error"]["code"] == "server_restart"
    assert status["delegate"]["recovered_from_disk"] is True


def test_recovery_skips_malformed_and_untrusted_delegate_ids(tmp_path: Path) -> None:
    root = tmp_path / "antigravity-delegates"
    bad_dir = root / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "metadata.json").write_text("{", encoding="utf-8")
    _write_metadata(
        root,
        "not-a-valid-id",
        {
            "delegate_id": "not-a-valid-id",
            "status": "succeeded",
            "completed": True,
        },
    )
    registry = ExecutorRegistry(codex_command="true")

    recovered = registry.recover_persisted_delegates(roots=[root])

    assert recovered["scanned"] == 2
    assert recovered["skipped"] == 2
    assert recovered["terminal_loaded"] == 0
