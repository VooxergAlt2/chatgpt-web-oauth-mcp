from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_tool_budget import AgyToolCallMeter
from chatgpt_web_oauth_mcp.delegate_harnesses import AntigravityHarness, GenericCliHarness, _antigravity_output
from chatgpt_web_oauth_mcp.delegate_process import Invocation
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.shell import JobRegistry


def _step(index: int, state: str, name: str = "read_file") -> str:
    return json.dumps({
        "event": "step_update",
        "step_update": {
            "step_index": index, "state": state, "tool_name": name,
        },
    }) + "\n"


def test_meter_deduplicates_active_done_across_incomplete_chunks(
    tmp_path: Path,
) -> None:
    log = tmp_path / "stdout.log"
    meter = AgyToolCallMeter(limit=2)
    log.write_bytes((_step(1, "ACTIVE")[:-3]).encode("utf-8"))
    assert meter.scan(log) > 0
    assert meter.count == 0
    with log.open("ab") as stream:
        stream.write(
            (_step(1, "ACTIVE")[-3:] +
             _step(1, "DONE") +
             _step(2, "ACTIVE", "run_command") +
             _step(2, "DONE", "run_command") +
             _step(3, "ACTIVE", "finish")).encode("utf-8")
        )
    assert meter.scan(log) > 0
    assert meter.count == 2
    assert not meter.exceeded
    assert meter.snapshot()["tool_call_counts"] == {
        "read_file": 1, "run_command": 1,
    }
    with log.open("ab") as stream:
        stream.write(_step(4, "ACTIVE").encode("utf-8"))
    meter.scan(log)
    assert meter.count == 3
    assert meter.exceeded


def test_meter_fails_closed_on_unidentified_tool_step(tmp_path: Path) -> None:
    log = tmp_path / "stdout.log"
    log.write_text(
        json.dumps({
            "event": "step_update",
            "step_update": {"state": "ACTIVE", "tool_name": "read_file"},
        }) + "\n", encoding="utf-8",
    )
    meter = AgyToolCallMeter(limit=3)
    meter.scan(log)
    assert meter.invalid_stream
    assert meter.exceeded
    assert meter.snapshot()["stream_valid"] is False


def _registry(tmp_path: Path, *, steps: int) -> ExecutorRegistry:
    script = tmp_path / "agent.py"
    script.write_text(
        "import json,sys,time\n"
        f"for i in range({steps}):\n"
        " print(json.dumps({'event':'step_update','step_update':"
        "{'step_index':i,'tool_name':'read_file','state':'ACTIVE'}}),flush=True)\n"
        " time.sleep(0.1)\n"
        "print(json.dumps({'event':'result','result':"
        "{'status':'SUCCESS','structured_output':"
        "{'status':'succeeded','summary':'done'}}}),flush=True)\n",
        encoding="utf-8",
    )
    command = shlex.join([sys.executable, str(script)])
    return ExecutorRegistry(
        codex_command=None,
        default_harness="antigravity",
        harnesses=[GenericCliHarness(
            name="antigravity", command=command,
            explore_command=command,
        )],
        durable_job_registry=JobRegistry(),
        durable_state_dir=tmp_path / "jobs",
        durable_harnesses=("antigravity",),
        delegate_state_root=tmp_path / "delegates",
    )


def _terminal(registry: ExecutorRegistry, delegate_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        result = registry.delegate_status(
            delegate_id=delegate_id, watch_seconds=0.4,
            poll_seconds=0.05,
        )["delegate"]
        if result.get("status") in {"succeeded", "failed", "timed_out", "cancelled"}:
            return result
    raise AssertionError("durable AGY delegate did not reach terminal state")


def test_durable_agy_reader_stops_after_hard_tool_budget(tmp_path: Path) -> None:
    registry = _registry(tmp_path, steps=8)
    response = registry.run_delegate(
        task="Perform bounded read-only inspection", kind="explore",
        harness="antigravity", cwd=tmp_path, max_tool_calls=2,
        execution_timeout_seconds=10, wait_seconds=0,
    )
    assert response["delegate_id"]
    final = _terminal(registry, response["delegate_id"])
    assert final["status"] == "failed"
    assert final["success"] is False
    assert final["error"]["code"] == "delegate_tool_budget_exceeded"
    assert final["tool_budget"]["used"] >= 3
    assert final["tool_budget"]["limit"] == 2
    assert final["durable_termination_reason"] == "tool_budget_exceeded"


def test_durable_agy_reader_finishes_under_budget(tmp_path: Path) -> None:
    registry = _registry(tmp_path, steps=2)
    response = registry.run_delegate(
        task="Inspect two lines", kind="explore",
        harness="antigravity", cwd=tmp_path, max_tool_calls=3,
        execution_timeout_seconds=10, wait_seconds=0,
    )
    final = _terminal(registry, response["delegate_id"])
    assert final["status"] == "succeeded"
    assert final["tool_budget"]["used"] == 2
    assert final["tool_budget"]["limit"] == 3


def test_completed_fast_agy_stream_cannot_bypass_budget(tmp_path: Path) -> None:
    """A terminal job is audited even when its supervisor missed all steps."""
    registry = _registry(tmp_path, steps=0)
    task = registry._make_task(
        harness="antigravity",
        project=registry.project_resolver.resolve(tmp_path), cwd=tmp_path,
        kind="explore", task="check bounded replay", goal=None, task_id=None,
        group_id=None, model="test-model", reasoning_effort="low",
        sandbox_mode="plan+sandbox", commit_mode="forbidden",
        execution_timeout_seconds=60, depends_on_group_ids=(),
        files_in_scope=[], out_of_scope=[], context_files=[],
        acceptance_criteria=[], done_means=[], verification_commands=[],
        output_schema=None, parse_structured_output=True,
        request_fingerprint="fast-complete-budget", max_tool_calls=2,
    )
    task.log_paths.stdout.write_text(
        "".join(_step(i, "ACTIVE") for i in (1, 2, 3))
        + json.dumps({"event": "result", "result": {
            "status": "SUCCESS",
            "structured_output": {"status": "succeeded", "summary": "misleading"},
        }}) + "\n", encoding="utf-8",
    )
    result = registry._finalize_durable_delegate(
        task,
        invocation=Invocation(
            args=["true"], use_shell=False,
            output_parser=_antigravity_output,
        ),
        before_status=None,
        job_status={
            "success": True, "status": "succeeded",
            "exit_code": 0, "elapsed_seconds": 0.1,
        },
        timed_out=False,
    )
    assert result["status"] == "failed"
    assert result["error"]["code"] == "delegate_tool_budget_exceeded"
    assert result["tool_budget"]["used"] == 3
    assert result["structured_output"]["summary"] == "misleading"


def test_scoped_agy_defaults_budget_but_unscoped_and_code_are_unlimited(
    tmp_path: Path, monkeypatch,
) -> None:
    import chatgpt_web_oauth_mcp.executors as module

    source = tmp_path / "service.py"
    source.write_text("def investigate_failure():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(module, "_command_available", lambda _command: True)
    monkeypatch.setattr(
        AntigravityHarness, "info",
        lambda _self: {"available": True, "explore_available": True},
    )
    monkeypatch.setattr(
        AntigravityHarness, "model_admission", lambda _self, _model: (True, "ok"),
    )
    registry = ExecutorRegistry(
        codex_command=None, default_harness="antigravity",
        harnesses=[AntigravityHarness(command="agy")],
        durable_job_registry=JobRegistry(), durable_state_dir=tmp_path / "jobs",
        durable_harnesses=("antigravity",),
        delegate_state_root=tmp_path / "delegates",
    )
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {"success": True, "status": "succeeded",
                "delegate_id": delegate_task.delegate_id}

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    registry.run_delegate(
        task="Check investigate_failure", kind="explore", harness="antigravity",
        cwd=tmp_path, files_in_scope=["service.py"], wait_seconds=2,
    )
    assert captured[-1].max_tool_calls == 24
    assert "Server-enforced AGY research tool-call budget:\n24" in captured[-1].prompt
    assert "service.py:1-2" in captured[-1].prompt

    registry.run_delegate(
        task="Explore the architecture broadly", kind="explore", harness="antigravity",
        cwd=tmp_path, wait_seconds=2,
    )
    assert captured[-1].max_tool_calls is None

    registry.run_delegate(
        task="Implement patch", kind="code", harness="antigravity",
        cwd=tmp_path, files_in_scope=["service.py"], wait_seconds=2,
    )
    assert captured[-1].max_tool_calls is None

    registry.agy_capacity_first = True
    registry.automatic_routing = True
    registry.run_delegate(
        task="Review investigate_failure across modules", kind="explore",
        harness=None, complexity="medium",
        cwd=tmp_path, files_in_scope=["service.py"], wait_seconds=2,
    )
    assert captured[-1].max_tool_calls == 48
    registry.run_delegate(
        task="Deep independent review of investigate_failure", kind="explore",
        harness=None, complexity="high",
        cwd=tmp_path, files_in_scope=["service.py"], wait_seconds=2,
    )
    assert captured[-1].max_tool_calls is None


def test_explicit_agy_budget_refuses_non_durable_runner(tmp_path: Path, monkeypatch) -> None:
    import chatgpt_web_oauth_mcp.executors as module

    monkeypatch.setattr(module, "_command_available", lambda _command: True)
    registry = ExecutorRegistry(
        codex_command=None, default_harness="antigravity",
        harnesses=[AntigravityHarness(command="agy")],
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegates",
    )
    response = registry.run_delegate(
        task="Read one file", kind="explore", harness="antigravity",
        cwd=tmp_path, max_tool_calls=5, wait_seconds=0,
    )
    assert response["success"] is False
    assert response["error"]["code"] == "agy_tool_budget_requires_durable_runner"
    assert not registry.scheduler.tasks


def test_tool_budget_refuses_code_writers(tmp_path: Path) -> None:
    registry = _registry(tmp_path, steps=1)
    code = registry.run_delegate(
        task="Write a patch", kind="code", cwd=tmp_path,
        harness="antigravity", max_tool_calls=3, wait_seconds=0,
    )
    assert code["success"] is False
    assert code["error"]["code"] == "tool_budget_unsupported_for_route"
    assert not registry.scheduler.tasks
