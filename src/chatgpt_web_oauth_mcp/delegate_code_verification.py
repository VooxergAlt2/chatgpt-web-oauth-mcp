from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .delegate_models import DelegateTask
from .process_env import sanitized_child_env


DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 300
MAX_VERIFICATION_OUTPUT_CHARS = 12000
VERIFICATION_TERM_GRACE_SECONDS = 0.5
VERIFICATION_KILL_GRACE_SECONDS = 2.0
MAX_IGNORED_BASELINE_FILES = 5000
MAX_IGNORED_BASELINE_BYTES = 256 * 1024 * 1024


def contract_hash(contract: dict[str, object] | None) -> str | None:
    if not isinstance(contract, dict):
        return None
    encoded = json.dumps(
        contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def build_code_contract(
    *,
    files_in_scope: list[str],
    acceptance_criteria: list[str],
    done_means: list[str],
    verification_commands: list[str],
    max_changed_files: int | None,
    max_added_lines: int | None,
    max_deleted_lines: int | None,
) -> dict[str, object] | None:
    """Build the server-owned acceptance contract for one code delegate.

    The contract is enabled only when the caller supplied at least one
    machine-checkable boundary or verification command. This preserves
    compatibility for legacy code-delegate calls while allowing new callers to
    opt into verified completion.
    """

    enabled = bool(
        files_in_scope
        or verification_commands
        or max_changed_files is not None
        or max_added_lines is not None
        or max_deleted_lines is not None
    )
    if not enabled:
        return None
    contract: dict[str, object] = {
        "version": 1,
        "files_in_scope": list(files_in_scope),
        "acceptance_criteria": list(acceptance_criteria),
        "done_means": list(done_means),
        "verification_commands": list(verification_commands),
        "max_changed_files": max_changed_files,
        "max_added_lines": max_added_lines,
        "max_deleted_lines": max_deleted_lines,
        "verification_timeout_seconds": DEFAULT_VERIFICATION_TIMEOUT_SECONDS,
        "baseline_requires_clean_worktree": True,
    }
    contract["contract_hash"] = contract_hash(contract)
    return contract


def capture_code_baseline(task: DelegateTask) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Capture a clean Git baseline immediately before verified code execution."""

    contract = task.code_contract
    if task.kind != "code" or not isinstance(contract, dict):
        return None, None
    if task.project.git_common_dir is None:
        return None, {
            "code": "code_verification_not_git",
            "message": "Verified code delegates require a Git working tree.",
        }
    head = _git_text(task.cwd, ["rev-parse", "--verify", "HEAD"])
    if head is None:
        return None, {
            "code": "code_verification_baseline_unavailable",
            "message": "Could not resolve the Git HEAD for the code-delegate baseline.",
        }
    status = _git_bytes(task.cwd, ["status", "--porcelain=v1", "-z"])
    if status is None:
        return None, {
            "code": "code_verification_baseline_unavailable",
            "message": "Could not capture the Git working-tree baseline.",
        }
    if status:
        return None, {
            "code": "code_verification_baseline_dirty",
            "message": (
                "Verified code delegates require a clean dedicated working tree. "
                "Create or select a clean worktree before delegation."
            ),
        }
    ignored_manifest, ignored_error = _ignored_manifest(task.cwd)
    if ignored_error is not None:
        return None, ignored_error
    baseline = {
        "head": head.strip(),
        "captured_clean": True,
        "captured_at_execution_start": True,
        "ignored_manifest": ignored_manifest or {},
    }
    return baseline, None


def verify_code_result(
    task: DelegateTask,
    *,
    baseline: dict[str, object] | None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Run server-owned preflight, declared checks, and postflight acceptance."""

    contract = task.code_contract
    if task.kind != "code" or not isinstance(contract, dict):
        return {"enabled": False}, None
    if not isinstance(baseline, dict):
        return {
            "enabled": True,
            "contract_hash": contract.get("contract_hash"),
            "verified": False,
        }, {
            "code": "code_verification_baseline_missing",
            "message": "Verified code delegate completed without a captured baseline.",
        }

    baseline_head = str(baseline.get("head") or "").strip()
    if not baseline_head:
        return {
            "enabled": True,
            "contract_hash": contract.get("contract_hash"),
            "verified": False,
        }, {
            "code": "code_verification_baseline_missing",
            "message": "Verified code delegate baseline did not contain a Git HEAD.",
        }

    preflight, preflight_error = _inspect_code_state(
        task,
        contract=contract,
        baseline=baseline,
    )
    if preflight_error is not None:
        payload = _verification_payload(contract, baseline, verified=False)
        payload.update(preflight)
        payload["verification_commands"] = []
        payload["verification_phase"] = "preflight"
        return payload, preflight_error

    delegated_fingerprint, fingerprint_error = _non_ignored_result_fingerprint(
        task.cwd,
        baseline_head,
    )
    if fingerprint_error is not None or delegated_fingerprint is None:
        payload = _verification_payload(contract, baseline, verified=False)
        payload.update(preflight)
        payload["verification_commands"] = []
        payload["verification_phase"] = "preflight"
        return payload, fingerprint_error or {
            "code": "code_verification_fingerprint_unavailable",
            "message": "Could not fingerprint the delegated repository result.",
        }
    ignored_before_verification, ignored_error = _ignored_manifest(task.cwd)
    if ignored_error is not None or ignored_before_verification is None:
        payload = _verification_payload(contract, baseline, verified=False)
        payload.update(preflight)
        payload["verification_commands"] = []
        payload["verification_phase"] = "preflight"
        return payload, ignored_error or {
            "code": "code_verification_ignored_manifest_unavailable",
            "message": "Could not capture ignored files before verification.",
        }

    verification_results = _run_verification_commands(
        task.cwd,
        [str(item) for item in contract.get("verification_commands", []) if str(item).strip()],
        timeout_seconds=_optional_positive_int(
            contract.get("verification_timeout_seconds"),
            DEFAULT_VERIFICATION_TIMEOUT_SECONDS,
        ),
    )
    failed_checks = [
        row for row in verification_results if row.get("passed") is not True
    ]

    post_verification_fingerprint, fingerprint_error = _non_ignored_result_fingerprint(
        task.cwd,
        baseline_head,
    )
    if fingerprint_error is not None or post_verification_fingerprint is None:
        payload = _verification_payload(contract, baseline, verified=False)
        payload.update(preflight)
        payload["delegated_result_fingerprint"] = delegated_fingerprint
        payload["verification_commands"] = verification_results
        payload["verification_phase"] = "postflight"
        return payload, fingerprint_error or {
            "code": "code_verification_fingerprint_unavailable",
            "message": "Could not fingerprint the repository after verification.",
        }
    ignored_after_verification, ignored_error = _ignored_manifest(task.cwd)
    if ignored_error is not None or ignored_after_verification is None:
        payload = _verification_payload(contract, baseline, verified=False)
        payload.update(preflight)
        payload["delegated_result_fingerprint"] = delegated_fingerprint
        payload["post_verification_result_fingerprint"] = post_verification_fingerprint
        payload["verification_commands"] = verification_results
        payload["verification_phase"] = "postflight"
        return payload, ignored_error or {
            "code": "code_verification_ignored_manifest_unavailable",
            "message": "Could not inspect ignored files after verification.",
        }

    verification_ignored_drift = _manifest_drift(
        ignored_before_verification,
        ignored_after_verification,
    )
    agent_ignored_drift = {
        str(path) for path in preflight.get("ignored_drift_paths", [])
    }
    agent_ignored_cache_drift = {
        str(path) for path in preflight.get("ignored_cache_drift_paths", [])
    }
    verification_agent_ignored_drift = sorted(
        agent_ignored_drift.intersection(verification_ignored_drift)
    )

    postflight, postflight_error = _inspect_code_state(
        task,
        contract=contract,
        baseline=baseline,
        ignored_drift_override=sorted(
            agent_ignored_drift | agent_ignored_cache_drift
        ),
    )
    payload = _verification_payload(contract, baseline, verified=False)
    payload.update(postflight)
    payload["delegated_result_fingerprint"] = delegated_fingerprint
    payload["post_verification_result_fingerprint"] = post_verification_fingerprint
    payload["verification_ignored_drift_paths"] = verification_ignored_drift
    payload["verification_agent_ignored_drift_paths"] = (
        verification_agent_ignored_drift
    )
    payload["verification_commands"] = verification_results
    payload["verification_phase"] = "postflight"
    if postflight_error is not None:
        return payload, postflight_error

    if post_verification_fingerprint != delegated_fingerprint:
        return payload, {
            "code": "code_verification_result_mutated",
            "message": (
                "A verification command changed the delegated non-ignored "
                "repository result."
            ),
        }
    if verification_agent_ignored_drift:
        return payload, {
            "code": "code_verification_agent_ignored_path_mutated",
            "message": (
                "A verification command changed an ignored path already changed "
                "by the code delegate: "
                + ", ".join(verification_agent_ignored_drift)
            ),
        }

    if failed_checks:
        failed_names = ", ".join(str(row.get("command") or "") for row in failed_checks)
        return payload, {
            "code": "code_verification_failed",
            "message": f"Server-owned verification failed: {failed_names}",
        }
    payload["verified"] = True
    return payload, None


def _inspect_code_state(
    task: DelegateTask,
    *,
    contract: dict[str, object],
    baseline: dict[str, object],
    ignored_drift_override: list[str] | None = None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    baseline_head = str(baseline.get("head") or "").strip()
    head_after = _git_text(task.cwd, ["rev-parse", "--verify", "HEAD"])
    if head_after is None:
        return {}, {
            "code": "code_verification_git_unavailable",
            "message": "Could not resolve Git HEAD after code delegate execution.",
        }
    normalized_head = head_after.strip()
    if ignored_drift_override is None:
        baseline_ignored = baseline.get("ignored_manifest")
        if not isinstance(baseline_ignored, dict):
            return {}, {
                "code": "code_verification_baseline_missing",
                "message": "Verified code baseline did not contain ignored-file audit state.",
            }
        ignored_after, ignored_error = _ignored_manifest(task.cwd)
        if ignored_error is not None or ignored_after is None:
            return {}, ignored_error or {
                "code": "code_verification_ignored_manifest_unavailable",
                "message": "Could not inspect ignored files during code verification.",
            }
        ignored_drift = _manifest_drift(
            {str(key): value for key, value in baseline_ignored.items()},
            ignored_after,
        )
    else:
        ignored_drift = sorted(set(ignored_drift_override))

    ignored_cache_drift = [
        path for path in ignored_drift if _is_transient_python_cache_path(path)
    ]
    ignored_drift = [
        path for path in ignored_drift if not _is_transient_python_cache_path(path)
    ]

    changed_files = _changed_files(task.cwd, baseline_head)
    if changed_files is None:
        return {}, {
            "code": "code_verification_diff_unavailable",
            "message": "Could not compute changed files for verified code delegate.",
        }
    changed_files = sorted(set(changed_files) | set(ignored_drift))
    line_counts = _diff_line_counts(task.cwd, baseline_head)
    if line_counts is None:
        return {}, {
            "code": "code_verification_diff_unavailable",
            "message": "Could not compute line changes for verified code delegate.",
        }
    additions, deletions = line_counts
    scope_violations = _scope_violations(
        changed_files,
        [str(item) for item in contract.get("files_in_scope", []) if str(item).strip()],
    )
    max_changed_files = _optional_nonnegative_int(contract.get("max_changed_files"))
    max_added_lines = _optional_nonnegative_int(contract.get("max_added_lines"))
    max_deleted_lines = _optional_nonnegative_int(contract.get("max_deleted_lines"))
    budget_violations: list[str] = []
    if max_changed_files is not None and len(changed_files) > max_changed_files:
        budget_violations.append(
            f"changed_files={len(changed_files)} exceeds max_changed_files={max_changed_files}"
        )
    if ignored_drift and (max_added_lines is not None or max_deleted_lines is not None):
        budget_violations.append(
            "ignored file changes prevent authoritative line-budget accounting: "
            + ", ".join(ignored_drift)
        )
    if max_added_lines is not None and additions > max_added_lines:
        budget_violations.append(
            f"added_lines={additions} exceeds max_added_lines={max_added_lines}"
        )
    if max_deleted_lines is not None and deletions > max_deleted_lines:
        budget_violations.append(
            f"deleted_lines={deletions} exceeds max_deleted_lines={max_deleted_lines}"
        )

    commit_violation = False
    commit_requirement_missing = False
    commit_non_fast_forward = False
    commits_since_baseline = 0
    if normalized_head != baseline_head and task.commit_mode != "forbidden":
        is_descendant = _git_is_ancestor(task.cwd, baseline_head, normalized_head)
        if is_descendant is None:
            return {}, {
                "code": "code_verification_git_unavailable",
                "message": "Could not validate delegated commit ancestry.",
            }
        commit_non_fast_forward = not is_descendant
    if task.commit_mode == "forbidden" and normalized_head != baseline_head:
        commit_violation = True
    elif task.commit_mode == "required" and normalized_head == baseline_head:
        commit_requirement_missing = True
    elif normalized_head != baseline_head and commit_non_fast_forward:
        commit_violation = True
    elif task.commit_mode == "required":
        commit_count = _git_commit_count(task.cwd, baseline_head, normalized_head)
        if commit_count is None:
            return {}, {
                "code": "code_verification_git_unavailable",
                "message": "Could not count delegated commits since the baseline.",
            }
        commits_since_baseline = commit_count
        commit_requirement_missing = commit_count < 1

    state = {
        "head_after": normalized_head,
        "changed_files": changed_files,
        "changed_file_count": len(changed_files),
        "added_lines": additions,
        "deleted_lines": deletions,
        "scope_violations": scope_violations,
        "budget_violations": budget_violations,
        "ignored_changed_files": ignored_drift,
        "ignored_drift_paths": ignored_drift,
        "ignored_cache_drift_paths": ignored_cache_drift,
        "line_budget_authoritative": not ignored_drift,
        "commit_violation": commit_violation,
        "commit_requirement_missing": commit_requirement_missing,
        "commit_non_fast_forward": commit_non_fast_forward,
        "commits_since_baseline": commits_since_baseline,
    }
    if scope_violations:
        return state, {
            "code": "code_scope_violation",
            "message": (
                "Code delegate changed files outside files_in_scope: "
                + ", ".join(scope_violations)
            ),
        }
    if budget_violations:
        return state, {
            "code": "code_change_budget_exceeded",
            "message": "; ".join(budget_violations),
        }
    if commit_violation:
        reason = (
            "Code delegate moved HEAD outside the baseline ancestry."
            if commit_non_fast_forward
            else "Code delegate changed HEAD while commit_mode=forbidden."
        )
        return state, {
            "code": "code_commit_mode_violation",
            "message": reason,
        }
    if commit_requirement_missing:
        return state, {
            "code": "code_commit_required",
            "message": (
                "Code delegate did not create a descendant commit while "
                "commit_mode=required."
            ),
        }
    return state, None


def _verification_payload(
    contract: dict[str, object],
    baseline: dict[str, object],
    *,
    verified: bool,
) -> dict[str, object]:
    return {
        "enabled": True,
        "version": int(contract.get("version") or 1),
        "contract_hash": contract.get("contract_hash"),
        "verified": bool(verified),
        "baseline": dict(baseline),
        "acceptance_criteria": list(contract.get("acceptance_criteria") or []),
        "done_means": list(contract.get("done_means") or []),
    }


def _changed_files(cwd: Path, baseline_head: str) -> list[str] | None:
    tracked = _git_bytes(
        cwd,
        ["diff", "--no-renames", "--name-only", "-z", baseline_head, "--"],
    )
    untracked = _git_bytes(
        cwd,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    if tracked is None or untracked is None:
        return None
    values = {
        item.decode("utf-8", errors="replace").replace(os.sep, "/")
        for blob in (tracked, untracked)
        for item in blob.split(b"\0")
        if item
    }
    return sorted(values)


def _non_ignored_result_fingerprint(
    cwd: Path,
    baseline_head: str,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Fingerprint HEAD, tracked diff bytes, and non-ignored untracked content."""

    head = _git_text(cwd, ["rev-parse", "--verify", "HEAD"])
    tracked_diff = _git_bytes(
        cwd,
        [
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            baseline_head,
            "--",
        ],
    )
    untracked = _git_bytes(
        cwd,
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    if head is None or tracked_diff is None or untracked is None:
        return None, {
            "code": "code_verification_fingerprint_unavailable",
            "message": "Could not read Git state for the delegated result fingerprint.",
        }

    digest = hashlib.sha256()
    normalized_head = head.strip()
    _update_fingerprint_part(digest, b"head", normalized_head.encode("ascii"))
    _update_fingerprint_part(digest, b"tracked-diff", tracked_diff)
    untracked_paths = sorted(item for item in untracked.split(b"\0") if item)
    untracked_bytes = 0
    for raw_path in untracked_paths:
        native_path = os.fsdecode(raw_path)
        path = cwd / native_path
        try:
            stat_result = path.lstat()
            content = (
                os.fsencode(os.readlink(path))
                if stat.S_ISLNK(stat_result.st_mode)
                else path.read_bytes()
            )
        except OSError as exc:
            display_path = native_path.replace(os.sep, "/")
            return None, {
                "code": "code_verification_fingerprint_unavailable",
                "message": (
                    f"Could not read untracked path {display_path!r}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        _update_fingerprint_part(digest, b"untracked-path", raw_path)
        _update_fingerprint_part(
            digest,
            b"untracked-mode",
            int(stat_result.st_mode).to_bytes(8, "big"),
        )
        _update_fingerprint_part(digest, b"untracked-content", content)
        untracked_bytes += len(content)

    return {
        "sha256": digest.hexdigest(),
        "head": normalized_head,
        "tracked_diff_bytes": len(tracked_diff),
        "untracked_file_count": len(untracked_paths),
        "untracked_content_bytes": untracked_bytes,
    }, None


def _update_fingerprint_part(
    digest: Any,
    label: bytes,
    value: bytes,
) -> None:
    digest.update(len(label).to_bytes(8, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _ignored_manifest(
    cwd: Path,
) -> tuple[dict[str, dict[str, int]] | None, dict[str, object] | None]:
    raw = _git_bytes(
        cwd,
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
    )
    if raw is None:
        return None, {
            "code": "code_verification_ignored_manifest_unavailable",
            "message": "Could not enumerate ignored files for verified code execution.",
        }
    raw_paths = [item for item in raw.split(b"\0") if item]
    if len(raw_paths) > MAX_IGNORED_BASELINE_FILES:
        return None, {
            "code": "code_verification_ignored_manifest_too_large",
            "message": (
                "Ignored-file manifest exceeds the bounded file limit of "
                f"{MAX_IGNORED_BASELINE_FILES}."
            ),
        }

    manifest: dict[str, dict[str, int]] = {}
    total_bytes = 0
    for raw_path in raw_paths:
        native_path = os.fsdecode(raw_path)
        display_path = native_path.replace(os.sep, "/")
        try:
            stat_result = (cwd / native_path).lstat()
        except OSError as exc:
            return None, {
                "code": "code_verification_ignored_manifest_unavailable",
                "message": (
                    f"Could not stat ignored path {display_path!r}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        total_bytes += max(0, int(stat_result.st_size))
        if total_bytes > MAX_IGNORED_BASELINE_BYTES:
            return None, {
                "code": "code_verification_ignored_manifest_too_large",
                "message": (
                    "Ignored-file manifest exceeds the bounded byte limit of "
                    f"{MAX_IGNORED_BASELINE_BYTES}."
                ),
            }
        manifest[display_path] = {
            "mode": int(stat_result.st_mode),
            "size": int(stat_result.st_size),
            "mtime_ns": int(stat_result.st_mtime_ns),
            "ctime_ns": int(stat_result.st_ctime_ns),
            "inode": int(stat_result.st_ino),
            "device": int(stat_result.st_dev),
        }
    return manifest, None


def _manifest_drift(
    before: dict[str, object],
    after: dict[str, object],
) -> list[str]:
    return sorted(
        path
        for path in set(before) | set(after)
        if before.get(path) != after.get(path)
    )


def _is_transient_python_cache_path(path: str) -> bool:
    """Return whether an ignored path is below a standard Python cache dir."""

    parts = path.replace("\\", "/").split("/")
    return any(
        part in {".pytest_cache", "__pycache__"}
        and index < len(parts) - 1
        for index, part in enumerate(parts)
    )


def _diff_line_counts(cwd: Path, baseline_head: str) -> tuple[int, int] | None:
    raw = _git_text(cwd, ["diff", "--no-renames", "--numstat", baseline_head, "--"])
    if raw is None:
        return None
    additions = 0
    deletions = 0
    for line in raw.splitlines():
        columns = line.split("\t", 2)
        if len(columns) < 3:
            continue
        if columns[0].isdigit():
            additions += int(columns[0])
        if columns[1].isdigit():
            deletions += int(columns[1])
    untracked = _git_bytes(cwd, ["ls-files", "--others", "--exclude-standard", "-z"])
    if untracked is None:
        return None
    for raw_path in untracked.split(b"\0"):
        if not raw_path:
            continue
        path = cwd / os.fsdecode(raw_path)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if b"\0" in data[:8192]:
            continue
        additions += len(data.splitlines())
    return additions, deletions


def _scope_violations(changed_files: list[str], scopes: list[str]) -> list[str]:
    if not scopes:
        return []
    normalized = [_normalize_scope(scope) for scope in scopes if _normalize_scope(scope)]
    violations: list[str] = []
    for changed in changed_files:
        candidate = changed.replace("\\", "/").lstrip("./")
        if any(_scope_matches(candidate, scope) for scope in normalized):
            continue
        violations.append(candidate)
    return violations


def _normalize_scope(scope: str) -> str:
    value = scope.strip().replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.rstrip("/")


def _scope_matches(path: str, scope: str) -> bool:
    if any(marker in scope for marker in ("*", "?", "[")):
        return fnmatch.fnmatch(path, scope)
    return path == scope or path.startswith(scope + "/")


def _run_verification_commands(
    cwd: Path,
    commands: list[str],
    *,
    timeout_seconds: int,
) -> list[dict[str, object]]:
    """Run server-owned checks within one deadline and isolated process groups."""

    results: list[dict[str, object]] = []
    deadline = time.monotonic() + max(0, timeout_seconds)
    for index, command in enumerate(commands):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.extend(
                _verification_deadline_result(item)
                for item in commands[index:]
            )
            break

        process: subprocess.Popen[bytes] | None = None
        timed_out = False
        return_code: int | None = None
        cleanup_error: str | None = None
        popen_kwargs: dict[str, object] = {
            "cwd": str(cwd),
            "shell": True,
            "stdin": subprocess.DEVNULL,
            "env": sanitized_child_env(),
            "close_fds": True,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        elif os.name == "nt":  # pragma: no cover - deployment target is POSIX.
            popen_kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )

        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            popen_kwargs["stdout"] = stdout_file
            popen_kwargs["stderr"] = stderr_file
            try:
                process = subprocess.Popen(command, **popen_kwargs)
                try:
                    return_code = process.wait(timeout=max(0.001, remaining))
                except subprocess.TimeoutExpired:
                    timed_out = True
                cleanup_error = _cleanup_verification_process(process)
                if process.returncode is not None:
                    return_code = int(process.returncode)
            except OSError as exc:
                cleanup_error = f"{type(exc).__name__}: {exc}"

            stdout, stdout_truncated = _read_bounded_verification_output(stdout_file)
            stderr, stderr_truncated = _read_bounded_verification_output(stderr_file)

        row: dict[str, object] = {
            "command": command,
            "passed": (
                return_code == 0
                and not timed_out
                and cleanup_error is None
            ),
            "exit_code": None if timed_out else return_code,
            "stdout": stdout,
            "stderr": stderr,
        }
        if stdout_truncated:
            row["stdout_truncated"] = True
        if stderr_truncated:
            row["stderr_truncated"] = True
        if timed_out:
            row["timed_out"] = True
        if cleanup_error is not None:
            if process is not None:
                row["cleanup_failed"] = True
            row["error"] = cleanup_error
        results.append(row)

        if timed_out:
            results.extend(
                _verification_deadline_result(item)
                for item in commands[index + 1 :]
            )
            break
    return results


def _verification_deadline_result(command: str) -> dict[str, object]:
    return {
        "command": command,
        "passed": False,
        "exit_code": None,
        "timed_out": True,
        "not_run": True,
        "error": "Aggregate verification deadline was exhausted before this command ran.",
    }


def _read_bounded_verification_output(stream: Any) -> tuple[str, bool]:
    """Read only the bounded prefix from a file-backed verification stream."""

    stream.flush()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    raw = stream.read(MAX_VERIFICATION_OUTPUT_CHARS)
    return (
        raw.decode("utf-8", errors="replace")[:MAX_VERIFICATION_OUTPUT_CHARS],
        size > len(raw),
    )


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_verification_process(process: subprocess.Popen[bytes]) -> str | None:
    """Stop/reap the command and establish that its POSIX group is empty."""

    if os.name == "posix" and hasattr(os, "killpg"):
        process_group_id = process.pid
        try:
            group_exists = _process_group_exists(process_group_id)
        except OSError as exc:
            return f"Could not inspect verification process group: {type(exc).__name__}: {exc}"

        if group_exists:
            try:
                os.killpg(process_group_id, signal.SIGTERM)
            except ProcessLookupError:
                group_exists = False
            except OSError as exc:
                return f"Could not terminate verification process group: {type(exc).__name__}: {exc}"

        term_deadline = time.monotonic() + VERIFICATION_TERM_GRACE_SECONDS
        while group_exists and time.monotonic() < term_deadline:
            time.sleep(0.01)
            group_exists = _process_group_exists(process_group_id)
        if group_exists:
            try:
                os.killpg(
                    process_group_id,
                    getattr(signal, "SIGKILL", signal.SIGTERM),
                )
            except ProcessLookupError:
                group_exists = False
            except OSError as exc:
                return f"Could not kill verification process group: {type(exc).__name__}: {exc}"

        try:
            process.wait(timeout=VERIFICATION_KILL_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Could not reap verification command: {type(exc).__name__}: {exc}"

        reap_deadline = time.monotonic() + VERIFICATION_KILL_GRACE_SECONDS
        while group_exists and time.monotonic() < reap_deadline:
            time.sleep(0.01)
            group_exists = _process_group_exists(process_group_id)
        if group_exists:
            return "Verification process-group descendants remained after SIGKILL."
        return None

    # Non-POSIX cannot reliably prove recursive cleanup with stdlib alone.
    # Reap the direct child and fail closed if even that cannot be established.
    if process.poll() is None:  # pragma: no cover - deployment target is POSIX.
        try:
            process.terminate()
        except OSError:
            pass
    try:  # pragma: no cover - deployment target is POSIX.
        process.wait(timeout=VERIFICATION_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=VERIFICATION_KILL_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"Could not reap verification command: {type(exc).__name__}: {exc}"
    except OSError as exc:  # pragma: no cover
        return f"Could not reap verification command: {type(exc).__name__}: {exc}"
    return None


def _git_is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            check=False,
            timeout=15,
            env=sanitized_child_env(),
            close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode == 0:
        return True
    if completed.returncode == 1:
        return False
    return None


def _git_commit_count(cwd: Path, baseline_head: str, head_after: str) -> int | None:
    raw = _git_text(cwd, ["rev-list", "--count", f"{baseline_head}..{head_after}"])
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _git_text(cwd: Path, args: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=sanitized_child_env(),
            close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _git_bytes(cwd: Path, args: list[str]) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            check=False,
            timeout=15,
            env=sanitized_child_env(),
            close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_positive_int(value: object, default: int) -> int:
    parsed = _optional_nonnegative_int(value)
    if parsed is None or parsed <= 0:
        return default
    return parsed
