"""Exclusive public MCP surface: only the three group router tools are visible."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastmcp import Client, FastMCP
from fastmcp.tools import ToolResult

from chatgpt_web_oauth_mcp.tool_groups import register_tool_groups


def _make_exclusive_servers(*, extra_backend_tools: int = 0):
    private = FastMCP("private Opss")
    public = FastMCP("public groups")

    @private.tool(name="read_text")
    def read_text(path: str) -> dict:
        return {"content": "private:" + path}

    @private.tool(name="job_start")
    def job_start(command: str) -> dict:
        return {"job_id": "job_example", "command": command}

    @private.tool(name="secret_hidden")
    def secret_hidden() -> str:
        return "NEVER_VISIBLE"

    private.disable(names={"secret_hidden"}, components={"tool"})

    @private.resource("skill://test/router-guide", mime_type="text/plain")
    def guide() -> str:
        return "existing resource kept"

    for i in range(extra_backend_tools):
        @private.tool(name=f"test_{i:03d}")
        def extra(test: int = i) -> int:
            return test

    public.add_provider(private.local_provider)
    # Hide all original registered tools even if the private tool profile disabled them.
    public.disable(
        names={"read_text", "job_start", "secret_hidden"} |
              {f"test_{i:03d}" for i in range(extra_backend_tools)},
        components={"tool"},
    )
    register_tool_groups(public, local_opss_server=private)
    return private, public


def test_exclusive_router_only_three_visible_tools_even_with_many_backend_tools() -> None:
    private, public = _make_exclusive_servers(extra_backend_tools=210)

    async def check() -> None:
        assert len(await private.list_tools()) == 212
        async with Client(public) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == {"tool_group_select", "tool_group_catalog", "tool_group_call"}
            resources = [str(item.uri) for item in await client.list_resources()]
            assert resources == ["skill://test/router-guide"]
            contents = await client.read_resource("skill://test/router-guide")
            assert contents[0].text == "existing resource kept"
            with pytest.raises(Exception, match="Unknown tool"):
                await client.call_tool("read_text", {"path": "sensitive"})
            with pytest.raises(Exception, match="Unknown tool"):
                await client.call_tool("secret_hidden", {})
            choose = await client.call_tool("tool_group_select", {"group": "opss"})
            assert choose.structured_content["total"] == 212
            assert choose.structured_content["native_tools"] is False
            assert "secret_hidden" not in {
                item["name"] for item in choose.structured_content["tools"]
            }
            catalog = await client.call_tool(
                "tool_group_catalog", {"group": "opss", "search": "job_start",
                                       "include_schema": True, "limit": 1}
            )
            assert catalog.structured_content["tools"][0]["input_schema"]["required"] == ["command"]
            result = await client.call_tool(
                "tool_group_call",
                {"group": "opss", "name": "job_start",
                 "arguments": {"command": "printf TEST_OK"}},
            )
            assert result.structured_content["job_id"] == "job_example"
            # Routing must not accidentally reduce the payload ceiling of
            # existing source-code and command tools to the remote 64 KiB cap.
            substantial = await client.call_tool(
                "tool_group_call",
                {"group": "opss", "name": "job_start",
                 "arguments": {"command": "x" * 70000}},
            )
            assert len(substantial.structured_content["command"]) == 70000
            hidden = await client.call_tool(
                "tool_group_call", {"group": "opss", "name": "secret_hidden"},
                raise_on_error=False,
            )
            assert hidden.is_error

    asyncio.run(check())


def test_exclusive_group_select_does_not_require_windows_connection() -> None:
    _, public = _make_exclusive_servers()

    async def check() -> None:
        async with Client(public) as client:
            opss = await client.call_tool("tool_group_select", {"group": "opss"})
            assert opss.structured_content["total"] == 2
            missing = await client.call_tool(
                "tool_group_select", {"group": "windows"}, raise_on_error=False
            )
            assert missing.is_error
            again = await client.call_tool(
                "tool_group_call", {"group": "opss", "name": "read_text",
                                   "arguments": {"path": "README.md"}}
            )
            assert again.structured_content == {"content": "private:README.md"}

    asyncio.run(check())


def test_real_opss_server_exclusive_profile_import_and_private_dispatch(tmp_path: Path) -> None:
    """Isolated Python process, no production server restart or VM connection."""
    script = r'''
import asyncio
from chatgpt_web_oauth_mcp import server
from chatgpt_web_oauth_mcp.tool_surface import tool_schema_footprint
async def main():
    exposed = await server.PUBLIC_MCP.list_tools()
    assert {item.name for item in exposed} == {
        "tool_group_select", "tool_group_catalog", "tool_group_call"
    }
    assert len(await server.mcp.list_tools()) == 58
    assert "delegate_supervisor" in {tool.name for tool in await server.mcp.list_tools()}
    assert "delegate_draft" in {tool.name for tool in await server.mcp.list_tools()}
    assert len(await server.PUBLIC_MCP.list_resources()) == 7
    assert tool_schema_footprint(exposed)["total_tokens"] < 1500
    choose = await server.PUBLIC_MCP.call_tool(
        "tool_group_select", {"group": "opss", "limit": 2}, run_middleware=False
    )
    assert choose.structured_content["total"] == 58
    catalog = await server.PUBLIC_MCP.call_tool(
        "tool_group_catalog",
        {"group": "opss", "search": "get_default_cwd", "include_schema": True, "limit": 1},
        run_middleware=False
    )
    assert catalog.structured_content["tools"][0]["name"] == "get_default_cwd"
    local = await server.PUBLIC_MCP.call_tool(
        "tool_group_call", {"group": "opss", "name": "get_default_cwd"},
        run_middleware=False
    )
    assert local.structured_content["success"] is True
    info = await server.PUBLIC_MCP.call_tool(
        "tool_group_call", {"group": "opss", "name": "server_info"},
        run_middleware=False
    )
    assert info.structured_content["tool_count"] == 3
    assert info.structured_content["tool_group_mode"] == "exclusive"
    workspaces = await server.PUBLIC_MCP.call_tool(
        "tool_group_catalog",
        {"group": "revit", "search": "workspace_submit", "limit": 1,
         "include_schema": True}, run_middleware=False
    )
    schema = workspaces.structured_content["tools"][0]["input_schema"]
    assert schema["required"] == ["workspace_id", "command", "arguments"]
    assert schema["additionalProperties"] is False
    try:
        await server.PUBLIC_MCP.call_tool(
            "read_text", {"path": "README.md"}, run_middleware=False
        )
    except Exception as exc:
        assert "Unknown tool" in str(exc)
    else:
        raise AssertionError("Private tools escaped into public MCP")
    print("EXCLUSIVE_REAL_SERVER_PASS")
asyncio.run(main())
'''
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "CHATGPT_MCP_TOOL_PROFILE": "lean",
        "CHATGPT_MCP_TOOL_GROUP_MODE": "exclusive",
        "CHATGPT_MCP_STATE_DIR": str(tmp_path / "state"),
        "CHATGPT_MCP_WORKSPACE_ROOT": str(tmp_path),
        "CHATGPT_MCP_HEALTH_USAGE_LIMITS_ENABLED": "false",
    })
    proc = subprocess.run(
        [sys.executable, "-u", "-c", script],
        capture_output=True, text=True, env=env, timeout=25,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert "EXCLUSIVE_REAL_SERVER_PASS" in proc.stdout
