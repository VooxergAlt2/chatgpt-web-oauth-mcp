import os
import subprocess
from pathlib import Path
import time

import pytest

from chatgpt_web_oauth_mcp.search import glob_files, grep_files, search_files


def test_search_files_finds_text_matches(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("hello world\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("ignore me\n", encoding="utf-8")

    result = search_files(tmp_path, query="hello", glob_pattern="*.py", limit=20)

    assert result["success"] is True
    assert len(result["matches"]) == 1
    assert result["matches"][0]["path"].endswith("one.py")


def test_search_files_supports_single_file_path(tmp_path: Path) -> None:
    target = tmp_path / "one.py"
    target.write_text("hello world\n", encoding="utf-8")

    result = search_files(target, query="hello", glob_pattern=None, limit=20)

    assert result["success"] is True
    assert len(result["matches"]) == 1
    assert result["matches"][0]["path"] == str(target)


def test_glob_files_matches_nested_paths(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.py").write_text("print('b')\n", encoding="utf-8")
    (tmp_path / "nested" / "c.txt").write_text("nope\n", encoding="utf-8")

    result = glob_files(tmp_path, pattern="*.py", limit=20, offset=0)

    assert result["success"] is True
    assert [Path(match["path"]).name for match in result["matches"]] == ["a.py", "b.py"]
    assert result["truncated"] is False


def test_grep_files_content_mode_supports_context_and_ignore_case(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("alpha\nBeta HELLO\ngamma\n", encoding="utf-8")

    result = grep_files(
        tmp_path,
        pattern="hello",
        glob_pattern="*.py",
        output_mode="content",
        before=1,
        after=1,
        ignore_case=True,
        head_limit=20,
        offset=0,
    )

    assert result["success"] is True
    assert len(result["matches"]) == 1
    assert result["matches"][0]["line_number"] == 2
    assert result["matches"][0]["context_before"] == ["alpha"]
    assert result["matches"][0]["context_after"] == ["gamma"]


def test_grep_files_files_with_matches_mode_returns_unique_paths(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("hello\nhello again\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("hello once\n", encoding="utf-8")

    result = grep_files(
        tmp_path,
        pattern="hello",
        glob_pattern="*.py",
        output_mode="files_with_matches",
        head_limit=20,
        offset=0,
    )

    assert result["success"] is True
    assert [Path(path).name for path in result["files"]] == ["one.py", "two.py"]


def test_grep_files_count_mode_returns_per_file_counts(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("hello\nhello\n", encoding="utf-8")
    (tmp_path / "two.py").write_text("hello\n", encoding="utf-8")

    result = grep_files(
        tmp_path,
        pattern="hello",
        glob_pattern="*.py",
        output_mode="count",
        head_limit=20,
        offset=0,
    )

    assert result["success"] is True
    assert result["counts"] == [
        {"path": str(tmp_path / "one.py"), "count": 2},
        {"path": str(tmp_path / "two.py"), "count": 1},
    ]


def test_grep_files_supports_single_file_path(tmp_path: Path) -> None:
    target = tmp_path / "one.py"
    target.write_text("alpha\nTODO: fix me\n", encoding="utf-8")

    result = grep_files(
        target,
        pattern=r"TODO:\s+\w+",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
    )

    assert result["success"] is True
    assert len(result["matches"]) == 1
    assert result["matches"][0]["path"] == str(target)


def test_search_defaults_hide_hidden_and_gitignored_paths(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("TODO hidden\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("TODO ignored\n", encoding="utf-8")
    (tmp_path / "visible.py").write_text("TODO visible\n", encoding="utf-8")

    default = search_files(tmp_path, query="TODO", glob_pattern=None, limit=20)
    assert default["success"] is True
    assert [Path(match["path"]).name for match in default["matches"]] == ["visible.py"]

    default_with_glob = search_files(tmp_path, query="TODO", glob_pattern="*.py", limit=20)
    assert [Path(match["path"]).name for match in default_with_glob["matches"]] == ["visible.py"]

    include_hidden = search_files(
        tmp_path,
        query="TODO",
        glob_pattern=None,
        limit=20,
        include_hidden=True,
    )
    assert {Path(match["path"]).name for match in include_hidden["matches"]} == {
        ".hidden.py",
        "visible.py",
    }

    include_ignored = search_files(
        tmp_path,
        query="TODO",
        glob_pattern=None,
        limit=20,
        respect_gitignore=False,
        include_hidden=True,
    )
    assert {Path(match["path"]).name for match in include_ignored["matches"]} == {
        ".hidden.py",
        "ignored.txt",
        "visible.py",
    }


def test_grep_files_literal_mode_handles_cjk_and_regex_metacharacters(tmp_path: Path) -> None:
    target = tmp_path / "prices.txt"
    target.write_text("价格 [上涨]? 然后 [上涨]?\n价格上涨\n", encoding="utf-8")

    result = grep_files(
        tmp_path,
        pattern="[上涨]?",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        fixed_strings=True,
        only_matching=True,
    )

    assert result["success"] is True
    assert [match["line"] for match in result["matches"]] == ["[上涨]?", "[上涨]?"]
    assert result["backend"]["name"] == "ripgrep"


def test_grep_files_multiline_uses_rust_regex_and_preserves_context(tmp_path: Path) -> None:
    target = tmp_path / "block.txt"
    target.write_text("before\nstart\n中间\nend\nafter\n", encoding="utf-8")

    result = grep_files(
        target,
        pattern=r"start.*end",
        glob_pattern=None,
        output_mode="content",
        before=1,
        after=1,
        head_limit=20,
        offset=0,
        multiline=True,
    )

    assert result["success"] is True
    assert result["matches"] == [
        {
            "path": str(target),
            "line_number": 2,
            "end_line_number": 4,
            "line": "start\n中间\nend",
            "context_before": ["before"],
            "context_after": ["after"],
        }
    ]


def test_grep_files_counts_occurrences_and_summary_ignores_pagination(tmp_path: Path) -> None:
    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("hit hit\n", encoding="utf-8")
    second.write_text("hit\n", encoding="utf-8")

    counts = grep_files(
        tmp_path,
        pattern="hit",
        glob_pattern=None,
        output_mode="count",
        head_limit=20,
        offset=0,
    )
    summary = grep_files(
        tmp_path,
        pattern="hit",
        glob_pattern=None,
        output_mode="summary",
        head_limit=1,
        offset=999,
    )

    assert counts["counts"] == [
        {"path": str(first), "count": 2},
        {"path": str(second), "count": 1},
    ]
    assert summary["summary"] == {"occurrences": 3, "matched_files": 2}
    assert summary["truncated"] is False
    assert summary["next_offset"] is None


def test_grep_files_supports_file_type_and_only_matching(tmp_path: Path) -> None:
    python_file = tmp_path / "app.py"
    text_file = tmp_path / "app.txt"
    python_file.write_text("TODO-12 and TODO-34\n", encoding="utf-8")
    text_file.write_text("TODO-56\n", encoding="utf-8")

    result = grep_files(
        tmp_path,
        pattern=r"TODO-\d+",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        file_type="py",
        only_matching=True,
    )

    assert result["success"] is True
    assert [match["line"] for match in result["matches"]] == ["TODO-12", "TODO-34"]
    assert {match["path"] for match in result["matches"]} == {str(python_file)}
    assert result["filters"]["file_type"] == "py"


def test_grep_files_supports_whole_word_and_per_file_result_cap(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("cat\nconcatenate\ncat\ncat\n", encoding="utf-8")
    second.write_text("cat\ncat\ncat\n", encoding="utf-8")

    result = grep_files(
        tmp_path,
        pattern="cat",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        fixed_strings=True,
        word=True,
        max_per_file=2,
    )

    assert result["success"] is True
    assert [Path(match["path"]).name for match in result["matches"]] == [
        "a.txt",
        "a.txt",
        "b.txt",
        "b.txt",
    ]
    assert [match["line"] for match in result["matches"]] == ["cat", "cat", "cat", "cat"]
    assert result["filters"]["word"] is True
    assert result["filters"]["max_per_file"] == 2

    first_page = grep_files(
        tmp_path,
        pattern="cat",
        glob_pattern=None,
        output_mode="content",
        head_limit=2,
        offset=0,
        fixed_strings=True,
        word=True,
        max_per_file=2,
    )
    second_page = grep_files(
        tmp_path,
        pattern="cat",
        glob_pattern=None,
        output_mode="content",
        head_limit=2,
        offset=first_page["next_offset"],
        fixed_strings=True,
        word=True,
        max_per_file=2,
    )
    assert [Path(match["path"]).name for match in first_page["matches"]] == ["a.txt", "a.txt"]
    assert first_page["next_offset"] == 2
    assert [Path(match["path"]).name for match in second_page["matches"]] == ["b.txt", "b.txt"]
    assert second_page["next_offset"] is None


def test_grep_files_supports_auto_regex_engine_and_validates_engine(tmp_path: Path) -> None:
    target = tmp_path / "one.txt"
    target.write_text("hello world\nhello there\n", encoding="utf-8")

    auto = grep_files(
        tmp_path,
        pattern=r"hello(?= world)",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        regex_engine="auto",
    )
    invalid = grep_files(
        tmp_path,
        pattern="hello",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        regex_engine="made-up",
    )
    literal_with_irrelevant_engine = grep_files(
        tmp_path,
        pattern="hello",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        fixed_strings=True,
        regex_engine="made-up",
    )

    assert auto["success"] is True
    assert [match["line"] for match in auto["matches"]] == ["hello world"]
    assert auto["filters"]["regex_engine"] == "auto"
    assert invalid["success"] is False
    assert invalid["error"]["code"] == "invalid_regex_engine"
    assert literal_with_irrelevant_engine["success"] is True


def test_grep_files_preserves_excludes_and_never_searches_git_metadata(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "keep.py").write_text("MARK\n", encoding="utf-8")
    (tmp_path / "skip.py").write_text("MARK\n", encoding="utf-8")
    (tmp_path / "excluded").mkdir()
    (tmp_path / "excluded" / "nested.py").write_text("MARK\n", encoding="utf-8")
    (tmp_path / ".git" / "custom.txt").write_text("MARK\n", encoding="utf-8")

    result = grep_files(
        tmp_path,
        pattern="MARK",
        glob_pattern=None,
        output_mode="files_with_matches",
        head_limit=20,
        offset=0,
        include_hidden=True,
        respect_gitignore=False,
        exclude_patterns=["skip.py", "excluded"],
    )

    assert result["success"] is True
    assert result["files"] == [str(tmp_path / "keep.py")]


def test_grep_files_pagination_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("page b1\npage b2\n", encoding="utf-8")
    (tmp_path / "a.txt").write_text("page a1\npage a2\n", encoding="utf-8")

    def page(offset: int) -> dict[str, object]:
        return grep_files(
            tmp_path,
            pattern="page",
            glob_pattern=None,
            output_mode="content",
            head_limit=2,
            offset=offset,
        )

    first = page(0)
    repeated = page(0)
    second = page(first["next_offset"])

    assert first == repeated
    assert [match["line"] for match in first["matches"]] == ["page a1", "page a2"]
    assert first["truncated"] is True
    assert first["next_offset"] == 2
    assert [match["line"] for match in second["matches"]] == ["page b1", "page b2"]
    assert second["truncated"] is False
    assert second["next_offset"] is None


def test_grep_files_reports_rust_regex_errors_and_unavailable_backend(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("hello\n", encoding="utf-8")

    invalid_regex = grep_files(
        tmp_path,
        pattern=r"hello(?= world)",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        regex_engine="default",
    )
    unavailable = grep_files(
        tmp_path,
        pattern="hello",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        rg_binary="/definitely/missing/rg",
    )

    assert invalid_regex["success"] is False
    assert invalid_regex["error"]["code"] == "invalid_pattern"
    assert "Rust regex syntax" in invalid_regex["error"]["message"]
    assert invalid_regex["backend"]["exit_code"] == 2
    assert unavailable["success"] is False
    assert unavailable["error"]["code"] == "backend_unavailable"
    assert unavailable["backend"] == {
        "name": "ripgrep",
        "binary": "/definitely/missing/rg",
        "status": "unavailable",
    }


def test_grep_files_reports_unexpected_backend_exit(tmp_path: Path) -> None:
    (tmp_path / "one.txt").write_text("hello\n", encoding="utf-8")
    fake_rg = tmp_path / "fake-rg"
    fake_rg.write_text("#!/bin/sh\necho 'simulated failure' >&2\nexit 7\n", encoding="utf-8")
    fake_rg.chmod(0o755)

    result = grep_files(
        tmp_path,
        pattern="hello",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        rg_binary=str(fake_rg),
    )

    assert result["success"] is False
    assert result["error"]["code"] == "backend_error"
    assert result["backend"] == {
        "name": "ripgrep",
        "binary": str(fake_rg),
        "status": "error",
        "exit_code": 7,
        "stderr": "simulated failure",
    }


def test_grep_backend_strips_control_plane_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = tmp_path / "captured-env.txt"
    fake_rg = tmp_path / "fake-rg"
    fake_rg.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        f"capture = {str(capture)!r}\n"
        "keys = [\n"
        "    'CHATGPT_MCP_AUTH_TOKEN',\n"
        "    'CHATGPT_MCP_HEALTH_TOKEN',\n"
        "    'CHATGPT_MCP_OAUTH_LOGIN_TOKEN',\n"
        "    'OPENAI_API_KEY',\n"
        "]\n"
        "with open(capture, 'w', encoding='utf-8') as handle:\n"
        "    handle.write('\\n'.join(os.getenv(key, 'unset') for key in keys))\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    fake_rg.chmod(0o755)
    monkeypatch.setenv("CHATGPT_MCP_AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("CHATGPT_MCP_HEALTH_TOKEN", "health-secret")
    monkeypatch.setenv("CHATGPT_MCP_OAUTH_LOGIN_TOKEN", "oauth-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")

    result = grep_files(
        tmp_path,
        pattern="anything",
        glob_pattern=None,
        output_mode="summary",
        head_limit=20,
        offset=0,
        rg_binary=str(fake_rg),
    )

    assert result["success"] is True
    assert capture.read_text(encoding="utf-8").splitlines() == [
        "unset",
        "unset",
        "unset",
        "provider-secret",
    ]


@pytest.mark.skipif(os.name != "posix", reason="Process-group termination is POSIX-specific.")
def test_grep_parse_error_terminates_backend_descendants(tmp_path: Path) -> None:
    child_ready = tmp_path / "child-ready"
    child_terminated = tmp_path / "child-terminated"
    fake_child = tmp_path / "fake-rg-child"
    fake_child.write_text(
        "#!/usr/bin/env python3\n"
        "import signal, time\n"
        f"ready = {str(child_ready)!r}\n"
        f"terminated = {str(child_terminated)!r}\n"
        "def stop(signum, frame):\n"
        "    open(terminated, 'w', encoding='utf-8').write(str(signum))\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "open(ready, 'w', encoding='utf-8').write('ready')\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    fake_child.chmod(0o755)
    fake_rg = tmp_path / "fake-rg"
    fake_rg.write_text(
        "#!/usr/bin/env python3\n"
        "import os, subprocess, sys, time\n"
        f"ready = {str(child_ready)!r}\n"
        f"child = {str(fake_child)!r}\n"
        "subprocess.Popen([child])\n"
        "deadline = time.monotonic() + 2\n"
        "while not os.path.exists(ready) and time.monotonic() < deadline: time.sleep(0.01)\n"
        "print('not-json', flush=True)\n",
        encoding="utf-8",
    )
    fake_rg.chmod(0o755)

    started_at = time.monotonic()
    result = grep_files(
        tmp_path,
        pattern="anything",
        glob_pattern=None,
        output_mode="content",
        head_limit=20,
        offset=0,
        rg_binary=str(fake_rg),
    )
    elapsed = time.monotonic() - started_at

    assert result["success"] is False
    assert result["error"]["code"] == "backend_error"
    assert elapsed < 1.0
    deadline = time.monotonic() + 2
    while not child_terminated.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_ready.exists()
    assert child_terminated.exists()
