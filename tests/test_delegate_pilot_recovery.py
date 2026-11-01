from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from chatgpt_web_oauth_mcp.delegate_code_verification import (
    build_code_contract,
    capture_code_baseline,
    verify_code_result,
)
from chatgpt_web_oauth_mcp.delegate_harnesses import AntigravityHarness, _antigravity_output
from chatgpt_web_oauth_mcp.delegate_process import Invocation
from chatgpt_web_oauth_mcp.delegate_recovery_policy import (
    plan_same_account_recovery,
    plan_verified_code_repair,
)
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.shell import JobRegistry


CONVERSATION = "123e4567-e89b-12d3-a456-426614174000"


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "qa@example.invalid"),
        ("config", "user.name", "QA"),
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True, timeout=10)
    (root / "calc.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."],
                   check=True, capture_output=True, timeout=10)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"],
                   check=True, capture_output=True, timeout=10)
    return root


def registry(tmp_path: Path) -> ExecutorRegistry:
    reg = ExecutorRegistry(
        codex_command=None,
        default_harness="antigravity",
        harnesses=[AntigravityHarness(command="agy")],
        durable_job_registry=JobRegistry(),
        durable_state_dir=tmp_path / "jobs",
        durable_harnesses=("antigravity",),
        delegate_state_root=tmp_path / "delegates",
    )
    reg.quota_admission_gate = None
    return reg


def code_task(reg: ExecutorRegistry, cwd: Path):
    checks = [
        f"{shlex.quote(sys.executable)} -c "
        "\"from pathlib import Path; assert 'return 2' in Path('calc.py').read_text()\""
    ]
    contract = build_code_contract(
        files_in_scope=["calc.py"],
        acceptance_criteria=["calc.value returns 2"],
        done_means=["server test passes"],
        verification_commands=checks,
        max_changed_files=1,
        max_added_lines=4,
        max_deleted_lines=4,
    )
    assert contract
    return reg._make_task(
        harness="antigravity", project=reg.project_resolver.resolve(cwd),
        cwd=cwd, kind="code", task="Change calc.value to return 2.",
        goal=None, task_id=None, group_id=None,
        model="gemini-3.8-flash-medium", reasoning_effort="medium",
        sandbox_mode="accept-edits", commit_mode="forbidden",
        execution_timeout_seconds=240, depends_on_group_ids=(),
        files_in_scope=["calc.py"], out_of_scope=[],
        context_files=[], acceptance_criteria=["calc.value returns 2"],
        done_means=["server test passes"], verification_commands=checks,
        output_schema=None, parse_structured_output=True,
        request_fingerprint="verified-repair", code_contract=contract,
    )


def test_blocked_git_review_retries_only_with_existing_scoped_packet(
    tmp_path: Path,
) -> None:
    root = repo(tmp_path)
    reg = registry(tmp_path)
    (root / "calc.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    task = reg._make_task(
        harness="antigravity", project=reg.project_resolver.resolve(root),
        cwd=root, kind="explore", task="Review git diff -- calc.py",
        goal=None, task_id=None, group_id=None,
        model="gemini-3.8-flash-medium", reasoning_effort="medium",
        sandbox_mode="plan+sandbox", commit_mode="forbidden",
        execution_timeout_seconds=180, depends_on_group_ids=(),
        files_in_scope=["calc.py"], out_of_scope=[],
        context_files=[], acceptance_criteria=[],
        done_means=[], verification_commands=[],
        output_schema=None, parse_structured_output=True,
        request_fingerprint="blocked-packet", max_tool_calls=8,
    )
    assert "BEGIN SCOPED DIFF" in task.prompt
    blocked = {
        "status": "failed", "success": False,
        "error": {"code": "delegate_reported_blocked", "message": "blocked"},
        "harness_metadata": {"conversation_id": CONVERSATION},
        "structured_output": {
            "status": "blocked",
            "blockers": [
                "run_command failed (fork/exec operation not permitted), "
                "preventing execution of git diff commands"
            ],
        },
        "duration_seconds": 10.0,
        "tool_budget": {"limit": 8, "used": 2, "stream_valid": True},
    }
    decision = plan_same_account_recovery(
        task=task, result=blocked, recovery_attempts=0,
    )
    assert decision.should_resume and decision.action == "resume_with_git_evidence"
    task.prompt = task.prompt.replace("BEGIN SCOPED DIFF", "OMITTED")
    assert not plan_same_account_recovery(
        task=task, result=blocked, recovery_attempts=0,
    ).should_resume
    task.prompt += "\nBEGIN SCOPED DIFF\n"
    blocked["structured_output"]["blockers"].append("Test environment unavailable")
    assert not plan_same_account_recovery(
        task=task, result=blocked, recovery_attempts=0,
    ).should_resume


def test_code_repair_policy_checks_authoritative_failed_tests(
    tmp_path: Path,
) -> None:
    root = repo(tmp_path)
    reg = registry(tmp_path)
    task = code_task(reg, root)
    baseline, error = capture_code_baseline(task)
    assert error is None and baseline
    task.code_baseline = baseline
    (root / "calc.py").write_text("def value():\n    return 3\n", encoding="utf-8")
    verification, err = verify_code_result(task, baseline=baseline)
    assert err is not None and err["code"] == "code_verification_failed"
    result = {
        "status": "failed", "error": err,
        "harness_metadata": {"conversation_id": CONVERSATION},
        "code_verification": verification, "duration_seconds": 2.0,
    }
    choice = plan_verified_code_repair(
        task=task, result=result, recovery_attempts=0,
    )
    assert choice.action == "repair_code_and_reverify"
    assert choice.retry_timeout_seconds == 120
    assert not plan_verified_code_repair(
        task=task, result=result, recovery_attempts=1,
    ).should_resume
    for code in ("code_scope_violation", "code_change_budget_exceeded",
                 "code_verification_result_mutated", "timed_out"):
        assert not plan_verified_code_repair(
            task=task, result={**result, "error": {"code": code}},
            recovery_attempts=0,
        ).should_resume
    unsafe = {**verification, "scope_violations": ["other.py"]}
    assert not plan_verified_code_repair(
        task=task, result={**result, "code_verification": unsafe},
        recovery_attempts=0,
    ).should_resume
    unreliable = {**verification, "verification_commands": [
        {**row, "timed_out": True} for row in verification["verification_commands"]
    ]}
    assert not plan_verified_code_repair(
        task=task, result={**result, "code_verification": unreliable},
        recovery_attempts=0,
    ).should_resume


def test_blocked_git_recovery_rejects_stale_snapshot(tmp_path: Path) -> None:
    root = repo(tmp_path)
    (root / "calc.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    reg = registry(tmp_path)
    task = reg._make_task(
        harness="antigravity", project=reg.project_resolver.resolve(root),
        cwd=root, kind="explore", task="Review git diff -- calc.py",
        goal=None, task_id=None, group_id=None,
        model="gemini-3.8-flash-medium", reasoning_effort="medium",
        sandbox_mode="plan+sandbox", commit_mode="forbidden",
        execution_timeout_seconds=180, depends_on_group_ids=(),
        files_in_scope=["calc.py"], out_of_scope=[],
        context_files=[], acceptance_criteria=[],
        done_means=[], verification_commands=[], output_schema=None,
        parse_structured_output=True, request_fingerprint="stale-git",
        max_tool_calls=8,
    )
    assert "BEGIN SCOPED DIFF" in task.prompt
    task.durable_job_id = "job_stale"
    (root / "calc.py").write_text("def value():\n    return 9\n", encoding="utf-8")
    previous = {
        "durable": True, "status": "failed", "success": False,
        "error": {"code": "delegate_reported_blocked", "message": "blocked"},
        "harness_metadata": {"conversation_id": CONVERSATION},
        "structured_output": {"status": "blocked", "blockers": [
            "run_command fork/exec operation not permitted for git diff"
        ]},
        "duration_seconds": 1.0,
        "tool_budget": {"used": 1, "limit": 8, "stream_valid": True},
    }
    assert reg._run_same_account_recovery(task=task, failed_result=previous) is None
    assert previous["recovery"]["reason"] == "git_evidence_stale_or_unavailable"
    assert not task.recovery_attempts


def test_writer_recovery_rejects_unattributed_changes(tmp_path: Path) -> None:
    root = repo(tmp_path)
    reg = registry(tmp_path)
    task = code_task(reg, root)
    task.state = "running"
    task.durable_job_id = "job_unsafe"
    baseline, error = capture_code_baseline(task)
    assert error is None and baseline is not None
    task.code_baseline = baseline
    (root / "calc.py").write_text("def value():\n    return 3\n", encoding="utf-8")
    verification, err = verify_code_result(task, baseline=baseline)
    assert err and err["code"] == "code_verification_failed"
    (root / "calc.py").write_text("def value():\n    return 9\n", encoding="utf-8")
    failed = {
        "durable": True, "status": "failed", "error": err,
        "harness_metadata": {"conversation_id": CONVERSATION},
        "code_verification": verification, "duration_seconds": 1.0,
    }
    assert reg._run_same_account_recovery(task=task, failed_result=failed) is None
    assert failed["recovery"]["reason"] == "code_candidate_changed_before_repair"
    assert not task.recovery_attempts


@pytest.mark.parametrize(
    ("kind", "unsafe_retry"),
    [("explore", False), ("code", False), ("code", True)],
)
def test_pilot_durable_two_stage_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    kind: str, unsafe_retry: bool,
) -> None:
    root = repo(tmp_path)
    reg = registry(tmp_path)
    if kind == "explore":
        (root / "calc.py").write_text("def value():\n    return 2\n", encoding="utf-8")
        task = reg._make_task(
            harness="antigravity", project=reg.project_resolver.resolve(root),
            cwd=root, kind="explore", task="Review git diff -- calc.py",
            goal=None, task_id=None, group_id=None,
            model="gemini-3.8-flash-medium", reasoning_effort="medium",
            sandbox_mode="plan+sandbox", commit_mode="forbidden",
            execution_timeout_seconds=180, depends_on_group_ids=(),
            files_in_scope=["calc.py"], out_of_scope=[],
            context_files=[], acceptance_criteria=[],
            done_means=[], verification_commands=[],
            output_schema=None, parse_structured_output=True,
            request_fingerprint="blocked-live-pilot", max_tool_calls=8,
        )
        assert "BEGIN SCOPED DIFF" in task.prompt
    else:
        task = code_task(reg, root)

    script = tmp_path / "fake_agy.py"
    script.write_text(
        """import json, pathlib, sys
conversation = "123e4567-e89b-12d3-a456-426614174000"
kind, stage, unsafe = sys.argv[1:]
if kind == "code":
    pathlib.Path("calc.py").write_text(
        "def value():\\n    return %s\\n" % ("3" if stage == "first" else "2")
    )
    if stage != "first" and unsafe == "true":
        pathlib.Path("unauthorized.py").write_text("outside scope\\n")
if kind == "code" and stage == "first":
    manifest = {
        "status": "succeeded", "summary": "first patch awaits server checks",
        "blockers": [], "findings": ["calc.py:2 needs verification"]
    }
elif kind == "explore" and stage == "first":
    manifest = {
        "status": "blocked", "summary": "git command sandbox unavailable",
        "blockers": ["run_command failed: fork/exec operation not permitted "
                     "prevented git diff commands"],
        "findings": []
    }
else:
    manifest = {
        "status": "succeeded", "summary": "finished after bounded correction",
        "blockers": [], "findings": ["calc.py:2 verified"]
    }
print(json.dumps({"event": "result", "result": {
    "status": "SUCCESS", "conversation_id": conversation,
    "structured_output": manifest
}}), flush=True)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(reg, "_harness_preflight_error", lambda **_kwargs: None)
    monkeypatch.setattr(
        reg, "_build_invocation_for_task",
        lambda delegated: Invocation(
            args=[sys.executable, str(script), kind,
                  "retry" if delegated.resume_conversation_id else "first",
                  "true" if unsafe_retry else "false"],
            use_shell=False, output_parser=_antigravity_output,
            read_only_enforced=False,
        ),
    )
    task.state = "running"
    result = reg._run_scheduled_task(task)
    assert result["status"] == ("failed" if unsafe_retry else "succeeded"), result.get("error")
    assert result["recovery"]["recovered"] is (not unsafe_retry)
    if unsafe_retry:
        assert result["error"]["code"] == "code_scope_violation"
    assert result["recovery"]["action"] == (
        "resume_with_git_evidence" if kind == "explore"
        else "repair_code_and_reverify"
    )
    assert result["route_chain"]["attempt_count"] == 2
    assert result["route_chain"]["recovery_count"] == 1
    assert result["route_chain"]["fallback_count"] == 0
    assert task.resume_conversation_id == CONVERSATION
    assert len(task.recovery_attempts) == 1
    first = task.log_paths.log_dir / "recovery-attempt-1-structured-result.json"
    assert first.is_file()
    assert first.stat().st_mode & 0o777 == 0o600
    first_answer = json.loads(first.read_text(encoding="utf-8"))["structured_output"]
    assert first_answer["status"] == (
        "blocked" if kind == "explore" else "succeeded"
    )
    new_ref = result["structured_result_ref"]
    current = task.log_paths.log_dir / "structured-result.json"
    assert hashlib.sha256(current.read_bytes()).hexdigest() == new_ref["sha256"]
    assert first.read_bytes() != current.read_bytes()
    if kind == "code":
        if unsafe_retry:
            assert result["code_verification"]["verified"] is False
            assert result["code_verification"]["scope_violations"] == ["unauthorized.py"]
        else:
            assert result["code_verification"]["verified"] is True
            assert result["code_verification"]["verification_level"] == "checks_passed"
        assert "return 2" in (root / "calc.py").read_text(encoding="utf-8")
        assert task.code_baseline["captured_clean"] is True
    else:
        assert result["research_evidence"]["status"] == "all_findings_anchored"
        assert result["tool_budget"]["limit"] == 8
        assert "return 2" in (root / "calc.py").read_text(encoding="utf-8")
