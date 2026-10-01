from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from chatgpt_web_oauth_mcp.tool_profiles import (
    LEAN_HIDDEN_TOOL_NAMES,
    apply_tool_profile,
    normalize_tool_profile,
)


def test_normalize_tool_profile_accepts_full_and_lean() -> None:
    assert normalize_tool_profile(None) == "full"
    assert normalize_tool_profile("") == "full"
    assert normalize_tool_profile(" FULL ") == "full"
    assert normalize_tool_profile("lean") == "lean"


def test_normalize_tool_profile_rejects_unknown_value() -> None:
    with pytest.raises(ValueError, match="CHATGPT_MCP_TOOL_PROFILE"):
        normalize_tool_profile("tiny")


def test_lean_profile_hides_advanced_tools_from_public_catalog() -> None:
    mcp = FastMCP("tool-profile-test")

    @mcp.tool(name="job_start")
    def job_start() -> str:
        return "job"

    @mcp.tool(name="tmux_start")
    def tmux_start() -> str:
        return "tmux"

    @mcp.tool(name="codex_runtime_acquire")
    def codex_runtime_acquire() -> str:
        return "runtime"

    state = apply_tool_profile(mcp, "lean")

    async def names() -> set[str]:
        return {tool.name for tool in await mcp.list_tools()}

    assert asyncio.run(names()) == {"job_start"}
    assert state["name"] == "lean"
    assert "tmux_start" in state["hidden_tools"]
    assert "codex_runtime_acquire" in state["hidden_tools"]
    assert len(LEAN_HIDDEN_TOOL_NAMES) == 14


def test_full_profile_keeps_advanced_tools_visible() -> None:
    mcp = FastMCP("tool-profile-full-test")

    @mcp.tool(name="tmux_start")
    def tmux_start() -> str:
        return "tmux"

    state = apply_tool_profile(mcp, "full")

    async def names() -> set[str]:
        return {tool.name for tool in await mcp.list_tools()}

    assert asyncio.run(names()) == {"tmux_start"}
    assert state == {"name": "full", "hidden_tools": []}
