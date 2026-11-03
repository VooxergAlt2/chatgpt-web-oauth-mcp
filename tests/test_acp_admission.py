"""Admission tests for public ACP writer boundaries and linked worktree allowlists."""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from test_acp_backend import registry


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize(("changes", "code"), [
    ({"files_in_scope": None}, "acp_writer_explicit_paths_required"),
    ({"files_in_scope": ["*.py"]}, "acp_writer_invalid_path"),
    ({"files_in_scope": ["../escape.py"]}, "acp_writer_invalid_path"),
    ({"verification_commands": []}, "acp_writer_verification_required"),
    ({"verification_commands": [""]}, "acp_writer_verification_required"),
    ({"commit_mode": "allowed"}, "acp_writer_commit_forbidden"),
    ({"execution_timeout_seconds": 20}, "acp_writer_timeout_too_short"),
    ({}, "acp_writer_dedicated_worktree_required"),
])
def test_writer_rejected_before_scheduling_without_machine_checkable_boundary(tmp_path, changes, code):
    scheduler, workspace = registry(tmp_path)
    response = scheduler.run_delegate(**{
        "task": "Change exactly sample.py", "cwd": workspace, "harness": "antigravity",
        "backend": "acp", "kind": "code", "files_in_scope": ["sample.py"],
        "verification_commands": ["/usr/bin/python3 -B -c 'assert True'"],
        "execution_timeout_seconds": 60, "wait_seconds": 0, **changes,
    })
    assert response["success"] is False
    assert response["error"]["code"] == code
    assert not scheduler.scheduler.tasks


@pytest.mark.parametrize("complexity", ["low", "medium", "high"])
def test_registered_linked_writer_uses_same_account_and_bounded_durable_protocol(tmp_path, monkeypatch, complexity):
    scheduler, main = registry(tmp_path)
    git(main, "init", "-q")
    git(main, "add", "sample.py")
    git(main, "-c", "user.name=QA", "-c", "user.email=qa@invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "--detach", "-q", str(linked))
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git(unrelated, "init", "-q")
    assert scheduler.acp_backend.workspace_allowed("antigravity", linked)
    assert not scheduler.acp_backend.workspace_allowed("antigravity", unrelated)

    observed = []
    def execute(*, delegate_task):
        observed.append(delegate_task)
        return {"success": True, "status": "succeeded", "execution_backend": "acp"}
    monkeypatch.setattr(scheduler, "_start_codex_delegate_impl", execute)
    response = scheduler.run_delegate(
        task="Change sample.py in the isolated writer", cwd=linked, harness="antigravity",
        backend="acp", kind="code", files_in_scope=["sample.py"],
        verification_commands=["/usr/bin/python3 -B -c 'assert True'"],
        logical_session_id="qa-writer-owner", wait_seconds=0, complexity=complexity,
    )
    assert response.get("delegate_id"), json.dumps(response, indent=2)
    task = scheduler.scheduler.get_task(response["delegate_id"])
    assert task.completed_event.wait(5)
    assert observed == [task]
    assert task.execution_backend == "acp" and task.harness == "antigravity"
    level = "medium" if complexity == "low" else "high"
    assert task.model == f"gemini-3.8-flash-{level}"
    assert task.reasoning_effort == level
    assert task.execution_timeout_seconds <= 900
    assert task.acp_resume_context["session_ref"]
    assert task.acp_scope["files_in_scope"] == ["sample.py"]
    invocation = scheduler._build_invocation_for_task(task)
    request = json.loads(invocation.stdin)
    assert request["kind"] == "code"
    assert request["code_contract"]["verification_commands"]
    assert request["scope"] == task.acp_scope
    assert request["session_ref"] == task.acp_resume_context["session_ref"]
    assert invocation.read_only_enforced is False
    assert "private copy" in task.prompt
    assert "independently run the declared verification commands" in task.prompt


def capture_dispatch(scheduler, monkeypatch):
    observed = []
    def execute(*, delegate_task):
        observed.append(delegate_task)
        return {"success": True, "status": "succeeded", "execution_backend": "acp"}
    monkeypatch.setattr(scheduler, "_start_codex_delegate_impl", execute)
    return observed


@pytest.mark.parametrize("complexity", ["medium", "high"])
@pytest.mark.parametrize("model", [None, "gemini-3.8-flash", "gemini-3.8-flash-high"])
@pytest.mark.parametrize("effort", [None, "high"])
def test_acp_complexity_uses_flash_high_on_the_pinned_account(tmp_path, monkeypatch, complexity, model, effort):
    scheduler, workspace = registry(tmp_path)
    observed = capture_dispatch(scheduler, monkeypatch)
    response = scheduler.run_delegate(
        task="Inspect sample.py", cwd=workspace, harness="antigravity", backend="acp",
        kind="explore", complexity=complexity, model=model, reasoning_effort=effort, wait_seconds=0,
    )
    assert response.get("delegate_id"), response
    task = scheduler.scheduler.get_task(response["delegate_id"])
    assert task.completed_event.wait(5)
    assert observed == [task]
    assert task.execution_backend == "acp" and task.harness == "antigravity"
    assert task.model == (model or "gemini-3.8-flash-high")
    assert task.reasoning_effort == "high"
    assert task.routing_mode == "explicit"
    request = json.loads(scheduler._build_invocation_for_task(task).stdin)
    assert request["account"] == "antigravity" and request["thinking"] == "high"


@pytest.mark.parametrize("complexity", ["medium", "high"])
@pytest.mark.parametrize(("model", "effort", "code"), [
    ("gemini-3.8-flash-low", None, "complexity_conflicts_with_model"),
    ("gemini-3.8-flash-medium", "high", "complexity_conflicts_with_model"),
    ("gemini-3.8-flash-high", "low", "complexity_conflicts_with_reasoning_effort"),
    ("gemini-3.8-flash", "medium", "complexity_conflicts_with_reasoning_effort"),
    (None, "xhigh", "complexity_conflicts_with_reasoning_effort"),
    ("unknown-model", None, "acp_flash_only"),
])
def test_acp_complexity_rejects_incompatible_explicit_overrides(tmp_path, complexity, model, effort, code):
    scheduler, workspace = registry(tmp_path)
    response = scheduler.run_delegate(
        task="Inspect sample.py", cwd=workspace, harness="antigravity", backend="acp",
        kind="explore", complexity=complexity, model=model, reasoning_effort=effort, wait_seconds=0,
    )
    assert response["success"] is False
    assert response["error"]["code"] == code
    assert not scheduler.scheduler.tasks


@pytest.mark.parametrize("backend", ["acp", "cli"])
def test_unknown_complexity_remains_rejected(tmp_path, backend):
    scheduler, workspace = registry(tmp_path)
    response = scheduler.run_delegate(
        task="Inspect sample.py", cwd=workspace, harness="antigravity", backend=backend,
        kind="explore", complexity="extreme", wait_seconds=0,
    )
    assert response["error"]["code"] == "unsupported_complexity"
    assert not scheduler.scheduler.tasks


@pytest.mark.parametrize("complexity", ["medium", "high"])
@pytest.mark.parametrize(("options", "code"), [
    ({"harness": "antigravity"}, "complexity_conflicts_with_harness"),
    ({"model": "gemini-3.8-flash-medium"}, "complexity_conflicts_with_model"),
    ({"resume_from_delegate_id": "c" * 12}, "complexity_conflicts_with_resume"),
])
def test_cli_complexity_guards_remain_enforced(tmp_path, complexity, options, code):
    scheduler, workspace = registry(tmp_path)
    response = scheduler.run_delegate(
        task="Inspect sample.py", cwd=workspace, backend="cli", kind="explore",
        complexity=complexity, wait_seconds=0, **options,
    )
    assert response["error"]["code"] == code
    assert not scheduler.scheduler.tasks


@pytest.mark.parametrize("complexity", ["medium", "high"])
@pytest.mark.parametrize(("source_model", "source_effort", "overrides", "error"), [
    ("gemini-3.8-flash-high", "high", {}, None),
    ("gemini-3.8-flash", "high", {}, None),
    ("gemini-3.8-flash-medium", "medium", {}, "complexity_conflicts_with_resume"),
    ("gemini-3.8-flash-medium", "medium", {"model": "gemini-3.8-flash-high"}, "acp_resume_model_mismatch"),
])
def test_acp_complexity_continuation_never_upgrades_source(
    tmp_path, monkeypatch, complexity, source_model, source_effort, overrides, error,
):
    scheduler, workspace = registry(tmp_path)
    observed = capture_dispatch(scheduler, monkeypatch)
    owner = "qa-resume-owner"
    source = {
        "execution_backend": "acp", "completed": True, "harness": "antigravity",
        "kind": "explore", "cwd": str(workspace),
        "project": scheduler.project_resolver.resolve(workspace).as_payload(),
        "model": source_model, "reasoning_effort": source_effort,
        "harness_metadata": {
            "session_ref": "a" * 32, "owner_digest": hashlib.sha256(owner.encode()).hexdigest(),
            "scope_digest": "b" * 64,
        },
        "acp_scope": {"files_in_scope": ["sample.py"], "out_of_scope": []},
    }
    monkeypatch.setattr(scheduler, "_resume_source_snapshot", lambda _: source)
    response = scheduler.run_delegate(
        task="Continue inspection", cwd=workspace, harness="antigravity", backend="acp",
        kind="explore", complexity=complexity, logical_session_id=owner,
        resume_from_delegate_id="c" * 12, wait_seconds=0, **overrides,
    )
    if error:
        assert response["error"]["code"] == error
        assert not scheduler.scheduler.tasks and not observed
    else:
        assert response.get("delegate_id"), response
        task = scheduler.scheduler.get_task(response["delegate_id"])
        assert task.completed_event.wait(5)
        assert observed == [task]
        assert (task.model, task.reasoning_effort) == (source_model, source_effort)
        assert task.acp_resume_context["session_ref"] == source["harness_metadata"]["session_ref"]
        assert task.harness == "antigravity" and task.execution_backend == "acp"
    assert (source["model"], source["reasoning_effort"]) == (source_model, source_effort)
