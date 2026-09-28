from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import chatgpt_web_oauth_mcp.executors as executors
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

def test_legacy_delegate_migration_moves_terminal_and_drops_unattributed(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    legacy_root = legacy / "codex-delegates"

    terminal_id = "abcdef123456"
    terminal_metadata = _write_metadata(
        legacy_root,
        terminal_id,
        {
            "delegate_id": terminal_id,
            "harness": "codex",
            "executor": "codex",
            "status": "succeeded",
            "success": True,
            "completed": True,
            "in_progress": False,
        },
    )
    terminal_dir = terminal_metadata.parent
    prompt = terminal_dir / "prompt.txt"
    stdout = terminal_dir / "stdout.log"
    stderr = terminal_dir / "stderr.log"
    prompt.write_text("prompt", encoding="utf-8")
    stdout.write_text("done", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    payload = json.loads(terminal_metadata.read_text(encoding="utf-8"))
    payload["logs"] = {
        "log_dir": str(terminal_dir),
        "prompt": "prompt.txt",
        "stdout": "stdout.log",
        "stderr": "stderr.log",
        "metadata": "metadata.json",
    }
    terminal_metadata.write_text(json.dumps(payload), encoding="utf-8")
    source_mtime = time.time() - 123
    os.utime(terminal_metadata, (source_mtime, source_mtime))
    stale_staging = (
        canonical
        / "codex-delegates"
        / f".{terminal_dir.name}.migrating-stale"
    )
    stale_staging.mkdir(parents=True)
    (stale_staging / "partial").write_text("partial", encoding="utf-8")

    queued_id = "111111111111"
    queued_metadata = _write_metadata(
        legacy_root,
        queued_id,
        {
            "delegate_id": queued_id,
            "harness": "codex",
            "executor": "codex",
            "status": "queued",
            "completed": False,
            "in_progress": True,
        },
    )

    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=canonical,
        legacy_delegate_state_roots=(legacy,),
    )

    migration = registry.migrate_legacy_persisted_delegates()

    target_dir = canonical / "codex-delegates" / terminal_dir.name
    target_metadata = target_dir / "metadata.json"
    migrated_payload = json.loads(target_metadata.read_text(encoding="utf-8"))
    assert migration["success"] is True
    assert migration["migrated_terminal"] == 1
    assert migration["unattributed_removed"] == 1
    assert terminal_dir.exists() is False
    assert queued_metadata.parent.exists() is False
    assert target_metadata.is_file()
    assert migrated_payload["logs"]["log_dir"] == str(target_dir)
    assert migrated_payload["logs"]["prompt"] == str(target_dir / "prompt.txt")
    assert migrated_payload["logs"]["stdout"] == str(target_dir / "stdout.log")
    assert migrated_payload["logs"]["stderr"] == str(target_dir / "stderr.log")
    assert migrated_payload["logs"]["metadata"] == str(target_metadata)
    assert migrated_payload["state_migrated_from"] == str(terminal_dir)
    assert abs(
        migrated_payload["state_source_metadata_mtime_epoch"] - source_mtime
    ) < 0.01
    assert stale_staging.exists() is False
    assert (
        registry.runtime_info()["state"]["last_migration"]["migrated_terminal"]
        == 1
    )


def test_legacy_delegate_migration_preserves_attributed_nonterminal(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    legacy_root = legacy / "codex-delegates"
    delegate_id = "222222222222"
    metadata = _write_metadata(
        legacy_root,
        delegate_id,
        {
            "delegate_id": delegate_id,
            "harness": "codex",
            "executor": "codex",
            "status": "running",
            "completed": False,
            "in_progress": True,
            "owner_pid": os.getpid(),
            "owner_process_identity": process_identity(os.getpid()),
        },
    )
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=canonical,
        legacy_delegate_state_roots=(legacy,),
    )

    migration = registry.migrate_legacy_persisted_delegates()

    assert migration["migrated_terminal"] == 0
    assert migration["unattributed_removed"] == 0
    assert metadata.is_file()


def test_legacy_delegate_migration_reconciles_completed_duplicate(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    delegate_id = "333333333333"
    legacy_metadata = _write_metadata(
        legacy / "codex-delegates",
        delegate_id,
        {
            "delegate_id": delegate_id,
            "harness": "codex",
            "executor": "codex",
            "status": "succeeded",
            "completed": True,
            "success": True,
        },
    )
    canonical_metadata = _write_metadata(
        canonical / "codex-delegates",
        delegate_id,
        {
            "delegate_id": delegate_id,
            "harness": "codex",
            "executor": "codex",
            "status": "succeeded",
            "completed": True,
            "success": True,
        },
    )
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=canonical,
        legacy_delegate_state_roots=(legacy,),
    )

    migration = registry.migrate_legacy_persisted_delegates()

    assert migration["duplicates_reconciled"] == 1
    assert migration["conflicts"] == 0
    assert legacy_metadata.parent.exists() is False
    assert canonical_metadata.is_file()


def test_legacy_delegate_migration_failure_keeps_source_and_removes_staging(
    tmp_path: Path,
    monkeypatch,
) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    delegate_id = "444444444444"
    source_metadata = _write_metadata(
        legacy / "codex-delegates",
        delegate_id,
        {
            "delegate_id": delegate_id,
            "harness": "codex",
            "executor": "codex",
            "status": "succeeded",
            "completed": True,
            "success": True,
        },
    )
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=canonical,
        legacy_delegate_state_roots=(legacy,),
    )
    monkeypatch.setattr(
        executors,
        "write_private_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )

    migration = registry.migrate_legacy_persisted_delegates()

    target_root = canonical / "codex-delegates"
    assert migration["success"] is False
    assert migration["errors"] == 1
    assert source_metadata.is_file()
    assert list(target_root.glob(".*.migrating-*")) == []
    assert not (target_root / source_metadata.parent.name).exists()


def test_recovery_prefers_canonical_record_for_duplicate_delegate_id(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    delegate_id = "555555555555"
    _write_metadata(
        canonical / "codex-delegates",
        delegate_id,
        {
            "delegate_id": delegate_id,
            "harness": "codex",
            "executor": "codex",
            "status": "succeeded",
            "completed": True,
            "success": True,
            "summary": "canonical",
        },
    )
    _write_metadata(
        legacy / "codex-delegates",
        delegate_id,
        {
            "delegate_id": delegate_id,
            "harness": "codex",
            "executor": "codex",
            "status": "failed",
            "completed": True,
            "success": False,
            "summary": "legacy",
        },
    )
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=canonical,
        legacy_delegate_state_roots=(legacy,),
    )

    recovered = registry.recover_persisted_delegates()
    status = registry.delegate_status(delegate_id=delegate_id, watch_seconds=0)

    assert recovered["terminal_loaded"] == 1
    assert recovered["duplicate_delegate_ids_skipped"] == 1
    assert status["delegate"]["status"] == "succeeded"
    assert status["delegate"]["summary"] == "canonical"



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
    runtime = registry.runtime_info()

    assert recovered["terminal_loaded"] == 1
    assert isinstance(recovered["recorded_at_epoch"], float)
    assert runtime["persisted_delegate_records"] == 1
    assert runtime["last_recovery"]["scanned"] == 1
    assert runtime["last_recovery"]["terminal_loaded"] == 1
    assert status["success"] is True
    assert status["delegate"]["status"] == "succeeded"
    assert status["delegate"]["recovered_from_disk"] is True
    assert status["delegate"]["logs"]["metadata"].endswith("metadata.json")


def test_delegate_maintenance_prunes_terminal_records_by_ttl_and_cap_but_keeps_queued(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "delegate-state"
    root = state_root / "codex-delegates"
    now = time.time()

    def create(delegate_id: str, status: str, age_seconds: float) -> Path:
        metadata = _write_metadata(
            root,
            delegate_id,
            {
                "delegate_id": delegate_id,
                "harness": "codex",
                "executor": "codex",
                "status": status,
                "success": status == "succeeded",
                "completed": status in {"succeeded", "failed", "cancelled", "timed_out"},
                "in_progress": status not in {"succeeded", "failed", "cancelled", "timed_out"},
            },
        )
        os.utime(metadata, (now - age_seconds, now - age_seconds))
        return metadata.parent

    newest = create("111111111111", "succeeded", 10)
    second = create("222222222222", "failed", 20)
    capped = create("333333333333", "cancelled", 30)
    expired = create("444444444444", "timed_out", 1000)
    queued = create("555555555555", "queued", 5000)
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=state_root,
        delegate_retention_seconds=100,
        max_terminal_delegate_records=2,
    )

    result = registry.maintain_persisted_delegates(force=True)

    assert result["success"] is True
    assert result["removed"] == 2
    assert newest.exists()
    assert second.exists()
    assert not capped.exists()
    assert not expired.exists()
    assert queued.exists()


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
