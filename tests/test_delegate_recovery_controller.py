from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.delegate_recovery_policy import plan_same_account_recovery
from chatgpt_web_oauth_mcp.delegate_process import write_private_json
from chatgpt_web_oauth_mcp.delegate_harnesses import AntigravityHarness, _antigravity_output
from chatgpt_web_oauth_mcp.delegate_process import Invocation
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.shell import JobRegistry


CONVERSATION = "123e4567-e89b-12d3-a456-426614174000"


def _task(registry: ExecutorRegistry, cwd: Path, *,
          kind: str = "explore", budget: int | None = 8):
    return registry._make_task(
        harness="antigravity", project=registry.project_resolver.resolve(cwd),
        cwd=cwd, kind=kind, task="Review review.py without writing.",
        goal=None, task_id=None, group_id=None,
        model="gemini-3.8-flash-medium", reasoning_effort="medium",
        sandbox_mode="plan+sandbox", commit_mode="forbidden",
        execution_timeout_seconds=300, depends_on_group_ids=(),
        files_in_scope=["review.py"], out_of_scope=[],
        context_files=[], acceptance_criteria=[],
        done_means=[], verification_commands=[],
        output_schema=None, parse_structured_output=True,
        request_fingerprint=f"recovery-{kind}-{budget}",
        max_tool_calls=budget,
    )


def _failed(*, code: str = "antigravity_stream_truncated",
            budget: int | None = 8, used: int = 2,
            message: str = "Stream ended incompletely") -> dict[str, object]:
    result: dict[str, object] = {
        "success": False, "status": "failed", "durable": True,
        "kind": "explore", "harness": "antigravity",
        "error": {"code": code, "message": message},
        "harness_metadata": {"conversation_id": CONVERSATION},
        "structured_output": None, "duration_seconds": 10.0,
    }
    if budget is not None:
        result["tool_budget"] = {
            "limit": budget, "used": used, "stream_valid": True,
        }
    return result


def test_policy_bounds_time_and_account_without_reducing_threshold(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="true", delegate_state_root=tmp_path / "state",
    )
    task = _task(registry, tmp_path)
    decision = plan_same_account_recovery(
        task=task, result=_failed(), recovery_attempts=0,
    )
    assert decision.should_resume
    assert decision.conversation_id == CONVERSATION
    assert decision.retry_timeout_seconds == 120
    assert decision.tool_call_threshold == 8
    assert decision.initial_tool_calls == 2
    assert plan_same_account_recovery(
        task=task, result=_failed(), recovery_attempts=1,
    ).reason == "recovery_attempt_limit"


@pytest.mark.parametrize(
    "code,message",
    [
        ("delegate_tool_budget_exceeded", "limit reached"),
        ("antigravity_quota_exhausted", "quota"),
        ("delegate_reported_blocked", "sandbox unavailable"),
        ("antigravity_result_error", "location is not supported"),
        ("antigravity_result_error", "permission denied"),
        ("code_scope_violation", "changed unauthorized files"),
        ("timed_out", "user supplied 120 second timeout"),
    ],
)
def test_policy_does_not_retry_unsafe_or_unrelated_failures(
    tmp_path: Path, code: str, message: str,
) -> None:
    registry = ExecutorRegistry(
        codex_command="true", delegate_state_root=tmp_path / "state",
    )
    task = _task(registry, tmp_path)
    assert not plan_same_account_recovery(
        task=task, result=_failed(code=code, message=message),
        recovery_attempts=0,
    ).should_resume


def test_policy_never_retries_code_completed_advisory_or_exhausted_time(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="true", delegate_state_root=tmp_path / "state",
    )
    writer = _task(registry, tmp_path, kind="code")
    assert not plan_same_account_recovery(
        task=writer, result=_failed(), recovery_attempts=0,
    ).should_resume
    reader = _task(registry, tmp_path)
    assert plan_same_account_recovery(
        task=reader,
        result={**_failed(), "structured_output": {"findings": ["advisory"]}},
        recovery_attempts=0,
    ).reason == "has_advisory_result"
    assert plan_same_account_recovery(
        task=reader, result={**_failed(), "duration_seconds": 295.0},
        recovery_attempts=0,
    ).reason == "insufficient_remaining_time"


@pytest.mark.parametrize("used", [7, 8, 20])
def test_policy_allows_resume_at_and_beyond_attention_threshold(
    tmp_path: Path, used: int,
) -> None:
    registry = ExecutorRegistry(codex_command="true", delegate_state_root=tmp_path / "state")
    decision = plan_same_account_recovery(
        task=_task(registry, tmp_path), result=_failed(used=used), recovery_attempts=0,
    )
    assert decision.should_resume
    assert decision.tool_call_threshold == 8
    assert decision.initial_tool_calls == used
    assert decision.retry_timeout_seconds == 120
    assert decision.as_dict()["tool_call_threshold"] == 8
    assert "remaining_tool_calls" not in decision.as_dict()


@pytest.mark.parametrize("budget,reason", [
    (None, "tool_budget_not_verifiable"),
    ({"limit": 8, "used": 9, "stream_valid": False}, "tool_budget_not_verifiable"),
    ({"limit": 8, "used": True, "stream_valid": True}, "tool_budget_inconsistent"),
    ({"limit": 8, "used": -1, "stream_valid": True}, "tool_budget_inconsistent"),
    ({"limit": 7, "used": 9, "stream_valid": True}, "tool_budget_inconsistent"),
])
def test_policy_preserves_observation_verification(
    tmp_path: Path, budget: object, reason: str,
) -> None:
    registry = ExecutorRegistry(codex_command="true", delegate_state_root=tmp_path / "state")
    decision = plan_same_account_recovery(
        task=_task(registry, tmp_path), result={**_failed(), "tool_budget": budget},
        recovery_attempts=0,
    )
    assert not decision.should_resume
    assert decision.reason == reason


def test_advisory_provider_eof_allows_bounded_same_account_resume(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="true", delegate_state_root=tmp_path / "state")
    task = _task(registry, tmp_path, budget=None)
    result = _failed(
        budget=None, code="antigravity_result_error",
        message='API error (attempt 2): request failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF',
    )
    result["structured_output"] = {
        "status": "succeeded", "summary": "Useful but unaccepted",
        "findings": ["review.py:1 existing function"], "blockers": [],
    }
    result["duration_seconds"] = 140.0
    decision = plan_same_account_recovery(
        task=task, result=result, recovery_attempts=0,
    )
    assert decision.should_resume
    assert decision.action == "resume_same_conversation"
    assert decision.conversation_id == CONVERSATION
    assert decision.retry_timeout_seconds == 120
    assert decision.tool_call_threshold is None
    assert not plan_same_account_recovery(
        task=task, result=result, recovery_attempts=1,
    ).should_resume


@pytest.mark.parametrize("message,status,blockers", [
    ("A tool reported an EOF while parsing a user file", "succeeded", []),
    ("API request failed: EOF", "blocked", []),
    ("API request failed: EOF", "succeeded", ["missing tests"]),
    ("User location is not supported; API request failed: EOF", "succeeded", []),
    ("RESOURCE_EXHAUSTED quota, API request failed: EOF", "succeeded", []),
])
def test_advisory_eof_does_not_override_unrelated_or_unsafe_results(
    tmp_path: Path, message: str, status: str, blockers: list[str],
) -> None:
    registry = ExecutorRegistry(codex_command="true", delegate_state_root=tmp_path / "state")
    task = _task(registry, tmp_path, budget=None)
    result = _failed(budget=None, code="antigravity_result_error", message=message)
    result["structured_output"] = {
        "status": status, "summary": "Review", "findings": [],
        "blockers": blockers,
    }
    assert not plan_same_account_recovery(
        task=task, result=result, recovery_attempts=0,
    ).should_resume


def test_controller_preserves_eof_advisory_when_resuming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "review.py").write_text("def review():\n    return 1\n", encoding="utf-8")
    registry = ExecutorRegistry(codex_command="true", delegate_state_root=tmp_path / "state")
    registry.quota_admission_gate = None
    task = _task(registry, tmp_path, budget=None)
    task.state = "running"
    task.durable_job_id = "job_initial_eof"
    attempts: list[tuple[str | None, str]] = []
    advisory = {
        "status": "succeeded", "summary": "Useful but unaccepted",
        "findings": ["review.py:1 verified"], "blockers": [],
    }

    def fake_start(*, delegate_task):
        attempts.append((delegate_task.resume_conversation_id, delegate_task.prompt))
        if len(attempts) == 1:
            result = _failed(
                code="antigravity_result_error", budget=None,
                message='request failed: Post "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF',
            )
            result["structured_output"] = advisory
            result["logs"] = delegate_task.log_paths.as_payload()
            result["duration_seconds"] = 14.0
            registry._process_runner._write_final_metadata(delegate_task, result)
            return result
        return {
            "success": True, "status": "succeeded", "durable": True,
            "kind": "explore", "harness": "antigravity",
            "structured_output": {
                "status": "succeeded", "summary": "Confirmed after EOF",
                "findings": ["review.py:1 is a function"], "blockers": [],
            }, "duration_seconds": 3.0,
            "logs": delegate_task.log_paths.as_payload(),
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    final = registry._run_scheduled_task(task)
    assert len(attempts) == 2
    assert attempts[1][0] == CONVERSATION
    assert "previous response contains advisory findings" in attempts[1][1]
    assert "NOT accepted" in attempts[1][1]
    assert final["status"] == "succeeded"
    assert final["recovery"]["recovered"] is True
    assert final["route_chain"]["attempt_count"] == 2
    assert final["route_chain"]["fallback_count"] == 0
    backup = task.log_paths.log_dir / "recovery-attempt-1-structured-result.json"
    assert backup.is_file()
    assert backup.stat().st_mode & 0o777 == 0o600
    previous = json.loads(backup.read_text(encoding="utf-8"))
    assert previous["structured_output"] == advisory
    saved = task.recovery_attempts[0]
    assert saved["previous_error_code"] == "antigravity_result_error"
    assert saved["previous_structured_result"] == str(backup)
    earlier = json.loads(
        (task.log_paths.log_dir / "recovery-attempt-1-metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == earlier[
        "structured_result_ref"
    ]["sha256"]


def test_controller_resumes_once_preserving_scope_and_metadata(
    tmp_path: Path, monkeypatch,
) -> None:
    source = tmp_path / "review.py"
    source.write_text("def review():\n    return 1\n", encoding="utf-8")
    registry = ExecutorRegistry(
        codex_command="true", delegate_state_root=tmp_path / "state",
    )
    registry.quota_admission_gate = None
    task = _task(registry, tmp_path)
    task.durable_job_id = "job_initial"
    task.state = "running"
    calls = []
    reviewed_scopes = []

    def fake_start(*, delegate_task):
        calls.append({
            "conversation": delegate_task.resume_conversation_id,
            "budget": delegate_task.max_tool_calls,
            "timeout": delegate_task.execution_timeout_seconds,
            "prompt": delegate_task.prompt,
        })
        if len(calls) == 1:
            result = _failed()
            result["logs"] = delegate_task.log_paths.as_payload()
            write_private_json(delegate_task.log_paths.metadata, {
                "delegate_id": delegate_task.delegate_id,
                "status": "failed", "durable": True,
                "harness": "antigravity",
                "logs": delegate_task.log_paths.as_payload(),
                "error": result["error"],
            })
            return result
        return {
            "success": True, "status": "succeeded", "durable": True,
            "kind": "explore", "harness": "antigravity",
            "structured_output": {
                "status": "succeeded", "summary": "Review complete",
                "findings": ["review.py:1 is present"],
            },
            "duration_seconds": 1.0, "logs": delegate_task.log_paths.as_payload(),
        }

    def fake_evidence(*, root, files_in_scope, structured_output):
        reviewed_scopes.extend(files_in_scope)
        return {"status": "all_findings_anchored"}

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.executors.assess_delegate_findings", fake_evidence,
    )
    final = registry._run_scheduled_task(task)
    assert len(calls) == 2
    assert calls[0]["conversation"] is None
    assert calls[1]["conversation"] == CONVERSATION
    assert calls[1]["budget"] == 8
    assert calls[1]["timeout"] == 120
    assert "Tool-call attention threshold: 8" in calls[1]["prompt"]
    assert "Previously observed tool calls: 2" in calls[1]["prompt"]
    assert "Remaining tool-call budget" not in calls[1]["prompt"]
    assert "Original files in scope:\n- review.py" in calls[1]["prompt"]
    assert reviewed_scopes == ["review.py"]
    assert final["status"] == "succeeded"
    assert final["recovery"]["recovered"] is True
    assert final["route_chain"]["attempt_count"] == 2
    assert final["route_chain"]["fallback_count"] == 0
    assert final["route_chain"]["recovery_count"] == 1
    assert task.recovery_attempts[0]["initial_tool_calls"] == 2
    previous = task.log_paths.log_dir / "recovery-attempt-1-metadata.json"
    assert json.loads(previous.read_text(encoding="utf-8"))["status"] == "failed"
    assert previous.stat().st_mode & 0o777 == 0o600
    assert (task.log_paths.log_dir / "recovery-attempt-1-prompt.txt").is_file()


def test_controller_declines_retry_after_fresh_quota_rejection(
    tmp_path: Path, monkeypatch,
) -> None:
    registry = ExecutorRegistry(
        codex_command="true", delegate_state_root=tmp_path / "state",
    )
    class Gate:
        def decision(self, *, harness, model, fresh):
            assert fresh is True
            return {"allowed": False, "reason": "quota_threshold_reached"}
    registry.quota_admission_gate = Gate()
    task = _task(registry, tmp_path)
    task.durable_job_id = "job_initial"
    task.state = "running"
    calls = []
    def fake_start(*, delegate_task):
        calls.append(delegate_task.delegate_id)
        return _failed()
    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry._run_scheduled_task(task)
    assert len(calls) == 1
    assert result["status"] == "failed"
    assert result["recovery"]["reason"] == "fresh_quota_admission_denied"
    assert not task.recovery_attempts


@pytest.mark.parametrize("initial_calls", [1, 2, 5])
def test_controller_replays_real_durable_agy_stream_once(
    tmp_path: Path, monkeypatch, initial_calls: int,
) -> None:
    """Real job supervisor: truncated AGY output -> same-account resume."""
    root = tmp_path / "repo"
    root.mkdir()
    for command in (
        ["init", "-q"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(
            ["git", "-C", str(root), *command], check=True,
            capture_output=True, timeout=10,
        )
    (root / "review.py").write_text("def review():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."],
                   check=True, capture_output=True, timeout=10)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"],
                   check=True, capture_output=True, timeout=10)
    script = tmp_path / "fake_agy.py"
    script.write_text(
        """import json, sys
conversation = "123e4567-e89b-12d3-a456-426614174000"
initial_calls = int(sys.argv[2])
if sys.argv[1] == "initial":
    print(json.dumps({"event": "init", "conversation_id": conversation}), flush=True)
    for index in range(1, initial_calls + 1):
        print(json.dumps({"event": "step_update", "step_update": {
            "conversation_id": conversation, "step_index": index,
            "tool_name": "read_file", "state": "DONE"
        }}), flush=True)
    print('{"event":"result","result":{"status":"SUCCESS",', end="", flush=True)
else:
    for index in range(1, initial_calls + 3):
        print(json.dumps({"event": "step_update", "step_update": {
            "conversation_id": conversation, "step_index": index,
            "tool_name": "read_file", "state": "DONE"
        }}), flush=True)
    print(json.dumps({"event": "result", "result": {
        "conversation_id": conversation, "status": "SUCCESS",
        "structured_output": {
            "status": "succeeded", "summary": "resumed and finished",
            "findings": ["review.py:1 verified"]
        }
    }}), flush=True)
""",
        encoding="utf-8",
    )
    registry = ExecutorRegistry(
        codex_command=None, default_harness="antigravity",
        harnesses=[AntigravityHarness(command="agy")],
        durable_job_registry=JobRegistry(),
        durable_state_dir=tmp_path / "jobs",
        durable_harnesses=("antigravity",),
        delegate_state_root=tmp_path / "delegates",
    )
    registry.quota_admission_gate = None
    monkeypatch.setattr(registry, "_harness_preflight_error", lambda **_k: None)
    monkeypatch.setattr(
        registry, "_build_invocation_for_task",
        lambda delegate: Invocation(
            args=[
                sys.executable, str(script),
                "resumed" if delegate.resume_conversation_id else "initial",
                str(initial_calls),
            ],
            use_shell=False, output_parser=_antigravity_output,
            read_only_enforced=False,
        ),
    )
    task = _task(registry, root, budget=2)
    task.state = "running"
    result = registry._run_scheduled_task(task)
    assert result["status"] == "succeeded"
    assert result["recovery"]["recovered"] is True
    assert result["route_chain"]["attempt_count"] == 2
    assert result["route_chain"]["fallback_count"] == 0
    assert result["route_chain"]["recovery_count"] == 1
    assert task.resume_conversation_id == CONVERSATION
    assert task.max_tool_calls == 2
    assert result["tool_budget"]["used"] == initial_calls + 2
    assert result["tool_budget"]["tool_call_counts"] == {"read_file": initial_calls + 2}
    assert result["tool_budget"]["stream_valid"] is True
    assert result["tool_call_attention"]["threshold"] == 2
    assert result["tool_call_attention"]["observed_calls"] == initial_calls + 2
    assert result["recovery"]["tool_call_threshold"] == 2
    assert result["recovery"]["initial_tool_calls"] == initial_calls
    observation = json.loads(
        (task.log_paths.metadata.parent / "tool-call-attention.json").read_text()
    )
    assert observation["tool_call_attention"] == result["tool_call_attention"]
    assert len(task.recovery_attempts) == 1
    first = json.loads(
        (task.log_paths.log_dir / "recovery-attempt-1-metadata.json").read_text(
            encoding="utf-8",
        )
    )
    assert first["error"]["code"] == "antigravity_stream_truncated"
    final = json.loads(task.log_paths.metadata.read_text(encoding="utf-8"))
    assert final["status"] == "succeeded"
    assert final["recovery"]["attempted"] is True
    assert final["route_chain"]["attempt_count"] == 2
    assert (task.log_paths.log_dir / "recovery-attempt-1-prompt.txt").is_file()


def test_controller_never_loops_after_second_incomplete_output(
    tmp_path: Path, monkeypatch,
) -> None:
    registry = ExecutorRegistry(
        codex_command="true", delegate_state_root=tmp_path / "state",
    )
    registry.quota_admission_gate = None
    task = _task(registry, tmp_path)
    task.state = "running"
    task.durable_job_id = "job_initial"
    calls: list[str | None] = []

    def fake_start(*, delegate_task):
        calls.append(delegate_task.resume_conversation_id)
        failed = _failed(
            budget=8 if len(calls) == 1 else 6,
            used=1,
        )
        failed["logs"] = delegate_task.log_paths.as_payload()
        write_private_json(
            delegate_task.log_paths.metadata,
            {"delegate_id": delegate_task.delegate_id, "status": "failed",
             "error": failed["error"], "durable": True},
        )
        return failed

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    final = registry._run_scheduled_task(task)
    assert len(calls) == 2
    assert calls == [None, CONVERSATION]
    assert final["status"] == "failed"
    assert final["recovery"]["recovered"] is False
    assert final["route_chain"]["recovery_count"] == 1
    assert not final.get("routing_fallback")


def test_recovery_trace_survives_durable_adoption(
    tmp_path: Path,
) -> None:
    registry = ExecutorRegistry(
        codex_command="true", delegate_state_root=tmp_path / "state",
    )
    task = _task(registry, tmp_path, budget=2)
    previous_stdout = tmp_path / "previous-stdout.log"
    def step(index: int) -> str:
        return json.dumps({"event": "step_update", "step_update": {
            "conversation_id": CONVERSATION, "step_index": index,
            "tool_name": "read_file", "state": "DONE",
        }}) + "\n"
    previous_stdout.write_text(step(1) + step(2) + '{"event":"result",')
    task.log_paths.stdout.write_text(step(2) + step(3))
    task.recovery_attempts.append({
        "attempt": 1,
        "action": "resume_same_conversation",
        "previous_error_code": "antigravity_stream_truncated",
        "initial_tool_calls": 2,
        "previous_logs": {"stdout": str(previous_stdout)},
        "tool_call_threshold": 2,
        "original_files_in_scope": ["review.py"],
    })
    task.resume_conversation_id = CONVERSATION
    task.acceptance_criteria = ("Verify review.py:1",)
    task.durable_job_id = "job_recovered"
    registry._write_durable_delegate_metadata(
        task,
        invocation=Invocation(args=["true"], use_shell=False),
        before_status=None, job_status={"status": "running"},
    )
    payload = json.loads(task.log_paths.metadata.read_text(encoding="utf-8"))
    restored = registry._task_from_persisted_durable(
        payload, task.log_paths.metadata,
    )
    assert restored is not None
    assert restored.resume_conversation_id == CONVERSATION
    assert restored.recovery_attempts == task.recovery_attempts
    assert restored.acceptance_criteria == ("Verify review.py:1",)
    active = registry._task_snapshot(restored)
    assert active["recovery"]["in_progress"] is True
    assert active["recovery"]["attempt_count"] == 1
    meter = registry._agy_tool_budget_meter(restored)
    assert meter is not None
    assert meter.count == 2
    assert meter.attention()["observed_calls"] == 2
    while meter.scan(restored.log_paths.stdout):
        pass
    assert meter.count == 3
    assert meter.invalid_stream is False
    registry._persist_tool_call_attention(restored, meter)
    observation = json.loads(
        (task.log_paths.metadata.parent / "tool-call-attention.json").read_text()
    )
    assert observation["tool_call_attention"]["observed_calls"] == 3
    assert observation["tool_call_attention"]["threshold"] == 2


@pytest.mark.parametrize("missing", [False, True])
def test_recovery_meter_rejects_unverifiable_previous_observations(
    tmp_path: Path, missing: bool,
) -> None:
    registry = ExecutorRegistry(codex_command="true", delegate_state_root=tmp_path / "state")
    task = _task(registry, tmp_path)
    previous_stdout = tmp_path / "previous-stdout.log"
    if not missing:
        previous_stdout.write_text("")
    task.recovery_attempts.append({
        "initial_tool_calls": 2,
        "previous_logs": {"stdout": str(previous_stdout)},
    })
    meter = registry._agy_tool_budget_meter(task)
    assert meter is not None
    assert meter.invalid_stream is True
    assert meter.snapshot()["stream_valid"] is False


def test_resumed_private_agy_conversation_never_routes_to_other_account(
    tmp_path: Path,
) -> None:
    registry = ExecutorRegistry(
        codex_command="true", delegate_state_root=tmp_path / "state",
    )
    task = _task(registry, tmp_path)
    task.routing_mode = "automatic"
    task.resume_conversation_id = CONVERSATION
    task.recovery_attempts.append({"attempt": 1, "action": "resume_same_conversation"})
    result = _failed(
        code="antigravity_result_error",
        message="The stream was interrupted. Please continue.",
    )
    assert registry._runtime_retry_reason(task=task, result=result) is None
