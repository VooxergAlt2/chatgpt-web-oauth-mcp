from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_evidence import assess_research_evidence
from chatgpt_web_oauth_mcp.delegate_telemetry import DelegateTelemetryStore


def test_research_evidence_validates_real_scoped_line(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Project Falcon\nDescription\n", encoding="utf-8")
    result = assess_research_evidence(
        root=tmp_path,
        files_in_scope=("README.md",),
        structured_output={
            "summary": "Project Falcon is the title (README.md:1).",
            "findings": ["The title is Project Falcon, README.md:1"],
        },
    )
    assert result["status"] == "evidence_anchored"
    assert result["anchors"] == [{"path": "README.md", "line": 1}]


def test_research_evidence_rejects_bad_or_out_of_scope_anchor(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Project Falcon\n", encoding="utf-8")
    for citation in ("README.md:25", "other.py:1", "../outside.py:1"):
        result = assess_research_evidence(
            root=tmp_path,
            files_in_scope=("README.md",),
            structured_output={"summary": f"Project Falcon ({citation})", "findings": []},
        )
        assert result["status"] == "missing_evidence"


def test_research_evidence_rejects_wrong_workflow_without_penalizing_unscoped(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Project Falcon\n", encoding="utf-8")
    wrong = assess_research_evidence(
        root=tmp_path,
        files_in_scope=("README.md",),
        structured_output={
            "summary": "Scout found no concrete review candidate requiring verification.",
            "findings": [],
        },
    )
    assert wrong["status"] == "wrong_workflow"
    unscoped = assess_research_evidence(
        root=tmp_path,
        files_in_scope=(),
        structured_output={"summary": "No line citation", "findings": []},
    )
    assert unscoped["status"] == "not_evaluated"


def test_telemetry_distinguishes_technical_success_from_research_evidence() -> None:
    missing = {
        "status": "succeeded",
        "content_acceptance": {"status": "missing_evidence", "reason": "no_valid_scoped_source_line"},
    }
    passed = {
        "status": "succeeded",
        "content_acceptance": {"status": "evidence_anchored", "anchors": [{"path": "README.md", "line": 1}]},
    }
    assert DelegateTelemetryStore._feedback_outcome(missing)[:2] == (True, False)
    assert DelegateTelemetryStore._feedback_outcome(passed)[:2] == (True, True)


def test_multiple_criteria_have_independent_source_coverage(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Project Falcon\nVersion: 2.1\n", encoding="utf-8"
    )
    assessed = assess_research_evidence(
        root=tmp_path, files_in_scope=("README.md",),
        acceptance_criteria=("Identify title", "Identify version"),
        structured_output={
            "summary": "Project Falcon (README.md:1)",
            "findings": [
                "Criterion 1: Title is Project Falcon (README.md:1)",
                "Criterion 2: Version is 2.1 (README.md:2)",
            ],
        },
    )
    assert assessed["status"] == "criteria_anchored"
    assert assessed["criteria_count"] == 2
    assert assessed["criteria_source_anchored_count"] == 2
    assert assessed["criteria_coverage"] == [
        {"index": 1, "coverage": "source_anchored"},
        {"index": 2, "coverage": "source_anchored"},
    ]
    assert "review_handoff" not in assessed
    assert DelegateTelemetryStore._feedback_outcome(
        {"status": "succeeded", "content_acceptance": assessed}
    )[:2] == (True, True)


def test_missing_criterion_produces_only_targeted_review_handoff(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text(
        "# Project Falcon\nVersion: 2.1\n", encoding="utf-8"
    )
    assessed = assess_research_evidence(
        root=tmp_path, files_in_scope=("README.md",),
        acceptance_criteria=("Identify title", "Identify version"),
        structured_output={
            "summary": "Found title (README.md:1).",
            "findings": [
                "Criterion 1: Title is Project Falcon (README.md:1)",
                "Criterion 2: Version might be 2.1 (README.md:999)",
            ],
        },
    )
    assert assessed["status"] == "criteria_incomplete"
    assert assessed["criteria_source_anchored_count"] == 1
    assert assessed["review_handoff"]["files_in_scope"] == ["README.md"]
    assert assessed["review_handoff"]["questions"] == [
        {
            "index": 2, "criterion": "Identify version",
            "issue": "unanchored",
            "candidate": "Version might be 2.1 (README.md:999)",
        }
    ]
    assert DelegateTelemetryStore._feedback_outcome(
        {"status": "succeeded", "content_acceptance": assessed}
    )[:2] == (True, False)


def test_unrelated_summary_citation_does_not_cover_a_criterion(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Falcon\n", encoding="utf-8")
    assessed = assess_research_evidence(
        root=tmp_path, files_in_scope=("README.md",),
        acceptance_criteria=("Identify title",),
        structured_output={
            "summary": "A source line exists (README.md:1)",
            "findings": ["Unrelated observation"],
        },
    )
    assert assessed["status"] == "criteria_incomplete"
    assert assessed["review_handoff"]["questions"][0]["issue"] == "unreported"


def test_criteria_count_limit_cannot_silently_pass(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Falcon\n", encoding="utf-8")
    assessed = assess_research_evidence(
        root=tmp_path, files_in_scope=("README.md",),
        acceptance_criteria=tuple(f"Requirement {i}" for i in range(18)),
        structured_output={
            "summary": "README.md:1",
            "findings": [f"Criterion {i}: README.md:1" for i in range(1, 19)],
        },
    )
    assert assessed["status"] == "criteria_incomplete"
    assert assessed["criteria_count"] == 18
    assert assessed["criteria_source_anchored_count"] == 16
    assert assessed["review_handoff"]["questions"] == [
        {"issue": "criterion_count_exceeds_bounded_check", "remaining": 2}
    ]


def test_criteria_without_source_scope_fail_closed(tmp_path: Path) -> None:
    assessed = assess_research_evidence(
        root=tmp_path, files_in_scope=(),
        acceptance_criteria=("Identify title",),
        structured_output={"summary": "Title may be Falcon", "findings": []},
    )
    assert assessed["status"] == "criteria_incomplete"
    assert assessed["review_handoff"]["status"] == "needs_source_scope"
    assert DelegateTelemetryStore._feedback_outcome(
        {"status": "succeeded", "content_acceptance": assessed}
    )[:2] == (True, False)


def test_wrong_workflow_handoff_preserves_criterion(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Falcon\n", encoding="utf-8")
    assessed = assess_research_evidence(
        root=tmp_path, files_in_scope=("README.md",),
        acceptance_criteria=("Identify title",),
        structured_output={
            "summary": "Scout found no concrete review candidate requiring verification.",
            "findings": [],
        },
    )
    assert assessed["status"] == "wrong_workflow"
    assert assessed["review_handoff"]["questions"] == [
        {"index": 1, "criterion": "Identify title", "issue": "wrong_workflow"}
    ]
