from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess

from chatgpt_web_oauth_mcp.code_graph.backend import BackendStatus
from chatgpt_web_oauth_mcp.tool_context import LOCAL_STATE_TOOL


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-qm", "initial")
    return repo


def _available_status(self) -> BackendStatus:
    return BackendStatus(
        enabled=True,
        available=True,
        analyzer_id=self.config.analyzer_id,
        image=self.config.image,
        version=self.config.version,
    )


def test_code_graph_tools_registered_with_local_state_annotations() -> None:
    from chatgpt_web_oauth_mcp import server

    async def scenario():
        list_tools = getattr(server.mcp, "_list_tools")
        try:
            tools = await list_tools()
        except TypeError:
            tools = await list_tools(None)
        return {
            tool.name: tool.annotations.model_dump(exclude_none=True)
            for tool in tools
        }

    descriptors = asyncio.run(scenario())
    for name in ("code_graph_status", "code_graph_prepare"):
        assert name in descriptors
        assert descriptors[name] == LOCAL_STATE_TOOL

    payload = _call(server.server_info)
    assert "code_graph_status" in payload["tools"]
    assert "code_graph_prepare" in payload["tools"]


def test_status_uses_committed_tree_and_safe_metadata(tmp_path: Path, monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tools_code_graph.JoernDockerBackend, "status", _available_status)
    (repo / "app.py").write_text("DIRTY = True\n", encoding="utf-8")

    result = _call(server.code_graph_status, cwd=str(repo), ref="HEAD")

    assert result["success"] is True
    assert result["identity_kind"] == "committed_git_tree"
    assert result["working_tree_included"] is False
    assert result["tree_sha"] == _git(repo, "rev-parse", "HEAD^{tree}")
    assert result["cache_status"] == "missing"
    assert result["backend"]["available"] is True
    flattened = repr(result)
    assert str(tmp_path / "state") not in flattened
    assert str(repo.resolve()) not in flattened


def test_prepare_starts_one_owned_job_and_returns_safe_summary(tmp_path: Path, monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tools_code_graph.JoernDockerBackend, "status", _available_status)
    monkeypatch.setattr(tools_code_graph, "_active_build_job", lambda ctx, name: None)
    calls: list[dict[str, object]] = []

    def fake_start_owned_job(ctx, **kwargs):
        calls.append(kwargs)
        return {"success": True, "job_id": "job_graph_test", "status": "running"}

    monkeypatch.setattr(tools_code_graph, "start_owned_job", fake_start_owned_job)

    result = _call(server.code_graph_prepare, cwd=str(repo), ref="HEAD")

    assert result["success"] is True
    assert result["status"] == "building"
    assert result["job_id"] == "job_graph_test"
    assert len(calls) == 1
    assert calls[0]["tool_name"] == "code_graph_prepare"
    assert str(calls[0]["name"]).startswith("code-graph-build:")
    command = str(calls[0]["command"])
    assert "chatgpt_web_oauth_mcp.code_graph.worker" in command
    assert "--tree-sha" in command
    assert result["tree_sha"] in command
    assert "command" not in result
    assert "stdout_log" not in result


def test_prepare_reuses_active_durable_build_without_duplicate_start(tmp_path: Path, monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tools_code_graph.JoernDockerBackend, "status", _available_status)
    monkeypatch.setattr(
        tools_code_graph,
        "_active_build_job",
        lambda ctx, name: {"job_id": "job_existing", "name": name, "status": "running"},
    )

    def fail_start(*args, **kwargs):
        raise AssertionError("duplicate durable job must not be started")

    monkeypatch.setattr(tools_code_graph, "start_owned_job", fail_start)
    result = _call(server.code_graph_prepare, cwd=str(repo), ref="HEAD")

    assert result["success"] is True
    assert result["status"] == "building"
    assert result["job_id"] == "job_existing"


def test_prepare_fails_closed_when_pinned_backend_unavailable(tmp_path: Path, monkeypatch) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")

    def unavailable(self):
        return BackendStatus(
            enabled=True,
            available=False,
            analyzer_id=self.config.analyzer_id,
            image=self.config.image,
            version=self.config.version,
            error_code="joern_image_unavailable",
            error_message="image missing",
        )

    monkeypatch.setattr(tools_code_graph.JoernDockerBackend, "status", unavailable)
    result = _call(server.code_graph_prepare, cwd=str(repo), ref="HEAD")

    assert result["success"] is False
    assert result["error"]["code"] == "joern_image_unavailable"
