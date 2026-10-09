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
