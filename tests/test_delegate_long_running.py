"""Opt-in extended execution lifetime stays separate from the MCP wait window."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastmcp import FastMCP

from chatgpt_web_oauth_mcp.tools_delegate import (
    LONG_RUNNING_DEFAULT_TIMEOUT_SECONDS,
    MAX_DELEGATE_EXECUTION_TIMEOUT_SECONDS,
    _resolve_execution_timeout,
    register_delegate_tools,
)


@pytest.mark.parametrize(
    ("long_running", "explicit", "expected"),
    [
        (False, None, None),
        (True, None, 28_800),
        (False, 120, 120),
        (True, 120, 120),
        (True, 86_400, 86_400),
    ],
)
def test_opt_in_timeout_selection(long_running: bool, explicit: int | None, expected: int | None) -> None:
    assert _resolve_execution_timeout(
        long_running=long_running, execution_timeout_seconds=explicit,
    ) == expected


@pytest.mark.parametrize("invalid", [0, -1, 86_401])
def test_timeout_selection_rejects_invalid_bound(invalid: int) -> None:
    with pytest.raises(ValueError):
        _resolve_execution_timeout(long_running=True, execution_timeout_seconds=invalid)


def test_timeout_constants_within_durable_job_limit() -> None:
    assert LONG_RUNNING_DEFAULT_TIMEOUT_SECONDS == 8 * 3600
    assert MAX_DELEGATE_EXECUTION_TIMEOUT_SECONDS == 24 * 3600


def test_public_mcp_tools_advertise_long_running_and_full_day_limit(tmp_path: Path) -> None:
    mcp = FastMCP("long-running-schema")
    register_delegate_tools(mcp, FakeContext(tmp_path, FakeRegistry()))
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    for name in ("delegate_task", "delegate_batch"):
        fields = tools[name].parameters["properties"]
        assert fields["long_running"]["default"] is False
        assert fields["long_running"]["type"] == "boolean"
        assert any(
            item.get("maximum") == 86_400 and item.get("minimum") == 1
            for item in fields["execution_timeout_seconds"]["anyOf"]
        )


class FakeMCP:
    def __init__(self) -> None:
        self.functions: dict[str, Any] = {}

    def tool(self, *, name: str, **kwargs: Any):
        def decorate(fn):
            self.functions[name] = fn
            return fn
        return decorate


class FakeRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_delegate(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delegate_task", kwargs))
        return {"success": True, "status": "running", "delegate_id": "test-delegate"}

    def run_delegate_batch(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delegate_batch", kwargs))
        return {"success": True, "status": "running", "group_id": "test-group"}


@dataclass
class FakeContext:
    workspace_root: Path
    registry: FakeRegistry


@pytest.fixture
def registered_tools(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import chatgpt_web_oauth_mcp.tools_delegate as module

    monkeypatch.setattr(module, "reserve_result_claim_capacity", lambda *a, **kw: None)
    monkeypatch.setattr(module, "release_result_claim_capacity", lambda *a, **kw: None)
    mcp = FakeMCP()
    registry = FakeRegistry()
    register_delegate_tools(mcp, FakeContext(workspace_root=tmp_path, registry=registry))
    return mcp.functions, registry


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, None),
        ({"long_running": True}, 28_800),
        ({"long_running": True, "execution_timeout_seconds": 86_400}, 86_400),
        ({"execution_timeout_seconds": 150}, 150),
        ({"long_running": True, "execution_timeout_seconds": 150}, 150),
    ],
)
def test_delegate_task_dispatch_keeps_response_wait_bounded(registered_tools, kwargs, expected):
    functions, registry = registered_tools
    result = functions["delegate_task"](task="Inspect this repository.", wait_seconds=0.0, **kwargs)
    assert result["status"] == "running"
    _, captured = registry.calls[-1]
    assert captured["execution_timeout_seconds"] == expected
    assert captured["wait_seconds"] == 0.0


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, None),
        ({"long_running": True}, 28_800),
        ({"long_running": True, "execution_timeout_seconds": 86_400}, 86_400),
        ({"execution_timeout_seconds": 160}, 160),
    ],
)
def test_delegate_batch_dispatch_keeps_response_wait_bounded(registered_tools, kwargs, expected):
    functions, registry = registered_tools
    result = functions["delegate_batch"](
        tasks=[{"task": "Read docs"}], wait_seconds=0.0, **kwargs,
    )
    assert result["status"] == "running"
    _, captured = registry.calls[-1]
    assert captured["execution_timeout_seconds"] == expected
    assert captured["wait_seconds"] == 0.0
