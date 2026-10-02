from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.delegate_code_verification import (
    MAX_VERIFICATION_OUTPUT_CHARS,
    _run_verification_commands,
    build_code_contract,
    capture_code_baseline,
    verify_code_result,
)
from chatgpt_web_oauth_mcp.delegate_harnesses import GenericCliHarness
from chatgpt_web_oauth_mcp.delegate_process import Invocation
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "delegate-test@example.invalid")
    _git(path, "config", "user.name", "Delegate Test")
    (path / "allowed.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "allowed.txt")
    _git(path, "commit", "-m", "baseline")


def _make_verified_task(
    registry: ExecutorRegistry,
    cwd: Path,
    *,
    files_in_scope: list[str] | None = None,
    verification_commands: list[str] | None = None,
    max_changed_files: int | None = None,
    max_added_lines: int | None = None,
    max_deleted_lines: int | None = None,
    commit_mode: str = "forbidden",
):
    project = registry.project_resolver.resolve(cwd)
    contract = build_code_contract(
        files_in_scope=files_in_scope or ["allowed.txt"],
        acceptance_criteria=["server acceptance passes"],
        done_means=["verified diff"],
        verification_commands=verification_commands or [],
        max_changed_files=max_changed_files,
        max_added_lines=max_added_lines,
        max_deleted_lines=max_deleted_lines,
    )
    assert contract is not None
    return registry._make_task(
        harness="codex",
        project=project,
        cwd=cwd,
        kind="code",
        task="modify allowed file",
        goal=None,
        task_id=None,
        group_id=None,
        model="default",
        reasoning_effort="default",
        sandbox_mode="danger-full-access",
        commit_mode=commit_mode,
        execution_timeout_seconds=30,
        depends_on_group_ids=(),
        files_in_scope=files_in_scope or ["allowed.txt"],
        out_of_scope=[],
        context_files=[],
        acceptance_criteria=["server acceptance passes"],
        done_means=["verified diff"],
        verification_commands=verification_commands or [],
        output_schema=None,
        parse_structured_output=False,
        request_fingerprint="verified-code-test",
        code_contract=contract,
    )


def test_verified_code_accepts_in_scope_diff_and_server_check(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        verification_commands=[
            "python -c \"from pathlib import Path; assert 'changed' in Path('allowed.txt').read_text()\""
        ],
        max_changed_files=1,
        max_added_lines=2,
        max_deleted_lines=2,
    )

    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    task.code_baseline = baseline
    (repo / "allowed.txt").write_text("base\nchanged\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification_error is None
    assert verification["verified"] is True
    assert verification["changed_files"] == ["allowed.txt"]
    assert verification["changed_file_count"] == 1
    assert verification["verification_commands"][0]["passed"] is True


def test_verified_code_rejects_out_of_scope_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(registry, repo)

    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    task.code_baseline = baseline
    (repo / "outside.txt").write_text("escape\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["verified"] is False
    assert verification["scope_violations"] == ["outside.txt"]
    assert verification_error is not None
    assert verification_error["code"] == "code_scope_violation"


def test_verified_code_rejects_change_budget(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        max_changed_files=1,
        max_added_lines=0,
    )

    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    task.code_baseline = baseline
    (repo / "allowed.txt").write_text("base\nnew-line\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["verified"] is False
    assert verification["added_lines"] == 1
    assert verification_error is not None
    assert verification_error["code"] == "code_change_budget_exceeded"


def test_cross_scope_rename_accounts_both_endpoints(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        files_in_scope=["renamed.txt"],
        max_changed_files=1,
    )

    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    _git(repo, "mv", "allowed.txt", "renamed.txt")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["changed_files"] == ["allowed.txt", "renamed.txt"]
    assert verification["changed_file_count"] == 2
    assert verification["scope_violations"] == ["allowed.txt"]
    assert verification["added_lines"] == 1
    assert verification["deleted_lines"] == 1
    assert "changed_files=2 exceeds max_changed_files=1" in verification["budget_violations"]
    assert verification_error is not None
    assert verification_error["code"] == "code_scope_violation"


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_zero_exit_verification_cleans_background_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "late-mutation.txt"
    command = (
        f"(sleep 0.4; printf late > {marker.name}) & "
        "printf passed; exit 0"
    )

    results = _run_verification_commands(tmp_path, [command], timeout_seconds=3)

    assert results[0]["passed"] is True
    assert results[0]["stdout"] == "passed"
    time.sleep(0.6)
    assert not marker.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_timed_out_verification_cleans_background_descendant(tmp_path: Path) -> None:
    marker = tmp_path / "late-timeout-mutation.txt"
    command = f"(sleep 1.5; printf late > {marker.name}) & sleep 5"

    started = time.monotonic()
    results = _run_verification_commands(tmp_path, [command], timeout_seconds=1)

    assert time.monotonic() - started < 4
    assert results[0]["passed"] is False
    assert results[0]["timed_out"] is True
    assert results[0].get("cleanup_failed") is not True
    time.sleep(0.7)
    assert not marker.exists()


def test_verification_commands_share_one_aggregate_deadline(tmp_path: Path) -> None:
    commands = [
        "sleep 0.7",
        "sleep 0.7",
        "printf should-not-run",
    ]

    started = time.monotonic()
    results = _run_verification_commands(tmp_path, commands, timeout_seconds=1)

    assert time.monotonic() - started < 2
    assert results[0]["passed"] is True
    assert results[1]["timed_out"] is True
    assert results[2]["not_run"] is True


def test_verification_output_capture_is_bounded(tmp_path: Path) -> None:
    command = "python -c \"import sys; sys.stdout.write('x' * 20000)\""

    results = _run_verification_commands(tmp_path, [command], timeout_seconds=3)

    assert results[0]["passed"] is True
    assert len(results[0]["stdout"]) == MAX_VERIFICATION_OUTPUT_CHARS
    assert results[0]["stdout_truncated"] is True


def test_verified_code_rejects_failing_server_check(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        verification_commands=["python -c \"raise SystemExit(7)\""],
    )

    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    task.code_baseline = baseline
    (repo / "allowed.txt").write_text("base\nchanged\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["verified"] is False
    assert verification["verification_commands"][0]["exit_code"] == 7
    assert verification_error is not None
    assert verification_error["code"] == "code_verification_failed"


def test_verified_code_requires_clean_baseline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(registry, repo)
    (repo / "allowed.txt").write_text("dirty\n", encoding="utf-8")

    baseline, error = capture_code_baseline(task)

    assert baseline is None
    assert error is not None
    assert error["code"] == "code_verification_baseline_dirty"


def test_process_finalize_converts_false_success_to_verification_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        verification_commands=["python -c \"raise SystemExit(9)\""],
    )
    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    task.code_baseline = baseline
    (repo / "allowed.txt").write_text("base\nchanged\n", encoding="utf-8")

    result = registry._process_runner.finalize_completed(
        task,
        invocation=Invocation(args=["true"], use_shell=False),
        stdout_raw=b"",
        stderr_raw=b"",
        exit_code=0,
        duration_seconds=0.1,
        before_status=None,
    )

    assert result["status"] == "failed"
    assert result["success"] is False
    assert result["error"]["code"] == "code_verification_failed"
    assert result["code_verification"]["verified"] is False


def test_registry_code_delegate_is_only_successful_after_server_verification(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    script = tmp_path / "agent.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('allowed.txt').write_text('base\\nverified\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    harness = GenericCliHarness(
        name="fake",
        command=f"{sys.executable} {script}",
    )
    registry = ExecutorRegistry(
        codex_command=None,
        default_harness="fake",
        harnesses=[harness],
    )

    result = registry.run_delegate(
        harness="fake",
        kind="code",
        task="modify allowed.txt",
        cwd=repo,
        files_in_scope=["allowed.txt"],
        verification_commands=[
            "python -c \"from pathlib import Path; assert 'verified' in Path('allowed.txt').read_text()\""
        ],
        max_changed_files=1,
        max_added_lines=2,
        max_deleted_lines=2,
        commit_mode="forbidden",
        wait_seconds=10,
    )

    assert result["status"] == "succeeded"
    assert result["success"] is True
    assert result["code_verification"]["verified"] is True
    assert result["code_verification"]["changed_files"] == ["allowed.txt"]


def test_registry_code_delegate_scope_violation_overrides_process_success(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    script = tmp_path / "agent.py"
    script.write_text(
        "from pathlib import Path\n"
        "Path('outside.txt').write_text('escaped\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    harness = GenericCliHarness(
        name="fake",
        command=f"{sys.executable} {script}",
    )
    registry = ExecutorRegistry(
        codex_command=None,
        default_harness="fake",
        harnesses=[harness],
    )

    result = registry.run_delegate(
        harness="fake",
        kind="code",
        task="modify one file",
        cwd=repo,
        files_in_scope=["allowed.txt"],
        commit_mode="forbidden",
        wait_seconds=10,
    )

    assert result["status"] == "failed"
    assert result["success"] is False
    assert result["error"]["code"] == "code_scope_violation"
    assert result["code_verification"]["scope_violations"] == ["outside.txt"]


def test_verified_code_detects_new_ignored_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore generated log")
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(registry, repo)

    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    (repo / "ignored.log").write_text("created\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["verified"] is False
    assert verification["changed_files"] == ["ignored.log"]
    assert verification["ignored_changed_files"] == ["ignored.log"]
    assert verification["ignored_cache_drift_paths"] == []
    assert verification["scope_violations"] == ["ignored.log"]
    assert verification_error is not None
    assert verification_error["code"] == "code_scope_violation"


def test_verified_code_detects_modified_preexisting_ignored_file_and_fails_closed_for_lines(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore generated log")
    (repo / "ignored.log").write_text("old\n", encoding="utf-8")
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        files_in_scope=["allowed.txt", "ignored.log"],
        max_added_lines=100,
    )

    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    assert "ignored.log" in baseline["ignored_manifest"]
    (repo / "ignored.log").write_text("new and different\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["verified"] is False
    assert verification["changed_files"] == ["ignored.log"]
    assert verification["ignored_drift_paths"] == ["ignored.log"]
    assert verification["ignored_cache_drift_paths"] == []
    assert verification["line_budget_authoritative"] is False
    assert "authoritative line-budget" in verification["budget_violations"][0]
    assert verification_error is not None
    assert verification_error["code"] == "code_change_budget_exceeded"


def test_agent_created_root_pytest_cache_is_accepted_and_reported(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".gitignore").write_text(".pytest_cache/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore pytest cache")
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        max_changed_files=0,
        max_added_lines=0,
    )

    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    cache = repo / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "nodeids").write_text("[]\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification_error is None
    assert verification["verified"] is True
    assert verification["changed_files"] == []
    assert verification["ignored_drift_paths"] == []
    assert verification["ignored_cache_drift_paths"] == [
        ".pytest_cache/v/cache/nodeids"
    ]
    assert verification["line_budget_authoritative"] is True


def test_agent_created_nested_pycache_pyc_is_accepted_and_reported(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore Python bytecode cache")
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(registry, repo, max_changed_files=0)

    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    cache = repo / "package" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "x.pyc").write_bytes(b"test bytecode")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification_error is None
    assert verification["verified"] is True
    assert verification["changed_files"] == []
    assert verification["ignored_drift_paths"] == []
    assert verification["ignored_cache_drift_paths"] == [
        "package/__pycache__/x.pyc"
    ]


def test_verification_command_does_not_run_when_preflight_scope_is_invalid(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        verification_commands=[
            "python -c \"from pathlib import Path; Path('verification-ran').write_text('yes')\""
        ],
    )
    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    (repo / "outside.txt").write_text("invalid before verification\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert not (repo / "verification-ran").exists()
    assert verification["verification_commands"] == []
    assert verification["verification_phase"] == "preflight"
    assert verification_error is not None
    assert verification_error["code"] == "code_scope_violation"


def test_verification_command_side_effect_is_caught_by_postflight(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        verification_commands=[
            "python -c \"from pathlib import Path; Path('outside.txt').write_text('side effect')\""
        ],
    )
    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    (repo / "allowed.txt").write_text("base\nchanged\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["verification_commands"][0]["passed"] is True
    assert verification["verification_phase"] == "postflight"
    assert verification["scope_violations"] == ["outside.txt"]
    assert verification_error is not None
    assert verification_error["code"] == "code_scope_violation"


def test_verification_ignored_cache_drift_is_tolerated_and_reported(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".gitignore").write_text(".pytest_cache/\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore pytest cache")
    cache = repo / ".pytest_cache" / "v" / "cache"
    cache.mkdir(parents=True)
    (cache / "nodeids").write_text("old\n", encoding="utf-8")
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        verification_commands=[
            "python -c \"from pathlib import Path; "
            "p=Path('.pytest_cache/v/cache'); p.mkdir(parents=True, exist_ok=True); "
            "(p/'nodeids').write_text('new\\n'); "
            "(p/'lastfailed').write_text('{}\\n')\""
        ],
    )
    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    (repo / "allowed.txt").write_text("base\nagent\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification_error is None
    assert verification["verified"] is True
    assert verification["changed_files"] == ["allowed.txt"]
    assert verification["ignored_drift_paths"] == []
    assert verification["verification_ignored_drift_paths"] == [
        ".pytest_cache/v/cache/lastfailed",
        ".pytest_cache/v/cache/nodeids",
    ]


def test_verification_rejects_same_line_count_tracked_mutation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        verification_commands=[
            "python -c \"from pathlib import Path; "
            "Path('allowed.txt').write_text('base\\ncheck\\n')\""
        ],
    )
    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    (repo / "allowed.txt").write_text("base\nagent\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["added_lines"] == 1
    assert verification["deleted_lines"] == 0
    assert verification_error is not None
    assert verification_error["code"] == "code_verification_result_mutated"
    assert (
        verification["delegated_result_fingerprint"]
        != verification["post_verification_result_fingerprint"]
    )


def test_verification_rejects_mutation_of_agent_touched_ignored_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore generated log")
    (repo / "ignored.log").write_text("baseline\n", encoding="utf-8")
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(
        registry,
        repo,
        files_in_scope=["allowed.txt", "ignored.log"],
        verification_commands=[
            "python -c \"from pathlib import Path; "
            "Path('ignored.log').write_text('verification\\n')\""
        ],
    )
    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    (repo / "ignored.log").write_text("agent\n", encoding="utf-8")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["ignored_drift_paths"] == ["ignored.log"]
    assert verification["verification_agent_ignored_drift_paths"] == ["ignored.log"]
    assert verification_error is not None
    assert (
        verification_error["code"]
        == "code_verification_agent_ignored_path_mutated"
    )


def test_required_commit_rejects_reset_back_to_baseline(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(registry, repo, commit_mode="required")
    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    (repo / "allowed.txt").write_text("base\ncommitted then reset\n", encoding="utf-8")
    _git(repo, "add", "allowed.txt")
    _git(repo, "commit", "-m", "temporary delegate commit")
    _git(repo, "reset", "--hard", str(baseline["head"]))

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["verified"] is False
    assert verification["commit_requirement_missing"] is True
    assert verification_error is not None
    assert verification_error["code"] == "code_commit_required"


def test_allowed_commit_rejects_history_rewrite(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(registry, repo, commit_mode="allowed")
    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    _git(repo, "commit", "--amend", "-m", "rewritten baseline")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification["verified"] is False
    assert verification["commit_non_fast_forward"] is True
    assert verification["commit_violation"] is True
    assert verification_error is not None
    assert verification_error["code"] == "code_commit_mode_violation"


def test_required_commit_accepts_forward_descendant_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    registry = ExecutorRegistry(codex_command="true")
    task = _make_verified_task(registry, repo, commit_mode="required")
    baseline, error = capture_code_baseline(task)
    assert error is None
    assert baseline is not None
    (repo / "allowed.txt").write_text("base\nforward commit\n", encoding="utf-8")
    _git(repo, "add", "allowed.txt")
    _git(repo, "commit", "-m", "delegate change")

    verification, verification_error = verify_code_result(task, baseline=baseline)

    assert verification_error is None
    assert verification["verified"] is True
    assert verification["commit_non_fast_forward"] is False
    assert verification["commits_since_baseline"] == 1
