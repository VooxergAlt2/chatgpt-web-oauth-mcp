from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

import chatgpt_web_oauth_mcp.executors as executors
from chatgpt_web_oauth_mcp.delegate_harnesses import GenericCliHarness
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.shell import JobRegistry


def _agent_command(script: Path) -> str:
    return shlex.join([sys.executable, str(script)])


def _registry(tmp_path: Path, command: str) -> ExecutorRegistry:
    harness = GenericCliHarness(
        name="antigravity",
        command=command,
        explore_command=command,
        display_name="Durable test agent",
    )
    return ExecutorRegistry(
        default_harness="antigravity",
        harnesses=[harness],
        durable_job_registry=JobRegistry(),
        durable_state_dir=tmp_path / "state",
        durable_harnesses=("antigravity",),
        cancel_grace_seconds=0.1,
    )


def _wait_for_durable_job(
    registry: ExecutorRegistry,
    delegate_id: str,
    *,
    timeout: float = 10.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        task = registry.scheduler.get_task(delegate_id)
        if task is not None:
            latest = {
                "delegate_id": task.delegate_id,
                "durable_job_id": task.durable_job_id,
                "status": task.state,
            }
            if task.durable_job_id:
                state_dir = registry.durable_state_dir
                assert state_dir is not None
                job = JobRegistry().job_status(
                    job_id=task.durable_job_id,
                    state_dir=state_dir,
                )
                if job.get("status") == "running":
                    return latest
        time.sleep(0.02)
    raise AssertionError(f"durable job was not published: {latest}")


def test_durable_delegate_survives_registry_shutdown_and_new_registry_observes_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(executors.tempfile, "gettempdir", lambda: str(tmp_path))
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, sys, time\n"
        "sys.stdin.read()\n"
        "time.sleep(5)\n"
        "print(json.dumps({'status': 'succeeded', 'source': 'durable-job'}))\n",
        encoding="utf-8",
    )
    command = _agent_command(script)
    first = _registry(tmp_path, command)

    started = first.run_delegate(
        task="durable review",
        cwd=tmp_path,
        harness="antigravity",
        kind="explore",
        wait_seconds=0,
        execution_timeout_seconds=12,
        parse_structured_output=True,
    )
    delegate_id = str(started["delegate_id"])
    published = _wait_for_durable_job(first, delegate_id)
    durable_job_id = str(published["durable_job_id"])
    job_metadata = json.loads(
        (tmp_path / "state" / "jobs" / durable_job_id / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert job_metadata["timeout_seconds"] == 12.0

    shutdown = first.shutdown(wait_seconds=0)

    assert shutdown["success"] is True
    assert shutdown["running_preserved"] == 1
    assert shutdown["preserved"] == [delegate_id]
    assert JobRegistry().job_status(
        job_id=durable_job_id,
        state_dir=tmp_path / "state",
    )["status"] == "running"

    second = _registry(tmp_path, command)
    root = executors._delegate_log_root_for_harness("antigravity")
    recovered = second.recover_persisted_delegates(roots=[root])

    assert recovered["interrupted"] == 0
    assert recovered["durable_running"] == 1
    assert recovered["durable_adopted"] == 1
    terminal = second.delegate_status(
        delegate_id=delegate_id,
        watch_seconds=7,
        poll_seconds=0.05,
    )["delegate"]

    assert terminal["status"] == "succeeded"
    assert terminal["completed"] is True
    assert terminal["durable"] is True
    assert terminal["durable_job_id"] == durable_job_id
    assert terminal["recovered_from_disk"] is True
    assert (terminal.get("error") or {}).get("code") != "server_restart"


def test_recovered_durable_delegate_can_be_cancelled_by_new_registry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(executors.tempfile, "gettempdir", lambda: str(tmp_path))
    script = tmp_path / "slow-agent.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdin.read()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    command = _agent_command(script)
    first = _registry(tmp_path, command)

    started = first.run_delegate(
        task="cancel after restart",
        cwd=tmp_path,
        harness="antigravity",
        kind="explore",
        wait_seconds=0,
    )
    delegate_id = str(started["delegate_id"])
    published = _wait_for_durable_job(first, delegate_id)
    durable_job_id = str(published["durable_job_id"])
    first.shutdown(wait_seconds=0)

    second = _registry(tmp_path, command)
    root = executors._delegate_log_root_for_harness("antigravity")
    recovered = second.recover_persisted_delegates(roots=[root])
    assert recovered["durable_running"] == 1
    assert recovered["durable_adopted"] == 1

    cancelled = second.delegate_cancel(delegate_id=delegate_id)["delegate"]

    assert cancelled["status"] == "cancelled"
    assert cancelled["completed"] is True
    assert cancelled["error"]["code"] == "cancelled"
    job = JobRegistry().job_status(
        job_id=durable_job_id,
        state_dir=tmp_path / "state",
    )
    assert job["status"] == "killed"


def test_recovered_durable_code_delegate_keeps_project_writer_slot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(executors.tempfile, "gettempdir", lambda: str(tmp_path))
    script = tmp_path / "slow-code-agent.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdin.read()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    command = _agent_command(script)
    first = _registry(tmp_path, command)

    started = first.run_delegate(
        task="first writer",
        cwd=tmp_path,
        harness="antigravity",
        kind="code",
        wait_seconds=0,
    )
    first_id = str(started["delegate_id"])
    _wait_for_durable_job(first, first_id)
    shutdown = first.shutdown(wait_seconds=0)
    assert shutdown["running_preserved"] == 1

    second = _registry(tmp_path, command)
    root = executors._delegate_log_root_for_harness("antigravity")
    recovered = second.recover_persisted_delegates(roots=[root])

    assert recovered["durable_running"] == 1
    assert recovered["durable_adopted"] == 1
    queued = second.run_delegate(
        task="second writer",
        cwd=tmp_path,
        harness="antigravity",
        kind="code",
        wait_seconds=0,
    )

    assert queued["status"] == "queued"
    project = second.delegate_status(project_cwd=tmp_path)
    assert project["project"]["counts"]["running"] == 1
    assert project["project"]["counts"]["queued"] == 1
    assert project["project"]["active"][0]["recovered_from_disk"] is True
    assert project["project"]["active"][0]["durable_job_status"] == "running"
    assert isinstance(project["project"]["active"][0]["pid"], int)

    second.delegate_cancel(delegate_id=first_id)
    second.delegate_cancel(delegate_id=str(queued["delegate_id"]))


def test_rolling_monitors_preserve_durable_timeout_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(executors.tempfile, "gettempdir", lambda: str(tmp_path))
    script = tmp_path / "timeout-agent.py"
    script.write_text(
        "import sys, time\n"
        "sys.stdin.read()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    command = _agent_command(script)
    first = _registry(tmp_path, command)

    started = first.run_delegate(
        task="timeout across reload",
        cwd=tmp_path,
        harness="antigravity",
        kind="explore",
        wait_seconds=0,
        execution_timeout_seconds=6,
    )
    delegate_id = str(started["delegate_id"])
    _wait_for_durable_job(first, delegate_id)
    shutdown = first.shutdown(wait_seconds=0)
    assert shutdown["running_preserved"] == 1

    second = _registry(tmp_path, command)
    root = executors._delegate_log_root_for_harness("antigravity")
    recovered = second.recover_persisted_delegates(roots=[root])
    assert recovered["durable_adopted"] == 1

    terminal = second.delegate_status(
        delegate_id=delegate_id,
        watch_seconds=8,
        poll_seconds=0.05,
    )["delegate"]

    assert terminal["status"] == "timed_out"
    assert terminal["error"]["code"] == "timed_out"
    assert terminal["durable_termination_reason"] == "timed_out"
    time.sleep(0.3)
    metadata = json.loads(Path(terminal["logs"]["metadata"]).read_text(encoding="utf-8"))
    assert metadata["status"] == "timed_out"
    assert metadata["error"]["code"] == "timed_out"
    assert metadata["durable_termination_reason"] == "timed_out"
