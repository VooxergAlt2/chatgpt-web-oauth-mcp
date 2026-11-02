"""Existing scheduler/durable-runner acceptance seam, without a real LLM."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys

import pytest

from chatgpt_web_oauth_mcp.acp_result import parse_acp_worker_output
from chatgpt_web_oauth_mcp.delegate_harnesses import HarnessTaskDefaults
from chatgpt_web_oauth_mcp.delegate_process import Invocation
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.shell import JobRegistry


class FixtureAcpHarness:
    name = "acp_fixture"
    display_name = "ACP test boundary"

    def __init__(self, script: Path):
        self.script = script
        self.command = shlex.join([sys.executable, str(script)])

    def task_defaults(self, kind):
        return HarnessTaskDefaults("gemini-3.8-flash", "low", "read-only")

    def command_for(self, kind):
        return self.command if kind == "explore" else None

    def supports_read_only(self):
        return True

    def info(self):
        return {"available": True, "explore_available": True, "read_only_supported": True}

    def build_invocation(self, task):
        return Invocation(
            args=[sys.executable, str(self.script)], use_shell=False,
            stdin=task.prompt.encode(), read_only_enforced=True,
            output_parser=parse_acp_worker_output,
        )


@pytest.mark.parametrize("failed", [False, True])
def test_acp_envelope_reaches_existing_durable_artifacts_without_false_acceptance(
    tmp_path: Path, failed: bool,
) -> None:
    (tmp_path / "sample.py").write_text("def sample():\n    return 1\n")
    report = {"status": "succeeded", "summary": "source inspected", "findings": ["sample.py:1 defines sample"]}
    outcome = {
        "success": not failed,
        "status": "failed" if failed else "succeeded",
        "answer": json.dumps(report),
        "harness_metadata": {"conversation_id": "qa-session", "tool_calls": 1},
        "session_ref": "b" * 32, "turn_id": "review",
        "outcome_uncertain": failed,
    }
    if failed:
        outcome["error"] = {"code": "acp_transport_error"}
    envelope = json.dumps({"event": "acp_result", "result": outcome})
    script = tmp_path / "fake_acp_worker.py"
    script.write_text(f"import sys\nsys.stdin.read()\nprint({envelope!r})\n")
    registry = ExecutorRegistry(
        codex_command=None, default_harness="acp_fixture",
        harnesses=[FixtureAcpHarness(script)],
        durable_job_registry=JobRegistry(), durable_state_dir=tmp_path / "jobs",
        durable_harnesses=("acp_fixture",), delegate_state_root=tmp_path / "delegates",
    )
    started = registry.run_delegate(
        task="Read sample.py and report a source-grounded finding",
        kind="explore", harness="acp_fixture", cwd=tmp_path,
        files_in_scope=["sample.py"], wait_seconds=0,
        execution_timeout_seconds=10,
    )
    task = registry.scheduler.get_task(started["delegate_id"])
    assert task is not None and task.completed_event.wait(10)
    final = registry.delegate_status(delegate_id=task.delegate_id)["delegate"]
    assert final["status"] == ("failed" if failed else "succeeded")
    assert final["acceptance_status"] != "accepted"
    assert final["structured_output"] == report
    assert final["harness_metadata"]["transport"] == "acp"
    assert final["structured_result_ref"]["sha256"]
    if failed:
        assert final["error"]["code"] == "acp_transport_error"
