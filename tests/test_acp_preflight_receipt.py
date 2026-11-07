"""Pre-inference capability evidence cannot replace final ACP acceptance."""
import copy
from contextlib import contextmanager

import pytest

from chatgpt_web_oauth_mcp.acp_code_verification import (
    _contract, build_verification_receipt, inspect_candidate,
)
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
from chatgpt_web_oauth_mcp.acp_workspace import AcpWorkspace
from chatgpt_web_oauth_mcp.delegate_code_verification import (
    build_code_contract, capture_code_baseline, contract_hash, verify_code_result,
)
from test_acp_code_verification import repository


PROBES = ["/usr/bin/python3 -B -c 'import unittest'", "test -r source.txt"]


def passed(commands):
    return [{"command": item, "passed": True, "exit_code": 0, "stdout": "ok", "stderr": ""}
            for item in commands]


@contextmanager
def candidate(repository, probes=PROBES):
    task, state, marker = repository
    task.code_contract = build_code_contract(
        files_in_scope=["source.txt"], acceptance_criteria=["after"], done_means=["verified"],
        verification_commands=["test final behavior"], verification_preflight_commands=probes,
        max_changed_files=1, max_added_lines=2, max_deleted_lines=2,
    )
    baseline, error = capture_code_baseline(task)
    assert error is None
    workspace = AcpWorkspace.prepare(task.cwd, ["source.txt"], state)
    try:
        (workspace.root / "source.txt").write_text("after\n")
        expected = inspect_candidate(workspace, task.code_contract)["snapshot_digest"]
        args = dict(delegate_id=task.delegate_id, contract=task.code_contract,
                    baseline=baseline, workspace=workspace, expected_digest=expected,
                    verification_results=passed(task.code_contract["verification_commands"]))
        yield task, baseline, workspace, args, marker
    finally:
        workspace.close()


def test_preflight_receipt_keeps_evidence_separate_and_bounded(repository, monkeypatch):
    import chatgpt_web_oauth_mcp.delegate_code_verification as canonical
    with candidate(repository) as (task, baseline, workspace, args, marker):
        proof = passed(PROBES)
        proof[0]["stdout"] = "x" * 13000
        proof[0]["stdout_truncated"] = True
        receipt = build_verification_receipt(**args, preflight_results=proof)
        assert [row["command"] for row in receipt["verification_preflight_commands"]] == PROBES
        assert len(receipt["verification_preflight_commands"][0]["stdout"]) == 12000
        assert receipt["verification_preflight_commands"][0]["stdout_truncated"] is True
        assert receipt["verification_commands"][0]["command"] == "test final behavior"
        workspace.promote(expected_digest=receipt["snapshot_digest"])

        def forbidden(*args, **kwargs):
            raise AssertionError("Canonical receipt validation must not run host checks")

        monkeypatch.setattr(canonical, "_run_verification_commands", forbidden)
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error is None and payload["verified"]
        assert payload["verification_preflight_commands"] == receipt["verification_preflight_commands"]
        assert payload["verification_commands"] == receipt["verification_commands"]
        assert not marker.exists()


@pytest.mark.parametrize("change", [
    "missing", "short", "extra", "reordered", "not-list", "not-row",
    "wrong-command", "passed-false", "passed-integer", "exit-nonzero", "exit-bool",
    "exit-missing", "timed_out", "cancelled", "not_run", "cleanup_failed", "error",
])
def test_builder_and_canonical_receipt_reject_bad_preflight_evidence(repository, change):
    proof = passed(PROBES)
    if change == "missing":
        proof = None
    elif change == "short":
        proof.pop()
    elif change == "extra":
        proof.append(proof[0].copy())
    elif change == "reordered":
        proof.reverse()
    elif change == "not-list":
        proof = tuple(proof)
    elif change == "not-row":
        proof[0] = "ok"
    elif change == "wrong-command":
        proof[0]["command"] = "other"
    elif change.startswith("passed"):
        proof[0]["passed"] = False if change == "passed-false" else 1
    elif change.startswith("exit"):
        proof[0]["exit_code"] = {"exit-nonzero": 1, "exit-bool": False, "exit-missing": None}[change]
    else:
        proof[0][change] = "fixture" if change == "error" else True
    with candidate(repository) as (task, baseline, workspace, args, _):
        with pytest.raises(AcpStateError):
            build_verification_receipt(**args, preflight_results=proof)
        receipt = build_verification_receipt(**args, preflight_results=passed(PROBES))
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        if change == "missing":
            receipt.pop("verification_preflight_commands")
        else:
            receipt["verification_preflight_commands"] = proof
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error is not None and payload["verified"] is False


@pytest.mark.parametrize("probes", [None, []])
@pytest.mark.parametrize("proof", [None, []])
def test_old_contract_receipts_remain_valid_without_preflight_key(repository, probes, proof):
    with candidate(repository, probes) as (task, baseline, workspace, args, _):
        receipt = build_verification_receipt(**args, preflight_results=proof)
        assert "verification_preflight_commands" not in task.code_contract
        assert "verification_preflight_commands" not in receipt
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error is None and payload["verified"]
        assert "verification_preflight_commands" not in payload


def test_undeclared_preflight_evidence_is_not_silently_accepted(repository):
    with candidate(repository, None) as (task, baseline, workspace, args, _):
        with pytest.raises(AcpStateError, match="receipt_invalid"):
            build_verification_receipt(**args, preflight_results=passed(PROBES))
        receipt = build_verification_receipt(**args)
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        receipt["verification_preflight_commands"] = passed(PROBES)
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error["code"] == "acp_code_verification_receipt_invalid"
        assert payload["verified"] is False


def test_passing_preflight_does_not_replace_final_behavioral_checks(repository):
    with candidate(repository) as (task, baseline, workspace, args, _):
        with pytest.raises(AcpStateError, match="receipt_invalid"):
            build_verification_receipt(**{**args, "verification_results": []},
                                       preflight_results=passed(PROBES))
        receipt = build_verification_receipt(**args, preflight_results=passed(PROBES))
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        receipt["verification_commands"] = receipt["verification_preflight_commands"]
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error is not None and payload["verified"] is False


@pytest.mark.parametrize("invalid", ["true", ["x"] * 5, [""], ["é" * 2049], ["true\0false"]])
def test_receipt_contract_reuses_strict_preflight_validator(invalid):
    contract = build_code_contract(
        files_in_scope=["source.txt"], acceptance_criteria=[], done_means=[],
        verification_commands=["final"], max_changed_files=1,
        max_added_lines=None, max_deleted_lines=None,
    )
    contract.pop("contract_hash")
    contract["verification_preflight_commands"] = invalid
    contract["contract_hash"] = contract_hash(contract)
    with pytest.raises(AcpStateError, match="acp_verification_preflight_commands_invalid"):
        _contract(contract)


@pytest.mark.parametrize("commands,valid", [
    (["x" * 8192], True), (["x" * 8193], False),
    (["é" * 4096], True), (["é" * 4097], False),
    (["true"] * 16, True), (["true"] * 17, False),
])
def test_receipt_final_command_limits_match_admission(commands, valid):
    contract = build_code_contract(
        files_in_scope=["source.txt"], acceptance_criteria=[], done_means=[],
        verification_commands=commands, max_changed_files=1,
        max_added_lines=None, max_deleted_lines=None,
    )
    if valid:
        assert _contract(contract)[1] == commands
    else:
        with pytest.raises(AcpStateError, match="acp_code_verification_commands_required"):
            _contract(contract)


def test_receipt_hash_binds_declared_preflight_contract(repository):
    with candidate(repository) as (task, baseline, workspace, args, _):
        receipt = build_verification_receipt(**args, preflight_results=passed(PROBES))
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        contract = copy.deepcopy(task.code_contract)
        contract.pop("contract_hash")
        contract["verification_preflight_commands"] = ["different capability"]
        contract["contract_hash"] = contract_hash(contract)
        task.code_contract = contract
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error["code"] == "acp_code_verification_receipt_invalid"
        assert payload["verified"] is False
