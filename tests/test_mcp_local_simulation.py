from __future__ import annotations

import contextlib
import shlex
import socket
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def _python_cmd(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _running_server(
    tmp_path: Path,
    monkeypatch,
    *,
    auth_token: str,
    codex_command: str | None = None,
):
    from chatgpt_web_oauth_mcp import server, session
    from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore

    session.registry.reset()
    monkeypatch.setattr(server, "AUTH_TOKEN", auth_token)
    monkeypatch.setattr(server, "WORKSPACE_ROOT", tmp_path)
    registry = ExecutorRegistry(codex_command=codex_command or _python_cmd("print('codex')"))
    monkeypatch.setattr(server, "registry", registry)
    monkeypatch.setattr(
        server,
        "checkpoint_store",
        SessionCheckpointStore(
            path=tmp_path / "session-checkpoints.json",
            ttl_seconds=86400,
        ),
    )

    app = server.build_http_app()
    port = _find_free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        lifespan="on",
    )
    uvicorn_server = uvicorn.Server(config)
    uvicorn_server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    else:
        raise AssertionError("Timed out waiting for MCP test server to start.")

    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)
        session.registry.reset()
        assert not thread.is_alive(), "uvicorn test server did not shut down cleanly"


@asynccontextmanager
async def _mcp_session(
    url: str,
    *,
    token: str,
    extra_headers: dict[str, str] | None = None,
):
    headers = {"Authorization": f"Bearer {token}", **(extra_headers or {})}
    async with httpx.AsyncClient(headers=headers, timeout=10.0) as client:
        async with streamable_http_client(url, http_client=client) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                yield session


async def _call_tool(session: ClientSession, name: str, arguments: dict[str, object]) -> dict[str, object]:
    result = await session.call_tool(name, arguments)
    assert result.isError is False, result
    assert result.structuredContent is not None
    return result.structuredContent


def test_session_checkpoint_resume_and_close_across_transports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token = "secret-token"
    project = tmp_path / "resume-project"
    project.mkdir()
    headers = {"X-OpenAI-Session": "resume-chat-a"}

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers,
            ) as first:
                saved = await _call_tool(
                    first,
                    "session_checkpoint",
                    {
                        "goal": "finish resume slice",
                        "current_slice": "acceptance",
                        "next_action": "run final gate",
                        "done_means": ["final gate passes"],
                        "job_ids": [],
                        "delegate_ids": [],
                        "cwd": str(project),
                    },
                )
                assert saved["success"] is True
                assert saved["ttl_seconds"] == 86400

            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers,
            ) as second:
                resumed = await _call_tool(second, "session_resume", {})
                assert resumed["resumable"] is True
                assert resumed["next_action"] == "run final gate"
                assert resumed["checkpoint"]["current_slice"] == "acceptance"
                cwd = await _call_tool(second, "get_default_cwd", {})
                assert cwd["session_cwd"] == str(project)

                closed = await _call_tool(second, "session_close", {})
                assert closed["closed"] is True
                assert closed["running_jobs_untouched"] is True

            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers,
            ) as third:
                resumed = await _call_tool(third, "session_resume", {})
                assert resumed["resumable"] is False
                assert resumed["checkpoint"] is None

        anyio.run(scenario)


def test_openai_logical_session_persists_cwd_across_transport_sessions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token = "secret-token"
    first = tmp_path / "logical-first"
    second = tmp_path / "logical-second"
    first.mkdir()
    second.mkdir()

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            headers_a = {"X-OpenAI-Session": "chat-session-a"}
            headers_b = {"X-OpenAI-Session": "chat-session-b"}

            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers_a,
            ) as session_a1:
                result = await _call_tool(
                    session_a1,
                    "set_default_cwd",
                    {"path": str(first)},
                )
                assert result["session_cwd"] == str(first)

            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers_a,
            ) as session_a2:
                result = await _call_tool(session_a2, "get_default_cwd", {})
                assert result["session_cwd"] == str(first)

            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers_b,
            ) as session_b:
                before = await _call_tool(session_b, "get_default_cwd", {})
                assert before["session_cwd"] is None
                set_b = await _call_tool(
                    session_b,
                    "set_default_cwd",
                    {"path": str(second)},
                )
                assert set_b["session_cwd"] == str(second)

            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers_a,
            ) as session_a3:
                result = await _call_tool(session_a3, "get_default_cwd", {})
                assert result["session_cwd"] == str(first)

        anyio.run(scenario)

        from chatgpt_web_oauth_mcp import session as session_state

        snapshot = session_state.registry.snapshot(
            idle_ttl_seconds=3600,
            request_stall_seconds=180,
            orchestration_quiet_seconds=180,
            limit=20,
        )
        assert snapshot["session_count"] == 2
        assert {item["project"] for item in snapshot["sessions"]} == {
            first.name,
            second.name,
        }


def test_mcp_sessions_keep_independent_default_cwd(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session_a:
                async with _mcp_session(url, token=token) as session_b:
                    set_a = await _call_tool(
                        session_a,
                        "set_default_cwd",
                        {"path": str(first)},
                    )
                    set_b = await _call_tool(
                        session_b,
                        "set_default_cwd",
                        {"path": str(second)},
                    )
                    assert set_a["session_cwd"] == str(first)
                    assert set_b["session_cwd"] == str(second)

                    cwd_a = await _call_tool(session_a, "get_default_cwd", {})
                    cwd_b = await _call_tool(session_b, "get_default_cwd", {})
                    assert cwd_a["session_cwd"] == str(first)
                    assert cwd_b["session_cwd"] == str(second)

                    info_a = await _call_tool(session_a, "server_info", {})
                    info_b = await _call_tool(session_b, "server_info", {})
                    assert info_a["session_cwd"] == str(first)
                    assert info_b["session_cwd"] == str(second)

        anyio.run(scenario)


def test_internal_health_observes_active_mcp_tool_request(tmp_path: Path, monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server

    token = "secret-token"
    monkeypatch.setattr(server, "HEALTH_TOKEN", "health-secret")

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:
        health_url = url.rsplit("/mcp", 1)[0] + "/internal/health"

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as mcp_session:
                completed: list[dict[str, object]] = []

                async def run_tool() -> None:
                    result = await _call_tool(
                        mcp_session,
                        "run_command",
                        {
                            "command": _python_cmd("import time; time.sleep(0.8); print('done')"),
                            "timeout": 5,
                        },
                    )
                    completed.append(result)

                async with anyio.create_task_group() as tg:
                    tg.start_soon(run_tool)
                    observed = None
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        for _ in range(30):
                            response = await client.get(
                                health_url,
                                headers={"X-Ops-Health-Token": "health-secret"},
                            )
                            assert response.status_code == 200
                            payload = response.json()
                            for item in payload.get("sessions", []):
                                if (
                                    item.get("state") == "active"
                                    and item.get("current_tool") == "run_command"
                                ):
                                    observed = item
                                    break
                            if observed is not None:
                                break
                            await anyio.sleep(0.05)
                    assert observed is not None

                assert completed[0]["success"] is True
                assert "done" in completed[0]["stdout"]

        anyio.run(scenario)


def test_mcp_run_command_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                result = await _call_tool(
                    session,
                    "run_command",
                    {
                        "command": _python_cmd("print('shell-ok')"),
                        "timeout": 5,
                    },
                )
                assert result["success"] is True
                assert "shell-ok" in result["stdout"]

        anyio.run(scenario)


def test_mcp_run_command_batch_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                result = await _call_tool(
                    session,
                    "run_command",
                    {
                        "commands": [
                            _python_cmd("print('batch-one')"),
                            _python_cmd("print('batch-two')"),
                        ],
                        "mode": "parallel",
                        "max_concurrency": 2,
                        "timeout": 5,
                    },
                )
                assert result["success"] is True
                assert result["mode"] == "batch"
                assert result["execution_mode"] == "parallel"
                assert [item["stdout"].strip() for item in result["results"]] == [
                    "batch-one",
                    "batch-two",
                ]

        anyio.run(scenario)


def test_mcp_delegate_tools_are_exposed_but_removed_taskboard_tools_are_not(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "run_command" in names
                assert "get_skill_index" in names
                assert "get_delegate_use" in names
                assert "delegate_task" in names
                assert "delegate_batch" in names
                assert "delegate_status" in names
                assert "delegate_cancel" in names
                assert "delegate_harnesses" in names
                assert "get_file_use" in names
                assert "get_process_use" in names
                assert "get_runtime_use" in names
                assert "get_git_use" in names
                for removed in {
                    "run_command_stream",
                    "wait_task",
                    "get_task",
                    "cancel_task",
                    "purge_tasks",
                    "taskboard_create",
                    "list_skills",
                }:
                    assert removed not in names
                assert not {name for name in names if name.startswith("obsidian_")}

        anyio.run(scenario)


def test_mcp_skill_tools_and_resources_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                index = await _call_tool(session, "get_skill_index", {})
                assert index["success"] is True
                assert [skill["name"] for skill in index["skills"]] == [
                    "file-use",
                    "process-use",
                    "delegate-use",
                    "runtime-use",
                    "git-use",
                ]

                for tool_name, heading in [
                    ("get_delegate_use", "# Delegate Use"),
                    ("get_file_use", "# File Use"),
                    ("get_process_use", "# Process Use"),
                    ("get_runtime_use", "# Runtime Use"),
                    ("get_git_use", "# Git Use"),
                ]:
                    guide = await _call_tool(session, tool_name, {})
                    assert guide["success"] is True
                    assert heading in guide["content"]

                resources = await session.list_resources()
                resource_uris = {str(resource.uri) for resource in resources.resources}
                assert {
                    "skill://chatgpt-web-oauth-mcp/index",
                    "skill://chatgpt-web-oauth-mcp/delegate-use",
                    "skill://chatgpt-web-oauth-mcp/file-use",
                    "skill://chatgpt-web-oauth-mcp/process-use",
                    "skill://chatgpt-web-oauth-mcp/runtime-use",
                    "skill://chatgpt-web-oauth-mcp/git-use",
                } <= resource_uris

                for uri, heading in [
                    ("skill://chatgpt-web-oauth-mcp/delegate-use", "# Delegate Use"),
                    ("skill://chatgpt-web-oauth-mcp/file-use", "# File Use"),
                    ("skill://chatgpt-web-oauth-mcp/process-use", "# Process Use"),
                    ("skill://chatgpt-web-oauth-mcp/runtime-use", "# Runtime Use"),
                    ("skill://chatgpt-web-oauth-mcp/git-use", "# Git Use"),
                ]:
                    guide_resource = await session.read_resource(uri)
                    assert heading in guide_resource.contents[0].text

        anyio.run(scenario)

def test_mcp_canonical_search_and_read_text_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    (tmp_path / "demo.py").write_text("alpha\nTODO item\n", encoding="utf-8")
    (tmp_path / ".hidden.py").write_text("TODO hidden\n", encoding="utf-8")

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                found = await _call_tool(
                    session,
                    "search",
                    {
                        "mode": "glob",
                        "path": ".",
                        "pattern": "*.py",
                    },
                )
                read = await _call_tool(
                    session,
                    "read_text",
                    {
                        "path": "demo.py",
                        "start_line": 2,
                        "line_limit": 1,
                        "include_line_numbers": True,
                    },
                )
                single_file_search = await _call_tool(
                    session,
                    "search",
                    {
                        "mode": "text",
                        "path": "demo.py",
                        "query": "TODO",
                    },
                )
                batch_search = await _call_tool(
                    session,
                    "search",
                    {
                        "mode": "parallel",
                        "path": ".",
                        "queries": [
                            {"mode": "glob", "pattern": "*.py"},
                            {"mode": "text", "path": "demo.py", "query": "alpha"},
                        ],
                        "max_concurrency": 2,
                    },
                )
                assert found["success"] is True
                assert found["mode"] == "glob"
                assert any(item["path"].endswith("demo.py") for item in found["matches"])
                assert all(not item["path"].endswith(".hidden.py") for item in found["matches"])
                assert read["success"] is True
                assert read["mode"] == "single"
                assert read["content"] == "2: TODO item"
                assert single_file_search["success"] is True
                assert single_file_search["mode"] == "text"
                assert len(single_file_search["matches"]) == 1
                assert single_file_search["matches"][0]["path"].endswith("demo.py")
                assert batch_search["success"] is True
                assert batch_search["mode"] == "batch"
                assert batch_search["execution_mode"] == "parallel"
                assert batch_search["max_concurrency"] == 2
                assert batch_search["results"][0]["mode"] == "glob"
                assert batch_search["results"][1]["mode"] == "text"

        anyio.run(scenario)
