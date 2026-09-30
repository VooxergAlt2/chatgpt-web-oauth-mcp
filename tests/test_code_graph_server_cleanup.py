from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp import server


def test_code_graph_shutdown_cleanup_skips_when_disabled(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setattr(server, "CODE_GRAPH_ENABLED", False)
    monkeypatch.setattr(
        server,
        "cleanup_owned_query_servers",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert server._cleanup_code_graph_query_runtime() == 0
    assert calls == []


def test_code_graph_shutdown_cleanup_is_strict_when_enabled(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def cleanup(*args, **kwargs):
        calls.append((args, kwargs))
        return 2

    monkeypatch.setattr(server, "CODE_GRAPH_ENABLED", True)
    monkeypatch.setattr(server, "JOERN_DOCKER_BINARY", "/usr/bin/docker")
    monkeypatch.setattr(server, "STATE_DIR", tmp_path)
    monkeypatch.setattr(server, "cleanup_owned_query_servers", cleanup)

    assert server._cleanup_code_graph_query_runtime() == 2
    assert calls == [
        (
            ("/usr/bin/docker", tmp_path),
            {"strict": True},
        )
    ]


def test_shutdown_runtime_aggregates_failures_without_skipping_cleanup(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(server.quota_window_manager, "stop", lambda: calls.append("quota"))
    monkeypatch.setattr(server.usage_limit_collector, "stop", lambda: calls.append("usage"))

    def code_graph_cleanup():
        calls.append("code_graph")
        raise RuntimeError("code graph cleanup failed")

    def registry_shutdown():
        calls.append("registry")
        raise ValueError("registry shutdown failed")

    monkeypatch.setattr(server, "_cleanup_code_graph_query_runtime", code_graph_cleanup)
    monkeypatch.setattr(server.registry, "shutdown", registry_shutdown)
    monkeypatch.setattr(
        server.foreground_process_registry,
        "shutdown",
        lambda: calls.append("foreground"),
    )
    monkeypatch.setattr(
        server.codex_runtime_manager,
        "shutdown",
        lambda: calls.append("codex"),
    )

    with pytest.raises(ExceptionGroup) as caught:
        asyncio.run(server._shutdown_mcp_runtime())

    messages = [str(exc) for exc in caught.value.exceptions]
    assert messages == ["code graph cleanup failed", "registry shutdown failed"]
    assert calls == [
        "quota",
        "usage",
        "code_graph",
        "registry",
        "foreground",
        "codex",
    ]
