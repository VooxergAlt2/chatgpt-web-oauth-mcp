from __future__ import annotations

import contextlib
import json
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


def test_execution_state_creates_automatic_resume_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token = "secret-token"
    project = tmp_path / "execution-resume-project"
    project.mkdir()
    headers = {"X-OpenAI-Session": "execution-resume-chat"}

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers,
            ) as first:
                state = await _call_tool(
                    first,
                    "execution_state",
                    {"cwd": str(project)},
                )
                assert state["state"] == "NEXT_ACTION_REQUIRED"
                assert state["required_action"] == "INVOKE_NEXT_TOOL_OR_RETURN_CHECKPOINT"

            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers,
            ) as second:
                resumed = await _call_tool(second, "session_resume", {})
                assert resumed["resumable"] is True
                assert resumed["resume_state"] == "runtime_checkpoint"
                assert resumed["runtime"]["last_tool"] == "execution_state"
                assert resumed["runtime"]["cwd"] == str(project)
                assert resumed["next_action"] == "INVOKE_NEXT_TOOL_OR_RETURN_CHECKPOINT"
                assert resumed["jobs"] == []
                assert resumed["delegates"] == []

                closed = await _call_tool(second, "session_close", {})
                assert closed["closed"] is True

        anyio.run(scenario)


def test_job_start_creates_automatic_resume_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token = "secret-token"
    project = tmp_path / "auto-resume-project"
    project.mkdir()
    headers = {"X-OpenAI-Session": "auto-resume-chat"}

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers,
            ) as first:
                started = await _call_tool(
                    first,
                    "job_start",
                    {
                        "command": _python_cmd(
                            "import time; time.sleep(0.3); print('AUTO_RESUME_OK')"
                        ),
                        "cwd": str(project),
                        "name": "auto-resume-test",
                    },
                )
                job_id = str(started["job_id"])
                assert started["status"] == "running"

            await anyio.sleep(0.7)

            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers,
            ) as second:
                resumed = await _call_tool(second, "session_resume", {})
                assert resumed["resumable"] is True
                assert resumed["resume_state"] == "terminal_results_ready"
                assert resumed["runtime"]["last_tool"] == "job_start"
                assert job_id in resumed["runtime"]["jobs"]
                assert "terminal" in resumed["next_action"].lower()
                matching = [item for item in resumed["jobs"] if item.get("job_id") == job_id]
                assert len(matching) == 1
                assert matching[0]["status"] == "succeeded"
                assert matching[0]["exit_code"] == 0

                closed = await _call_tool(second, "session_close", {})
                assert closed["closed"] is True
                assert closed["continuation_abandoned"] is True
                assert closed["pending_result_count"] == 1
                assert closed["owned_in_progress_count"] == 0

        anyio.run(scenario)



def test_terminal_job_surfaces_in_result_inbox_on_next_tool_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token = "secret-token"
    project = tmp_path / "result-inbox-project"
    project.mkdir()
    headers = {"X-OpenAI-Session": "result-inbox-chat"}

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers,
            ) as first:
                started = await _call_tool(
                    first,
                    "job_start",
                    {
                        "command": _python_cmd(
                            "import time; time.sleep(0.2); print('INBOX_OK')"
                        ),
                        "cwd": str(project),
                        "name": "result-inbox-test",
                    },
                )
                job_id = str(started["job_id"])

            await anyio.sleep(0.6)

            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers,
            ) as second:
                unrelated = await second.call_tool("get_default_cwd", {})
                assert unrelated.isError is False
                assert unrelated.meta is not None
                continuation = unrelated.meta["session_continuation"]
                assert continuation["state"] == "RESULT_UNCONSUMED"
                assert continuation["required_action"] == "RESULT_REQUIRES_CONSUMPTION"
                assert continuation["pending_count"] == 1
                assert continuation["results"][0]["kind"] == "job"
                assert continuation["results"][0]["id"] == job_id
                assert continuation["results"][0]["status"] == "succeeded"

                pending = await _call_tool(second, "pending_results", {})
                matches = [
                    item
                    for item in pending["pending_results"]
                    if item.get("kind") == "job" and item.get("id") == job_id
                ]
                assert len(matches) == 1
                assert matches[0]["continuation_state"] == "RESULT_REQUIRES_CONSUMPTION"
                assert matches[0]["exit_code"] == 0

                consumed = await _call_tool(
                    second,
                    "mark_result_consumed",
                    {"kind": "job", "result_id": job_id},
                )
                assert consumed["success"] is True
                assert consumed["already_consumed"] is False
                assert consumed["pending_results"] == []

                after = await second.call_tool("get_default_cwd", {})
                assert after.isError is False
                assert not after.meta or "session_continuation" not in after.meta

                repeated = await _call_tool(
                    second,
                    "mark_result_consumed",
                    {"kind": "job", "result_id": job_id},
                )
                assert repeated["success"] is True
                assert repeated["already_consumed"] is True

                closed = await _call_tool(second, "session_close", {})
                assert closed["closed"] is True

        anyio.run(scenario)


def test_await_job_rejects_result_owned_by_other_logical_session(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token = "secret-token"
    project = tmp_path / "ownership-project"
    project.mkdir()
    owner_headers = {"X-OpenAI-Session": "owner-chat"}
    other_headers = {"X-OpenAI-Session": "other-chat"}

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(
                url,
                token=token,
                extra_headers=owner_headers,
            ) as owner:
                started = await _call_tool(
                    owner,
                    "job_start",
                    {
                        "command": _python_cmd("import time; time.sleep(2)"),
                        "cwd": str(project),
                        "name": "owned-job",
                    },
                )
                job_id = str(started["job_id"])

            async with _mcp_session(
                url,
                token=token,
                extra_headers=other_headers,
            ) as other:
                await _call_tool(
                    other,
                    "session_checkpoint",
                    {
                        "goal": "other session",
                        "current_slice": "ownership",
                        "next_action": "do not inherit foreign work",
                        "done_means": [],
                        "job_ids": [job_id],
                        "delegate_ids": [],
                        "cwd": str(project),
                    },
                )
                rejected = await _call_tool(
                    other,
                    "await_job",
                    {"job_id": job_id, "wait_seconds": 0},
                )
                assert rejected["success"] is False
                assert rejected["error"]["code"] == "result_not_owned"

            async with _mcp_session(
                url,
                token=token,
                extra_headers=owner_headers,
            ) as owner_again:
                killed = await _call_tool(
                    owner_again,
                    "job_kill",
                    {"job_id": job_id, "signal": "TERM"},
                )
                assert killed["success"] is True
                consumed = await _call_tool(
                    owner_again,
                    "mark_result_consumed",
                    {"kind": "job", "result_id": job_id},
                )
                assert consumed["success"] is True
                await _call_tool(owner_again, "session_close", {})

            async with _mcp_session(
                url,
                token=token,
                extra_headers=other_headers,
            ) as other_again:
                await _call_tool(other_again, "session_close", {})

        anyio.run(scenario)



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
                assert resumed["resume_state"] == "semantic_checkpoint"
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
    project = tmp_path / "health-project"
    project.mkdir()
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
                            "cwd": str(project),
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
                    assert observed["project"] == project.name
                    assert observed["scope"] == "transport"
                    assert observed["sticky"] is False

                assert completed[0]["success"] is True
                assert "done" in completed[0]["stdout"]
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(
                        health_url,
                        headers={"X-Ops-Health-Token": "health-secret"},
                    )
                after = response.json()
                assert after["summary"]["ephemeral_idle_sessions"] >= 1
                assert not any(
                    item.get("project") == project.name
                    for item in after.get("sessions", [])
                )

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
                assert "get_guide" in names
                assert "get_code_graph_use" in names
                assert "delegate_task" in names
                assert "delegate_batch" in names
                assert "delegate_status" in names
                assert "delegate_cancel" in names
                assert "delegate_harnesses" in names
                for removed in {
                    "run_command_stream",
                    "wait_task",
                    "get_task",
                    "cancel_task",
                    "purge_tasks",
                    "taskboard_create",
                    "list_skills",
                    "get_skill_index",
                    "get_delegate_use",
                    "get_file_use",
                    "get_process_use",
                    "get_runtime_use",
                    "get_git_use",
                }:
                    assert removed not in names
                assert not {name for name in names if name.startswith("obsidian_")}

        anyio.run(scenario)


def test_mcp_skill_tools_and_resources_end_to_end(tmp_path: Path, monkeypatch) -> None:
    token = "secret-token"
    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(url, token=token) as session:
                for guide_name, heading in [
                    ("delegate-use", "# Delegate Use"),
                    ("file-use", "# File Use"),
                    ("code-graph-use", "# Code Graph Use"),
                    ("process-use", "# Process Use"),
                    ("runtime-use", "# Runtime Use"),
                    ("git-use", "# Git Use"),
                ]:
                    guide = await _call_tool(session, "get_guide", {"name": guide_name})
                    assert guide["success"] is True
                    assert heading in guide["content"]
                compatibility = await _call_tool(session, "get_code_graph_use", {})
                assert compatibility["success"] is True
                assert "# Code Graph Use" in compatibility["content"]

                resources = await session.list_resources()
                resource_uris = {str(resource.uri) for resource in resources.resources}
                assert {
                    "skill://chatgpt-web-oauth-mcp/index",
                    "skill://chatgpt-web-oauth-mcp/code-graph-use",
                    "skill://chatgpt-web-oauth-mcp/delegate-use",
                    "skill://chatgpt-web-oauth-mcp/file-use",
                    "skill://chatgpt-web-oauth-mcp/process-use",
                    "skill://chatgpt-web-oauth-mcp/runtime-use",
                    "skill://chatgpt-web-oauth-mcp/git-use",
                } <= resource_uris

                for uri, heading in [
                    ("skill://chatgpt-web-oauth-mcp/code-graph-use", "# Code Graph Use"),
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


def test_mcp_tool_usage_telemetry_records_names_not_payloads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server

    token = "secret-token"
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    headers = {"X-OpenAI-Session": "tool-usage-telemetry-chat"}
    (tmp_path / "demo.txt").write_text("needle\n", encoding="utf-8")

    with _running_server(tmp_path, monkeypatch, auth_token=token) as url:

        async def scenario() -> None:
            async with _mcp_session(
                url,
                token=token,
                extra_headers=headers,
            ) as client:
                cwd = await _call_tool(client, "get_default_cwd", {})
                assert cwd["success"] is True
                found = await _call_tool(
                    client,
                    "search",
                    {
                        "mode": "text",
                        "path": "demo.txt",
                        "query": "needle",
                    },
                )
                assert found["success"] is True

        anyio.run(scenario)

    telemetry_path = tmp_path / "tool-usage.json"
    raw = telemetry_path.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert payload["tools"]["get_default_cwd"]["calls"] == 1
    assert payload["tools"]["search"]["calls"] == 1
    assert payload["transitions"]["get_default_cwd->search"] == 1
    assert "needle" not in raw
    assert "demo.txt" not in raw
    assert "tool-usage-telemetry-chat" not in raw
