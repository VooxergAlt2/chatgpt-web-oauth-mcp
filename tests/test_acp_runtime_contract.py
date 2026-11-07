"""Runtime selection is part of ACP admission identity and trusted receipts."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp.acp_code_verification import (
    _contract, build_verification_receipt, inspect_candidate,
)
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
from chatgpt_web_oauth_mcp.acp_workspace import AcpWorkspace
from chatgpt_web_oauth_mcp.delegate_code_verification import (
    build_code_contract, capture_code_baseline, contract_hash, verify_code_result,
)
from chatgpt_web_oauth_mcp.executors import _delegate_request_fingerprint
from test_acp_admission import git
from test_acp_backend import registry
from test_acp_code_verification import repository


RUNTIME = {"id": "pytest-fixture", "root": "/opt/operator-pytest-fixture",
           "manifest_sha256": "a" * 64, "python": "bin/python"}


def contract(runtime=None):
    return build_code_contract(
        files_in_scope=["source.txt"], acceptance_criteria=[], done_means=[],
        verification_commands=["test final behavior"], max_changed_files=1,
        max_added_lines=2, max_deleted_lines=2, verification_runtime=runtime,
    )


def test_runtime_contract_preserves_absence_and_binds_each_identity_field():
    previous = contract()
    assert previous == contract(None)
    assert "verification_runtime" not in previous
    declared = copy.deepcopy(RUNTIME)
    selected = contract(declared)
    declared["id"] = "mutated-after-binding"
    assert selected["verification_runtime"] == RUNTIME
    identities = [RUNTIME, {**RUNTIME, "id": "other"},
                  {**RUNTIME, "root": "/opt/different-fixture"},
                  {**RUNTIME, "manifest_sha256": "b" * 64}]
    assert len({previous["contract_hash"], *(contract(value)["contract_hash"] for value in identities)}) == 5


def test_request_identity_includes_selected_runtime():
    args = dict(
        task="implement", goal=None, task_id=None, cwd=Path("/repo"), harness="antigravity",
        kind="code", group_id=None, files_in_scope=["source.txt"], out_of_scope=None,
        context_files=None, acceptance_criteria=None, done_means=None,
        verification_commands=["final"], commit_mode="forbidden", model="flash",
        reasoning_effort="medium", complexity="low", max_tool_calls=8,
        output_schema=None, parse_structured_output=True, depends_on_group_ids=None,
        execution_backend="acp",
    )
    previous = _delegate_request_fingerprint(**args)
    assert previous == _delegate_request_fingerprint(**args, verification_runtime=None)
    selected = _delegate_request_fingerprint(**args, verification_runtime=RUNTIME)
    assert selected != previous
    assert selected != _delegate_request_fingerprint(
        **args, verification_runtime={**RUNTIME, "manifest_sha256": "b" * 64})


class RuntimeBackend:
    def __init__(self, backend, runtime):
        self.backend = backend
        self.verification_runtime = runtime

    def __getattr__(self, name):
        return getattr(self.backend, name)


@pytest.mark.parametrize("backend", ["acp", "auto"])
def test_selected_operator_runtime_reaches_worker_request(tmp_path, monkeypatch, backend):
    scheduler, main = registry(tmp_path)
    git(main, "init", "-q")
    git(main, "add", "sample.py")
    git(main, "-c", "user.name=QA", "-c", "user.email=qa@invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "--detach", "-q", str(linked))
    validations = []
    runtime = SimpleNamespace(validate=lambda: validations.append(True), identity=lambda: dict(RUNTIME))
    scheduler.acp_backend = RuntimeBackend(scheduler.acp_backend, runtime)
    monkeypatch.setattr(scheduler, "_start_codex_delegate_impl",
                        lambda **kwargs: {"success": True, "status": "succeeded"})
    response = scheduler.run_delegate(
        task="Change sample.py", cwd=linked, harness="antigravity", backend=backend,
        kind="code", files_in_scope=["sample.py"], verification_commands=["true"],
        logical_session_id="runtime-owner", execution_timeout_seconds=60, wait_seconds=0,
    )
    assert response.get("delegate_id"), response
    task = scheduler.scheduler.get_task(response["delegate_id"])
    assert task.completed_event.wait(5)
    assert validations == [True]
    request = json.loads(scheduler._build_invocation_for_task(task).stdin)
    assert request["code_contract"]["verification_runtime"] == RUNTIME
    assert request["code_contract"]["contract_hash"] == task.code_contract["contract_hash"]


def test_invalid_selected_runtime_fails_before_scheduling(tmp_path):
    scheduler, main = registry(tmp_path)
    git(main, "init", "-q")
    git(main, "add", "sample.py")
    git(main, "-c", "user.name=QA", "-c", "user.email=qa@invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "--detach", "-q", str(linked))

    def invalid():
        raise AcpStateError("acp_verification_runtime_changed")

    scheduler.acp_backend = RuntimeBackend(
        scheduler.acp_backend, SimpleNamespace(validate=invalid, identity=lambda: dict(RUNTIME)))
    result = scheduler.run_delegate(
        task="Change sample.py", cwd=linked, harness="antigravity", backend="acp", kind="code",
        files_in_scope=["sample.py"], verification_commands=["true"], wait_seconds=0,
    )
    assert result["error"]["code"] == "acp_verification_runtime_changed"
    assert not scheduler.scheduler.tasks


@pytest.mark.parametrize("backend,kind", [("cli", "code"), ("acp", "explore")])
def test_runtime_is_not_bound_to_cli_or_explore(tmp_path, monkeypatch, backend, kind):
    scheduler, workspace = registry(tmp_path)

    def unexpected():
        raise AssertionError("Runtime is only selected for ACP code")

    scheduler.acp_backend = RuntimeBackend(
        scheduler.acp_backend, SimpleNamespace(validate=unexpected, identity=unexpected))
    monkeypatch.setattr(scheduler, "_start_codex_delegate_impl",
                        lambda **kwargs: {"success": True, "status": "succeeded"})
    response = scheduler.run_delegate(
        task="Inspect sample.py", cwd=workspace, harness="antigravity", backend=backend,
        kind=kind, files_in_scope=["sample.py"], verification_commands=["true"], wait_seconds=0,
    )
    assert response.get("delegate_id"), response
    task = scheduler.scheduler.get_task(response["delegate_id"])
    assert task.completed_event.wait(5)
    assert "verification_runtime" not in (task.code_contract or {})


@contextmanager
def prepared(repository, runtime=RUNTIME):
    task, state, _ = repository
    task.code_contract = contract(runtime)
    baseline, error = capture_code_baseline(task)
    assert error is None
    workspace = AcpWorkspace.prepare(task.cwd, ["source.txt"], state)
    try:
        (workspace.root / "source.txt").write_text("after\n")
        digest = inspect_candidate(workspace, task.code_contract)["snapshot_digest"]
        yield task, baseline, workspace, dict(
            delegate_id=task.delegate_id, contract=task.code_contract, baseline=baseline,
            workspace=workspace, expected_digest=digest,
            verification_results=[{"command": "test final behavior", "passed": True,
                                   "exit_code": 0, "stdout": "", "stderr": ""}],
        )
    finally:
        workspace.close()


def test_runtime_receipt_validates_pinned_identity_without_current_host_files(repository):
    assert not Path(RUNTIME["root"]).exists()
    with prepared(repository) as (task, baseline, workspace, args):
        receipt = build_verification_receipt(**args, runtime_identity=RUNTIME)
        assert receipt["verification_runtime"] == RUNTIME
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error is None and payload["verified"]
        assert payload["verification_runtime"] == RUNTIME


@pytest.mark.parametrize("proof", [None, {}, {**RUNTIME, "id": "other"},
                                  {**RUNTIME, "manifest_sha256": "b" * 64},
                                  {**RUNTIME, "root": "/opt/other-fixture"}])
def test_runtime_builder_and_postflight_reject_missing_or_mismatched_identity(repository, proof):
    with prepared(repository) as (task, baseline, workspace, args):
        with pytest.raises(AcpStateError, match="acp_verification_runtime_mismatch"):
            build_verification_receipt(**args, runtime_identity=proof)
        receipt = build_verification_receipt(**args, runtime_identity=RUNTIME)
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        if proof is None:
            receipt.pop("verification_runtime")
        else:
            receipt["verification_runtime"] = proof
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error is not None and payload["verified"] is False


def test_no_profile_contract_rejects_unexpected_runtime_evidence(repository):
    with prepared(repository, None) as (task, baseline, workspace, args):
        with pytest.raises(AcpStateError, match="acp_verification_runtime_mismatch"):
            build_verification_receipt(**args, runtime_identity=RUNTIME)
        receipt = build_verification_receipt(**args)
        assert "verification_runtime" not in receipt
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        receipt["verification_runtime"] = RUNTIME
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error is not None and payload["verified"] is False


def test_contract_rejects_malformed_runtime_even_with_recomputed_hash():
    value = contract()
    value.pop("contract_hash")
    value["verification_runtime"] = {**RUNTIME, "root": "/opt/../home"}
    value["contract_hash"] = contract_hash(value)
    with pytest.raises(AcpStateError, match="acp_verification_runtime_invalid"):
        _contract(value)


@pytest.mark.parametrize("phase", ["final", "preflight"])
@pytest.mark.parametrize("flag", ["output_limit_exceeded", "capture_failed"])
def test_failed_output_capture_cannot_be_accepted_as_passed(repository, phase, flag):
    with prepared(repository, None) as (_, _, _, args):
        if phase == "preflight":
            value = args["contract"]
            value.pop("contract_hash")
            value["verification_preflight_commands"] = ["capability"]
            value["contract_hash"] = contract_hash(value)
            args["preflight_results"] = [{"command": "capability", "passed": True, "exit_code": 0,
                                           flag: True}]
        else:
            args["verification_results"][0][flag] = True
        with pytest.raises(AcpStateError, match="acp_code_verification_failed"):
            build_verification_receipt(**args)
