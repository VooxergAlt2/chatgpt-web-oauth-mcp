from __future__ import annotations

import shlex
import subprocess
import sys
import time
from pathlib import Path

import chatgpt_web_oauth_mcp.executors as executors
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def _python_command(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_hard_timeout_kills_descendant_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "descendant-survived"
    child_code = (
        "import time; from pathlib import Path; "
        "time.sleep(2); Path('descendant-survived').write_text('alive')"
    )
    parent_code = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(10)"
    )
    registry = ExecutorRegistry(codex_command=_python_command(parent_code), cancel_grace_seconds=0.1)

    result = registry.run_codex(
        task="timeout tree",
        cwd=tmp_path,
        execution_timeout_seconds=1,
        wait_seconds=3,
    )

    assert result["status"] == "timed_out"
    time.sleep(1.3)
    assert not marker.exists()


def test_cancel_running_code_releases_project_writer_slot(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command=_python_command("import time; time.sleep(10)"),
        cancel_grace_seconds=0.1,
    )
    running = registry.run_codex(task="blocked writer", cwd=tmp_path, wait_seconds=0)
    registry.codex_command = _python_command("print('next')")
    queued = registry.run_codex(task="next writer", cwd=tmp_path, wait_seconds=0)

    assert queued["status"] == "queued"
    cancelled = registry.delegate_cancel(delegate_id=str(running["delegate_id"]))
    assert cancelled["delegate"]["status"] == "cancelled"

    next_result = registry.delegate_status(
        delegate_id=str(queued["delegate_id"]),
        watch_seconds=2,
        poll_seconds=0.02,
    )
    if next_result["delegate"]["status"] == "running":
        next_result = registry.delegate_status(
            delegate_id=str(queued["delegate_id"]),
            watch_seconds=2,
            poll_seconds=0.02,
        )
    assert next_result["delegate"]["status"] == "succeeded"


def test_cancel_group_cancels_running_and_queued_children(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command=_python_command("import time; time.sleep(10)"),
        max_explore_per_project=1,
        cancel_grace_seconds=0.1,
        allow_unsafe_explore_command=True,
    )
    group = registry.run_codex_batch(
        tasks=[{"task": "one"}, {"task": "two"}, {"task": "three"}],
        cwd=tmp_path,
        wait_seconds=0,
    )

    cancelled = registry.delegate_cancel(group_id=str(group["group_id"]))

    assert cancelled["group"]["completed"] is True
    assert cancelled["group"]["status"] == "failed"
    assert cancelled["group"]["counts"]["cancelled"] == 3


def test_registry_shutdown_cancels_running_and_queued_without_dispatch(
    tmp_path: Path,
) -> None:
    registry = ExecutorRegistry(
        codex_command=_python_command("import time; time.sleep(30)"),
        cancel_grace_seconds=0.1,
    )
    running = registry.run_codex(task="running", cwd=tmp_path, wait_seconds=0)
    queued = registry.run_codex(task="queued", cwd=tmp_path, wait_seconds=0)

    shutdown = registry.shutdown(wait_seconds=2)

    assert shutdown["success"] is True
    assert shutdown["queued_cancelled"] == 1
    assert shutdown["running_cancel_requested"] == 1
    running_status = registry.delegate_status(
        delegate_id=str(running["delegate_id"]),
        watch_seconds=0,
    )["delegate"]
    queued_status = registry.delegate_status(
        delegate_id=str(queued["delegate_id"]),
        watch_seconds=0,
    )["delegate"]
    assert running_status["status"] == "cancelled"
    assert running_status["error"]["code"] == "server_shutdown"
    assert queued_status["status"] == "cancelled"
    assert queued_status["error"]["code"] == "server_shutdown"
    assert registry.scheduler.nonterminal_tasks() == []

    repeated = registry.shutdown(wait_seconds=0)
    assert repeated["success"] is True
    assert repeated["queued_cancelled"] == 0
    assert repeated["running_cancel_requested"] == 0

    rejected = registry.run_codex(
        task="must-not-start",
        cwd=tmp_path,
        wait_seconds=0,
    )
    assert rejected["success"] is False
    assert rejected["error"]["code"] == "server_shutdown"

    batch_rejected = registry.run_codex_batch(
        tasks=[{"task": "must-not-start-batch"}],
        cwd=tmp_path,
        wait_seconds=0,
    )
    assert batch_rejected["success"] is False
    assert batch_rejected["error"]["code"] == "server_shutdown"


def test_readonly_audit_detects_repository_mutation(tmp_path: Path) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init"], check=True, capture_output=True)
    registry = ExecutorRegistry(
        codex_command=_python_command("from pathlib import Path; Path('mutated').write_text('x')"),
        allow_unsafe_explore_command=True,
    )

    result = registry.run_codex(
        task="attempt mutation",
        kind="explore",
        cwd=tmp_path,
        wait_seconds=2,
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "readonly_violation"
    assert result["sandbox_mode"] == "read-only"


def test_enforced_readonly_allows_external_repository_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init"], check=True, capture_output=True)
    registry = ExecutorRegistry(codex_command="codex")

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        returncode = 0

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def communicate(self, timeout=None):
            return b"review complete", b""

    statuses = iter([b"", b"?? orchestrator-change\x00"])
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    monkeypatch.setattr(registry._process_runner, "popen_factory", FakeProcess)
    monkeypatch.setattr(
        registry._process_runner,
        "_git_status",
        lambda _task: next(statuses),
    )

    result = registry.run_codex(
        task="review while orchestrator edits",
        kind="explore",
        cwd=tmp_path,
        wait_seconds=2,
    )

    assert result["status"] == "succeeded"
    audit = result["harness_metadata"]["readonly_audit"]
    assert audit["enforced_by_harness"] is True
    assert audit["repository_drift"] is True
    assert audit["audit_unavailable"] is False
    assert audit["attribution"] == "unknown_external_or_delegate"


def test_enforced_readonly_tolerates_unavailable_git_audit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init"], check=True, capture_output=True)
    registry = ExecutorRegistry(codex_command="codex")

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        returncode = 0

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def communicate(self, timeout=None):
            return b"review complete", b""

    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    monkeypatch.setattr(registry._process_runner, "popen_factory", FakeProcess)
    monkeypatch.setattr(registry._process_runner, "_git_status", lambda _task: None)

    result = registry.run_codex(
        task="review with unavailable audit",
        kind="explore",
        cwd=tmp_path,
        wait_seconds=2,
    )

    assert result["status"] == "succeeded"
    audit = result["harness_metadata"]["readonly_audit"]
    assert audit["enforced_by_harness"] is True
    assert audit["repository_drift"] is False
    assert audit["audit_unavailable"] is True


def test_readonly_audit_fails_closed_when_git_snapshot_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init"], check=True, capture_output=True)
    registry = ExecutorRegistry(
        codex_command=_python_command("print('should-not-run')"),
        allow_unsafe_explore_command=True,
    )
    monkeypatch.setattr(registry._process_runner, "_git_status", lambda task: None)

    result = registry.run_codex(
        task="audit unavailable",
        kind="explore",
        cwd=tmp_path,
        wait_seconds=2,
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "readonly_audit_unavailable"
