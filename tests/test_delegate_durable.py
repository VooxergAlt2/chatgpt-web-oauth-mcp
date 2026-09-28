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
        delegate_state_root=tmp_path / "delegate-state",
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
    root = executors._delegate_log_root_for_harness(
        "antigravity",
        state_root=second.delegate_state_root,
    )
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
    root = executors._delegate_log_root_for_harness(
        "antigravity",
        state_root=second.delegate_state_root,
    )
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
    root = executors._delegate_log_root_for_harness(
        "antigravity",
        state_root=second.delegate_state_root,
    )
    recovered = second.recover_persisted_delegates(roots=[root])

    assert recovered["durable_running"] == 1
    assert recovered["durable_adopted"] == 1
    runtime = second.runtime_info()
    assert runtime["durable"]["running"] == 1
    assert runtime["durable"]["recovered_running"] == 1
    assert runtime["recovered_tasks"] == 1
    assert runtime["last_recovery"]["durable_adopted"] == 1
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
    root = executors._delegate_log_root_for_harness(
        "antigravity",
        state_root=second.delegate_state_root,
    )
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


def test_recovered_durable_batch_restores_group_barrier_and_dependency(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = tmp_path / "batch-agent.py"
    script.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "prompt = sys.stdin.read()\n"
        "if 'dependent writer' in prompt:\n"
        "    time.sleep(0.05)\n"
        "    print(json.dumps({'status': 'succeeded', 'role': 'writer'}))\n"
        "else:\n"
        "    release = Path(__file__).with_name('release-readers')\n"
        "    while not release.exists():\n"
        "        time.sleep(0.05)\n"
        "    print(json.dumps({'status': 'succeeded', 'role': 'reader'}))\n",
        encoding="utf-8",
    )
    command = _agent_command(script)
    first = _registry(tmp_path, command)

    batch = first.run_delegate_batch(
        tasks=[{"task": "reader one"}, {"task": "reader two"}],
        cwd=tmp_path,
        harness="antigravity",
        max_concurrency=2,
        wait_seconds=0,
        execution_timeout_seconds=15,
    )
    group_id = str(batch["group_id"])
    child_ids = [str(child["delegate_id"]) for child in batch["children"]]
    assert len(child_ids) == 2
    for delegate_id in child_ids:
        _wait_for_durable_job(first, delegate_id)

    shutdown = first.shutdown(wait_seconds=0)
    assert shutdown["running_preserved"] == 2

    second = _registry(tmp_path, command)
    root = executors._delegate_log_root_for_harness(
        "antigravity",
        state_root=second.delegate_state_root,
    )
    recovered = second.recover_persisted_delegates(roots=[root])

    assert recovered["durable_adopted"] == 2
    repeated = second.recover_persisted_delegates(roots=[root])
    assert repeated["groups_restored"] == 0
    assert repeated["durable_adopted"] == 0
    assert repeated["durable_already_attached"] == 2
    assert repeated["durable_unattached"] == 0

    restored_group = second.delegate_status(group_id=group_id)["group"]
    assert restored_group["status"] == "running"
    assert restored_group["counts"]["running"] == 2
    assert restored_group["max_concurrency"] == 2

    dependent = second.run_delegate(
        task="dependent writer",
        cwd=tmp_path,
        harness="antigravity",
        kind="code",
        depends_on_group_ids=[group_id],
        wait_seconds=0,
        execution_timeout_seconds=15,
    )
    assert dependent["status"] == "queued"

    (tmp_path / "release-readers").write_text("go", encoding="utf-8")
    deadline = time.monotonic() + 8
    final_group = second.delegate_status(group_id=group_id)["group"]
    while not final_group["completed"] and time.monotonic() < deadline:
        time.sleep(0.05)
        final_group = second.delegate_status(group_id=group_id)["group"]
    assert final_group["status"] == "succeeded"
    terminal_writer = second.delegate_status(
        delegate_id=str(dependent["delegate_id"]),
        watch_seconds=3,
        poll_seconds=0.05,
    )["delegate"]
    assert terminal_writer["status"] == "succeeded"


def test_recovered_durable_batch_restores_terminal_and_running_children(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = tmp_path / "mixed-batch-agent.py"
    script.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "prompt = sys.stdin.read()\n"
        "if 'fast reader' in prompt:\n"
        "    time.sleep(0.05)\n"
        "else:\n"
        "    release = Path(__file__).with_name('release-slow-reader')\n"
        "    while not release.exists():\n"
        "        time.sleep(0.05)\n"
        "print(json.dumps({'status': 'succeeded'}))\n",
        encoding="utf-8",
    )
    command = _agent_command(script)
    first = _registry(tmp_path, command)

    batch = first.run_delegate_batch(
        tasks=[{"task": "fast reader"}, {"task": "slow reader"}],
        cwd=tmp_path,
        harness="antigravity",
        max_concurrency=1,
        wait_seconds=0,
        execution_timeout_seconds=15,
    )
    group_id = str(batch["group_id"])
    child_ids = [str(child["delegate_id"]) for child in batch["children"]]
    fast = first.delegate_status(
        delegate_id=child_ids[0],
        watch_seconds=3,
        poll_seconds=0.05,
    )["delegate"]
    assert fast["status"] == "succeeded"
    _wait_for_durable_job(first, child_ids[1])

    shutdown = first.shutdown(wait_seconds=0)
    assert shutdown["running_preserved"] == 1

    second = _registry(tmp_path, command)
    root = executors._delegate_log_root_for_harness(
        "antigravity",
        state_root=second.delegate_state_root,
    )
    recovered = second.recover_persisted_delegates(roots=[root])

    assert recovered["groups_restored"] == 1
    assert recovered["group_terminal_restored"] == 1
    assert recovered["durable_adopted"] == 1
    restored = second.delegate_status(group_id=group_id)["group"]
    assert restored["max_concurrency"] == 1
    assert restored["counts"]["succeeded"] == 1
    assert restored["counts"]["running"] == 1

    (tmp_path / "release-slow-reader").write_text("go", encoding="utf-8")
    deadline = time.monotonic() + 5
    while not restored["completed"] and time.monotonic() < deadline:
        time.sleep(0.05)
        restored = second.delegate_status(group_id=group_id)["group"]
    assert restored["status"] == "succeeded"
