from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.code_graph.queries import (
    CodeGraphQueryError,
    JoernStructuralQueryEngine,
    STRUCTURAL_QUERY_BLOCK,
    build_structural_query,
    extract_opss_b64,
)
from chatgpt_web_oauth_mcp.code_graph.server_runtime import QueryServerResult


GRAPH_ID = "a" * 64
CPG_SHA256 = "d" * 64


def _encoded_payload(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def test_structural_block_filters_external_and_operator_noise_by_default() -> None:
    assert '!m.isExternal && !m.name.startsWith("<operator>.")' in STRUCTURAL_QUERY_BLOCK
    assert "internal_non_operator_methods_only" in STRUCTURAL_QUERY_BLOCK
    assert 'methodFullName == "<unknownFullName>"' in STRUCTURAL_QUERY_BLOCK
    assert '"unresolved_call_sites"' in STRUCTURAL_QUERY_BLOCK
    assert '"call_resolution_complete"' in STRUCTURAL_QUERY_BLOCK


def test_build_structural_query_base64_encodes_user_strings() -> None:
    symbol = 'danger"; System.exit(1); //'
    target = "persist"
    query = build_structural_query(
        mode="path",
        symbol=symbol,
        target=target,
        max_depth=6,
        limit=20,
        include_external=False,
    )

    assert symbol not in query
    assert target not in query
    assert base64.b64encode(symbol.encode()).decode() in query
    assert base64.b64encode(target.encode()).decode() in query
    assert 'val maxDepth = 6' in query
    assert 'val limit = 20' in query
    assert 'val includeExternal = false' in query
    assert '"OPSS_B64="' in query


def test_extract_opss_b64_uses_last_marker_and_rejects_invalid_payload() -> None:
    old = _encoded_payload({"old": True})
    latest = _encoded_payload({"results": [{"name": "persist"}]})
    output = (
        "\x1b[33mval\x1b[0m res1: String = \"OPSS_B64=" + old + "\"\n"
        "diagnostics\n"
        "\x1b[33mval\x1b[0m res2: String = \"OPSS_B64=" + latest + "\"\n"
    )
    assert extract_opss_b64(output) == {"results": [{"name": "persist"}]}

    with pytest.raises(CodeGraphQueryError, match="missing an OPSS_B64"):
        extract_opss_b64("no marker here")
    with pytest.raises(CodeGraphQueryError, match="Invalid OPSS_B64"):
        extract_opss_b64("OPSS_B64=%%%%")


def test_engine_validates_bounds_before_starting_runtime(tmp_path: Path) -> None:
    class NeverRuntime:
        def query(self, **kwargs):
            raise AssertionError("runtime should not be called")

    engine = JoernStructuralQueryEngine(NeverRuntime())  # type: ignore[arg-type]
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"x")

    with pytest.raises(ValueError, match="non-empty"):
        engine.run(graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=CPG_SHA256, mode="callers", symbol="")
    with pytest.raises(ValueError, match="target"):
        engine.run(graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=CPG_SHA256, mode="path", symbol="api", target="")
    with pytest.raises(ValueError, match="max_depth"):
        engine.run(
            graph_id=GRAPH_ID,
            cpg_path=cpg,
            cpg_sha256=CPG_SHA256,
            mode="impact",
            symbol="persist",
            max_depth=21,
        )
    with pytest.raises(ValueError, match="limit"):
        engine.run(
            graph_id=GRAPH_ID,
            cpg_path=cpg,
            cpg_sha256=CPG_SHA256,
            mode="callers",
            symbol="persist",
            limit=201,
        )


def test_engine_passes_graph_identity_and_parses_machine_json(tmp_path: Path) -> None:
    class FakeRuntime:
        def __init__(self) -> None:
            self.calls = []

        def query(self, **kwargs):
            self.calls.append(kwargs)
            payload = {
                "mode": "impact",
                "matches": [{"name": "persist"}],
                "ambiguous": False,
                "total_results": 2,
                "call_resolution_evaluated": True,
                "call_resolution_scope": "incoming_same_name_unknown_full_name",
                "call_resolution_complete": False,
                "total_unresolved_call_sites": 1,
                "unresolved_call_sites": [
                    {
                        "name": "persist",
                        "code": "self.store.persist()",
                        "method_full_name": "<unknownFullName>",
                        "line": 42,
                        "caller_name": "worker",
                        "caller_full_name": "app.py:<module>.worker",
                        "caller_file": "app.py",
                    }
                ],
                "query_truncated": False,
                "results": [{"name": "persist"}, {"name": "api"}],
            }
            return QueryServerResult(
                stdout=(
                    "\x1b[33mval\x1b[0m res1: String = \"OPSS_B64="
                    + _encoded_payload(payload)
                    + "\"\n"
                ),
                duration_seconds=0.25,
                cold_start=False,
            )

    runtime = FakeRuntime()
    engine = JoernStructuralQueryEngine(runtime)  # type: ignore[arg-type]
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"x")

    result = engine.run(
        graph_id=GRAPH_ID,
        cpg_path=cpg,
        cpg_sha256=CPG_SHA256,
        mode="impact",
        symbol="persist",
        max_depth=4,
        limit=20,
    )

    assert [item["name"] for item in result["results"]] == ["persist", "api"]
    assert result["query_duration_seconds"] == 0.25
    assert result["query_runtime"] == "joern-rest-server"
    assert result["query_cold_start"] is False
    assert result["call_resolution_complete"] is False
    assert result["total_unresolved_call_sites"] == 1
    assert result["unresolved_call_sites"][0]["caller_full_name"] == "app.py:<module>.worker"
    assert runtime.calls[0]["graph_id"] == GRAPH_ID
    assert runtime.calls[0]["cpg_path"] == cpg
    assert runtime.calls[0]["cpg_sha256"] == CPG_SHA256
    assert runtime.calls[0]["allow_cold_start"] is False
    query = runtime.calls[0]["query"]
    assert "persist" not in query
    assert base64.b64encode(b"persist").decode() in query
