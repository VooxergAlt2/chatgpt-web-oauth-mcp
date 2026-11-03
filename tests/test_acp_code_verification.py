import copy
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp.acp_code_verification import (
    build_verification_receipt, inspect_candidate,
)
from chatgpt_web_oauth_mcp.acp_sandbox import verification_command
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
from chatgpt_web_oauth_mcp.acp_workspace import AcpWorkspace
from chatgpt_web_oauth_mcp.delegate_code_verification import (
    _run_verification_commands, build_code_contract, capture_code_baseline,
    verify_code_result,
)


def git(cwd, *arguments):
    return subprocess.check_output(
        ["/usr/bin/git", "-C", str(cwd), *arguments], stderr=subprocess.DEVNULL,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
    ).decode().strip()


def contract(commands=None, **limits):
    return build_code_contract(
        files_in_scope=["source.txt"], acceptance_criteria=["fixture result"],
        done_means=["verified"], verification_commands=commands or ["test fixture"],
        max_changed_files=limits.get("max_changed_files", 1),
        max_added_lines=limits.get("max_added_lines", 2),
        max_deleted_lines=limits.get("max_deleted_lines", 2),
    )


@pytest.fixture
def repository(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q")
    git(main, "config", "user.name", "ACP fixture")
    git(main, "config", "user.email", "acp-fixture@example.invalid")
    (main / "source.txt").write_text("before\n")
    (main / "other.txt").write_text("unchanged\n")
    (main / ".gitattributes").write_text("source.txt filter=fixture\n")
    git(main, "add", ".")
    git(main, "commit", "-qm", "fixture")
    source = tmp_path / "worktree"
    git(main, "worktree", "add", "-qb", "fixture-worktree", str(source))
    marker = tmp_path / "filter-ran"
    executable = tmp_path / "filter"
    executable.write_text("#!/bin/sh\ntouch " + shlex.quote(str(marker)) + "\ncat\n")
    executable.chmod(0o700)
    git(main, "config", "filter.fixture.clean", str(executable))
    task = SimpleNamespace(kind="code", execution_backend="acp", cwd=source,
                           code_contract=contract(), commit_mode="forbidden",
                           delegate_id="acp-receipt-fixture")
    return task, tmp_path / "state", marker


def ready(repository):
    task, state, marker = repository
    baseline, error = capture_code_baseline(task)
    assert error is None
    workspace = AcpWorkspace.prepare(task.cwd, ["source.txt"], state)
    (workspace.root / "source.txt").write_text("after\n")
    expected = inspect_candidate(workspace, task.code_contract)["snapshot_digest"]
    rows = [{"command": item, "passed": True, "exit_code": 0, "stdout": "ok", "stderr": ""}
            for item in task.code_contract["verification_commands"]]
    receipt = build_verification_receipt(
        delegate_id=task.delegate_id, contract=task.code_contract, baseline=baseline,
        workspace=workspace, verification_results=rows, expected_digest=expected,
    )
    return task, baseline, workspace, receipt, marker


def test_compact_receipt_verifies_promoted_raw_source_without_host_tests_or_filters(repository, monkeypatch):
    import chatgpt_web_oauth_mcp.delegate_code_verification as canonical
    task, baseline, workspace, receipt, marker = ready(repository)
    assert "manifest" not in baseline and len(json.dumps(baseline)) < 1024
    assert set(receipt["manifest"]) == {"source.txt"}
    assert receipt["added_lines"] == 1 and receipt["deleted_lines"] == 1
    def forbidden(*args, **kwargs):
        raise AssertionError("Host Git status/diff or verification executed")
    monkeypatch.setattr(canonical, "_git_bytes", forbidden)
    monkeypatch.setattr(canonical, "_run_verification_commands", forbidden)
    try:
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error is None and payload["verified"]
        assert payload["verification_level"] == "checks_passed"
        assert payload["post_verification_result_fingerprint"] == receipt["snapshot_digest"]
        assert not marker.exists()
    finally:
        workspace.close()


@pytest.mark.parametrize("field,value", [
    ("delegate_id", "foreign-task"), ("contract_hash", "0" * 16),
    ("baseline_head", "0" * 40), ("snapshot_digest", "0" * 64),
    ("execution_boundary", "host-shell"), ("version", 2),
])
def test_foreign_or_stale_receipts_never_accept(repository, field, value):
    task, baseline, workspace, receipt, _ = ready(repository)
    try:
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        receipt[field] = value
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error is not None and not payload["verified"]
    finally:
        workspace.close()


@pytest.mark.parametrize("changes", [
    {"command": "different"}, {"passed": False}, {"exit_code": 1},
    {"exit_code": True}, {"cancelled": True}, {"timed_out": True},
    {"not_run": True}, {"cleanup_failed": True},
])
def test_receipt_requires_exact_successful_declared_checks(repository, changes):
    task, baseline, workspace, receipt, _ = ready(repository)
    try:
        row = copy.deepcopy(receipt["verification_commands"][0])
        row.update(changes)
        with pytest.raises(AcpStateError, match="verification_failed"):
            build_verification_receipt(
                delegate_id=task.delegate_id, contract=task.code_contract, baseline=baseline,
                workspace=workspace, verification_results=[row], expected_digest=receipt["snapshot_digest"])
    finally:
        workspace.close()


def test_missing_receipt_does_not_fall_back_to_host_verification(repository, monkeypatch):
    import chatgpt_web_oauth_mcp.delegate_code_verification as canonical
    task, _, _ = repository
    baseline, error = capture_code_baseline(task)
    assert error is None
    def forbidden(*args, **kwargs):
        raise AssertionError("host fallback")
    monkeypatch.setattr(canonical, "_run_verification_commands", forbidden)
    monkeypatch.setattr(canonical, "_git_bytes", forbidden)
    payload, error = verify_code_result(task, baseline=baseline)
    assert error is not None and not payload["verified"]


def test_candidate_budget_scope_and_verification_mutation_fail_before_promotion(repository):
    task, state, _ = repository
    baseline, error = capture_code_baseline(task)
    assert error is None
    with AcpWorkspace.prepare(task.cwd, ["source.txt"], state) as workspace:
        (workspace.root / "source.txt").write_text("line1\nline2\nline3\n")
        with pytest.raises(AcpStateError, match="budget"):
            inspect_candidate(workspace, task.code_contract)
        (workspace.root / "source.txt").write_text("after\n")
        before = inspect_candidate(workspace, task.code_contract)
        (workspace.root / "source.txt").write_text("mutated by test\n")
        rows = [{"command": "test fixture", "passed": True, "exit_code": 0}]
        with pytest.raises(AcpStateError, match="result_changed"):
            build_verification_receipt(delegate_id=task.delegate_id, contract=task.code_contract,
                baseline=baseline, workspace=workspace, verification_results=rows,
                expected_digest=before["snapshot_digest"])
        (workspace.root / "other.txt").write_text("outside scope\n")
        with pytest.raises(AcpStateError, match="scope_violation"):
            inspect_candidate(workspace, task.code_contract)
        assert (task.cwd / "source.txt").read_text() == "before\n"


def test_post_promotion_source_drift_rejects_receipt(repository):
    task, baseline, workspace, receipt, _ = ready(repository)
    try:
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        (task.cwd / "other.txt").write_text("external change\n")
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error["code"] == "acp_code_promoted_result_changed"
        assert not payload["verified"]
    finally:
        workspace.close()


def test_newline_only_change_is_counted_in_line_budget(repository):
    task, state, _ = repository
    task.code_contract = contract(max_added_lines=0, max_deleted_lines=0)
    with AcpWorkspace.prepare(task.cwd, ["source.txt"], state) as workspace:
        (workspace.root / "source.txt").write_text("before")
        with pytest.raises(AcpStateError, match="budget"):
            inspect_candidate(workspace, task.code_contract)


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="Linux sandbox required")
def test_declared_check_runs_in_real_readonly_verifier_and_receipt_accepts(repository):
    task, state, marker = repository
    host_namespace = os.readlink("/proc/self/ns/mnt")
    check = ("import os,pathlib; "
             f"assert os.readlink('/proc/self/ns/mnt') != {host_namespace!r}; "
             "assert pathlib.Path('source.txt').read_text() == 'after\\n'; "
             "assert not pathlib.Path('/profile').exists(); print('isolated fixture passed')")
    command = "/usr/bin/python3 -c " + shlex.quote(check)
    task.code_contract = contract([command])
    baseline, error = capture_code_baseline(task)
    assert error is None
    with AcpWorkspace.prepare(task.cwd, ["source.txt"], state) as workspace:
        (workspace.root / "source.txt").write_text("after\n")
        before = inspect_candidate(workspace, task.code_contract)
        results = _run_verification_commands(
            workspace.root, [command], timeout_seconds=5,
            argv_factory=lambda value: verification_command(workspace.root, task.cwd, value))
        assert results[0]["passed"], results
        assert "isolated fixture passed" in results[0]["stdout"]
        receipt = build_verification_receipt(
            delegate_id=task.delegate_id, contract=task.code_contract, baseline=baseline,
            workspace=workspace, verification_results=results, expected_digest=before["snapshot_digest"])
        workspace.promote(expected_digest=receipt["snapshot_digest"])
        payload, error = verify_code_result(task, baseline=baseline, receipt=receipt)
        assert error is None and payload["verified"]
        assert not marker.exists()


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="Linux sandbox required")
def test_isolated_command_cancel_reaps_process_and_skips_remaining(repository):
    task, state, _ = repository
    with AcpWorkspace.prepare(task.cwd, ["source.txt"], state) as workspace:
        started = time.monotonic()
        results = _run_verification_commands(
            workspace.root, ["sleep 20", "echo should-not-run"], timeout_seconds=10,
            argv_factory=lambda value: verification_command(workspace.root, task.cwd, value),
            cancel_check=lambda: time.monotonic() - started > 0.15)
        assert time.monotonic() - started < 4
        assert results[0]["cancelled"] and not results[0]["passed"]
        assert results[1]["cancelled"] and results[1]["not_run"]
        assert not any(row.get("cleanup_failed") for row in results)


def test_invalid_launcher_never_falls_back_to_shell(tmp_path):
    sentinel = tmp_path / "host-shell"
    results = _run_verification_commands(tmp_path, ["touch " + shlex.quote(str(sentinel))],
                                        timeout_seconds=2, argv_factory=lambda command: [])
    assert not results[0]["passed"] and not sentinel.exists()
