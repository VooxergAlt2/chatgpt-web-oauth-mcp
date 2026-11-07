"""ACP writer acceptance from a trusted isolated-worker receipt.

Tests run once in the worker's verification sandbox. The canonical process
finalizer only validates that receipt against its task contract and the raw
promoted source snapshot; it never runs repository code or Git status/diff.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .acp_session_store import AcpStateError
from .acp_workspace import capture_source_snapshot, validate_permitted_paths

RECEIPT_VERSION = 2
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_REVIEW_DIFF_BYTES = 12288
MAX_LINE_FILE_BYTES = 2 * 1024 * 1024
MAX_LINE_COUNT = 100000


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _preflight_commands(contract: dict) -> list[str]:
    from .delegate_code_verification import validate_verification_preflight_commands
    try:
        return validate_verification_preflight_commands(contract.get("verification_preflight_commands"))
    except ValueError:
        raise AcpStateError("acp_verification_preflight_commands_invalid") from None


def _runtime_identity(contract):
    if "verification_runtime" not in contract:
        return None
    from .acp_verification_runtime import validate_runtime_identity
    return validate_runtime_identity(contract["verification_runtime"])


def _runtime_proof(contract, identity):
    expected = _runtime_identity(contract)
    if expected is None:
        if identity is not None:
            raise AcpStateError("acp_verification_runtime_mismatch")
        return None
    from .acp_verification_runtime import validate_runtime_identity
    try:
        actual = validate_runtime_identity(identity)
    except AcpStateError:
        raise AcpStateError("acp_verification_runtime_mismatch") from None
    if actual != expected:
        raise AcpStateError("acp_verification_runtime_mismatch")
    return actual


def _contract(contract):
    if not isinstance(contract, dict):
        raise AcpStateError("acp_code_contract_invalid")
    from .delegate_code_verification import contract_hash
    expected = contract_hash({key: value for key, value in contract.items() if key != "contract_hash"})
    if not expected or contract.get("contract_hash") != expected:
        raise AcpStateError("acp_code_contract_invalid")
    raw_scopes = contract.get("files_in_scope")
    if not isinstance(raw_scopes, list):
        raise AcpStateError("acp_code_contract_invalid")
    scopes = validate_permitted_paths(raw_scopes)
    commands = contract.get("verification_commands")
    if (not isinstance(commands, list) or not commands or len(commands) > 16
            or any(not isinstance(item, str) or not item.strip() or "\0" in item
                   or len(item.encode("utf-8")) > 8192
                   for item in commands)):
        raise AcpStateError("acp_code_verification_commands_required")
    _preflight_commands(contract)
    _runtime_identity(contract)
    for key in ("max_changed_files", "max_added_lines", "max_deleted_lines"):
        value = contract.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise AcpStateError("acp_code_contract_invalid")
    return scopes, commands


def _text(root: Path, relative: str, expected: dict | None) -> str:
    if expected is None:
        return ""
    if expected["size"] > MAX_LINE_FILE_BYTES:
        raise AcpStateError("acp_code_line_accounting_limit")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    opened = []
    current = root_fd
    try:
        parts = Path(relative).parts
        for index, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            current = os.open(part, flags, dir_fd=current)
            opened.append(current)
        metadata = os.fstat(current)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise AcpStateError("acp_code_result_changed")
        chunks = []
        size = 0
        while chunk := os.read(current, min(65536, MAX_LINE_FILE_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_LINE_FILE_BYTES:
                raise AcpStateError("acp_code_line_accounting_limit")
        data = b"".join(chunks)
        if len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
            raise AcpStateError("acp_code_result_changed")
        if b"\0" in data:
            raise AcpStateError("acp_code_binary_change")
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise AcpStateError("acp_code_binary_change") from None
        if len(text.splitlines()) > MAX_LINE_COUNT:
            raise AcpStateError("acp_code_line_accounting_limit")
        return text
    finally:
        for fd in reversed(opened):
            os.close(fd)
        os.close(root_fd)


def inspect_candidate(workspace, contract: dict) -> dict:
    """Validate scope and line/file budgets before any verification/promotion."""
    scopes, _ = _contract(contract)
    manifest = workspace.result_manifest()
    baseline = workspace.baseline_manifest()
    changed = sorted(path for path in manifest.keys() | baseline.keys()
                     if manifest.get(path) != baseline.get(path))
    if set(changed) - set(scopes) or set(workspace.permitted_paths) != set(scopes):
        raise AcpStateError("acp_code_scope_violation")
    added = deleted = 0
    for path in changed:
        before = _text(workspace.baseline_root, path, baseline.get(path)).splitlines(keepends=True)
        after = _text(workspace.root, path, manifest.get(path)).splitlines(keepends=True)
        for kind, i, j, x, y in difflib.SequenceMatcher(None, before, after).get_opcodes():
            if kind in {"insert", "replace"}:
                added += y - x
            if kind in {"delete", "replace"}:
                deleted += j - i
    for key, actual in (("max_changed_files", len(changed)),
                        ("max_added_lines", added), ("max_deleted_lines", deleted)):
        limit = contract.get(key)
        if limit is not None and actual > limit:
            raise AcpStateError("acp_code_change_budget_exceeded")
    return {"snapshot_digest": manifest_digest(manifest),
            "baseline_digest": manifest_digest(baseline),
            "changed_files": changed, "changed_file_count": len(changed),
            "added_lines": added, "deleted_lines": deleted,
            "line_budget_authoritative": True,
            "manifest": {path: manifest.get(path) for path in changed}}


def _checks(contract, results):
    _, commands = _contract(contract)
    return _check_results(commands, results)


def _preflight_checks(contract, results):
    commands = _preflight_commands(contract)
    if not commands and results is None:
        results = []
    return _check_results(commands, results)


def _check_results(commands, results):
    if not isinstance(results, list) or len(results) != len(commands):
        raise AcpStateError("acp_code_verification_receipt_invalid")
    normalized = []
    for command, row in zip(commands, results, strict=True):
        if (not isinstance(row, dict) or row.get("command") != command
                or row.get("passed") is not True or type(row.get("exit_code")) is not int
                or row["exit_code"] != 0
                or any(row.get(flag) for flag in ("timed_out", "cancelled", "not_run", "cleanup_failed",
                                                 "output_limit_exceeded", "capture_failed", "error"))):
            raise AcpStateError("acp_code_verification_failed")
        normalized.append({"command": command, "passed": True, "exit_code": 0,
                           "stdout": str(row.get("stdout", ""))[:12000],
                           "stderr": str(row.get("stderr", ""))[:12000],
                           "stdout_truncated": bool(row.get("stdout_truncated")),
                           "stderr_truncated": bool(row.get("stderr_truncated"))})
    return normalized


def _review_evidence(workspace, candidate: dict) -> dict:
    """Capture exact before/after evidence while both immutable views exist."""
    before = workspace.baseline_manifest()
    after = workspace.result_manifest()
    parts = []
    size = 0
    truncated = False
    returned_files = []
    for path in candidate["changed_files"]:
        old = _text(workspace.baseline_root, path, before.get(path))
        new = _text(workspace.root, path, after.get(path))
        lines = difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile="a/" + path if path in before else "/dev/null",
            tofile="b/" + path if path in after else "/dev/null",
        )
        returned_files.append(path)
        for line in lines:
            if not line.endswith("\n"):
                line += "\n\\ No newline at end of file\n"
            length = len(line.encode("utf-8"))
            if size + length > MAX_REVIEW_DIFF_BYTES:
                truncated = True
                break
            parts.append(line)
            size += length
        if truncated:
            break
    diff = "".join(parts)
    return {
        "version": 1, "origin": "server_private_candidate",
        "baseline_digest": candidate["baseline_digest"],
        "snapshot_digest": candidate["snapshot_digest"],
        "diff": diff, "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        "truncated": truncated, "returned_files": returned_files,
        "total_changed_files": len(candidate["changed_files"]),
    }


def _validated_review_evidence(receipt: dict) -> dict:
    evidence = receipt.get("review_evidence")
    required = {"version", "origin", "baseline_digest", "snapshot_digest", "diff",
                "diff_sha256", "truncated", "returned_files", "total_changed_files"}
    if (not isinstance(evidence, dict) or set(evidence) != required
            or evidence["version"] != 1 or evidence["origin"] != "server_private_candidate"
            or evidence["baseline_digest"] != receipt["baseline_digest"]
            or evidence["snapshot_digest"] != receipt["snapshot_digest"]
            or not isinstance(evidence["diff"], str)
            or len(evidence["diff"].encode()) > MAX_REVIEW_DIFF_BYTES
            or hashlib.sha256(evidence["diff"].encode()).hexdigest() != evidence["diff_sha256"]
            or type(evidence["truncated"]) is not bool
            or not isinstance(evidence["returned_files"], list)
            or evidence["returned_files"] != receipt["changed_files"][:len(evidence["returned_files"])]
            or type(evidence["total_changed_files"]) is not int
            or evidence["total_changed_files"] != receipt["changed_file_count"]
            or (not evidence["truncated"] and evidence["returned_files"] != receipt["changed_files"])):
        raise AcpStateError("acp_code_review_evidence_invalid")
    return dict(evidence)


def build_verification_receipt(*, delegate_id: str, contract: dict, baseline: dict,
                               workspace, verification_results: list,
                               expected_digest: str, preflight_results: list | None = None,
                               runtime_identity: dict | None = None) -> dict:
    """Construct a compact receipt before CAS promotion, after native teardown."""
    if (not isinstance(delegate_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", delegate_id)
            or not isinstance(baseline, dict) or baseline.get("captured_clean") is not True
            or baseline.get("head") != workspace.baseline_head):
        raise AcpStateError("acp_code_baseline_mismatch")
    candidate = inspect_candidate(workspace, contract)
    if (baseline.get("snapshot_digest") != candidate["baseline_digest"]
            or expected_digest != candidate["snapshot_digest"]):
        raise AcpStateError("acp_code_result_changed")
    results = _checks(contract, verification_results)
    preflight = _preflight_checks(contract, preflight_results)
    runtime = _runtime_proof(contract, runtime_identity)
    receipt = {"version": RECEIPT_VERSION, "delegate_id": delegate_id,
               "contract_hash": contract["contract_hash"], "baseline_head": baseline["head"],
               "baseline_digest": baseline["snapshot_digest"], **candidate,
               "verification_commands": results, "verification_level": "checks_passed",
               "execution_boundary": "acp_verification_sandbox",
               "review_evidence": _review_evidence(workspace, candidate)}
    if preflight:
        receipt["verification_preflight_commands"] = preflight
    if runtime is not None:
        receipt["verification_runtime"] = runtime
    if len(json.dumps(receipt, ensure_ascii=False).encode()) > MAX_RECEIPT_BYTES:
        raise AcpStateError("acp_code_receipt_too_large")
    return receipt


def capture_acp_code_baseline(task):
    try:
        _contract(task.code_contract)
        snapshot = capture_source_snapshot(task.cwd, require_clean=True)
        return {"head": snapshot["head"], "snapshot_digest": snapshot["digest"],
                "captured_clean": True, "captured_at_execution_start": True,
                "backend": "acp"}, None
    except (AcpStateError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, AcpStateError) else "acp_code_baseline_unavailable"
        return None, {"code": code, "message": "ACP source baseline could not be validated."}


def verify_acp_code_receipt(task, *, baseline, receipt):
    """Canonical postflight: trusted worker evidence plus fresh raw source hash."""
    contract = task.code_contract
    payload = {"enabled": True, "version": 1, "verified": False,
               "contract_hash": contract.get("contract_hash") if isinstance(contract, dict) else None,
               "verification_phase": "postflight", "verification_backend": "acp_receipt"}
    try:
        scopes, commands = _contract(contract)
        if (task.commit_mode != "forbidden" or not isinstance(baseline, dict)
                or baseline.get("captured_clean") is not True):
            raise AcpStateError("acp_code_baseline_mismatch")
        required = {"version", "delegate_id", "contract_hash", "baseline_head", "baseline_digest",
                    "snapshot_digest", "changed_files", "changed_file_count", "added_lines", "deleted_lines",
                    "line_budget_authoritative", "manifest", "verification_commands",
                    "verification_level", "execution_boundary", "review_evidence"}
        if _preflight_commands(contract):
            required.add("verification_preflight_commands")
        if _runtime_identity(contract) is not None:
            required.add("verification_runtime")
        if (not isinstance(receipt, dict) or set(receipt) != required
                or receipt.get("version") != RECEIPT_VERSION
                or receipt.get("delegate_id") != task.delegate_id
                or receipt.get("contract_hash") != contract["contract_hash"]
                or receipt.get("baseline_head") != baseline.get("head")
                or receipt.get("baseline_digest") != baseline.get("snapshot_digest")
                or receipt.get("execution_boundary") != "acp_verification_sandbox"
                or receipt.get("verification_level") != "checks_passed"
                or receipt.get("line_budget_authoritative") is not True
                or not isinstance(receipt.get("snapshot_digest"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", receipt["snapshot_digest"])):
            raise AcpStateError("acp_code_verification_receipt_invalid")
        changed = receipt["changed_files"]
        if (not isinstance(changed, list) or any(not isinstance(path, str) for path in changed)
                or changed != sorted(set(changed)) or set(changed) - set(scopes)
                or type(receipt["changed_file_count"]) is not int
                or receipt["changed_file_count"] != len(changed)
                or not isinstance(receipt["manifest"], dict) or set(receipt["manifest"]) != set(changed)):
            raise AcpStateError("acp_code_scope_violation")
        for key, field in (("max_changed_files", "changed_file_count"),
                           ("max_added_lines", "added_lines"), ("max_deleted_lines", "deleted_lines")):
            actual = receipt[field]
            limit = contract.get(key)
            if type(actual) is not int or actual < 0 or (limit is not None and actual > limit):
                raise AcpStateError("acp_code_change_budget_exceeded")
        evidence = _validated_review_evidence(receipt)
        results = _checks(contract, receipt["verification_commands"])
        preflight = _preflight_checks(contract, receipt.get("verification_preflight_commands"))
        runtime = _runtime_proof(contract, receipt.get("verification_runtime"))
        snapshot = capture_source_snapshot(task.cwd, require_clean=False)
        if (snapshot["head"] != baseline.get("head") or snapshot["digest"] != receipt["snapshot_digest"]
                or any(snapshot["manifest"].get(path) != record for path, record in receipt["manifest"].items())):
            raise AcpStateError("acp_code_promoted_result_changed")
        payload.update({key: receipt[key] for key in
                        ("changed_files", "changed_file_count", "added_lines", "deleted_lines",
                         "line_budget_authoritative", "verification_level")})
        payload.update({"verified": True, "baseline": dict(baseline), "head_after": snapshot["head"],
                        "verification_commands": results, "scope_violations": [], "budget_violations": [],
                        "commit_violation": False, "commit_requirement_missing": False,
                        "commit_non_fast_forward": False, "commits_since_baseline": 0,
                        "delegated_result_fingerprint": receipt["snapshot_digest"],
                        "post_verification_result_fingerprint": snapshot["digest"],
                        "acceptance_criteria": list(contract.get("acceptance_criteria", [])),
                        "done_means": list(contract.get("done_means", [])),
                        "review_evidence": evidence})
        if preflight:
            payload["verification_preflight_commands"] = preflight
        if runtime is not None:
            payload["verification_runtime"] = runtime
        return payload, None
    except (AcpStateError, OSError, TypeError, ValueError) as exc:
        code = exc.code if isinstance(exc, AcpStateError) else "acp_code_verification_receipt_invalid"
        return payload, {"code": code, "message": "ACP isolated verification receipt or promoted source did not match."}
