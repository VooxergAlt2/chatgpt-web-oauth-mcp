from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.shell import MAX_COMMAND_TIMEOUT_SECONDS


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _python_cmd(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def test_server_apply_patch_tool_updates_file(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    target = tmp_path / "note.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")

    result = _call(
        server.apply_patch,
        patch="\n".join(
            [
                "*** Begin Patch",
                f"*** Update File: {target}",
                "@@",
                " hello",
                "-world",
                "+there",
                "*** End Patch",
            ]
        )
    )

    assert result["success"] is True
    assert target.read_text(encoding="utf-8") == "hello\nthere\n"


def test_server_read_text_tool_returns_multiple_file_results(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("alpha\n", encoding="utf-8")
    second.write_text("beta\n", encoding="utf-8")

    result = _call(server.read_text, paths=[str(first), str(second)])

    assert result["success"] is True
    assert result["mode"] == "batch"
    assert [item["content"] for item in result["results"]] == ["alpha", "beta"]


def test_server_search_tool_unifies_regex_text_and_glob(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.py"
    second = tmp_path / "two.txt"
    first.write_text("alpha\nTODO: fix me\n", encoding="utf-8")
    second.write_text("beta\nTODO: docs\n", encoding="utf-8")

    glob_result = _call(server.search, mode="glob", path=str(tmp_path), pattern="*.py")
    text_result = _call(server.search, mode="text", path=str(tmp_path), query="TODO", limit=10)
    regex_result = _call(
        server.search,
        mode="regex",
        path=str(tmp_path),
        pattern=r"TODO:\s+\w+",
        output_mode="files_with_matches",
    )

    assert glob_result["success"] is True
    assert glob_result["mode"] == "glob"
    assert [Path(item["path"]).name for item in glob_result["matches"]] == ["one.py"]

    assert text_result["success"] is True
    assert text_result["mode"] == "text"
    assert len(text_result["matches"]) == 2

    assert regex_result["success"] is True
    assert regex_result["mode"] == "regex"
    assert {Path(path).name for path in regex_result["files"]} == {"one.py", "two.txt"}


def test_server_search_threads_word_cap_and_regex_engine(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("cat\nconcatenate\ncat\ncat\n", encoding="utf-8")
    second.write_text("cat\ncat\ncat\n", encoding="utf-8")

    result = _call(
        server.search,
        mode="text",
        path=str(tmp_path),
        query="cat",
        word=True,
        max_per_file=2,
    )
    regex_result = _call(
        server.search,
        mode="regex",
        path=str(tmp_path),
        pattern=r"cat(?=\b)",
        regex_engine="auto",
        max_per_file=1,
    )

    assert result["success"] is True
    assert [Path(match["path"]).name for match in result["matches"]] == [
        "one.txt",
        "one.txt",
        "two.txt",
        "two.txt",
    ]
    assert regex_result["success"] is True
    assert [Path(match["path"]).name for match in regex_result["matches"]] == [
        "one.txt",
        "two.txt",
    ]

    batch = _call(
        server.search,
        mode="sequential",
        path=str(tmp_path),
        word=True,
        max_per_file=1,
        queries=[
            {"mode": "text", "query": "cat"},
            {
                "mode": "text",
                "query": "cat",
                "word": False,
                "max_per_file": None,
                "regex_engine": "made-up",
            },
        ],
    )
    assert batch["success"] is True
    assert [len(item["matches"]) for item in batch["results"]] == [2, 7]


def test_server_search_git_ref_adds_python_enclosing_symbol(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    source = tmp_path / "module.py"
    source.write_text(
        "class Alpha:\n"
        "    def method(self):\n"
        "        marker = 'needle'\n"
        "        return marker\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "module.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "old"], cwd=tmp_path, check=True)
    old_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source.write_text("marker = 'current'\n", encoding="utf-8")

    result = _call(
        server.search,
        mode="text",
        path=str(tmp_path),
        query="needle",
        git_ref=old_ref,
        enclosing_symbol=True,
        glob="*.py",
    )

    assert result["success"] is True
    assert result["source"]["requested_ref"] == old_ref
    assert result["matches"][0]["repo_path"] == "module.py"
    assert result["matches"][0]["enclosing_symbol"]["qualname"] == "Alpha.method"
    assert result["matches"][0]["enclosing_symbol"]["kind"] == "method"


def test_server_search_supports_batch_modes(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.py"
    second = tmp_path / "two.txt"
    first.write_text("alpha\nTODO: fix me\n", encoding="utf-8")
    second.write_text("beta\nTODO: docs\n", encoding="utf-8")

    sequential = _call(
        server.search,
        mode="sequential",
        path=str(tmp_path),
        queries=[
            {"mode": "glob", "pattern": "*.py"},
            {"mode": "text", "query": "TODO", "limit": 10},
        ],
    )
    parallel = _call(
        server.search,
        mode="parallel",
        path=str(tmp_path),
        queries=[
            {"mode": "regex", "pattern": r"TODO:\s+\w+", "output_mode": "files_with_matches"},
            {"mode": "text", "path": str(first), "query": "alpha"},
        ],
        max_concurrency=2,
    )

    assert sequential["success"] is True
    assert sequential["mode"] == "batch"
    assert sequential["execution_mode"] == "sequential"
    assert sequential["max_concurrency"] == 1
    assert [item["index"] for item in sequential["results"]] == [0, 1]
    assert [Path(item["path"]).name for item in sequential["results"][0]["matches"]] == ["one.py"]
    assert len(sequential["results"][1]["matches"]) == 2

    assert parallel["success"] is True
    assert parallel["execution_mode"] == "parallel"
    assert parallel["max_concurrency"] == 2
    assert [item["index"] for item in parallel["results"]] == [0, 1]
    assert {Path(path).name for path in parallel["results"][0]["files"]} == {"one.py", "two.txt"}
    assert parallel["results"][1]["matches"][0]["path"] == str(first)


def test_server_search_batch_validates_inputs(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    empty = _call(server.search, mode="parallel", queries=[])
    too_much_concurrency = _call(
        server.search,
        mode="parallel",
        path=str(tmp_path),
        queries=[{"mode": "glob", "pattern": "*.py"}],
        max_concurrency=4,
    )

    assert empty["success"] is False
    assert empty["error"]["code"] == "invalid_arguments"
    assert too_much_concurrency["success"] is False
    assert too_much_concurrency["error"]["code"] == "invalid_arguments"


def test_server_run_command_supports_batch_modes(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    sequential = _call(
        server.run_command,
        context=SimpleNamespace(
                request_context=SimpleNamespace(request_id="batch-sequential"),
                request_id="batch-sequential",
            ),
        commands=[_python_cmd("print('one')"), _python_cmd("print('two')")],
        cwd=str(tmp_path),
        timeout=5,
        mode="sequential",
    )
    parallel = _call(
        server.run_command,
        context=SimpleNamespace(request_id="batch-parallel"),
        commands=[_python_cmd("print('red')"), _python_cmd("print('blue')")],
        cwd=str(tmp_path),
        timeout=5,
        mode="parallel",
        max_concurrency=2,
    )

    assert sequential["success"] is True
    assert sequential["mode"] == "batch"
    assert sequential["execution_mode"] == "sequential"
    assert [item["stdout"].strip() for item in sequential["results"]] == ["one", "two"]

    assert parallel["success"] is True
    assert parallel["execution_mode"] == "parallel"
    assert parallel["max_concurrency"] == 2
    assert [item["stdout"].strip() for item in parallel["results"]] == ["red", "blue"]


def test_server_run_command_caps_openai_foreground_without_splitting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, session

    monkeypatch.setattr(server, "COMMAND_TIMEOUT", 300)
    monkeypatch.setattr(server, "OPENAI_FOREGROUND_TIMEOUT", 105)
    binding = session.bind_session("openai:test-foreground-cap")
    try:
        defaulted = _call(
            server.run_command,
            context=SimpleNamespace(
                request_context=SimpleNamespace(request_id="defaulted"),
                request_id="defaulted",
            ),
            command=_python_cmd("print('defaulted')"),
            cwd=str(tmp_path),
        )
        rejected = _call(
            server.run_command,
            context=SimpleNamespace(
                request_context=SimpleNamespace(request_id="rejected"),
                request_id="rejected",
            ),
            command=_python_cmd("print('must-not-run')"),
            cwd=str(tmp_path),
            timeout=106,
        )
    finally:
        session.reset_session_binding(binding)

    assert defaulted["success"] is True
    assert defaulted["timeout"] == 105
    assert defaulted["stdout"].strip() == "defaulted"
    assert rejected["success"] is False
    assert rejected["error"]["code"] == "openai_foreground_timeout_exceeds_budget"
    assert rejected["error"]["max_timeout_seconds"] == 105
    assert rejected["hint"] == "use_one_durable_job_for_long_coherent_work"
    assert "Do not split a coherent command" in rejected["error"]["message"]


def test_server_run_command_keeps_direct_local_ceiling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, session

    monkeypatch.setattr(server, "COMMAND_TIMEOUT", 300)
    monkeypatch.setattr(server, "OPENAI_FOREGROUND_TIMEOUT", 105)
    binding = session.bind_session("transport:local-test")
    try:
        result = _call(
            server.run_command,
            context=SimpleNamespace(
                request_context=SimpleNamespace(request_id="local"),
                request_id="local",
            ),
            command=_python_cmd("print('local')"),
            cwd=str(tmp_path),
            timeout=300,
        )
    finally:
        session.reset_session_binding(binding)

    assert result["success"] is True
    assert result["timeout"] == 300
    assert result["stdout"].strip() == "local"


def test_server_run_command_timeout_limit_requires_force(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    rejected = _call(
        server.run_command,
        context=SimpleNamespace(request_id="timeout-rejected"),
        command="echo hi",
        cwd=str(tmp_path),
        timeout=MAX_COMMAND_TIMEOUT_SECONDS + 1,
    )
    forced = _call(
        server.run_command,
        context=SimpleNamespace(request_id="timeout-forced"),
        command=_python_cmd("print('forced')"),
        cwd=str(tmp_path),
        timeout=MAX_COMMAND_TIMEOUT_SECONDS + 1,
        force=True,
    )

    assert rejected["success"] is False
    assert rejected["error"]["code"] == "timeout_exceeds_limit"
    assert rejected["error"]["approval_required"] is True
    assert forced["success"] is True
    assert forced["force"] is True
    assert forced["stdout"].strip() == "forced"


def test_server_run_command_uses_runtime_output_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatgpt_web_oauth_mcp import server

    monkeypatch.setattr(server, "RUN_TOKEN_BUDGET", 40)
    monkeypatch.setattr(server, "RUN_CAPTURE_MAX_BYTES", 2048)
    result = _call(
        server.run_command,
        context=SimpleNamespace(request_id="output-limits"),
        command=_python_cmd("[print(f'line-{index:03d}') for index in range(200)]"),
        cwd=str(tmp_path),
        timeout=5,
    )

    metadata = result["output_metadata"]["aggregate"]
    assert result["success"] is True
    assert metadata["effective_token_budget"] == 40
    assert metadata["capture_memory_limit_bytes"] == 2048
    assert metadata["token_count"] <= 40
    assert metadata["displayed_payload_fits"] is True


def test_server_job_reads_use_runtime_output_token_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatgpt_web_oauth_mcp import server

    observed: list[tuple[str, int]] = []

    class CapturingRegistry:
        def list_jobs(self, **kwargs):
            observed.append(("list", kwargs["max_tokens"]))
            return {"success": True}

        def output_job(self, **kwargs):
            observed.append(("output", kwargs["max_tokens"]))
            return {"success": True}

    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(server, "TOOL_OUTPUT_TOKEN_BUDGET", 222)
    monkeypatch.setattr(server, "JOB_OUTPUT_TOKEN_BUDGET", 111)
    monkeypatch.setattr(server, "job_registry", CapturingRegistry())

    _call(server.job_list)
    _call(server.job_output, job_id="job_runtime_budget")

    assert observed == [("list", 222), ("output", 111)]


def test_server_read_text_supports_single_and_batch_modes(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    first.write_text("alpha\nbeta\n", encoding="utf-8")
    second.write_text("gamma\ndelta\n", encoding="utf-8")

    single = _call(server.read_text, path=str(first), start_line=2, line_limit=1)
    batch = _call(
        server.read_text,
        paths=[str(first), str(second)],
        start_line=1,
        line_limit=1,
    )

    assert single["success"] is True
    assert single["mode"] == "single"
    assert single["content"] == "beta"

    assert batch["success"] is True
    assert batch["mode"] == "batch"
    assert [item["content"] for item in batch["results"]] == ["alpha", "gamma"]


def test_server_read_text_can_include_line_numbers(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.txt"
    first.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    single = _call(
        server.read_text,
        path=str(first),
        start_line=2,
        line_limit=2,
        include_line_numbers=True,
    )

    assert single["success"] is True
    assert single["mode"] == "single"
    assert single["content"] == "2: beta\n3: gamma"


def test_server_read_text_uses_runtime_read_token_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatgpt_web_oauth_mcp import server

    target = tmp_path / "tokens.txt"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    monkeypatch.setattr(server, "READ_TOKEN_BUDGET", 3)

    first = _call(server.read_text, path=str(target), line_limit=10)
    second = _call(
        server.read_text,
        path=str(target),
        start_line=first["next_offset"],
        line_limit=10,
    )

    assert first["content"] == "alpha\nbeta"
    assert first["next_offset"] == 3
    assert first["page"]["stop_reason"] == "token_budget"
    assert first["page"]["effective_budgets"]["tokens"] == 3
    assert second["content"] == "gamma"
    assert second["next_offset"] is None


def test_server_read_text_uses_lossless_byte_pagination_for_batch(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    oversized = "x" * 33000
    first.write_text(f"head\n{oversized}\ntail\n", encoding="utf-8")
    second.write_text(f"start\n{oversized}\nend\n", encoding="utf-8")

    batch = _call(
        server.read_text,
        paths=[str(first), str(second)],
        start_line=1,
        line_limit=3,
    )
    oversized_page = _call(
        server.read_text,
        path=str(first),
        start_line=batch["results"][0]["next_offset"],
        line_limit=3,
    )

    assert [item["content"] for item in batch["results"]] == ["head", "start"]
    assert [item["end_line"] for item in batch["results"]] == [1, 1]
    assert [item["next_offset"] for item in batch["results"]] == [2, 2]
    assert oversized_page["content"] == oversized
    assert oversized_page["oversized_line"] is True
    assert oversized_page["end_line"] == 2
    assert oversized_page["next_offset"] == 3
    assert oversized_page["page"]["stop_reason"] == "byte_budget"
    assert oversized_page["page"]["budget_exceeded"] == {"bytes": True, "tokens": False}


def test_server_read_text_requires_exactly_one_path_argument(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    both_missing = _call(server.read_text)
    both_present = _call(server.read_text, path="one.txt", paths=["two.txt"])

    assert both_missing["success"] is False
    assert both_missing["error"]["code"] == "invalid_arguments"
    assert both_present["success"] is False
    assert both_present["error"]["code"] == "invalid_arguments"


def test_server_search_validates_mode_and_required_fields(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    invalid_mode = _call(server.search, mode="unknown", path=str(tmp_path))
    missing_regex_pattern = _call(server.search, mode="regex", path=str(tmp_path))
    missing_glob_pattern = _call(server.search, mode="glob", path=str(tmp_path))

    assert invalid_mode["success"] is False
    assert invalid_mode["error"]["code"] == "invalid_mode"
    assert missing_regex_pattern["success"] is False
    assert missing_regex_pattern["error"]["code"] == "missing_pattern"
    assert missing_glob_pattern["success"] is False
    assert missing_glob_pattern["error"]["code"] == "missing_pattern"


def test_server_search_supports_single_file_path_for_text_and_regex(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    target = tmp_path / "one.py"
    target.write_text("alpha\nTODO: fix me\n", encoding="utf-8")

    text_result = _call(server.search, mode="text", path=str(target), query="TODO")
    regex_result = _call(server.search, mode="regex", path=str(target), pattern=r"TODO:\s+\w+")

    assert text_result["success"] is True
    assert text_result["mode"] == "text"
    assert len(text_result["matches"]) == 1
    assert text_result["matches"][0]["path"] == str(target)

    assert regex_result["success"] is True
    assert regex_result["mode"] == "regex"
    assert len(regex_result["matches"]) == 1
    assert regex_result["matches"][0]["path"] == str(target)


def test_server_search_exposes_ripgrep_controls_and_literal_text(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    python_file = tmp_path / "one.py"
    text_file = tmp_path / "two.txt"
    python_file.write_text("标签 [x] [x]\n", encoding="utf-8")
    text_file.write_text("标签 [x]\n", encoding="utf-8")

    content = _call(
        server.search,
        mode="text",
        path=str(tmp_path),
        query="[x]",
        file_type="py",
        only_matching=True,
    )
    summary = _call(
        server.search,
        mode="text",
        path=str(tmp_path),
        query="[x]",
        output_mode="summary",
        limit=1,
        offset=50,
    )

    assert [match["line"] for match in content["matches"]] == ["[x]", "[x]"]
    assert {match["path"] for match in content["matches"]} == {str(python_file)}
    assert summary["summary"] == {"occurrences": 3, "matched_files": 2}


def test_server_search_uses_runtime_ripgrep_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatgpt_web_oauth_mcp import server

    (tmp_path / "one.txt").write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(server, "RIPGREP_BINARY", "/missing/runtime/rg")

    result = _call(server.search, mode="text", path=str(tmp_path), query="hello")

    assert result["success"] is False
    assert result["error"]["code"] == "backend_unavailable"
    assert result["backend"]["binary"] == "/missing/runtime/rg"


def test_server_search_regex_uses_rust_regex_semantics(tmp_path: Path) -> None:
    from chatgpt_web_oauth_mcp import server

    (tmp_path / "one.txt").write_text("hello world\n", encoding="utf-8")

    result = _call(
        server.search,
        mode="regex",
        path=str(tmp_path),
        pattern=r"hello(?= world)",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "invalid_pattern"
    assert "Rust regex syntax" in result["error"]["message"]








def test_programmatic_mcp_call_tool_run_command_without_request_context(
    tmp_path: Path,
) -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> None:
        result = await server.mcp.call_tool(
            "run_command",
            {
                "command": _python_cmd("print('PROGRAMMATIC_OK')"),
                "cwd": str(tmp_path),
            },
        )
        assert result.structured_content is not None
        assert result.structured_content["success"] is True
        assert result.structured_content["stdout"].strip() == "PROGRAMMATIC_OK"

    asyncio.run(scenario())


def test_session_resume_marks_missing_non_durable_delegate_interrupted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, session
    from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore

    class MissingDelegateRegistry:
        def delegate_status(self, **_kwargs):
            return {
                "success": False,
                "error": {
                    "code": "delegate_not_found",
                    "message": "missing after restart",
                },
            }

    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.put(
        session_key="openai:resume-test",
        checkpoint={
            "goal": "finish review",
            "current_slice": "delegate audit",
            "next_action": "poll delegate then continue",
            "delegate_ids": ["delegate-old"],
            "cwd": str(tmp_path),
        },
    )
    store.record_runtime(
        session_key="openai:resume-test",
        last_tool="delegate_status",
        delegates={
            "delegate-old": {
                "status": "running",
                "terminal": False,
                "harness": "antigravity",
            }
        },
        next_action="poll delegate",
    )

    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", MissingDelegateRegistry())
    binding = session.bind_session("openai:resume-test")
    try:
        resumed = _call(server.session_resume)
    finally:
        session.reset_session_binding(binding)

    assert resumed["resumable"] is True
    assert resumed["resume_state"] == "delegate_interrupted_by_server_restart"
    assert "delegate-old" in resumed["next_action"]
    assert "poll delegate then continue" in resumed["next_action"]
    delegate = resumed["delegates"][0]["delegate"]
    assert delegate["status"] == "cancelled"
    assert delegate["completed"] is True
    assert delegate["error"]["code"] == "server_restart"


def test_session_resume_preserves_terminal_delegate_snapshot_after_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, session
    from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore

    class MissingDelegateRegistry:
        def delegate_status(self, **_kwargs):
            return {
                "success": False,
                "error": {
                    "code": "delegate_not_found",
                    "message": "missing after restart",
                },
            }

    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:resume-terminal",
        last_tool="delegate_status",
        delegates={
            "delegate-done": {
                "status": "succeeded",
                "terminal": True,
                "completed": True,
                "success": True,
                "harness": "antigravity",
                "summary": "terminal review result",
                "logs": {"stdout": "/tmp/delegate/stdout.log"},
                "structured_output": {
                    "status": "succeeded",
                    "findings": ["clean"],
                },
            }
        },
        next_action="consume delegate result",
    )

    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", MissingDelegateRegistry())
    binding = session.bind_session("openai:resume-terminal")
    try:
        resumed = _call(server.session_resume)
    finally:
        session.reset_session_binding(binding)

    assert resumed["resumable"] is True
    assert resumed["resume_state"] == "terminal_results_ready"
    delegate = resumed["delegates"][0]["delegate"]
    assert delegate["status"] == "succeeded"
    assert delegate["success"] is True
    assert delegate["summary"] == "terminal review result"
    assert delegate["logs"]["stdout"] == "/tmp/delegate/stdout.log"
    assert delegate["structured_output"] == {
        "status": "succeeded",
        "findings": ["clean"],
    }
    assert delegate["error"] if "error" in delegate else None is None


def test_session_resume_prioritizes_owned_work_over_semantic_next_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, session
    from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore

    class RunningJobRegistry:
        def job_status(self, **kwargs):
            return {
                "success": True,
                "job_id": kwargs["job_id"],
                "status": "running",
                "exit_code": None,
                "cwd": str(tmp_path),
            }

    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.put(
        session_key="openai:resume-running",
        checkpoint={
            "goal": "finish acceptance",
            "current_slice": "runtime verification",
            "next_action": "run final gate",
            "done_means": ["full suite passes"],
            "job_ids": ["job_running"],
            "delegate_ids": [],
            "cwd": str(tmp_path),
        },
    )
    store.record_runtime(
        session_key="openai:resume-running",
        last_tool="job_start",
        jobs={
            "job_running": {
                "status": "running",
                "terminal": False,
                "cwd": str(tmp_path),
            }
        },
        next_action="Poll or inspect the owned durable job until terminal.",
    )

    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "job_registry", RunningJobRegistry())
    binding = session.bind_session("openai:resume-running")
    try:
        resumed = _call(server.session_resume)
    finally:
        session.reset_session_binding(binding)

    assert resumed["resumable"] is True
    assert resumed["resume_state"] == "owned_work_in_progress"
    assert "until terminal" in resumed["next_action"]
    assert "run final gate" in resumed["next_action"]
    assert resumed["jobs"][0]["status"] == "running"


def test_passive_owned_work_refresh_preserves_runtime_next_action(
    tmp_path: Path,
) -> None:
    from chatgpt_web_oauth_mcp import session_continuation
    from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore

    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:passive-refresh",
        last_tool="job_start",
        jobs={
            "job-running": {
                "status": "running",
                "terminal": False,
                "cwd": str(tmp_path),
            }
        },
        next_action="preserve this plan",
    )

    class RunningJobRegistry:
        def job_status(self, **kwargs):
            return {
                "success": True,
                "job_id": kwargs["job_id"],
                "status": "running",
                "exit_code": None,
                "cwd": str(tmp_path),
            }

    ctx = SimpleNamespace(
        checkpoint_store=store,
        job_registry=RunningJobRegistry(),
        state_dir=tmp_path,
        registry=SimpleNamespace(),
        tool_output_token_budget=8500,
    )

    session_continuation.refresh_session_owned_work(
        ctx,
        session_key="openai:passive-refresh",
    )

    checkpoint = store.get("openai:passive-refresh")
    assert checkpoint is not None
    assert checkpoint["runtime"]["last_tool"] == "job_start"
    assert checkpoint["runtime"]["next_action"] == "preserve this plan"


def test_explicit_job_refresh_preserves_terminal_snapshot_on_status_error(
    tmp_path: Path,
) -> None:
    from chatgpt_web_oauth_mcp import session_continuation
    from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore

    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:terminal-job",
        last_tool="job_status",
        jobs={
            "job-terminal": {
                "status": "succeeded",
                "terminal": True,
                "success": True,
                "exit_code": 0,
                "cwd": str(tmp_path),
            }
        },
        next_action="consume terminal job",
    )

    class MissingJobRegistry:
        def job_status(self, **kwargs):
            return {
                "success": False,
                "job_id": kwargs["job_id"],
                "status": "unknown",
                "error": {"code": "job_not_found"},
            }

    ctx = SimpleNamespace(
        checkpoint_store=store,
        job_registry=MissingJobRegistry(),
        state_dir=tmp_path,
        registry=SimpleNamespace(),
        tool_output_token_budget=8500,
    )

    refreshed = session_continuation.refresh_session_owned_work(
        ctx,
        session_key="openai:terminal-job",
        kind="job",
        result_id="job-terminal",
    )

    assert refreshed["jobs"][0]["status"] == "succeeded"
    checkpoint = store.get("openai:terminal-job")
    assert checkpoint is not None
    state = checkpoint["runtime"]["jobs"]["job-terminal"]
    assert state["terminal"] is True
    assert state["status"] == "succeeded"
    assert state["continuation_state"] == "RESULT_REQUIRES_CONSUMPTION"


def test_group_observer_fetches_full_terminal_child_before_recording(
    tmp_path: Path,
) -> None:
    from chatgpt_web_oauth_mcp import session, session_continuation
    from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore

    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:group-owner",
        last_tool="delegate_batch",
        delegates={
            "delegate-done": {
                "status": "running",
                "terminal": False,
                "group_id": "group-1",
            }
        },
    )

    class Registry:
        def delegate_status(self, **kwargs):
            assert kwargs["delegate_id"] == "delegate-done"
            return {
                "success": True,
                "delegate": {
                    "delegate_id": "delegate-done",
                    "status": "succeeded",
                    "completed": True,
                    "success": True,
                    "summary": "full terminal result",
                    "structured_output": {"findings": ["kept"]},
                    "group_id": "group-1",
                    "cwd": str(tmp_path),
                },
            }

    ctx = SimpleNamespace(
        checkpoint_store=store,
        registry=Registry(),
        tool_output_token_budget=8500,
    )
    binding = session.bind_session("openai:group-owner")
    try:
        session_continuation.observe_delegate_group_result(
            ctx,
            tool_name="delegate_status",
            result={
                "success": True,
                "group": {
                    "group_id": "group-1",
                    "children": [
                        {
                            "delegate_id": "delegate-done",
                            "status": "succeeded",
                            "completed": True,
                        }
                    ],
                },
            },
            cwd=str(tmp_path),
        )
    finally:
        session.reset_session_binding(binding)

    pending = store.pending_results("openai:group-owner")
    assert len(pending) == 1
    assert pending[0]["summary"] == "full terminal result"
    assert pending[0]["structured_output"] == {"findings": ["kept"]}


def test_group_observer_records_all_children_in_one_checkpoint_transaction() -> None:
    from chatgpt_web_oauth_mcp import session, session_continuation

    calls: list[dict[str, object]] = []

    class RecordingStore:
        def result_ownership_scope(self, *_args, **_kwargs):
            return "owned_here"

        def record_runtime(self, **kwargs):
            calls.append(kwargs)
            return {}

    ctx = SimpleNamespace(
        checkpoint_store=RecordingStore(),
        registry=SimpleNamespace(),
        tool_output_token_budget=8500,
    )
    binding = session.bind_session("openai:group-owner")
    try:
        session_continuation.observe_delegate_group_result(
            ctx,
            tool_name="delegate_status",
            result={
                "success": True,
                "group": {
                    "group_id": "group-atomic",
                    "children": [
                        {
                            "delegate_id": "delegate-a",
                            "status": "running",
                            "completed": False,
                        },
                        {
                            "delegate_id": "delegate-b",
                            "status": "queued",
                            "completed": False,
                        },
                    ],
                },
            },
        )
    finally:
        session.reset_session_binding(binding)

    assert len(calls) == 1
    delegates = calls[0]["delegates"]
    assert isinstance(delegates, dict)
    assert set(delegates) == {"delegate-a", "delegate-b"}


def test_session_resume_marks_disk_recovered_restart_delegate_interrupted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, session
    from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore

    class RecoveredDelegateRegistry:
        def delegate_status(self, **_kwargs):
            return {
                "success": True,
                "delegate": {
                    "delegate_id": "delegate-recovered",
                    "status": "cancelled",
                    "completed": True,
                    "in_progress": False,
                    "success": False,
                    "recovered_from_disk": True,
                    "error": {
                        "code": "server_restart",
                        "message": "interrupted during restart",
                    },
                },
            }

    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:resume-recovered",
        last_tool="delegate_status",
        delegates={
            "delegate-recovered": {
                "status": "running",
                "terminal": False,
                "harness": "antigravity",
            }
        },
        next_action="poll delegate",
    )

    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", RecoveredDelegateRegistry())
    binding = session.bind_session("openai:resume-recovered")
    try:
        resumed = _call(server.session_resume)
    finally:
        session.reset_session_binding(binding)

    assert resumed["resumable"] is True
    assert resumed["resume_state"] == "delegate_interrupted_by_server_restart"
    assert "delegate-recovered" in resumed["next_action"]
    delegate = resumed["delegates"][0]["delegate"]
    assert delegate["recovered_from_disk"] is True
    assert delegate["error"]["code"] == "server_restart"


def test_session_resume_marks_graceful_shutdown_delegate_interrupted(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, session
    from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore

    class ShutdownDelegateRegistry:
        def delegate_status(self, **_kwargs):
            return {
                "success": True,
                "delegate": {
                    "delegate_id": "delegate-shutdown",
                    "status": "cancelled",
                    "completed": True,
                    "in_progress": False,
                    "success": False,
                    "error": {
                        "code": "server_shutdown",
                        "message": "interrupted during graceful MCP shutdown",
                    },
                },
            }

    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:resume-shutdown",
        last_tool="delegate_status",
        delegates={
            "delegate-shutdown": {
                "status": "running",
                "terminal": False,
                "harness": "antigravity",
            }
        },
        next_action="poll delegate",
    )

    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", ShutdownDelegateRegistry())
    binding = session.bind_session("openai:resume-shutdown")
    try:
        resumed = _call(server.session_resume)
    finally:
        session.reset_session_binding(binding)

    assert resumed["resumable"] is True
    assert resumed["resume_state"] == "delegate_interrupted_by_server_restart"
    assert "delegate-shutdown" in resumed["next_action"]
    delegate = resumed["delegates"][0]["delegate"]
    assert delegate["error"]["code"] == "server_shutdown"


def test_server_apply_patch_tool_description_uses_generic_patch_language() -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> str:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        apply_patch_tool = next(tool for tool in tools if tool.name == "apply_patch")
        return apply_patch_tool.description

    description = asyncio.run(scenario())

    assert "codex-style" not in description
    assert "*** Begin Patch" in description




def test_code_map_descriptions_explain_development_usage() -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> dict[str, str]:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {tool.name: tool.description for tool in tools}

    descriptions = asyncio.run(scenario())
    assert "code_map_symbols to find definitions" in server.MCP_INSTRUCTIONS
    assert "code_map_references to estimate impact" in server.MCP_INSTRUCTIONS
    assert "code_map_imports to inspect module boundaries" in server.MCP_INSTRUCTIONS
    assert "files_in_scope" in server.MCP_INSTRUCTIONS
    assert "precise rename, type inference, or call graph analysis" in server.MCP_INSTRUCTIONS
    assert "get_code_graph_use" in server.MCP_INSTRUCTIONS
    assert "Canonical Code Graph startup is code_graph_status(ref)" in server.MCP_INSTRUCTIONS
    assert "query_ready=false" in server.MCP_INSTRUCTIONS
    assert "await_job(job_id)" in server.MCP_INSTRUCTIONS
    assert "code_graph_prepare is the graph/runtime launcher" in server.MCP_INSTRUCTIONS
    assert "do not search for a Joern shell command" in server.MCP_INSTRUCTIONS
    assert "semantic callers, callees" in server.MCP_INSTRUCTIONS
    assert "committed diff impact" in server.MCP_INSTRUCTIONS
    assert "code_graph_diff_impact" in server.MCP_INSTRUCTIONS
    assert "affected files/tests" in server.MCP_INSTRUCTIONS
    assert "module-scope changes" in server.MCP_INSTRUCTIONS
    assert "committed-tree evidence" in server.MCP_INSTRUCTIONS
    assert "semantic queries never cold-start it" in server.MCP_INSTRUCTIONS
    assert "call_resolution_complete" in server.MCP_INSTRUCTIONS
    assert "unresolved_call_sites" in server.MCP_INSTRUCTIONS
    assert "Static CPG evidence does not replace focused tests or runtime acceptance" in server.MCP_INSTRUCTIONS
    assert "job_list discovers records from the current state directory" in server.MCP_INSTRUCTIONS
    assert "raw-byte cursor" in server.MCP_INSTRUCTIONS
    assert "job_tail remains the backward-compatible last-N-lines API" in server.MCP_INSTRUCTIONS
    assert "Execution-loop contract" in server.MCP_INSTRUCTIONS
    assert "execution_state" in server.MCP_INSTRUCTIONS
    assert "NEXT_ACTION_REQUIRED" in server.MCP_INSTRUCTIONS
    assert "QUIET requires recheck" in server.MCP_INSTRUCTIONS
    assert "STALLED_SUSPECTED" in server.MCP_INSTRUCTIONS
    assert "never kills a process automatically" in server.MCP_INSTRUCTIONS
    assert "delegate_task/delegate_batch" in server.MCP_INSTRUCTIONS
    assert "get_guide(name='delegate-use')" in server.MCP_INSTRUCTIONS
    assert "Never treat an agent's success claim as acceptance" in server.MCP_INSTRUCTIONS
    assert "do not split a command solely to reduce wall-clock duration" in server.MCP_INSTRUCTIONS
    assert "work must survive a client disconnect" in server.MCP_INSTRUCTIONS
    assert "shared foreground wall-clock budget" in server.MCP_INSTRUCTIONS
    assert "bounded work expected to finish" in descriptions["run_command"]
    assert "shared foreground wall-clock budget" in descriptions["run_command"]
    assert "do not split a command solely to reduce wall-clock duration" in descriptions["run_command"]

    assert "before edits or reviews" in descriptions["code_map_symbols"]
    assert "candidate files_in_scope" in descriptions["code_map_symbols"]
    assert "estimate impact" in descriptions["code_map_references"]
    assert "definition lines may also appear" in descriptions["code_map_references"]
    assert "module boundaries" in descriptions["code_map_imports"]
    assert "dependency direction" in descriptions["code_map_imports"]


def test_registered_tool_input_schemas_document_parameters() -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> dict[str, dict[str, object]]:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {tool.name: tool.parameters for tool in tools}

    schemas = asyncio.run(scenario())
    missing = []
    for tool_name, schema in schemas.items():
        for param_name, spec in schema.get("properties", {}).items():
            if not spec.get("description"):
                missing.append(f"{tool_name}.{param_name}")

    assert missing == []
    for name in ["command", "commands", "mode", "max_concurrency", "force"]:
        assert name in schemas["run_command"]["properties"]
    for name in [
        "queries",
        "mode",
        "max_concurrency",
        "file_type",
        "only_matching",
        "word",
        "max_per_file",
        "regex_engine",
        "git_ref",
        "enclosing_symbol",
    ]:
        assert name in schemas["search"]["properties"]
    for name in ["cwd", "include_packages"]:
        assert name in schemas["env_snapshot"]["properties"]
    for name in ["left", "right"]:
        assert name in schemas["env_diff"]["properties"]
    for name in ["path", "language", "limit"]:
        assert name in schemas["code_map_symbols"]["properties"]
        assert name in schemas["code_map_imports"]["properties"]
    for name in ["path", "symbol", "glob", "limit"]:
        assert name in schemas["code_map_references"]["properties"]
    for name in ["path", "base_ref", "mode", "branch"]:
        assert name in schemas["git_worktree_create"]["properties"]
    for name in ["path", "force"]:
        assert name in schemas["git_worktree_remove"]["properties"]
    for name in ["status", "offset", "limit"]:
        assert name in schemas["job_list"]["properties"]
    for name in ["job_id", "stream", "cursor", "max_bytes", "wait_ms"]:
        assert name in schemas["job_output"]["properties"]
    assert schemas["run_command"]["properties"]["mode"]["enum"] == [
        "sequential",
        "parallel",
    ]
    assert schemas["git_worktree_create"]["properties"]["mode"]["enum"] == [
        "clean",
        "detached",
    ]


def test_server_tools_expose_chatgpt_compatible_annotations() -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario() -> dict[str, dict[str, object]]:
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {
            tool.name: {
                "title": tool.title,
                "annotations": tool.annotations.model_dump(exclude_none=True),
            }
            for tool in tools
        }

    descriptors = asyncio.run(scenario())
    annotations = {name: value["annotations"] for name, value in descriptors.items()}

    assert annotations
    assert all(value["title"] for value in descriptors.values())
    assert all(value for value in annotations.values())
    assert annotations["server_info"]["readOnlyHint"] is True
    assert annotations["execution_state"]["readOnlyHint"] is True
    assert annotations["env_snapshot"]["readOnlyHint"] is True
    assert annotations["env_diff"]["readOnlyHint"] is True
    assert annotations["search"]["readOnlyHint"] is True
    assert annotations["code_map_symbols"]["readOnlyHint"] is True
    assert annotations["code_map_references"]["readOnlyHint"] is True
    assert annotations["code_map_imports"]["readOnlyHint"] is True
    assert annotations["write_file"]["readOnlyHint"] is False
    assert annotations["write_file"]["destructiveHint"] is True
    assert annotations["git_worktree_create"]["readOnlyHint"] is False
    assert annotations["git_worktree_list"]["readOnlyHint"] is True
    assert annotations["git_worktree_status"]["readOnlyHint"] is True
    assert annotations["git_worktree_remove"]["destructiveHint"] is True
    assert annotations["run_command"]["openWorldHint"] is True
    assert annotations["job_list"]["readOnlyHint"] is True
    assert annotations["job_output"]["readOnlyHint"] is True
    assert annotations["get_guide"]["readOnlyHint"] is True
    assert annotations["get_code_graph_use"]["readOnlyHint"] is True
    for removed in [
        "run_command_stream",
        "get_task",
        "wait_task",
        "cancel_task",
        "purge_tasks",
        "taskboard_create",
        "taskboard_delegate",
        "taskboard_status",
        "list_skills",
        "get_skill_index",
        "get_delegate_use",
        "get_file_use",
        "get_process_use",
        "get_runtime_use",
        "get_git_use",
    ]:
        assert removed not in annotations


def test_execution_state_marks_idle_as_next_action_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatgpt_web_oauth_mcp import server, tools_core

    class FakeJobRegistry:
        def list_jobs(self, **_kwargs):
            return {"success": True, "jobs": [{"job_id": "job_other", "status": "running", "cwd": "/other"}], "total": 1}

    class FakeTmuxClient:
        def __init__(self, **_kwargs):
            pass

        def list_sessions(self, *, include_panes: bool = False):
            assert include_panes is True
            return {"success": True, "session_count": 0, "sessions": []}

    monkeypatch.setattr(server, "job_registry", FakeJobRegistry())
    monkeypatch.setattr(tools_core, "TmuxClient", FakeTmuxClient)

    result = _call(server.execution_state, cwd="/scope")

    assert result["success"] is True
    assert result["state"] == "NEXT_ACTION_REQUIRED"
    assert result["activity_verdict"] == "IDLE"
    assert result["waiting_justified"] is False
    assert result["required_action"] == "INVOKE_NEXT_TOOL_OR_RETURN_CHECKPOINT"
    assert result["running_job_count"] == 0
    assert result["global_running_job_count"] == 1


def test_execution_state_marks_running_job_as_active(monkeypatch: pytest.MonkeyPatch) -> None:
    from chatgpt_web_oauth_mcp import server, tools_core
    from chatgpt_web_oauth_mcp.activity import ActivityTracker

    class FakeJobRegistry:
        def list_jobs(self, **_kwargs):
            return {
                "success": True,
                "jobs": [{"job_id": "job_test", "status": "running", "cwd": "/scope"}],
                "total": 1,
            }

        def job_activity_snapshot(self, **_kwargs):
            return {
                "success": True,
                "job_id": "job_test",
                "status": "running",
                "pid": 101,
                "pgid": 101,
                "process_identity_match": True,
                "process_group_verified": True,
                "process_group_member_count": 1,
                "process_group_signature": [{"pid": 101, "identity": "linux-start-ticks:1"}],
                "process_group_cpu_seconds": 0.0,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "last_output_at": None,
                "elapsed_seconds": 1.0,
            }

    class FakeTmuxClient:
        def __init__(self, **_kwargs):
            pass

        def list_sessions(self, *, include_panes: bool = False):
            assert include_panes is True
            return {"success": True, "session_count": 0, "sessions": []}

    monkeypatch.setattr(server, "job_registry", FakeJobRegistry())
    monkeypatch.setattr(server, "activity_tracker", ActivityTracker())
    monkeypatch.setattr(tools_core, "TmuxClient", FakeTmuxClient)

    result = _call(server.execution_state, cwd="/scope")

    assert result["success"] is True
    assert result["state"] == "ACTIVE_PROCESS"
    assert result["activity_verdict"] == "ACTIVE"
    assert result["waiting_justified"] is True
    assert result["required_action"] == "POLL_OR_INSPECT_ACTIVE_PROCESS"
    assert result["job_activity"][0]["verdict"] == "ACTIVE"


def test_execution_state_escalates_quiet_job_after_repeated_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, tools_core
    from chatgpt_web_oauth_mcp.activity import ActivityTracker

    class FakeJobRegistry:
        def list_jobs(self, **_kwargs):
            return {
                "success": True,
                "jobs": [{"job_id": "job_quiet", "status": "running", "cwd": "/scope"}],
                "total": 1,
            }

        def job_activity_snapshot(self, **_kwargs):
            return {
                "success": True,
                "job_id": "job_quiet",
                "status": "running",
                "pid": 202,
                "pgid": 202,
                "process_identity_match": True,
                "process_group_verified": True,
                "process_group_member_count": 1,
                "process_group_signature": [{"pid": 202, "identity": "linux-start-ticks:2"}],
                "process_group_cpu_seconds": 1.0,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "last_output_at": None,
                "elapsed_seconds": 500.0,
            }

    class FakeTmuxClient:
        def __init__(self, **_kwargs):
            pass

        def list_sessions(self, *, include_panes: bool = False):
            assert include_panes is True
            return {"success": True, "session_count": 0, "sessions": []}

    monkeypatch.setattr(server, "job_registry", FakeJobRegistry())
    monkeypatch.setattr(
        server,
        "activity_tracker",
        ActivityTracker(stall_after_seconds=0, stall_min_observations=2),
    )
    monkeypatch.setattr(tools_core, "TmuxClient", FakeTmuxClient)

    first = _call(server.execution_state, cwd="/scope")
    second = _call(server.execution_state, cwd="/scope")

    assert first["state"] == "QUIET_PROCESS_REQUIRES_RECHECK"
    assert first["activity_verdict"] == "QUIET"
    assert first["waiting_justified"] is False
    assert second["state"] == "STALLED_PROCESS_REQUIRES_INSPECTION"
    assert second["activity_verdict"] == "STALLED_SUSPECTED"
    assert second["waiting_justified"] is False


def test_execution_state_does_not_treat_other_cwd_tmux_pane_as_scoped_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, tools_core

    class FakeJobRegistry:
        def list_jobs(self, **_kwargs):
            return {"success": True, "jobs": [], "total": 0, "truncated": False}

    class FakeTmuxClient:
        def __init__(self, **_kwargs):
            pass

        def list_sessions(self, *, include_panes: bool = False):
            assert include_panes is True
            return {
                "success": True,
                "session_count": 1,
                "sessions": [
                    {
                        "session_name": "mixed",
                        "panes": [
                            {"current_path": "/scope", "pane_dead": True},
                            {"current_path": "/other", "pane_dead": False},
                        ],
                    }
                ],
            }

    monkeypatch.setattr(server, "job_registry", FakeJobRegistry())
    monkeypatch.setattr(tools_core, "TmuxClient", FakeTmuxClient)

    result = _call(server.execution_state, cwd="/scope")

    assert result["state"] == "NEXT_ACTION_REQUIRED"
    assert result["activity_verdict"] == "IDLE"
    assert result["tmux_session_count"] == 1
    assert result["live_tmux_session_count"] == 0


def test_execution_state_refuses_idle_when_running_job_snapshot_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, tools_core

    class FakeJobRegistry:
        def list_jobs(self, **kwargs):
            assert kwargs["limit"] == 200
            return {
                "success": True,
                "jobs": [{"job_id": "job_other", "status": "running", "cwd": "/other"}],
                "total": 250,
                "truncated": True,
            }

    class FakeTmuxClient:
        def __init__(self, **_kwargs):
            pass

        def list_sessions(self, *, include_panes: bool = False):
            assert include_panes is True
            return {"success": True, "session_count": 0, "sessions": []}

    monkeypatch.setattr(server, "job_registry", FakeJobRegistry())
    monkeypatch.setattr(tools_core, "TmuxClient", FakeTmuxClient)

    result = _call(server.execution_state, cwd="/scope")

    assert result["success"] is False
    assert result["state"] == "ACTIVITY_UNKNOWN"
    assert result["activity_verdict"] == "UNKNOWN"
    assert result["waiting_justified"] is False
    assert result["global_running_job_count"] == 250
    assert result["observation_errors"]["jobs"]["code"] == "job_list_truncated"


@pytest.mark.parametrize(
    ("snapshot_status", "identity_match", "group_verified", "expected_state", "expected_verdict"),
    [
        ("running", False, False, "DEAD_PROCESS_REQUIRES_RECONCILIATION", "DEAD"),
        ("succeeded", True, True, "TERMINAL_PROCESS_REQUIRES_NEXT_ACTION", "TERMINAL"),
    ],
)
def test_execution_state_preserves_dead_and_terminal_reasons(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_status: str,
    identity_match: bool,
    group_verified: bool,
    expected_state: str,
    expected_verdict: str,
) -> None:
    from chatgpt_web_oauth_mcp import server, tools_core
    from chatgpt_web_oauth_mcp.activity import ActivityTracker

    class FakeJobRegistry:
        def list_jobs(self, **_kwargs):
            return {
                "success": True,
                "jobs": [{"job_id": "job_race", "status": "running", "cwd": "/scope"}],
                "total": 1,
                "truncated": False,
            }

        def job_activity_snapshot(self, **_kwargs):
            return {
                "success": True,
                "job_id": "job_race",
                "status": snapshot_status,
                "pid": 303,
                "pgid": 303,
                "process_identity_match": identity_match,
                "process_group_verified": group_verified,
                "process_group_member_count": 0,
                "process_group_signature": [],
                "process_group_cpu_seconds": None,
                "stdout_bytes": 0,
                "stderr_bytes": 0,
                "last_output_at": None,
                "elapsed_seconds": 500.0,
            }

    class FakeTmuxClient:
        def __init__(self, **_kwargs):
            pass

        def list_sessions(self, *, include_panes: bool = False):
            return {"success": True, "session_count": 0, "sessions": []}

    monkeypatch.setattr(server, "job_registry", FakeJobRegistry())
    monkeypatch.setattr(server, "activity_tracker", ActivityTracker())
    monkeypatch.setattr(tools_core, "TmuxClient", FakeTmuxClient)

    result = _call(server.execution_state, cwd="/scope")

    assert result["state"] == expected_state
    assert result["activity_verdict"] == expected_verdict
    assert result["waiting_justified"] is False



@pytest.mark.parametrize(
    ("delegate_activity", "expected_state", "expected_verdict", "waiting", "required_action"),
    [
        ("active", "ACTIVE_DELEGATE", "ACTIVE", True, "POLL_DELEGATE_STATUS"),
        (
            "starting_or_quiet",
            "QUIET_DELEGATE_REQUIRES_RECHECK",
            "QUIET",
            False,
            "RECHECK_DELEGATE_STATUS_OR_INSPECT_LOGS",
        ),
        (
            "suspected_stalled",
            "STALLED_DELEGATE_REQUIRES_INSPECTION",
            "STALLED_SUSPECTED",
            False,
            "INSPECT_DELEGATE_STATUS_LOGS_OR_CONTINUE_INDEPENDENT_WORK",
        ),
        ("queued", "DELEGATE_QUEUED_REQUIRES_STATUS", "QUEUED", False, "POLL_DELEGATE_STATUS"),
    ],
)
def test_execution_state_includes_scoped_delegate_activity(
    monkeypatch: pytest.MonkeyPatch,
    delegate_activity: str,
    expected_state: str,
    expected_verdict: str,
    waiting: bool,
    required_action: str,
) -> None:
    from chatgpt_web_oauth_mcp import server, tools_core

    class FakeJobRegistry:
        def list_jobs(self, **_kwargs):
            return {"success": True, "jobs": [], "total": 0, "truncated": False}

    class FakeRegistry:
        def delegate_status(self, **kwargs):
            assert str(kwargs["project_cwd"]) == "/scope"
            return {
                "success": True,
                "project": {
                    "status": "running",
                    "active": [
                        {
                            "delegate_id": "delegate-1",
                            "harness": "antigravity",
                            "status": "running" if delegate_activity != "queued" else "queued",
                            "cwd": "/scope",
                            "pid": 1234,
                            "activity_state": delegate_activity,
                            "stdout_bytes": 10 if delegate_activity == "active" else 0,
                            "stderr_bytes": 0,
                            "last_output_seconds_ago": 1.0,
                        }
                    ],
                },
            }

    class FakeTmuxClient:
        def __init__(self, **_kwargs):
            pass

        def list_sessions(self, *, include_panes: bool = False):
            assert include_panes is True
            return {"success": True, "session_count": 0, "sessions": []}

    monkeypatch.setattr(server, "job_registry", FakeJobRegistry())
    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(tools_core, "TmuxClient", FakeTmuxClient)

    result = _call(server.execution_state, cwd="/scope")

    assert result["success"] is True
    assert result["state"] == expected_state
    assert result["activity_verdict"] == expected_verdict
    assert result["waiting_justified"] is waiting
    assert result["required_action"] == required_action
    assert result["active_delegate_count"] == 1
    assert result["active_delegates"][0]["delegate_id"] == "delegate-1"


def test_execution_state_delegate_observation_failure_prevents_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chatgpt_web_oauth_mcp import server, tools_core

    class FakeJobRegistry:
        def list_jobs(self, **_kwargs):
            return {"success": True, "jobs": [], "total": 0, "truncated": False}

    class FakeRegistry:
        def delegate_status(self, **_kwargs):
            return {
                "success": False,
                "error": {"code": "delegate_status_unavailable", "message": "boom"},
            }

    class FakeTmuxClient:
        def __init__(self, **_kwargs):
            pass

        def list_sessions(self, *, include_panes: bool = False):
            return {"success": True, "session_count": 0, "sessions": []}

    monkeypatch.setattr(server, "job_registry", FakeJobRegistry())
    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(tools_core, "TmuxClient", FakeTmuxClient)

    result = _call(server.execution_state, cwd="/scope")

    assert result["success"] is False
    assert result["state"] == "ACTIVITY_UNKNOWN"
    assert result["waiting_justified"] is False
    assert result["observation_errors"]["delegates"]["code"] == "delegate_status_unavailable"
