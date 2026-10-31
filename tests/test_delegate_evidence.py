from pathlib import Path
import json
import subprocess
import time

import chatgpt_web_oauth_mcp.delegate_harnesses as harnesses
import chatgpt_web_oauth_mcp.executors as executors
from chatgpt_web_oauth_mcp.delegate_harnesses import GenericCliHarness

from chatgpt_web_oauth_mcp.delegate_evidence import (
    assess_delegate_findings, assess_research_evidence,
)
from chatgpt_web_oauth_mcp.delegate_telemetry import DelegateTelemetryStore
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def test_advisory_findings_check_each_claim_independently(tmp_path: Path) -> None:
    (tmp_path / "module.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("private data\n", encoding="utf-8")
    result = assess_delegate_findings(
        root=tmp_path, files_in_scope=("module.py",),
        structured_output={
            "summary": "review completed",
            "findings": [
                "First claim (module.py:1)",
                "Invented line (module.py:99)",
                "Out of scope (other.py:1)",
            ],
        },
    )
    assert result["status"] == "partial_coverage"
    assert result["finding_count"] == 3
    assert result["anchored_finding_count"] == 1
    assert [x["status"] for x in result["findings"]] == [
        "line_exists", "unanchored", "unanchored",
    ]
    assert result["findings"][0]["anchors"] == [{"path": "module.py", "line": 1}]
    assert result["semantics_verified"] is False
    assert result["source_commit"] is None


def test_advisory_findings_distinguish_unscoped_and_no_defects(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("pass\n", encoding="utf-8")
    assert assess_delegate_findings(
        root=tmp_path, files_in_scope=(),
        structured_output={"findings": ["Unverified guess a.py:1"]},
    )["status"] == "not_evaluated"
    assert assess_delegate_findings(
        root=tmp_path, files_in_scope=("a.py",),
        structured_output={"findings": []},
    )["status"] == "no_findings"
    assert assess_delegate_findings(
        root=tmp_path, files_in_scope=("a.py",),
        structured_output=None,
    )["reason"] == "no_structured_manifest"


def test_advisory_directory_and_glob_are_bounded_to_cited_source(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "module.py").write_text("def run():\n    pass\n", encoding="utf-8")
    (tmp_path / "secret.py").write_text("classified\n", encoding="utf-8")
    findings = {
        "findings": [
            "Scoped src/module.py:1",
            "Outside secret.py:1",
            "Imaginary src/missing.py:1",
        ],
    }
    for scope in (("src",), ("src/*.py",)):
        result = assess_delegate_findings(
            root=tmp_path, files_in_scope=scope, structured_output=findings,
        )
        assert result["status"] == "partial_coverage"
        assert result["anchored_finding_count"] == 1
        assert result["findings"][0]["anchors"] == [
            {"path": "src/module.py", "line": 1},
        ]
        assert not result["findings"][1]["anchors"]
    unsafe = assess_delegate_findings(
        root=tmp_path, files_in_scope=("../src",),
        structured_output={"findings": ["src/module.py:1"]},
    )
    assert unsafe["status"] == "unanchored"


def test_advisory_evidence_records_git_head_not_semantic_verification(
    tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Proof\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run([
        "git", "-C", str(tmp_path),
        "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-qm", "fixture",
    ], check=True)
    expected_sha = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True,
    ).strip()
    result = assess_delegate_findings(
        root=tmp_path, files_in_scope=("README.md",),
        structured_output={"findings": ["Observed README.md:1"]},
    )
    assert result["source_commit"] == expected_sha
    assert result["source_state"] == "working_tree_at_evaluation"
    assert result["semantics_verified"] is False


def test_unscoped_cloud_research_checks_only_git_tracked_citations(
    tmp_path: Path,
) -> None:
    (tmp_path / "tracked.py").write_text("def saved():\n    return 1\n", encoding="utf-8")
    (tmp_path / "private.py").write_text("a secret\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.py"], check=True)
    subprocess.run([
        "git", "-C", str(tmp_path), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
    ], check=True)
    result = assess_delegate_findings(
        root=tmp_path, files_in_scope=(),
        structured_output={"findings": [
            "Observed tracked.py:1",
            "Do not inspect untracked private.py:1",
            "Does not exist tracked.py:999",
        ]},
    )
    assert result["status"] == "tracked_unscoped"
    assert result["reason"] == "git_tracked_citations_without_task_scope"
    assert result["scope_basis"] == "git_tracked_unscoped"
    assert result["scope_verified"] is False
    assert result["anchored_finding_count"] == 1
    assert result["findings"][0]["anchors"] == [{"path": "tracked.py", "line": 1}]
    assert result["findings"][1]["anchors"] == []
    assert result["findings"][2]["anchors"] == []
    assert result["semantics_verified"] is False


def test_unscoped_research_does_not_trust_symlink_escapes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1\n", encoding="utf-8")
    (root / "escape.py").symlink_to(outside)
    (root / "secret.py").write_text("untracked = True\n", encoding="utf-8")
    (root / "indirect.py").symlink_to(root / "secret.py")
    (root / "known.py").write_text("known = True\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "known.py", "escape.py", "indirect.py"], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "user.name=Test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture",
    ], check=True)
    result = assess_delegate_findings(
        root=root, files_in_scope=(),
        structured_output={"findings": [
            "Unsafe escape.py:1", "Untracked secret.py:1", "Indirect indirect.py:1",
        ]},
    )
    assert result["status"] == "not_evaluated"
    assert result["anchored_finding_count"] == 0


def test_advisory_findings_hard_cap_prevents_false_full_coverage(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("pass\n", encoding="utf-8")
    report = assess_delegate_findings(
        root=tmp_path, files_in_scope=("a.py",),
        structured_output={"findings": ["a.py:1"] * 20},
    )
    assert report["status"] == "partial_coverage"
    assert report["truncated"] is True
    assert report["checked_finding_count"] == 12
    assert report["anchored_finding_count"] == 12
    assert report["finding_count"] == 20


def test_cloud_research_quality_is_persisted_without_overriding_process_success(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / "module.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(executors, "_command_available", lambda _cmd: True)
    monkeypatch.setattr(harnesses, "command_available", lambda _cmd: True)
    registry = ExecutorRegistry(
        harnesses=[
            GenericCliHarness(
                name="antigravity", command="fake-cli", explore_command="fake-cli",
            ),
        ],
        default_harness="antigravity",
        automatic_routing=False,
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegates",
        telemetry_state_path=tmp_path / "telemetry.json",
    )

    def fake_executor(*, delegate_task):
        return {
            "success": True,
            "status": "succeeded",
            "delegate_id": delegate_task.delegate_id,
            "harness": delegate_task.harness,
            "kind": delegate_task.kind,
            "structured_output": {
                "status": "succeeded",
                "summary": "Scoped review",
                "findings": [
                    "First finding module.py:1",
                    "Unsupported finding module.py:90",
                ],
            },
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_executor)
    output = registry.run_delegate(
        cwd=tmp_path, harness="antigravity", kind="explore",
        task="Inspect function run with concrete line evidence.",
        files_in_scope=["module.py"], wait_seconds=3,
    )
    assert output["status"] == "succeeded"
    assert output["research_evidence"]["status"] == "partial_coverage"
    assert output["research_evidence"]["anchored_finding_count"] == 1
    assert output["research_evidence"]["semantics_verified"] is False
    assert output["research_evidence"]["scope_basis"] == "explicit"
    source_task = registry.scheduler.get_task(output["delegate_id"])
    assert source_task is not None
    assert "src/component.py:42" in source_task.prompt
    assert "rather than inventing a reference" in source_task.prompt
    metadata = json.loads(
        registry.scheduler.get_task(output["delegate_id"]).log_paths.metadata.read_text()
    )
    assert metadata["research_evidence"]["status"] == "partial_coverage"
    # Scheduler intentionally signals the terminal result before its
    # on_terminal telemetry callback completes. Telemetry is eventually
    # consistent, not an atomic part of the delegate response.
    deadline = time.monotonic() + 5
    report = registry.telemetry.snapshot()
    while (
        report["overall"]["research_evidence"]["statuses"] != {"partial_coverage": 1}
        and time.monotonic() < deadline
    ):
        time.sleep(0.02)
        report = registry.telemetry.snapshot()
    assert report["overall"]["research_evidence"]["statuses"] == {
        "partial_coverage": 1,
    }
    assert report["overall"]["succeeded"] == 1


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
    # A valid file:line anchor is not independent semantic acceptance.
    assert DelegateTelemetryStore._feedback_outcome(passed)[:2] == (True, False)
    passed["acceptance_review"] = {"status": "accepted"}
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
    )[:2] == (True, False)


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
