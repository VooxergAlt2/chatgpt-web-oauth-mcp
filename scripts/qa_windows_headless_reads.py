#!/usr/bin/env python3
"""Live, read-only Windows gateway smoke against an isolated foreign GUI fence.

Uses the real gateway and a synthetic state file in a private temporary
directory. It NEVER reads or changes the production Revit workspace ledger,
launches Revit, sends PowerShell, or requests mutating Windows tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time

from dotenv import dotenv_values
from fastmcp import Client, FastMCP

from chatgpt_web_oauth_mcp.revit_workspace_backend import NodeWorkspaceBackend
from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager
from chatgpt_web_oauth_mcp.tool_groups import register_tool_groups
from chatgpt_web_oauth_mcp.upstream_gateway import gateway_transport_factory_from_env


async def verify(env_file: Path) -> None:
    if not env_file.is_file():
        raise RuntimeError("GATEWAY_ENV_FILE_UNAVAILABLE")
    values = dotenv_values(env_file)
    factory = gateway_transport_factory_from_env(environ=values)
    if factory is None:
        raise RuntimeError("GATEWAY_NOT_CONFIGURED")

    with tempfile.TemporaryDirectory(prefix="opss-headless-windows-qa-") as directory:
        state_path = Path(directory) / "shadow-ledger.json"
        state_path.write_text(json.dumps({
            "version": 1, "workspaces": {},
            "ui": {
                "owner": "openai:synthetic-foreign-chat",
                "workspace_id": None,
                "token": "synthetic-fence-no-live-credentials",
                "operation_token": "synthetic-operation-token",
                "state": "draining", "operation": "windows_Click",
                "started_at": time.time(),
            },
        }), encoding="utf-8")
        manager = WorkspaceManager(
            state_path, NodeWorkspaceBackend(factory),
            owner_provider=lambda: "openai:synthetic-qa-chat",
        )
        before = await manager.ui_status()
        if before["state"] != "draining" or before["owner_matches"]:
            raise RuntimeError("INVALID_SHADOW_FOREIGN_FENCE")

        public = FastMCP("isolated-windows-readonly-acceptance")
        register_tool_groups(public, transport_factory=factory, workspace_manager=manager)
        checks = (
            ("windows_Process", {"mode": "list", "limit": 3}),
            ("windows_DisplayInventory", {}),
            ("windows_FileSystem", {"mode": "info", "path": r"C:\Windows"}),
        )
        async with Client(public) as client:
            for name, arguments in checks:
                outcome = await client.call_tool("tool_group_call", {
                    "group": "windows", "name": name, "arguments": arguments,
                }, raise_on_error=False)
                if outcome.is_error:
                    raise RuntimeError("LIVE_WINDOWS_READ_FAILED:" + name)
                if await manager.ui_status() != before:
                    raise RuntimeError("SHADOW_FOREIGN_FENCE_CHANGED")

        print("LIVE_WINDOWS_READ_ONLY_PASS process=list display_inventory filesystem=info")
        print("FOREIGN_GUI_FENCE_UNCHANGED source=isolated_shadow_ledger")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-env", required=True, type=Path)
    asyncio.run(verify(parser.parse_args().gateway_env))
