"""Bounded, deterministic evidence checks for local factual-research results.

This does not claim semantic correctness. It only verifies that a factual
research answer points to existing lines of explicitly scoped source files.
"""

from __future__ import annotations

import hashlib
import fnmatch
import re
import subprocess
from pathlib import Path
from typing import Mapping

_LINE_REFERENCE = re.compile(
    r"(?<![\w/])(?P<path>[\w./+-]+\.[\w+-]+):(?P<line>[1-9][0-9]{0,5})(?![0-9])"
)
_MAX_LINE_REFERENCES = 12
_MAX_CRITERIA = 16
_MAX_NEGATIVE_SCOPE_FILES = 12
_MAX_NEGATIVE_FILE_BYTES = 2_000_000
_MAX_NEGATIVE_TOTAL_BYTES = 8_000_000
_ABSENCE_SUBJECT = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]{3,99})`|"
    r"(?<![\w])(__[A-Za-z_][A-Za-z0-9_]{5,99}|"
    r"[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]{5,99})(?![\w])"
)
_ABSENCE_CLAIM = re.compile(
    r"\b(?:not\s+(?:present|found|defined|documented)|"
    r"absent|missing|no\s+(?:matches|occurrences|definition))\b|"
    r"\b(?:не\s+найден|не\s+определён|отсутствует|совпадений\s+нет)\b",
    re.IGNORECASE,
)
_ABSENCE_ALLOWED = re.compile(
    r"\b(?:if\s+(?:absent|missing|not\s+found)|"
    r"or\s+(?:report\s+)?absence|report\s+if\s+(?:absent|missing))\b|"
    r"\b(?:если\s+(?:отсутствует|не\s+найден)|при\s+отсутствии)\b",
    re.IGNORECASE,
)
_WRONG_WORKFLOW = (
    "no concrete review candidate",
    "no review candidates",
    "verifier skipped because there were no candidates",
)


_MAX_REVIEW_FINDINGS = 12
_MAX_FINDING_TEXT_CHARS = 4096


def assess_delegate_findings(
    *,
    root: Path,
    files_in_scope: tuple[str, ...],
    structured_output: object,
) -> dict[str, object]:
    """Advisory, bounded source-line coverage for any explore delegate.

    A line anchor proves a source line exists in the *current* working tree.
    This is not semantic verification, test acceptance or a claim that this
    content equals the Git blob at HEAD.
    """
    if not files_in_scope:
        return {
            "status": "not_evaluated", "reason": "no_explicit_file_scope",
            "semantics_verified": False,
        }
    if not isinstance(structured_output, Mapping):
        return {
            "status": "not_evaluated", "reason": "no_structured_manifest",
            "semantics_verified": False,
        }
    findings = structured_output.get("findings")
    if not isinstance(findings, list) or not all(isinstance(f, str) for f in findings):
        return {
            "status": "not_evaluated", "reason": "invalid_findings_manifest",
            "semantics_verified": False,
        }
    source_commit: str | None = None
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, check=False, timeout=3,
        )
        if head.returncode == 0 and re.fullmatch(
            r"[0-9a-fA-F]{40,64}", head.stdout.strip()
        ):
            source_commit = head.stdout.strip().lower()
    except (OSError, subprocess.TimeoutExpired):
        pass
    checked = [
        item[:_MAX_FINDING_TEXT_CHARS]
        for item in findings[:_MAX_REVIEW_FINDINGS]
    ]
    # Resolve ONLY the paths that the agent actually cited. Accept an
    # explicitly scoped directory or glob without crawling the repository.
    # Symlink escapes and unscoped citations never become approved evidence.
    base = root.resolve()
    allowed: list[tuple[Path | None, str | None]] = []
    for scope in files_in_scope[:32]:
        normalized = scope.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if not normalized:
            continue
        if any(char in normalized for char in "*?["):
            allowed.append((None, normalized))
        else:
            candidate = (base / normalized).resolve()
            if candidate.is_relative_to(base):
                allowed.append((candidate, None))
    referenced: list[str] = []
    for finding in checked:
        for match in list(_LINE_REFERENCE.finditer(finding))[:_MAX_LINE_REFERENCES]:
            rel_path = match.group("path")
            candidate = (base / rel_path).resolve()
            if not candidate.is_relative_to(base) or not candidate.is_file():
                continue
            if not any(
                (
                    scoped is not None
                    and (candidate == scoped or (
                        scoped.is_dir() and candidate.is_relative_to(scoped)
                    ))
                ) or (
                    pattern is not None and fnmatch.fnmatch(rel_path, pattern)
                )
                for scoped, pattern in allowed
            ):
                continue
            if rel_path not in referenced:
                referenced.append(rel_path)
            if len(referenced) >= _MAX_REVIEW_FINDINGS * _MAX_LINE_REFERENCES:
                break
    records: list[dict[str, object]] = []
    anchored = 0
    for index, finding in enumerate(checked, start=1):
        evidence = assess_research_evidence(
            root=root,
            files_in_scope=tuple(referenced),
            structured_output={"summary": finding, "findings": []},
        )
        valid = evidence.get("status") == "evidence_anchored"
        if valid:
            anchored += 1
        records.append({
            "index": index,
            "status": "line_exists" if valid else "unanchored",
            "anchors": list(evidence.get("anchors") or [])[:_MAX_LINE_REFERENCES],
        })
    truncated = len(findings) > _MAX_REVIEW_FINDINGS
    if not findings:
        status = "no_findings"
    elif anchored == len(findings) and not truncated:
        status = "all_findings_anchored"
    elif anchored == 0:
        status = "unanchored"
    else:
        status = "partial_coverage"
    return {
        "status": status,
        "reason": "source_line_existence_only",
        "source_commit": source_commit,
        "source_state": "working_tree_at_evaluation",
        "semantics_verified": False,
        "finding_count": len(findings),
        "checked_finding_count": len(checked),
        "anchored_finding_count": anchored,
        "truncated": truncated,
        "findings": records,
    }


def _verify_scoped_literal_absence(
    *,
    root: Path,
    files_in_scope: tuple[str, ...],
    criterion: str,
    statement: str,
) -> dict[str, object] | None:
    """Independently prove a narrow negative claim over whole scoped files.

    Do not trust a delegate's search counts or partial command output.
    Refuse ambiguous identifiers, missing files, oversized/binary sources,
    and file mutations during the bounded scan.
    """
    if not _ABSENCE_ALLOWED.search(criterion) or not _ABSENCE_CLAIM.search(statement):
        return None
    subjects = {
        match.group(1) or match.group(2)
        for match in _ABSENCE_SUBJECT.finditer(criterion)
    }
    if len(subjects) != 1:
        return None
    subject = next(iter(subjects))
    if subject not in statement:
        return None
    if not 0 < len(files_in_scope) <= _MAX_NEGATIVE_SCOPE_FILES:
        return None
    base = root.resolve()
    scanned: list[dict[str, object]] = []
    total = 0
    for name in files_in_scope:
        path = (base / name).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            return None
        try:
            before = path.stat()
            if before.st_size > _MAX_NEGATIVE_FILE_BYTES:
                return None
            with path.open("rb") as handle:
                data = handle.read(_MAX_NEGATIVE_FILE_BYTES + 1)
            after = path.stat()
        except OSError:
            return None
        if (
            len(data) > _MAX_NEGATIVE_FILE_BYTES
            or (before.st_size, before.st_mtime_ns)
            != (after.st_size, after.st_mtime_ns)
            or b"\x00" in data
        ):
            return None
        total += len(data)
        if total > _MAX_NEGATIVE_TOTAL_BYTES:
            return None
        offset = data.find(subject.encode("ascii"))
        if offset != -1:
            return {
                # Literal presence does not prove the identifier is defined.
                # It does disprove a bare literal-absence claim, and requires
                # semantic definition review rather than automatic acceptance.
                "status": "literal_present",
                "subject": subject,
                "counterexample": {
                    "path": name,
                    "line": data[:offset].count(b"\n") + 1,
                },
            }
        scanned.append({
            "path": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        })
    return {
        "status": "absence_verified",
        "subject": subject,
        "comparison": "exact_case_sensitive_literal",
        "scanned_files": scanned,
        "scanned_bytes": total,
    }


def assess_research_evidence(
    *,
    root: Path,
    files_in_scope: tuple[str, ...],
    structured_output: object,
    acceptance_criteria: tuple[str, ...] = (),
) -> dict[str, object]:
    """Report source anchoring separately from process success.

    A valid anchor proves the referenced line exists, not that the conclusion
    about that line is true. Broader tasks without an explicit source scope
    remain unassessed rather than being scored as evidence failures.
    """
    if not files_in_scope:
        if acceptance_criteria:
            return {
                "status": "criteria_incomplete",
                "reason": "cannot_verify_criteria_without_explicit_file_scope",
                "criteria_count": len(acceptance_criteria),
                "criteria_source_anchored_count": 0,
                "review_handoff": {
                    "status": "needs_source_scope",
                    "files_in_scope": [],
                    "questions": [
                        {"index": index, "criterion": text[:400], "issue": "no_source_scope"}
                        for index, text in enumerate(
                            acceptance_criteria[:_MAX_CRITERIA], start=1
                        )
                    ],
                },
            }
        return {"status": "not_evaluated", "reason": "no_explicit_file_scope"}
    if not isinstance(structured_output, Mapping):
        return {"status": "missing_evidence", "reason": "no_structured_manifest"}
    summary = str(structured_output.get("summary") or "")
    findings = structured_output.get("findings")
    if not isinstance(findings, list) or not all(isinstance(x, str) for x in findings):
        return {"status": "missing_evidence", "reason": "invalid_findings"}
    answer = "\n".join([summary, *findings])
    if any(fragment in answer.lower() for fragment in _WRONG_WORKFLOW):
        result: dict[str, object] = {
            "status": "wrong_workflow", "reason": "defect_review_instead_of_research",
        }
        if acceptance_criteria:
            result["criteria_count"] = len(acceptance_criteria)
            result["criteria_source_anchored_count"] = 0
            result["review_handoff"] = {
                "status": "needs_targeted_verification",
                "files_in_scope": list(files_in_scope[:_MAX_LINE_REFERENCES]),
                "questions": [
                    {"index": index, "criterion": text[:400], "issue": "wrong_workflow"}
                    for index, text in enumerate(
                        acceptance_criteria[:_MAX_CRITERIA], start=1
                    )
                ],
            }
        return result
    if not answer.strip():
        return {"status": "missing_evidence", "reason": "empty_answer"}

    resolved_scope: dict[str, Path] = {}
    base = root.resolve()
    for path in files_in_scope:
        target = (base / path).resolve()
        if target.is_relative_to(base) and target.is_file():
            resolved_scope[path.replace("\\", "/")] = target

    anchors: list[dict[str, object]] = []
    for match in _LINE_REFERENCE.finditer(answer):
        rel_path = match.group("path")
        source = resolved_scope.get(rel_path)
        if source is None:
            continue
        line_number = int(match.group("line"))
        # Keep validation bounded even if the agent invents a huge line number.
        if line_number > 100_000:
            continue
        try:
            exists = False
            with source.open("r", encoding="utf-8", errors="replace") as handle:
                for index, _line in enumerate(handle, start=1):
                    if index == line_number:
                        exists = True
                        break
        except OSError:
            continue
        if exists:
            anchor = {"path": rel_path, "line": line_number}
            if anchor not in anchors:
                anchors.append(anchor)
            if len(anchors) >= _MAX_LINE_REFERENCES:
                break
    if anchors:
        result: dict[str, object] = {
            "status": "evidence_anchored",
            "reason": "source_lines_exist_not_semantic_verification",
            "anchors": anchors,
        }
    else:
        result = {"status": "missing_evidence", "reason": "no_valid_scoped_source_line"}
    if not acceptance_criteria:
        return result

    # These are free-text criteria, not executable predicates. Source anchoring
    # establishes evidence coverage only, never semantic acceptance.
    criteria_records: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []
    absence_proofs: list[dict[str, object]] = []
    for idx, criterion in enumerate(acceptance_criteria[:_MAX_CRITERIA], start=1):
        pattern = re.compile(
            rf"^\s*(?:criterion|критерий)\s*#?0*{idx}\s*:\s*(.+)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        statements = [
            match.group(1).strip()
            for finding in findings
            if (match := pattern.match(finding))
        ]
        negative_intent = (
            bool(_ABSENCE_ALLOWED.search(criterion))
            and any(_ABSENCE_CLAIM.search(statement) for statement in statements)
        )
        negative_check = None
        if negative_intent:
            for statement in statements:
                negative_check = _verify_scoped_literal_absence(
                    root=root,
                    files_in_scope=files_in_scope,
                    criterion=criterion,
                    statement=statement,
                )
                if negative_check is not None:
                    break
        anchored = False
        if not negative_intent:
            for statement in statements:
                evidence = assess_research_evidence(
                    root=root,
                    files_in_scope=files_in_scope,
                    structured_output={"summary": statement, "findings": []},
                )
                if evidence["status"] == "evidence_anchored":
                    anchored = True
                    break
        coverage = (
            "source_anchored" if anchored else
            "absence_verified" if negative_check and
            negative_check["status"] == "absence_verified" else
            "literal_present" if negative_check and
            negative_check["status"] == "literal_present" else
            "unanchored" if statements else "unreported"
        )
        criteria_records.append({"index": idx, "coverage": coverage})
        if coverage == "absence_verified":
            absence_proofs.append({"index": idx, **negative_check})
        elif coverage != "source_anchored":
            question: dict[str, object] = {
                "index": idx,
                "criterion": criterion[:400],
                "issue": coverage,
                "candidate": statements[0][:400] if statements else None,
            }
            if coverage == "literal_present" and negative_check:
                question["counterexample"] = negative_check["counterexample"]
            unresolved.append(question)
    if len(acceptance_criteria) > _MAX_CRITERIA:
        unresolved.append({
            "issue": "criterion_count_exceeds_bounded_check",
            "remaining": len(acceptance_criteria) - _MAX_CRITERIA,
        })
    result["criteria_count"] = len(acceptance_criteria)
    result["criteria_source_anchored_count"] = sum(
        record["coverage"] == "source_anchored" for record in criteria_records
    )
    result["criteria_absence_verified_count"] = len(absence_proofs)
    result["criteria_evidenced_count"] = (
        result["criteria_source_anchored_count"] + len(absence_proofs)
    )
    if absence_proofs:
        result["absence_proofs"] = absence_proofs
    result["criteria_coverage"] = criteria_records
    if unresolved:
        result["status"] = "criteria_incomplete"
        result["reason"] = "one_or_more_criteria_lack_scoped_line_evidence"
        result["review_handoff"] = {
            "status": "needs_targeted_verification",
            "files_in_scope": list(files_in_scope[:_MAX_LINE_REFERENCES]),
            "questions": unresolved,
        }
    elif absence_proofs:
        result["status"] = "criteria_evidenced"
        result["reason"] = "source_lines_and_bounded_absence_verified_not_semantics"
    elif result["status"] == "evidence_anchored":
        result["status"] = "criteria_anchored"
        result["reason"] = "all_criteria_have_source_lines_not_semantic_verification"
    return result
