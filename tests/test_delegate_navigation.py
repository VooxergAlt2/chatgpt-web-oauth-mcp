from __future__ import annotations

from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_navigation import scoped_navigation_hints
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def test_scoped_navigation_prioritizes_referenced_methods(tmp_path: Path) -> None:
    source = tmp_path / "service.py"
    source.write_text(
        "class Service:\n"
        "    def selected_provider(self):\n"
        "        return 1\n"
        "    def unrelated_method(self):\n"
        "        return 2\n"
        "    def retry_provider(self):\n"
        "        return self.selected_provider()\n",
        encoding="utf-8",
    )
    hints = scoped_navigation_hints(
        cwd=tmp_path,
        task_text="Inspect retry_provider and selected_provider, not unrelated methods",
        files_in_scope=["service.py"],
        context_files=[],
    )
    assert any(hint.startswith("service.py:6-7 method Service.retry_provider") for hint in hints)
    assert any(hint.startswith("service.py:2-3 method Service.selected_provider") for hint in hints)
    assert len(hints) <= 12
    prompt = ExecutorRegistry(codex_command="true")._build_prompt(
        harness="antigravity", task="Inspect retry_provider",
        goal=None, navigation_hints=hints,
        context_files=[], acceptance_criteria=[], verification_commands=[],
        commit_mode="forbidden", kind="explore",
    )
    assert "Bounded Code Map navigation hints (not evidence)" in prompt
    assert hints[0] in prompt
    assert "Verify source lines independently" in prompt


def test_navigation_rejects_absolute_traversal_and_external_symlinks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktree"
    root.mkdir()
    outside = tmp_path / "secret.py"
    outside.write_text("def outside_secret():\n    return 1\n", encoding="utf-8")
    (root / "outside_link.py").symlink_to(outside)
    safe = root / "source.py"
    safe.write_text("def scoped_function():\n    return 1\n", encoding="utf-8")
    hints = scoped_navigation_hints(
        cwd=root, task_text="scoped_function",
        files_in_scope=[
            "../secret.py", str(outside), "outside_link.py", "source.py",
        ],
        context_files=[".", "missing.py"],
    )
    assert len(hints) == 1
    assert hints[0].startswith("source.py:1-2 function scoped_function")
    assert all("secret" not in hint for hint in hints)


def test_navigation_with_no_explicit_files_skips_repository_traversal(
    tmp_path: Path,
) -> None:
    assert scoped_navigation_hints(
        cwd=tmp_path, task_text="Find everything",
        files_in_scope=[], context_files=[],
    ) == []


def test_large_class_map_selects_only_explicit_methods_without_parent_span(
    tmp_path: Path,
) -> None:
    source = tmp_path / "large.py"
    source.write_text(
        "class ExecutorRegistry:\n"
        + "".join(
            f"    def unrelated_method_{index}(self):\n"
            f"        return {index}\n"
            for index in range(130)
        )
        + "    def _run_same_account_recovery(self):\n        return 1\n"
        + "    def _run_scheduled_task(self):\n        return 2\n",
        encoding="utf-8",
    )
    hints = scoped_navigation_hints(
        cwd=tmp_path,
        task_text=(
            "Review ExecutorRegistry._run_same_account_recovery and "
            "ExecutorRegistry._run_scheduled_task. "
            "Do not scan the entire class or unrelated methods."
        ),
        files_in_scope=["large.py"],
        context_files=[],
    )
    assert len(hints) == 2
    assert all("method ExecutorRegistry._run_" in hint for hint in hints)
    assert not any("class ExecutorRegistry" in hint for hint in hints)
    assert not any("unrelated_method" in hint for hint in hints)
