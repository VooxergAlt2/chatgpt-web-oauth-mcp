from __future__ import annotations

from pathlib import Path
import subprocess

from chatgpt_web_oauth_mcp.delegate_git_evidence import scoped_git_diff_evidence
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def _git(root: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=False, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "review.cs").write_text("public class Old {}\n", encoding="utf-8")
    (root / "other.cs").write_text("original-other\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    return root


def test_git_diff_evidence_is_scoped_and_records_provenance(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "review.cs").write_text("public class Updated {}\n", encoding="utf-8")
    (repo / "other.cs").write_text("CHANGED-OUTSIDE-SCOPE\n", encoding="utf-8")
    (repo / "untracked.cs").write_text("UNTRACKED-CONTENT\n", encoding="utf-8")
    packet = scoped_git_diff_evidence(
        cwd=repo,
        request="Execute git diff -- review.cs and inspect the source.",
        files_in_scope=["review.cs"],
    )
    assert packet is not None
    assert "BEGIN SCOPED DIFF" in packet
    assert "+public class Updated {}" in packet
    assert "SHA256 of raw diff:" in packet
    assert "CHANGED-OUTSIDE-SCOPE" not in packet
    assert "UNTRACKED-CONTENT" not in packet
    assert "Untracked files are NOT included" in packet
    assert (repo / "untracked.cs").is_file()


def test_git_diff_does_not_escape_scope_or_leak_oversized_data(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "outside.cs"
    outside.write_text("private", encoding="utf-8")
    for invalid in ("../outside.cs", str(outside), ":(glob)*.cs", "review?.cs"):
        assert scoped_git_diff_evidence(
            cwd=repo, request="git diff",
            files_in_scope=["review.cs", invalid],
        ) is None
    assert scoped_git_diff_evidence(
        cwd=repo, request="read only", files_in_scope=["review.cs"],
    ) is None
    (repo / "review.cs").write_text("changed\n" + "x" * (60 * 1024), encoding="utf-8")
    packet = scoped_git_diff_evidence(
        cwd=repo, request="git diff", files_in_scope=["review.cs"],
    )
    assert packet is not None
    assert "OMITTED" in packet
    assert "x" * 100 not in packet
    assert "Do not claim complete diff coverage" in packet


def test_git_evidence_is_injected_for_bounded_agy_readonly_task(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "review.cs").write_text("public class Better {}\n", encoding="utf-8")
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=tmp_path / "delegate-state",
    )
    task = registry._make_task(
        harness="antigravity",
        project=registry.project_resolver.resolve(repo),
        cwd=repo,
        kind="explore",
        task="Inspect git diff -- review.cs using read_file where needed",
        goal=None, task_id=None, group_id=None,
        model="gemini-3.8-flash-medium",
        reasoning_effort="medium", sandbox_mode="plan+sandbox",
        commit_mode="forbidden", execution_timeout_seconds=300,
        depends_on_group_ids=(),
        files_in_scope=["review.cs"],
        out_of_scope=[], context_files=[], acceptance_criteria=[],
        done_means=[], verification_commands=[], output_schema=None,
        parse_structured_output=True,
        request_fingerprint="git-preflight-smoke",
    )
    assert "Git evidence acquisition fallback:" in task.prompt
    assert "+public class Better {}" in task.prompt
    assert "Do not stop the entire review solely because" in task.prompt
    assert task.log_paths.prompt.read_text(encoding="utf-8") == task.prompt
