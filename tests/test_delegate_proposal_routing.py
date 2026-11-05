"""Proposal is an explicit, fresh, tool-less ACP reader sharing analysis capacity."""
import json

import pytest

from test_acp_backend import registry
from chatgpt_web_oauth_mcp.delegate_models import resolve_work_role


@pytest.fixture
def setup(tmp_path, monkeypatch):
    r, workspace = registry(tmp_path)
    monkeypatch.setattr(r.scheduler, "_start_threads", lambda tasks: None)
    return r, workspace


def submit(r, workspace, **overrides):
    params = dict(task="Return complete literal Python implementing the supplied contract.",
                  cwd=workspace, kind="explore", work_role="proposal",
                  backend="acp", harness="antigravity", wait_seconds=0,
                  logical_session_id="proposal-owner")
    params.update(overrides)
    return r.run_delegate(**params)


@pytest.mark.parametrize("overrides", [
    {"backend": "auto"}, {"backend": "cli"}, {"harness": None},
    {"harness": "codex"}, {"kind": "code"}, {"task": None},
    {"files_in_scope": []}, {"out_of_scope": []}, {"context_files": []},
    {"verification_commands": []}, {"max_changed_files": 0},
    {"max_added_lines": 0}, {"max_deleted_lines": 0},
    {"resume_from_delegate_id": "a" * 12},
    {"draft_from_delegate_id": "a" * 12}, {"draft_snapshot_id": "snapshot"},
    {"group_id": "grp-" + "a" * 12}, {"depends_on_group_ids": []},
    {"output_schema": {}}, {"parse_structured_output": False},
    {"commit_mode": "allowed"},
])
def test_proposal_admission_rejects_other_surfaces_before_submission(setup, overrides):
    r, workspace = setup
    result = submit(r, workspace, **overrides)
    assert result["success"] is False
    assert result["error"]["code"] in {"proposal_arguments_invalid", "invalid_work_role"}
    assert not r.scheduler.tasks


@pytest.mark.parametrize("overrides", [
    {"task": "x" * 65537}, {"goal": "ю" * 32769},
    {"acceptance_criteria": ["x"] * 65}, {"done_means": ["x"] * 65},
    {"acceptance_criteria": [{}]}, {"done_means": "text"},
    {"task": {}}, {"task": "\ud800"},
])
def test_proposal_contract_is_bounded_and_textual(setup, overrides):
    r, workspace = setup
    result = submit(r, workspace, **overrides)
    assert result["error"]["code"] == "proposal_contract_invalid"
    assert not r.scheduler.tasks


def test_proposal_prompt_has_only_contract_and_receipts_persist_role(setup, monkeypatch):
    r, workspace = setup
    def forbidden(*args, **kwargs):
        pytest.fail("Proposal must not inspect repository context/evidence/navigation")
    monkeypatch.setattr(r, "_project_prompt_context", forbidden)
    monkeypatch.setattr("chatgpt_web_oauth_mcp.executors.scoped_git_diff_evidence", forbidden)
    monkeypatch.setattr("chatgpt_web_oauth_mcp.executors.scoped_navigation_hints", forbidden)
    result = submit(r, workspace, acceptance_criteria=["Return the full function."],
                    done_means=["The code is literal and complete."])
    task = r.scheduler.get_task(result["delegate_id"])
    assert result["work_role"] == task.work_role == "proposal"
    assert task.kind == "explore" and task.capacity_role == "analysis" and task.lane == "reader"
    assert task.execution_backend == "acp" and task.resume_from_delegate_id is None
    assert task.routing_mode == "explicit"
    prompt = task.prompt
    assert "Do not use tools" in prompt and "complete literal code" in prompt
    assert "Return the full function." in prompt and "The code is literal and complete." in prompt
    assert "unverified" in prompt and "next_execution_prompt" in prompt
    for excluded in ("PROJECT CONTEXT", "FILES IN SCOPE", "SOURCE NAVIGATION",
                     "SERVER GIT EVIDENCE", str(workspace)):
        assert excluded not in prompt
    saved = json.loads(task.log_paths.metadata.read_text())
    assert saved["work_role"] == "proposal"
    assessment = {"status": "succeeded", "structured_output": {"summary": "def f(): return 1"}}
    r._assess_explore_result(task, assessment)
    assert "research_evidence" not in assessment and "content_acceptance" not in assessment
    assert r._runtime_retry_reason(task=task, result={"status": "failed"}) is None


def test_proposal_direct_process_runner_never_reads_git(setup, monkeypatch):
    from chatgpt_web_oauth_mcp.delegate_process import Invocation

    registry, workspace = setup
    result = submit(registry, workspace)
    task = registry.scheduler.get_task(result["delegate_id"])
    runner = registry._process_runner
    monkeypatch.setattr(runner, "_git_status", lambda *_: pytest.fail("unexpected repository audit"))
    response = runner.run(task, invocation_builder=lambda _: Invocation(
        args=["/usr/bin/printf", "%s", '{"status":"succeeded","summary":"candidate","findings":[]}'],
        use_shell=False, read_only_enforced=True))
    assert response["status"] == "succeeded"
    assert "readonly_audit" not in (response.get("harness_metadata") or {})


def test_proposal_consumes_shared_analysis_role_and_account_capacity(setup):
    r, workspace = setup
    r.scheduler.max_readers_per_role["analysis"] = 1
    r.scheduler.max_active_per_harness["antigravity"] = 3
    r.scheduler.max_explore_per_harness["antigravity"] = 3
    first = submit(r, workspace)
    assert first["status"] == "running"
    waiting = submit(r, workspace, task="An ordinary analysis", work_role="analysis")
    assert waiting["status"] == "queued"
    review = submit(r, workspace, task="Independent review", work_role="review")
    assert review["status"] == "running"
    proposal = r.scheduler.get_task(first["delegate_id"])
    r.scheduler._execute_task(proposal, runner=lambda task: {"status": "succeeded"})
    assert r.scheduler.get_task(waiting["delegate_id"]).state == "running"


def test_proposal_batch_is_rejected_and_unhashable_roles_are_validation_errors(setup):
    r, workspace = setup
    for role in ("proposal", [], {}):
        result = r.run_delegate_batch(tasks=[{"task": "contract", "work_role": role}],
                                      cwd=workspace, harness="antigravity", wait_seconds=0)
        assert result["error"]["code"] == "invalid_work_role"
        assert not r.scheduler.tasks
    for role in ([], {}):
        with pytest.raises(ValueError):
            resolve_work_role("explore", role)

def test_proposal_recovery_preserves_role_and_fingerprint_is_distinct(setup):
    r, workspace = setup
    proposed = submit(r, workspace)
    normal = submit(r, workspace, work_role="analysis")
    task = r.scheduler.get_task(proposed["delegate_id"])
    analysis = r.scheduler.get_task(normal["delegate_id"])
    assert task.request_fingerprint != analysis.request_fingerprint
    saved = json.loads(task.log_paths.metadata.read_text())
    # Running durable metadata includes the canonical sidecar log mapping.
    saved["logs"] = task.log_paths.as_payload()
    recovered = r._task_from_persisted_durable(saved, task.log_paths.metadata)
    assert recovered.work_role == "proposal"
    assert recovered.capacity_role == "analysis"
    assert recovered.request_fingerprint == task.request_fingerprint


@pytest.mark.parametrize("owner, expected", [
    ("proposal-owner", "proposal_continuation_forbidden"),
    ("foreign-owner", "acp_resume_owner_mismatch"),
])
def test_normal_explore_cannot_resume_proposal_and_owner_is_checked_first(setup, monkeypatch, owner, expected):
    import hashlib
    r, workspace = setup
    source = {
        "work_role": "proposal", "execution_backend": "acp", "completed": True,
        "harness": "antigravity", "kind": "explore", "cwd": str(workspace),
        "project": r.project_resolver.resolve(workspace).as_payload(),
        "model": "gemini-3.8-flash-medium", "reasoning_effort": "medium",
        "harness_metadata": {
            "session_ref": "a" * 32, "scope_digest": "b" * 64,
            "owner_digest": hashlib.sha256(b"proposal-owner").hexdigest(),
        },
        "acp_scope": {"files_in_scope": [], "out_of_scope": []},
    }
    monkeypatch.setattr(r, "_resume_source_snapshot", lambda identifier: source)
    result = submit(r, workspace, work_role=None, resume_from_delegate_id="c" * 12,
                    logical_session_id=owner)
    assert result["error"]["code"] == expected
    assert not r.scheduler.tasks
