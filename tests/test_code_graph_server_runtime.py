from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from chatgpt_web_oauth_mcp.code_graph.backend import JoernBackendConfig
from chatgpt_web_oauth_mcp.code_graph.server_runtime import (
    ANALYZER_LABEL,
    GRAPH_LABEL,
    ROLE_LABEL,
    ROLE_VALUE,
    JoernQueryExecutionError,
    JoernQueryServerConfig,
    JoernQueryServerRuntime,
    JoernQueryTransportError,
)


PINNED = (
    "ghcr.io/joernio/joern@"
    "sha256:71a7af77e78d4a84cab0291d2fc1a1490bb27f60fbb64e4f0f1e3191e98b6bc3"
)
GRAPH_ID = "b" * 64


def _backend(**overrides) -> JoernBackendConfig:
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


def _runtime(**overrides) -> JoernQueryServerRuntime:
    values = {
        "backend": _backend(),
        "start_timeout_seconds": 30,
        "max_containers": 1,
    }
    values.update(overrides)
    return JoernQueryServerRuntime(JoernQueryServerConfig(**values))


def test_start_argv_is_hardened_offline_without_host_port(tmp_path: Path, monkeypatch) -> None:
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"graph")
    runtime = _runtime()
    monkeypatch.setattr(runtime, "_docker_binary", lambda: "/usr/bin/docker")

    argv = runtime.start_argv(graph_id=GRAPH_ID, cpg_path=cpg)

    assert argv[:4] == ["/usr/bin/docker", "run", "-d", "--rm"]
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert argv[argv.index("--workdir") + 1] == "/tmp"
    assert "-p" not in argv
    assert "--publish" not in argv
    mounts = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--mount"]
    assert any("dst=/cpg.bin" in mount and mount.endswith(",readonly") for mount in mounts)
    labels = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "--label"]
    assert f"{ROLE_LABEL}={ROLE_VALUE}" in labels
    assert f"{GRAPH_LABEL}={GRAPH_ID}" in labels
    assert any(item.startswith(f"{ANALYZER_LABEL}=joern:4.0.640@sha256:") for item in labels)
    assert argv[-7:] == [
        "joern",
        "--server",
        "--server-host",
        "127.0.0.1",
        "--server-port",
        "8080",
        "/cpg.bin",
    ]


def test_exec_query_argv_uses_stdin_and_container_loopback(monkeypatch) -> None:
    runtime = _runtime()
    monkeypatch.setattr(runtime, "_docker_binary", lambda: "/usr/bin/docker")

    argv = runtime.exec_query_argv(graph_id=GRAPH_ID, max_time_seconds=7)

    assert argv[:4] == ["/usr/bin/docker", "exec", "-i", runtime.container_name(GRAPH_ID)]
    assert "curl" in argv
    assert "--data-binary" in argv
    assert argv[argv.index("--data-binary") + 1] == "@-"
    assert argv[-1] == "http://127.0.0.1:8080/query-sync"
    assert argv[argv.index("--max-time") + 1] == "7"


def test_container_match_requires_graph_image_and_hardening(tmp_path: Path) -> None:
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"graph")
    runtime = _runtime()
    inspected = {
        "State": {"Running": True},
        "Config": {
            "Image": PINNED,
            "WorkingDir": "/tmp",
            "Labels": {
                ROLE_LABEL: ROLE_VALUE,
                GRAPH_LABEL: GRAPH_ID,
                ANALYZER_LABEL: runtime.config.backend.analyzer_id,
            },
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
        },
        "Mounts": [
            {
                "Destination": "/cpg.bin",
                "Source": str(cpg.resolve()),
                "RW": False,
            }
        ],
    }

    assert runtime._container_matches(inspected, graph_id=GRAPH_ID, cpg_path=cpg) is True
    inspected["HostConfig"]["NetworkMode"] = "bridge"
    assert runtime._container_matches(inspected, graph_id=GRAPH_ID, cpg_path=cpg) is False


def test_decode_rest_response_distinguishes_transport_and_query_failure() -> None:
    runtime = _runtime()
    ok = subprocess.CompletedProcess(
        ["docker"],
        0,
        stdout='{"success":true,"stdout":"value"}',
        stderr="",
    )
    assert runtime._decode_rest_response(ok) == "value"

    failed_query = subprocess.CompletedProcess(
        ["docker"],
        0,
        stdout='{"success":false,"stdout":"scala compile error"}',
        stderr="",
    )
    with pytest.raises(JoernQueryExecutionError, match="scala compile error"):
        runtime._decode_rest_response(failed_query)

    failed_transport = subprocess.CompletedProcess(
        ["docker"],
        7,
        stdout="",
        stderr="connection refused",
    )
    with pytest.raises(JoernQueryTransportError, match="connection refused"):
        runtime._decode_rest_response(failed_transport)


def test_query_reuses_warm_server_without_restart(tmp_path: Path, monkeypatch) -> None:
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"graph")
    runtime = _runtime()
    ensures: list[tuple[str, Path]] = []
    posts: list[str] = []

    def ensure(*, graph_id, cpg_path):
        ensures.append((graph_id, cpg_path))
        return False

    def post(*, graph_id, query, timeout_seconds):
        posts.append(query)
        return "warm-result"

    monkeypatch.setattr(runtime, "_ensure_server", ensure)
    monkeypatch.setattr(runtime, "_post_query", post)

    result = runtime.query(graph_id=GRAPH_ID, cpg_path=cpg, query="1 + 1")

    assert result.stdout == "warm-result"
    assert result.cold_start is False
    assert ensures == [(GRAPH_ID, cpg)]
    assert posts == ["1 + 1"]


def test_transport_failure_restarts_once_but_query_failure_does_not(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"graph")
    runtime = _runtime()
    ensure_calls = 0
    post_calls = 0
    removed: list[str] = []

    def ensure(*, graph_id, cpg_path):
        nonlocal ensure_calls
        ensure_calls += 1
        return ensure_calls > 1

    def post(*, graph_id, query, timeout_seconds):
        nonlocal post_calls
        post_calls += 1
        if post_calls == 1:
            raise JoernQueryTransportError("lost transport")
        return "recovered"

    monkeypatch.setattr(runtime, "_ensure_server", ensure)
    monkeypatch.setattr(runtime, "_post_query", post)
    monkeypatch.setattr(runtime, "_remove_container", removed.append)

    result = runtime.query(graph_id=GRAPH_ID, cpg_path=cpg, query="1")

    assert result.stdout == "recovered"
    assert result.cold_start is True
    assert ensure_calls == 2
    assert post_calls == 2
    assert removed == [runtime.container_name(GRAPH_ID)]

    monkeypatch.setattr(
        runtime,
        "_post_query",
        lambda **kwargs: (_ for _ in ()).throw(JoernQueryExecutionError("bad CPGQL")),
    )
    with pytest.raises(JoernQueryExecutionError, match="bad CPGQL"):
        runtime.query(graph_id=GRAPH_ID, cpg_path=cpg, query="broken")


def test_capacity_removes_oldest_other_server(monkeypatch) -> None:
    runtime = _runtime()
    target = runtime.container_name(GRAPH_ID)
    removed: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_owned_containers",
        lambda: [
            ("2026-01-01T00:00:00Z", "opss-codegraph-server-old"),
            ("2026-02-01T00:00:00Z", target),
        ],
    )
    monkeypatch.setattr(runtime, "_remove_container", removed.append)

    runtime._enforce_capacity(target_name=target)

    assert removed == ["opss-codegraph-server-old"]
