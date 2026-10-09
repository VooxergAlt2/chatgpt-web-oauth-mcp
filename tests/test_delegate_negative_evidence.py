"""Negative evidence requires an independently complete, bounded source scan."""

from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_evidence import assess_research_evidence
from chatgpt_web_oauth_mcp.delegate_telemetry import DelegateTelemetryStore

TOKEN = "__EVIDENCE_SENTINEL_UNDOCUMENTED_20261009"
CONDITION = f"Find exact defining line for {TOKEN} in README.md; if absent mark unverified"
FINDING = f"Criterion 2: {TOKEN} is NOT defined in README.md; no matches found."


def evaluate(root: Path, *, files=("README.md",), criterion=CONDITION,
             statement=None, criteria=None):
    return assess_research_evidence(
        root=root, files_in_scope=files,
        acceptance_criteria=criteria or (criterion,),
        structured_output={
            "summary": "README.md:1",
            "findings": [statement or FINDING.replace("Criterion 2", "Criterion 1")],
        },
    )


def test_absence_verified_over_whole_scoped_file_with_hash(tmp_path: Path) -> None:
    source = tmp_path / "README.md"
    source.write_text("# Falcon\nSome other text\n", encoding="utf-8")
    assessed = evaluate(tmp_path)
    assert assessed["status"] == "criteria_evidenced"
    assert assessed["criteria_absence_verified_count"] == 1
    assert assessed["criteria_evidenced_count"] == 1
    assert assessed["criteria_source_anchored_count"] == 0
    assert assessed["criteria_coverage"] == [
        {"index": 1, "coverage": "absence_verified"}
    ]
    assert assessed["absence_proofs"][0]["subject"] == TOKEN
    assert assessed["absence_proofs"][0]["scanned_bytes"] == source.stat().st_size
    assert len(assessed["absence_proofs"][0]["scanned_files"][0]["sha256"]) == 64
    assert "review_handoff" not in assessed
    assert DelegateTelemetryStore._feedback_outcome(
        {"status": "succeeded", "content_acceptance": assessed}
    )[:2] == (True, True)


def test_mixed_positive_and_negative_criteria(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Falcon\n", encoding="utf-8")
    assessed = evaluate(
        tmp_path,
        criteria=("Identify title", CONDITION),
        statement=f"Criterion 1: Falcon (README.md:1)",
    )
    assert assessed["status"] == "criteria_incomplete"
    assert assessed["criteria_source_anchored_count"] == 1
    assert assessed["review_handoff"]["questions"][0]["index"] == 2

    full = assess_research_evidence(
        root=tmp_path, files_in_scope=("README.md",),
        acceptance_criteria=("Identify title", CONDITION),
        structured_output={
            "summary": "Falcon README.md:1",
            "findings": ["Criterion 1: Falcon (README.md:1)", FINDING],
        },
    )
    assert full["status"] == "criteria_evidenced"
    assert full["criteria_source_anchored_count"] == 1
    assert full["criteria_absence_verified_count"] == 1
    assert full["criteria_evidenced_count"] == 2


def test_existing_literal_overrides_misleading_citation(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        f"# Falcon\n{TOKEN} = 1\n", encoding="utf-8"
    )
    assessed = evaluate(
        tmp_path, statement=FINDING.replace("Criterion 2", "Criterion 1") + " README.md:1"
    )
    assert assessed["status"] == "criteria_incomplete"
    assert assessed["criteria_coverage"][0]["coverage"] == "literal_present"
    assert assessed["review_handoff"]["questions"][0]["counterexample"] == {
        "path": "README.md", "line": 2,
    }
    assert DelegateTelemetryStore._feedback_outcome(
        {"status": "succeeded", "content_acceptance": assessed}
    )[:2] == (True, False)


def test_absence_not_an_acceptable_substitute_for_required_definition(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Falcon\n", encoding="utf-8")
    assessed = evaluate(
        tmp_path,
        criterion=f"Find exact defining line for {TOKEN} in README.md",
    )
    assert assessed["status"] == "criteria_incomplete"
    assert assessed["review_handoff"]["questions"][0]["issue"] == "unanchored"


def test_incomplete_oversized_or_escaped_scope_is_not_absence_proof(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Falcon\n", encoding="utf-8")
    for files in (("README.md", "missing.md"), ("README.md", "../outside.md")):
        assessed = evaluate(tmp_path, files=files)
        assert assessed["status"] == "criteria_incomplete"

    (tmp_path / "huge.md").write_bytes(b"A" * 2_000_001)
    assessed = evaluate(tmp_path, files=("huge.md",))
    assert assessed["status"] == "criteria_incomplete"


def test_ambiguous_criterion_subject_is_not_accepted(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Falcon\n", encoding="utf-8")
    assessed = evaluate(
        tmp_path,
        criterion=f"Find {TOKEN} and __OTHER_IDENTIFIER; if absent mark unverified",
    )
    assert assessed["status"] == "criteria_incomplete"
