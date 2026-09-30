from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from chatgpt_web_oauth_mcp.code_graph import server_runtime as server_runtime_module
from chatgpt_web_oauth_mcp.code_graph.backend import JoernBackendConfig
from chatgpt_web_oauth_mcp.code_graph.server_runtime import (
    ANALYZER_LABEL,
    CPG_FINGERPRINT_LABEL,
    GRAPH_LABEL,
    ROLE_LABEL,
    ROLE_VALUE,
    RUNTIME_SPEC_LABEL,
    JoernQueryExecutionError,
    JoernQueryServerError,
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


def _cpg_sha256(cpg: Path) -> str:
    return hashlib.sha256(cpg.read_bytes()).hexdigest()


def _matching_inspect(runtime: JoernQueryServerRuntime, cpg: Path) -> dict:
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    backend = runtime.config.backend
    return {
        "State": {"Running": True},
        "Config": {
            "Image": PINNED,
            "WorkingDir": "/tmp",
            "User": f"{uid}:{gid}",
            "Cmd": [
                "joern",
                "--server",
                "--server-host",
                "127.0.0.1",
                "--server-port",
                "8080",
                "/cpg.bin",
            ],
            "Entrypoint": None,
            "Env": ["HOME=/tmp/joern-home"],
            "Labels": {
                ROLE_LABEL: ROLE_VALUE,
                GRAPH_LABEL: GRAPH_ID,
                ANALYZER_LABEL: backend.analyzer_id,
                RUNTIME_SPEC_LABEL: runtime._runtime_spec_id(),
                CPG_FINGERPRINT_LABEL: _cpg_sha256(cpg),
            },
        },
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Memory": backend.memory_mb * 1024 * 1024,
            "NanoCpus": backend.cpus * 1_000_000_000,
            "PidsLimit": backend.pids_limit,
            "Tmpfs": {"/tmp": f"rw,exec,size={backend.tmpfs_mb}m"},
            "Binds": None,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "PortBindings": {},
        },
        "Mounts": [
            {
                "Destination": "/cpg.bin",
                "Source": str(cpg.resolve()),
                "RW": False,
                "Type": "bind",
            }
        ],
    }


def test_start_argv_is_hardened_offline_without_host_port(tmp_path: Path, monkeypatch) -> None:
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"graph")
    runtime = _runtime()
    monkeypatch.setattr(runtime, "_docker_binary", lambda: "/usr/bin/docker")

    argv = runtime.start_argv(graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=_cpg_sha256(cpg))

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
    assert f"{RUNTIME_SPEC_LABEL}={runtime._runtime_spec_id()}" in labels
    assert f"{CPG_FINGERPRINT_LABEL}={_cpg_sha256(cpg)}" in labels
    assert argv[-7:] == [
        "joern",
        "--server",
        "--server-host",
        "127.0.0.1",
        "--server-port",
        "8080",
        "/cpg.bin",
    ]

    with pytest.raises(JoernQueryTransportError, match="changed before server start"):
        runtime.start_argv(
            graph_id=GRAPH_ID,
            cpg_path=cpg,
            cpg_sha256="f" * 64,
        )


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
    inspected = _matching_inspect(runtime, cpg)

    assert runtime._container_matches(inspected, graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=_cpg_sha256(cpg)) is True
    inspected["HostConfig"]["NetworkMode"] = "bridge"
    assert runtime._container_matches(inspected, graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=_cpg_sha256(cpg)) is False


def test_warm_container_match_uses_validated_digest_without_rehash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"graph")
    runtime = _runtime()
    cpg_sha256 = _cpg_sha256(cpg)
    inspected = _matching_inspect(runtime, cpg)

    monkeypatch.setattr(
        server_runtime_module,
        "sha256_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("warm reuse must not rehash the CPG")
        ),
    )

    assert runtime._container_matches(
        inspected,
        graph_id=GRAPH_ID,
        cpg_path=cpg,
        cpg_sha256=cpg_sha256,
    )


def test_container_reuse_requires_exact_runtime_spec_and_cpg_fingerprint(tmp_path: Path) -> None:
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"graph-v1")
    runtime = _runtime()
    inspected = _matching_inspect(runtime, cpg)
    assert runtime._container_matches(inspected, graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=_cpg_sha256(cpg))

    different_resources = _runtime(backend=_backend(memory_mb=4096))
    assert not different_resources._container_matches(
        inspected,
        graph_id=GRAPH_ID,
        cpg_path=cpg,
        cpg_sha256=_cpg_sha256(cpg),
    )

    assert not runtime._container_matches(
        inspected,
        graph_id=GRAPH_ID,
        cpg_path=cpg,
        cpg_sha256="f" * 64,
    )

    inspected = _matching_inspect(runtime, cpg)
    inspected["HostConfig"]["Memory"] = 4096 * 1024 * 1024
    assert not runtime._container_matches(inspected, graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=_cpg_sha256(cpg))


def test_query_fails_closed_when_code_graph_disabled(tmp_path: Path, monkeypatch) -> None:
    cpg = tmp_path / "cpg.bin"
    cpg.write_bytes(b"graph")
    runtime = _runtime(backend=_backend(enabled=False))
    monkeypatch.setattr(
        runtime,
        "_ensure_server",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not start server")),
    )

    with pytest.raises(JoernQueryServerError, match="disabled"):
        runtime.query(graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=_cpg_sha256(cpg), query="1")


def test_remove_container_surfaces_failure_and_tolerates_missing(monkeypatch) -> None:
    runtime = _runtime()
    monkeypatch.setattr(runtime, "_docker_binary", lambda: "/usr/bin/docker")
    monkeypatch.setattr(
        runtime,
        "_capture",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr="permission denied",
        ),
    )
    with pytest.raises(JoernQueryTransportError, match="permission denied"):
        runtime._remove_container("opss-test")

    monkeypatch.setattr(
        runtime,
        "_capture",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            1,
            stdout="",
            stderr="Error: No such container: opss-test",
        ),
    )
    runtime._remove_container("opss-test")


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

    def ensure(*, graph_id, cpg_path, cpg_sha256):
        ensures.append((graph_id, cpg_path, cpg_sha256))
        return False

    def post(*, graph_id, query, timeout_seconds):
        posts.append(query)
        return "warm-result"

    monkeypatch.setattr(runtime, "_ensure_server", ensure)
    monkeypatch.setattr(runtime, "_post_query", post)

    result = runtime.query(graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=_cpg_sha256(cpg), query="1 + 1")

    assert result.stdout == "warm-result"
    assert result.cold_start is False
    assert ensures == [(GRAPH_ID, cpg, _cpg_sha256(cpg))]
    assert posts == ["1 + 1"]


def test_queries_for_different_graphs_share_one_lifecycle_lane(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock_path = tmp_path / "query-runtime.lock"
    cpg_a = tmp_path / "a.bin"
    cpg_b = tmp_path / "b.bin"
    cpg_a.write_bytes(b"a")
    cpg_b.write_bytes(b"b")
    runtime_a = _runtime(lifecycle_lock_path=lock_path)
    runtime_b = _runtime(lifecycle_lock_path=lock_path)
    graph_b = "c" * 64
    first_inside = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()

    monkeypatch.setattr(runtime_a, "_ensure_server", lambda **kwargs: False)
    monkeypatch.setattr(runtime_b, "_ensure_server", lambda **kwargs: False)

    def post_a(**kwargs):
        first_inside.set()
        assert release_first.wait(timeout=2)
        return "a"

    def post_b(**kwargs):
        second_inside.set()
        return "b"

    monkeypatch.setattr(runtime_a, "_post_query", post_a)
    monkeypatch.setattr(runtime_b, "_post_query", post_b)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime_a.query, graph_id=GRAPH_ID, cpg_path=cpg_a, cpg_sha256=_cpg_sha256(cpg_a), query="1")
        assert first_inside.wait(timeout=1)
        second = pool.submit(runtime_b.query, graph_id=graph_b, cpg_path=cpg_b, cpg_sha256=_cpg_sha256(cpg_b), query="2")
        time.sleep(0.05)
        assert second_inside.is_set() is False
        release_first.set()
        assert first.result(timeout=2).stdout == "a"
        assert second.result(timeout=2).stdout == "b"
        assert second_inside.is_set() is True


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

    def ensure(*, graph_id, cpg_path, cpg_sha256):
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

    result = runtime.query(graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=_cpg_sha256(cpg), query="1")

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
        runtime.query(graph_id=GRAPH_ID, cpg_path=cpg, cpg_sha256=_cpg_sha256(cpg), query="broken")


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


def test_stop_orphaned_removes_only_servers_with_missing_cpg_mount(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _runtime(lifecycle_lock_path=tmp_path / "runtime.lock")
    live_cpg = tmp_path / "live.bin"
    live_cpg.write_bytes(b"live")
    removed: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_owned_containers",
        lambda: [
            ("2026-01-01T00:00:00Z", "opss-live"),
            ("2026-01-02T00:00:00Z", "opss-orphan"),
        ],
    )

    def inspect(name: str):
        source = live_cpg if name == "opss-live" else tmp_path / "missing.bin"
        return {
            "Mounts": [
                {
                    "Destination": "/cpg.bin",
                    "Source": str(source),
                    "RW": False,
                    "Type": "bind",
                }
            ]
        }

    monkeypatch.setattr(runtime, "_inspect", inspect)
    monkeypatch.setattr(runtime, "_remove_container", removed.append)

    assert runtime.stop_orphaned() == 1
    assert removed == ["opss-orphan"]
