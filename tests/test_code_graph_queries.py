from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from chatgpt_web_oauth_mcp.code_graph.backend import (
    BackendStatus,
    CodeGraphBackendError,
    JoernBackendConfig,
    JoernDockerBackend,
    QueryProcessResult,
)
from chatgpt_web_oauth_mcp.code_graph.queries import (
    CodeGraphQueryError,
    JoernStructuralQueryEngine,
    STRUCTURAL_QUERY_SCRIPT,
    extract_opss_json,
)


PINNED = (
    "ghcr.io/joernio/joern@"
    "sha256:71a7af77e78d4a84cab0291d2fc1a1490bb27f60fbb64e4f0f1e3191e98b6bc3"
)


def test_structural_script_filters_external_and_operator_noise_by_default() -> None:
    assert '!m.isExternal && !m.name.startsWith("<operator>.")' in STRUCTURAL_QUERY_SCRIPT
    assert "internal_non_operator_methods_only" in STRUCTURAL_QUERY_SCRIPT


def _config(**overrides) -> JoernBackendConfig:
    values = {
        "enabled": True,
        "docker_binary": "docker",
        "image": PINNED,
        "version": "4.0.640",
        "memory_mb": 8192,
        "cpus": 4,
        "pids_limit": 512,
        "tmpfs_mb": 2048,
        "build_timeout_seconds": 1800,
        "query_timeout_seconds": 20,
    }
    values.update(overrides)
    return JoernBackendConfig(**values)


def test_query_argv_is_hardened_offline_and_uses_writable_tmp_workdir(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cpg = tmp_path / "cpg.bin"
    script = tmp_path / "query.sc"
    cpg.write_bytes(b"graph")
    script.write_text("@main def exec() = {}", encoding="utf-8")
    backend = JoernDockerBackend(_config())
    monkeypatch.setattr(backend, "_docker_binary", lambda: "/usr/bin/docker")

    argv = backend.query_argv(
        cpg_path=cpg,
        script_path=script,
        params=[("mode", "callers"), ("symbol", "persist")],
        container_name="opss-query-test",
    )

    assert argv[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--workdir") + 1] == "/tmp"
    mounts = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--mount"]
    assert any("dst=/cpg.bin" in mount and mount.endswith(",readonly") for mount in mounts)
    assert any("dst=/query.sc" in mount and mount.endswith(",readonly") for mount in mounts)
    assert ["--param", "mode=callers"] == argv[-4:-2]
    assert ["--param", "symbol=persist"] == argv[-2:]
    assert "pull" not in argv


def test_extract_opss_json_uses_last_marker_and_rejects_missing_or_invalid() -> None:
    output = '\n'.join(
        [
            "Joern startup",
            'OPSS_JSON={"old":true}',
            "more diagnostics",
            'OPSS_JSON={"results":[{"name":"persist"}]}',
            "closing project",
        ]
    )
    assert extract_opss_json(output) == {"results": [{"name": "persist"}]}

    with pytest.raises(CodeGraphQueryError, match="without an OPSS_JSON"):
        extract_opss_json("no marker here")
    with pytest.raises(CodeGraphQueryError, match="Invalid OPSS_JSON"):
        extract_opss_json("OPSS_JSON={not-json}")


def test_engine_validates_bounds_before_starting_backend(tmp_path: Path) -> None:
    class NeverBackend:
        def query(self, **kwargs):
            raise AssertionError("backend should not be called")

    engine = JoernStructuralQueryEngine(NeverBackend())  # type: ignore[arg-type]
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"x")

    with pytest.raises(ValueError, match="non-empty"):
        engine.run(cpg_path=cpg, mode="callers", symbol="")
    with pytest.raises(ValueError, match="target"):
        engine.run(cpg_path=cpg, mode="path", symbol="api", target="")
    with pytest.raises(ValueError, match="max_depth"):
        engine.run(cpg_path=cpg, mode="impact", symbol="persist", max_depth=21)
    with pytest.raises(ValueError, match="limit"):
        engine.run(cpg_path=cpg, mode="callers", symbol="persist", limit=201)


def test_engine_passes_structured_params_and_parses_machine_json(tmp_path: Path) -> None:
    class FakeBackend:
        def __init__(self) -> None:
            self.calls = []

        def query(self, **kwargs):
            self.calls.append(kwargs)
            return QueryProcessResult(
                duration_seconds=0.25,
                stdout_tail=(
                    "noise\n"
                    'OPSS_JSON={"mode":"impact","matches":[{"name":"persist"}],'
                    '"ambiguous":false,"total_results":2,"query_truncated":false,'
                    '"results":[{"name":"persist"},{"name":"api"}]}\n'
                ),
                stderr_tail="",
            )

    backend = FakeBackend()
    engine = JoernStructuralQueryEngine(backend)  # type: ignore[arg-type]
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"x")

    result = engine.run(
        cpg_path=cpg,
        mode="impact",
        symbol="persist",
        max_depth=4,
        limit=20,
    )

    assert [item["name"] for item in result["results"]] == ["persist", "api"]
    assert result["query_duration_seconds"] == 0.25
    params = dict(backend.calls[0]["params"])
    assert params["mode"] == "impact"
    assert params["symbol"] == "persist"
    assert params["maxDepth"] == "4"
    assert params["includeExternal"] == "false"


def test_query_timeout_force_removes_container_and_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp.code_graph import backend as backend_module

    cpg = tmp_path / "cpg.bin"
    script = tmp_path / "query.sc"
    cpg.write_bytes(b"graph")
    script.write_text("@main def exec() = {}", encoding="utf-8")
    backend = JoernDockerBackend(_config(query_timeout_seconds=1))
    monkeypatch.setattr(
        backend,
        "status",
        lambda: BackendStatus(
            enabled=True,
            available=True,
            analyzer_id=backend.config.analyzer_id,
            image=backend.config.image,
            version=backend.config.version,
        ),
    )
    monkeypatch.setattr(backend, "_docker_binary", lambda: "/usr/bin/docker")
    removed: list[str] = []
    monkeypatch.setattr(backend, "_force_remove_container", removed.append)
    monkeypatch.setattr(backend_module.os, "killpg", lambda *args, **kwargs: None)

    class FakeProcess:
        pid = 999999

        def wait(self, timeout):
            raise subprocess.TimeoutExpired("docker", timeout)

        def terminate(self):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(backend_module.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    with pytest.raises(CodeGraphBackendError, match="timed out after 1s"):
        backend.query(cpg_path=cpg, script_path=script, params=[])
    assert len(removed) == 1
    assert removed[0].startswith("opss-codegraph-query-")


def test_query_nonzero_exit_surfaces_bounded_diagnostics(tmp_path: Path, monkeypatch) -> None:
    from chatgpt_web_oauth_mcp.code_graph import backend as backend_module

    cpg = tmp_path / "cpg.bin"
    script = tmp_path / "query.sc"
    cpg.write_bytes(b"graph")
    script.write_text("@main def exec() = {}", encoding="utf-8")
    backend = JoernDockerBackend(_config())
    monkeypatch.setattr(
        backend,
        "status",
        lambda: BackendStatus(
            enabled=True,
            available=True,
            analyzer_id=backend.config.analyzer_id,
            image=backend.config.image,
            version=backend.config.version,
        ),
    )
    monkeypatch.setattr(backend, "_docker_binary", lambda: "/usr/bin/docker")

    class FakeProcess:
        pid = 999998

        def __init__(self, **kwargs) -> None:
            kwargs["stderr"].write(b"synthetic query failure")
            kwargs["stderr"].flush()

        def wait(self, timeout):
            return 7

    def fake_popen(*args, **kwargs):
        return FakeProcess(**kwargs)

    monkeypatch.setattr(backend_module.subprocess, "Popen", fake_popen)

    with pytest.raises(CodeGraphBackendError, match="synthetic query failure"):
        backend.query(cpg_path=cpg, script_path=script, params=[])
