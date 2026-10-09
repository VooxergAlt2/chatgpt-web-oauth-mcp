"""Bounded, deterministic evidence checks for local factual-research results.

This does not claim semantic correctness. It only verifies that a factual
research answer points to existing lines of explicitly scoped source files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

_LINE_REFERENCE = re.compile(
    r"(?<![\w/])(?P<path>[\w./+-]+\.[\w+-]+):(?P<line>[1-9][0-9]{0,5})(?![0-9])"
)
_MAX_LINE_REFERENCES = 12
_WRONG_WORKFLOW = (
    "no concrete review candidate",
    "no review candidates",
    "verifier skipped because there were no candidates",
)


def assess_research_evidence(
    *,
    root: Path,
    files_in_scope: tuple[str, ...],
    structured_output: object,
) -> dict[str, object]:
    """Report source anchoring separately from process success.

    A valid anchor proves the referenced line exists, not that the conclusion
    about that line is true. Broader tasks without an explicit source scope
    remain unassessed rather than being scored as evidence failures.
    """
    if not files_in_scope:
        return {"status": "not_evaluated", "reason": "no_explicit_file_scope"}
    if not isinstance(structured_output, Mapping):
        return {"status": "missing_evidence", "reason": "no_structured_manifest"}
    summary = str(structured_output.get("summary") or "")
    findings = structured_output.get("findings")
    if not isinstance(findings, list) or not all(isinstance(x, str) for x in findings):
        return {"status": "missing_evidence", "reason": "invalid_findings"}
    answer = "\n".join([summary, *findings])
    if any(fragment in answer.lower() for fragment in _WRONG_WORKFLOW):
        return {"status": "wrong_workflow", "reason": "defect_review_instead_of_research"}
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
        return {
            "status": "evidence_anchored",
            "reason": "source_lines_exist_not_semantic_verification",
            "anchors": anchors,
        }
    return {"status": "missing_evidence", "reason": "no_valid_scoped_source_line"}
