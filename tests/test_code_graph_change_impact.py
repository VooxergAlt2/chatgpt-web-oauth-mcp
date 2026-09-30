from __future__ import annotations

from pathlib import Path
import subprocess

from chatgpt_web_oauth_mcp.code_graph.change_impact import (
    collect_python_change_set,
    resolve_comparison_base,
)
from chatgpt_web_oauth_mcp.code_graph.snapshot import resolve_git_snapshot


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    return repo


def test_collect_python_change_set_maps_modify_add_delete_and_module_scope(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "app.py").write_text(
        "VALUE = 1\n\n"
        "def keep():\n"
        "    return 1\n\n"
        "def remove_me():\n"
        "    return 2\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "def test_keep():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    base_commit = _commit(repo, "base")

    (repo / "app.py").write_text(
        "VALUE = 2\n\n"
        "def keep():\n"
        "    return 10\n\n"
        "def added():\n"
        "    return 3\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_app.py").write_text(
        "def test_keep():\n"
        "    assert 1 == 1\n",
        encoding="utf-8",
    )
    _commit(repo, "head")

    base = resolve_git_snapshot(repo, base_commit)
    head = resolve_git_snapshot(repo, "HEAD")
    result = collect_python_change_set(base, head)

    by_qualname = {
        item["qualname"]: item for item in result["changed_symbols"]
    }
    assert by_qualname["keep"]["status"] == "modified"
    assert by_qualname["added"]["status"] == "added"
    assert by_qualname["remove_me"]["status"] == "deleted"
    assert by_qualname["test_keep"]["status"] == "modified"
    assert "tests/test_app.py" in result["changed_test_files"]
    assert any(
        entry["new_path"] == "app.py"
        and any(item["start"] == 1 for item in entry["new_ranges"])
        for entry in result["module_scope_changes"]
    )
    assert result["analysis_errors"] == []


def test_collect_python_change_set_tracks_decorators_as_symbol_lines(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "app.py").write_text(
        "def deco(fn):\n"
        "    return fn\n\n"
        "@deco\n"
        "def target():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    base_commit = _commit(repo, "base")
    (repo / "app.py").write_text(
        "def deco(fn):\n"
        "    return fn\n\n"
        "@deco\n"
        "@deco\n"
        "def target():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    _commit(repo, "head")

    result = collect_python_change_set(
        resolve_git_snapshot(repo, base_commit),
        resolve_git_snapshot(repo, "HEAD"),
    )

    target = next(
        item for item in result["changed_symbols"] if item["qualname"] == "target"
    )
    assert target["status"] == "modified"
    assert target["line"] == 4


def test_collect_python_change_set_reports_non_python_files_without_guessing(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    base_commit = _commit(repo, "base")
    (repo / "README.md").write_text("two\n", encoding="utf-8")
    _commit(repo, "head")

    result = collect_python_change_set(
        resolve_git_snapshot(repo, base_commit),
        resolve_git_snapshot(repo, "HEAD"),
    )

    assert result["changed_symbols"] == []
    assert result["unsupported_changed_files"] == ["README.md"]
    assert result["changed_files"] == ["README.md"]


def test_resolve_comparison_base_uses_merge_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("def base():\n    return 1\n", encoding="utf-8")
    root_commit = _commit(repo, "root")
    _git(repo, "checkout", "-b", "feature")
    (repo / "app.py").write_text("def base():\n    return 2\n", encoding="utf-8")
    _commit(repo, "feature")
    _git(repo, "checkout", "main")
    (repo / "main.py").write_text("MAIN = True\n", encoding="utf-8")
    _commit(repo, "main advance")

    resolved = resolve_comparison_base(
        repo,
        base_ref="main",
        head_ref="feature",
        use_merge_base=True,
    )

    expected_tree = _git(repo, "rev-parse", f"{root_commit}^{{tree}}")
    assert resolved.tree_sha == expected_tree


def test_collect_python_change_set_marks_pure_rename_as_module_scope_gap(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    (repo / "old_name.py").write_text(
        "def stable():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    base_commit = _commit(repo, "base")
    _git(repo, "mv", "old_name.py", "new_name.py")
    _commit(repo, "rename")

    result = collect_python_change_set(
        resolve_git_snapshot(repo, base_commit),
        resolve_git_snapshot(repo, "HEAD"),
    )

    assert result["changed_symbols"] == []
    assert result["module_scope_changes"] == [
        {
            "status": "R",
            "old_path": "old_name.py",
            "new_path": "new_name.py",
            "path_change": True,
            "old_ranges": [],
            "new_ranges": [],
        }
    ]
