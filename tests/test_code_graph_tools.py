from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess

from chatgpt_web_oauth_mcp.code_graph.backend import BackendStatus
from chatgpt_web_oauth_mcp.code_graph.models import GraphEntry, GraphManifest, GraphStatus
from chatgpt_web_oauth_mcp.tool_context import LOCAL_STATE_TOOL, READ_ONLY_TOOL


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


def _ready_entry(tmp_path: Path, repository_id: str, graph_id: str) -> GraphEntry:
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"graph")
    return GraphEntry(
        graph_id=graph_id,
        repository_id=repository_id,
        status=GraphStatus.READY,
        path=tmp_path,
        manifest=GraphManifest(
            manifest_version=2,
            graph_id=graph_id,
            repository_id=repository_id,
            git_tree_sha="tree",
            analyzer_id="analyzer",
            schema_version=1,
            options={},
            payload_filename="cpg.bin",
            payload_size_bytes=cpg.stat().st_size,
            payload_sha256="d" * 64,
            created_at="2026-01-01T00:00:00+00:00",
        ),
        payload_path=cpg,
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
    for name in (
        "code_graph_callers",
        "code_graph_callees",
        "code_graph_impact",
        "code_graph_diff_impact",
        "code_graph_path",
    ):
        assert name in descriptors
        assert descriptors[name] == READ_ONLY_TOOL

    payload = _call(server.server_info)
    assert "code_graph_status" in payload["tools"]
    assert "code_graph_prepare" in payload["tools"]
    assert "code_graph_callers" in payload["tools"]
    assert "code_graph_callees" in payload["tools"]
    assert "code_graph_impact" in payload["tools"]
    assert "code_graph_diff_impact" in payload["tools"]
    assert "code_graph_path" in payload["tools"]


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
    assert result["query_runtime_ready"] is False
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
    assert "--query-timeout-seconds" in command
    assert "--query-server-start-timeout-seconds" in command
    assert "--query-server-max-containers" in command
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


def test_prepare_cache_hit_starts_warming_job_when_runtime_is_cold(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tools_code_graph.JoernDockerBackend, "status", _available_status)
    monkeypatch.setattr(
        tools_code_graph.CodeGraphCache,
        "status",
        lambda self, repository_id, graph_id, **kwargs: _ready_entry(
            tmp_path, repository_id, graph_id
        ),
    )
    monkeypatch.setattr(
        tools_code_graph.JoernQueryServerRuntime,
        "is_ready",
        lambda self, **kwargs: False,
    )
    monkeypatch.setattr(tools_code_graph, "_active_build_job", lambda ctx, name: None)
    calls: list[dict[str, object]] = []

    def fake_start_owned_job(ctx, **kwargs):
        calls.append(kwargs)
        return {"success": True, "job_id": "job_warm", "status": "running"}

    monkeypatch.setattr(tools_code_graph, "start_owned_job", fake_start_owned_job)

    result = _call(server.code_graph_prepare, cwd=str(repo), ref="HEAD")

    assert result["success"] is True
    assert result["status"] == "warming"
    assert result["cache_hit"] is True
    assert result["runtime_ready"] is False
    assert result["job_id"] == "job_warm"
    assert len(calls) == 1


def test_prepare_cache_and_runtime_ready_returns_without_job(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(tools_code_graph.JoernDockerBackend, "status", _available_status)
    monkeypatch.setattr(
        tools_code_graph.CodeGraphCache,
        "status",
        lambda self, repository_id, graph_id, **kwargs: _ready_entry(
            tmp_path, repository_id, graph_id
        ),
    )
    monkeypatch.setattr(
        tools_code_graph.JoernQueryServerRuntime,
        "is_ready",
        lambda self, **kwargs: True,
    )
    monkeypatch.setattr(
        tools_code_graph,
        "start_owned_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ready runtime must not start a job")
        ),
    )

    result = _call(server.code_graph_prepare, cwd=str(repo), ref="HEAD")

    assert result["status"] == "ready"
    assert result["cache_hit"] is True
    assert result["runtime_ready"] is True
    assert result["query_runtime"] == "persistent-rest"


def test_structural_query_maps_cold_runtime_to_prepare_action(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        tools_code_graph.CodeGraphCache,
        "status",
        lambda self, repository_id, graph_id, **kwargs: _ready_entry(
            tmp_path, repository_id, graph_id
        ),
    )
    monkeypatch.setattr(
        tools_code_graph.JoernStructuralQueryEngine,
        "run",
        lambda self, **kwargs: (_ for _ in ()).throw(
            tools_code_graph.JoernQueryRuntimeNotReady("run code_graph_prepare")
        ),
    )

    result = _call(
        server.code_graph_callers,
        symbol="answer",
        cwd=str(repo),
        ref="HEAD",
    )

    assert result["success"] is False
    assert result["error"]["code"] == "code_graph_runtime_not_ready"
    assert "code_graph_prepare" in result["next_action"]


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


def test_structural_query_respects_disabled_feature_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(server, "CODE_GRAPH_ENABLED", False)

    result = _call(server.code_graph_callers, symbol="answer", cwd=str(repo), ref="HEAD")

    assert result["success"] is False
    assert result["error"]["code"] == "code_graph_disabled"


def test_structural_query_fails_closed_when_graph_is_not_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")

    result = _call(server.code_graph_callers, symbol="answer", cwd=str(repo), ref="HEAD")

    assert result["success"] is False
    assert result["error"]["code"] == "code_graph_not_ready"
    assert result["cache_status"] == "missing"
    assert result["next_action"].startswith("Call code_graph_prepare")
    assert result["tree_sha"] == _git(repo, "rev-parse", "HEAD^{tree}")


def test_structural_query_uses_ready_payload_and_preserves_ambiguity_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"graph")
    monkeypatch.setattr(server, "STATE_DIR", state_dir)

    def ready_status(self, repository_id, graph_id, *, expected_identity=None):
        return GraphEntry(
            graph_id=graph_id,
            repository_id=repository_id,
            status=GraphStatus.READY,
            path=tmp_path,
            manifest=GraphManifest(
                manifest_version=2,
                graph_id=graph_id,
                repository_id=repository_id,
                git_tree_sha="tree",
                analyzer_id="analyzer",
                schema_version=1,
                options={},
                payload_filename="cpg.bin",
                payload_size_bytes=cpg.stat().st_size,
                payload_sha256="d" * 64,
                created_at="2026-01-01T00:00:00+00:00",
            ),
            payload_path=cpg,
        )

    calls: list[dict[str, object]] = []

    def fake_run(self, **kwargs):
        calls.append(kwargs)
        return {
            "mode": "callers",
            "symbol": "answer",
            "target": "",
            "resolution": "name",
            "target_resolution": "none",
            "matches": [
                {
                    "name": "answer",
                    "full_name": "app.py:<module>.answer",
                    "file": "app.py",
                    "line": 1,
                    "is_external": False,
                    "depth": 0,
                }
            ],
            "target_matches": [],
            "total_matches": 1,
            "total_target_matches": 0,
            "ambiguous": False,
            "target_ambiguous": False,
            "not_found": False,
            "target_not_found": False,
            "include_external": False,
            "filter_policy": "internal_methods_only",
            "max_depth": 1,
            "limit": 25,
            "total_results": 0,
            "call_resolution_evaluated": True,
            "call_resolution_scope": "incoming_same_name_unknown_full_name",
            "call_resolution_complete": False,
            "total_unresolved_call_sites": 1,
            "unresolved_call_sites": [
                {
                    "name": "answer",
                    "code": "self.target.answer()",
                    "method_full_name": "<unknownFullName>",
                    "line": 7,
                    "caller_name": "worker",
                    "caller_full_name": "app.py:<module>.worker",
                    "caller_file": "app.py",
                }
            ],
            "query_truncated": False,
            "results": [],
            "query_duration_seconds": 0.01,
        }

    monkeypatch.setattr(tools_code_graph.CodeGraphCache, "status", ready_status)
    monkeypatch.setattr(tools_code_graph.JoernStructuralQueryEngine, "run", fake_run)

    result = _call(
        server.code_graph_callers,
        symbol="answer",
        cwd=str(repo),
        ref="HEAD",
        limit=25,
    )

    assert result["success"] is True
    assert result["ambiguous"] is False
    assert result["matches"][0]["full_name"] == "app.py:<module>.answer"
    assert result["complete"] is True
    assert result["call_resolution_complete"] is False
    assert result["returned_unresolved_call_sites"] == 1
    assert result["unresolved_call_sites"][0]["caller_name"] == "worker"
    assert result["working_tree_included"] is False
    assert calls[0]["cpg_path"] == cpg
    assert calls[0]["cpg_sha256"] == "d" * 64
    assert calls[0]["limit"] == 25


def test_query_budget_trims_unresolved_call_sites() -> None:
    from chatgpt_web_oauth_mcp import tools_code_graph

    class Ctx:
        tool_output_token_budget = 500

    payload = {
        "success": True,
        "query_truncated": False,
        "results": [],
        "matches": [],
        "target_matches": [],
        "unresolved_call_sites": [
            {
                "name": "persist",
                "code": "self.service.persist(" + ("x" * 160) + ")",
                "method_full_name": "<unknownFullName>",
                "line": index,
                "caller_name": f"worker_{index}",
                "caller_full_name": f"app.py:<module>.worker_{index}",
                "caller_file": "app.py",
            }
            for index in range(20)
        ],
    }

    result = tools_code_graph._fit_query_payload(Ctx(), payload)

    assert result["partial"] is True
    assert result["stop_reason"] == "token_budget"
    assert 0 <= result["returned_unresolved_call_sites"] < 20
    assert result["returned_unresolved_call_sites"] == len(
        result["unresolved_call_sites"]
    )


def test_diff_impact_separates_exact_and_unresolved_candidate_tests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        tools_code_graph,
        "collect_python_change_set",
        lambda base, head: {
            "changed_files": ["app.py"],
            "changed_test_files": [],
            "unsupported_changed_files": [],
            "module_scope_changes": [],
            "analysis_errors": [],
            "changed_symbols": [
                {
                    "status": "modified",
                    "name": "answer",
                    "qualname": "answer",
                    "kind": "function",
                    "file": "app.py",
                    "line": 1,
                    "end_line": 2,
                    "full_name": "app.py:<module>.answer",
                    "base_full_name": "app.py:<module>.answer",
                    "head_full_name": "app.py:<module>.answer",
                    "test_symbol": False,
                }
            ],
        },
    )
    monkeypatch.setattr(
        tools_code_graph.CodeGraphCache,
        "status",
        lambda self, repository_id, graph_id, **kwargs: _ready_entry(
            tmp_path, repository_id, graph_id
        ),
    )
    monkeypatch.setattr(
        tools_code_graph.JoernStructuralQueryEngine,
        "run",
        lambda self, **kwargs: {
            "ambiguous": False,
            "not_found": False,
            "query_truncated": False,
            "call_resolution_complete": False,
            "total_results": 3,
            "results": [
                {"full_name": "app.py:<module>.answer", "file": "app.py", "depth": 0},
                {
                    "full_name": "service.py:<module>.use_answer",
                    "file": "service.py",
                    "depth": 1,
                },
                {
                    "full_name": "tests/test_answer.py:<module>.test_answer",
                    "file": "tests/test_answer.py",
                    "depth": 1,
                },
            ],
            "total_unresolved_call_sites": 1,
            "unresolved_call_sites": [
                {
                    "caller_full_name": "tests/test_candidate.py:<module>.test_candidate",
                    "caller_file": "tests/test_candidate.py",
                    "line": 10,
                }
            ],
            "query_duration_seconds": 0.1,
        },
    )

    result = _call(
        server.code_graph_diff_impact,
        cwd=str(repo),
        base_ref="HEAD",
        head_ref="HEAD",
        use_merge_base=False,
    )

    assert result["success"] is True
    assert "service.py" in result["exact_affected_files"]
    assert "tests/test_answer.py" in result["exact_affected_tests"]
    assert "tests/test_candidate.py" not in result["exact_affected_tests"]
    assert "tests/test_candidate.py" in result["candidate_affected_tests"]
    assert result["symbol_impacts"][0]["impact_status"] == "ok"
    assert result["symbol_impacts"][0]["call_resolution_complete"] is False
    assert result["symbol_impacts"][0]["unresolved_scope_aggregated"] is True
    assert result["symbol_impacts"][0]["unresolved_scope_reason"] == "specific_symbol_name"
    assert result["semantic_scope_complete"] is False
    assert result["impact_complete"] is False


def test_diff_impact_keeps_dunder_unresolved_evidence_without_global_scope_noise(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    monkeypatch.setattr(server, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        tools_code_graph,
        "collect_python_change_set",
        lambda base, head: {
            "changed_files": ["app.py"],
            "changed_test_files": [],
            "unsupported_changed_files": [],
            "module_scope_changes": [],
            "analysis_errors": [],
            "changed_symbols": [
                {
                    "status": "modified",
                    "name": "__init__",
                    "qualname": "Service.__init__",
                    "kind": "method",
                    "file": "app.py",
                    "line": 1,
                    "end_line": 3,
                    "full_name": "app.py:<module>.Service.__init__",
                    "base_full_name": "app.py:<module>.Service.__init__",
                    "head_full_name": "app.py:<module>.Service.__init__",
                    "test_symbol": False,
                }
            ],
        },
    )
    monkeypatch.setattr(
        tools_code_graph.CodeGraphCache,
        "status",
        lambda self, repository_id, graph_id, **kwargs: _ready_entry(
            tmp_path, repository_id, graph_id
        ),
    )
    monkeypatch.setattr(
        tools_code_graph.JoernStructuralQueryEngine,
        "run",
        lambda self, **kwargs: {
            "ambiguous": False,
            "not_found": False,
            "query_truncated": False,
            "call_resolution_complete": False,
            "total_results": 1,
            "results": [
                {
                    "full_name": "app.py:<module>.Service.__init__",
                    "file": "app.py",
                    "depth": 0,
                }
            ],
            "total_unresolved_call_sites": 8,
            "unresolved_call_sites": [
                {
                    "caller_full_name": f"other_{index}.py:<module>.Other.__init__",
                    "caller_file": f"other_{index}.py",
                    "line": 10 + index,
                }
                for index in range(8)
            ],
            "query_duration_seconds": 0.1,
        },
    )

    result = _call(
        server.code_graph_diff_impact,
        cwd=str(repo),
        base_ref="HEAD",
        head_ref="HEAD",
        use_merge_base=False,
    )

    assert result["success"] is True
    assert result["candidate_affected_files"] == []
    assert result["candidate_affected_tests"] == []
    impact = result["symbol_impacts"][0]
    assert impact["total_unresolved_call_sites"] == 8
    assert impact["returned_unresolved_call_sites"] == 5
    assert impact["omitted_unresolved_call_sites"] == 3
    assert len(impact["unresolved_call_sites"]) == 5
    assert impact["call_resolution_complete"] is False
    assert impact["unresolved_scope_aggregated"] is False
    assert impact["unresolved_scope_reason"] == "dunder_symbol_noise"
    assert result["semantic_scope_complete"] is False
    assert result["impact_complete"] is False


def test_diff_impact_deleted_symbol_requires_base_graph_without_head_query(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    monkeypatch.setattr(
        tools_code_graph,
        "collect_python_change_set",
        lambda base, head: {
            "changed_files": ["app.py"],
            "changed_test_files": [],
            "unsupported_changed_files": [],
            "module_scope_changes": [],
            "analysis_errors": [],
            "changed_symbols": [
                {
                    "status": "deleted",
                    "name": "old",
                    "qualname": "old",
                    "kind": "function",
                    "file": "app.py",
                    "line": 1,
                    "end_line": 2,
                    "full_name": "app.py:<module>.old",
                    "base_full_name": "app.py:<module>.old",
                    "head_full_name": None,
                    "test_symbol": False,
                }
            ],
        },
    )
    monkeypatch.setattr(
        tools_code_graph.JoernStructuralQueryEngine,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("deleted-only page must not query the head graph")
        ),
    )

    result = _call(
        server.code_graph_diff_impact,
        cwd=str(repo),
        base_ref="HEAD",
        head_ref="HEAD",
        use_merge_base=False,
    )

    assert result["success"] is True
    assert result["base_graph_required"] is True
    assert result["total_deleted_symbols"] == 1
    assert result["symbol_impacts"][0]["impact_status"] == "base_graph_required"
    assert result["impact_complete"] is False


def test_diff_impact_symbol_pagination_is_lossless_for_direct_test_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp import server
    from chatgpt_web_oauth_mcp import tools_code_graph

    repo = _repo(tmp_path)
    symbols = [
        {
            "status": "modified",
            "name": f"test_{index}",
            "qualname": f"test_{index}",
            "kind": "function",
            "file": f"tests/test_{index}.py",
            "line": 1,
            "end_line": 2,
            "full_name": f"tests/test_{index}.py:<module>.test_{index}",
            "base_full_name": f"tests/test_{index}.py:<module>.test_{index}",
            "head_full_name": f"tests/test_{index}.py:<module>.test_{index}",
            "test_symbol": True,
        }
        for index in range(3)
    ]
    monkeypatch.setattr(
        tools_code_graph,
        "collect_python_change_set",
        lambda base, head: {
            "changed_files": [item["file"] for item in symbols],
            "changed_test_files": [item["file"] for item in symbols],
            "unsupported_changed_files": [],
            "module_scope_changes": [],
            "analysis_errors": [],
            "changed_symbols": symbols,
        },
    )

    first = _call(
        server.code_graph_diff_impact,
        cwd=str(repo),
        base_ref="HEAD",
        head_ref="HEAD",
        use_merge_base=False,
        symbol_limit=2,
    )
    second = _call(
        server.code_graph_diff_impact,
        cwd=str(repo),
        base_ref="HEAD",
        head_ref="HEAD",
        use_merge_base=False,
        offset=first["next_offset"],
        symbol_limit=2,
    )

    assert [item["name"] for item in first["symbol_impacts"]] == ["test_0", "test_1"]
    assert first["next_offset"] == 2
    assert first["partial"] is True
    assert [item["name"] for item in second["symbol_impacts"]] == ["test_2"]
    assert second["next_offset"] is None
    assert second["partial"] is False


def test_diff_impact_budget_preserves_continuation_after_row_trimming() -> None:
    from chatgpt_web_oauth_mcp import tools_code_graph

    class Ctx:
        tool_output_token_budget = 700

    impacts = [
        {
            "full_name": f"app.py:<module>.changed_{index}",
            "impact_status": "ok",
            "symbol_padding": "z" * 500,
            "impact_methods": [
                {
                    "full_name": f"service.py:<module>.caller_{index}_{caller}",
                    "file": "service.py",
                    "depth": 1,
                    "padding": "x" * 180,
                }
                for caller in range(5)
            ],
            "unresolved_call_sites": [],
        }
        for index in range(6)
    ]
    payload = {
        "success": True,
        "offset": 4,
        "page_symbol_count": len(impacts),
        "next_offset": 10,
        "partial": True,
        "stop_reason": "symbol_page",
        "symbol_impacts": impacts,
        "changed_files": ["app.py"],
        "changed_test_files": [],
        "unsupported_changed_files": [],
        "module_scope_changes": [],
        "exact_affected_files": ["app.py", "service.py"],
        "exact_affected_tests": [],
        "candidate_affected_files": [],
        "candidate_affected_tests": [],
    }

    original_count = len(impacts)
    result = tools_code_graph._fit_diff_impact_payload(Ctx(), payload)

    assert result["partial"] is True
    assert result["returned_symbol_impacts"] < original_count
    assert result["next_offset"] == 4 + result["returned_symbol_impacts"]
    assert result["stop_reason"] == "token_budget"
