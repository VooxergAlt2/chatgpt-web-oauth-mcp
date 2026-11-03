"""Admission tests for public ACP writer boundaries and linked worktree allowlists."""
from __future__ import annotations

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


def test_registered_linked_writer_uses_same_account_and_bounded_durable_protocol(tmp_path, monkeypatch):
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
        logical_session_id="qa-writer-owner", wait_seconds=0,
    )
    assert response.get("delegate_id"), json.dumps(response, indent=2)
    task = scheduler.scheduler.get_task(response["delegate_id"])
    assert task.completed_event.wait(5)
    assert observed == [task]
    assert task.execution_backend == "acp" and task.harness == "antigravity"
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
