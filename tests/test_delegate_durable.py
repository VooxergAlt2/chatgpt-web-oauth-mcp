from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

import chatgpt_web_oauth_mcp.executors as executors
from chatgpt_web_oauth_mcp.delegate_harnesses import GenericCliHarness
from chatgpt_web_oauth_mcp.delegate_process import Invocation
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


def test_bwrap_readonly_durable_job_preserves_stdout(tmp_path: Path, monkeypatch) -> None:
    import pytest

    bwrap = shutil.which("bwrap") if os.name == "posix" else None
    if not bwrap:
        pytest.skip("bubblewrap unavailable")
    registry = _registry(tmp_path, "true")
    agent = [
        bwrap, "--die-with-parent", "--bind", "/", "/",
        "--ro-bind", str(tmp_path), str(tmp_path),
        "--", sys.executable, "-c",
        "import sys; sys.stdin.read(); print('{\"status\":\"succeeded\",\"summary\":\"isolated\"}', flush=True)",
    ]
    monkeypatch.setattr(
        registry,
        "_build_invocation_for_task",
        lambda task: Invocation(
            args=agent,
            use_shell=False,
            stdin=task.prompt.encode("utf-8"),
            read_only_enforced=True,
        ),
    )
    result = registry.run_delegate(
        task="inspect", cwd=tmp_path, kind="explore",
        harness="antigravity", wait_seconds=15,
        execution_timeout_seconds=12,
    )
    assert result["status"] == "succeeded", result
    assert result["structured_output"]["summary"] == "isolated"


def test_verified_code_baseline_is_persisted_before_durable_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path, "true")
    baseline = {
        "head": "a" * 40,
        "captured_clean": True,
        "captured_at_execution_start": True,
    }
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        executors,
        "capture_code_baseline",
        lambda _task: (dict(baseline), None),
    )

    def fake_durable(task):
        metadata = json.loads(task.log_paths.metadata.read_text(encoding="utf-8"))
        observed["metadata"] = metadata
        return registry._process_runner._result(
            task,
            status="succeeded",
            exit_code=0,
            error=None,
            structured_output=None,
            duration_seconds=0.0,
        )

    monkeypatch.setattr(registry, "_run_durable_delegate", fake_durable)

    result = registry.run_delegate(
        task="verified durable implementation",
        cwd=tmp_path,
        harness="antigravity",
        kind="code",
        files_in_scope=["src/module.py"],
        commit_mode="forbidden",
        wait_seconds=2,
    )

    assert result["status"] == "succeeded"
    metadata = observed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["code_baseline"] == baseline
    assert metadata["baseline_persisted_before_execution"] is True
    assert isinstance(metadata["code_contract"], dict)


def test_verified_code_does_not_start_durable_job_when_baseline_persist_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path, "true")
    baseline = {
        "head": "b" * 40,
        "captured_clean": True,
        "captured_at_execution_start": True,
    }
    monkeypatch.setattr(
        executors,
        "capture_code_baseline",
        lambda _task: (dict(baseline), None),
    )
    monkeypatch.setattr(
        registry,
        "_persist_code_baseline",
        lambda _task: {
            "code": "code_verification_baseline_persist_failed",
            "message": "synthetic persistence failure",
        },
    )

    def must_not_start(_task):
        raise AssertionError("durable job must not start without a persisted baseline")

    monkeypatch.setattr(registry, "_run_durable_delegate", must_not_start)

    result = registry.run_delegate(
        task="verified durable implementation",
        cwd=tmp_path,
        harness="antigravity",
        kind="code",
        files_in_scope=["src/module.py"],
        commit_mode="forbidden",
        wait_seconds=2,
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "code_verification_baseline_persist_failed"
    assert result["code_verification"]["phase"] == "baseline_persist"


def test_cancelled_durable_delegate_does_not_start_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = _registry(tmp_path, "true")
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="antigravity",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="cancel before durable launch",
        goal=None,
        task_id=None,
        group_id=None,
        model="test-model",
        reasoning_effort="low",
        sandbox_mode="plan+sandbox",
        commit_mode="forbidden",
        execution_timeout_seconds=30,
        depends_on_group_ids=(),
        files_in_scope=[],
        out_of_scope=[],
        context_files=[],
        acceptance_criteria=[],
        done_means=[],
        verification_commands=[],
        output_schema=None,
        parse_structured_output=True,
        request_fingerprint="cancel-before-durable-launch",
    )
    task.cancel_requested = True
    task.cancel_reason = "explicit_cancel"
    task.cancel_source = "explicit_delegate_cancel"
    task.cancel_rationale = "cancel before durable launch"
    start_calls: list[object] = []

    def forbidden_start_job(**_kwargs):
        start_calls.append(object())
        raise AssertionError("cancelled delegate must not start a durable job")

    assert registry.durable_job_registry is not None
    monkeypatch.setattr(registry.durable_job_registry, "start_job", forbidden_start_job)

    result = registry._run_durable_delegate(task)

    assert result["status"] == "cancelled"
    assert result["error"]["code"] == "explicit_cancel"
    assert result["cancel_source"] == "explicit_delegate_cancel"
    assert result["cancel_rationale"] == "cancel before durable launch"
    assert start_calls == []
    assert task.durable_job_id is None


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


def test_durable_low_confidence_annotations_are_persisted_before_recovery(
    tmp_path: Path,
) -> None:
    script = tmp_path / "low-confidence-agent.py"
    script.write_text(
        "import json, sys\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'status': 'succeeded', 'confidence': 0.3}))\n",
        encoding="utf-8",
    )
    registry = _registry(tmp_path, _agent_command(script))

    result = registry.run_delegate(
        task="durable low confidence review",
        cwd=tmp_path,
        harness="antigravity",
        kind="explore",
        complexity="low",
        wait_seconds=5,
        execution_timeout_seconds=10,
        parse_structured_output=True,
    )

    assert result["status"] == "succeeded"
    assert result["failure_analysis"]["failure_type"] == "low_confidence"
    assert result["replan"]["action"] == "add_reviewer"
    delegate_id = str(result["delegate_id"])
    task = registry.scheduler.get_task(delegate_id)
    assert task is not None
    persisted = json.loads(task.log_paths.metadata.read_text(encoding="utf-8"))
    assert persisted["failure_analysis"]["failure_type"] == "low_confidence"
    assert persisted["replan"]["action"] == "add_reviewer"


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

    protected = second.delegate_cancel(delegate_id=delegate_id)
    assert protected["success"] is False
    assert protected["error"]["code"] == "delegate_cancel_requires_force"
    cancelled = second.delegate_cancel(
        delegate_id=delegate_id,
        rationale="operator cleanup after restart",
        force_running=True,
    )["delegate"]

    assert cancelled["status"] == "cancelled"
    assert cancelled["completed"] is True
    assert cancelled["error"]["code"] == "explicit_cancel"
    assert cancelled["cancel_source"] == "explicit_delegate_cancel"
    assert cancelled["cancel_rationale"] == "operator cleanup after restart"
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

    second.delegate_cancel(delegate_id=first_id, force_running=True)
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


def test_recovered_planner_split_restores_plan_and_synthesis_context(
    tmp_path: Path,
) -> None:
    script = tmp_path / "planner-split-agent.py"
    script.write_text(
        "import json, time\n"
        "from pathlib import Path\n"
        "release = Path(__file__).with_name('release-planner-split')\n"
        "while not release.exists():\n"
        "    time.sleep(0.05)\n"
        "print(json.dumps({'status':'succeeded','findings':[]}))\n",
        encoding="utf-8",
    )
    command = _agent_command(script)
    first = _registry(tmp_path, command)
    plan_id = "plan-recovery-0001"
    planner_plan = {
        "mode": "active",
        "plan_kind": "split",
        "authority": "planner",
        "plan_id": plan_id,
        "parent_plan_id": None,
        "node_id": None,
        "depends_on_node_ids": [],
        "split_plan": {
            "executable": True,
            "reason": "bounded_explicit_file_scope",
            "target_harness": "antigravity",
            "max_concurrency": 2,
            "shards": [],
            "synthesis_contract": "deterministic_findings_union_v1",
        },
    }

    tasks = []
    for index in range(2):
        child_plan = dict(planner_plan)
        child_plan["node_id"] = f"shard-{index + 1:02d}"
        tasks.append(
            {
                "task": f"planner shard {index + 1}",
                "task_id": f"shard-{index + 1:02d}",
                "route_decision": {
                    "legacy_complexity": "low",
                    "task_shape_hash": f"shape-{index}",
                    "task_shape": {"purpose": "review", "breadth": "small"},
                    "initial_route": "antigravity:explore:claude-opus-4-6-thinking:none",
                    "shadow_route": "antigravity:explore:claude-opus-4-6-thinking:none",
                    "planner": child_plan,
                },
            }
        )

    batch = first.run_delegate_batch(
        tasks=tasks,
        cwd=tmp_path,
        harness="antigravity",
        model="claude-opus-4-6-thinking",
        max_concurrency=2,
        wait_seconds=0,
        execution_timeout_seconds=15,
        planner_plan=planner_plan,
    )
    group_id = str(batch["group_id"])
    child_ids = [str(child["delegate_id"]) for child in batch["children"]]
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
    assert recovered["groups_restored"] == 1

    restored = second.delegate_status(group_id=group_id)["group"]
    assert restored["planner_plan"]["plan_id"] == plan_id
    assert restored["planner_plan"]["plan_kind"] == "split"
    assert restored["planner_plan"]["node_id"] is None

    (tmp_path / "release-planner-split").write_text("go", encoding="utf-8")
    deadline = time.monotonic() + 5
    while not restored["completed"] and time.monotonic() < deadline:
        time.sleep(0.05)
        restored = second.delegate_status(group_id=group_id)["group"]
    assert restored["status"] == "succeeded"
    assert (
        restored["planner_synthesis"]["contract"]
        == "deterministic_findings_union_v1"
    )
