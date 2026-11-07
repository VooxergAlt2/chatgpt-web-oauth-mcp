"""Stateless, lazy tool groups for the Windows/Revit QA gateway.

Exactly three groups: opss (existing local tools), windows (remote UI),
and revit (remote Revit API). The MCP tools/list surface stays constant.
The model chooses its group explicitly on each call, receives the group's
schemas, and invokes exact tool names through a single dispatcher.

No server-wide switches or sticky session state: those break concurrent
chats, transports with per-request MCP session ids, and tools/list caches.
"""
from __future__ import annotations

import asyncio
import json
import ntpath
import os
import re
import hashlib
from pathlib import Path
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Literal

from fastmcp import Client
from fastmcp.tools import ToolResult
from mcp.types import CallToolResult, Tool, ToolAnnotations, TextContent
from pydantic import Field
from jsonschema import Draft202012Validator

from .revit_workspace_backend import NodeWorkspaceBackend
from .revit_workspaces import (
    READONLY_WINDOWS, READONLY_LEGACY_REVIT, WorkspaceManager, workspace_tools,
    _headless_windows_read,
)

ToolGroup = Literal["opss", "windows", "revit"]
REMOTE_GROUPS = frozenset({"windows", "revit"})
ALL_GROUPS = ("opss", "windows", "revit")
REMOTE_CALL_TIMEOUT_SECONDS = 25
# Leave transport time to receive the Windows result before its own deadline.
WINDOWS_POWERSHELL_TIMEOUT_SECONDS = 20

# Independent defense in depth: the Windows gateway also restricts the
# Revit namespace, because its native localhost TCP endpoint can execute C#.
# Revisit only after the in-process authorization lease has native QA.
READONLY_REVIT_TOOLS = READONLY_LEGACY_REVIT


def legacy_revit_tool_schemas() -> dict[str, Tool]:
    """Static stable schemas for three legacy reads, without dialing Windows.

    Workspace catalogs must also work when the VM is offline, but the
    existing read-only Revit API must remain discoverable for chats which
    have not claimed any workspace.
    """
    specs = (
        ("get_project_info", "Read current Revit project metadata; unavailable while document bindings are active.",
         {"includePhases": {"type": "boolean"}, "includeWorksets": {"type": "boolean"},
          "includeLinks": {"type": "boolean"}, "includeLevels": {"type": "boolean"},
          "compact": {"type": "boolean"}}),
        ("get_phases", "Read current Revit project phases; unavailable while document bindings are active.",
         {"includePhaseFilters": {"type": "boolean"}}),
        ("get_selected_elements", "Read the current Revit selection; unavailable while document bindings are active.",
         {"limit": {"type": "number"}}),
    )
    return {
        "revit_" + name: Tool(
            name="revit_" + name,
            description=description,
            inputSchema={"type": "object", "properties": properties, "additionalProperties": False},
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        )
        for name, description, properties in specs
    }
MAX_SEARCH = 128
MAX_PAGE_SIZE = 20
MAX_SCHEMA_PAGE_SIZE = 4
MAX_CALL_ARGS_BYTES = 64 * 1024
MAX_OPSS_CALL_ARGS_BYTES = 2 * 1024 * 1024
MAX_TEXT_RESULT_BYTES = 128 * 1024
MAX_IMAGE_RESULT_BYTES = 5 * 1024 * 1024

# The Windows Revit 2023 QA VM repeatedly stalled in Autodesk licensing
# when Revit.exe was launched directly instead of through the existing
# interactive Explorer's Revit 2023 shortcut ListItem/InvokePattern.
REVIT_NATIVE_QA_GUIDE = "/home/vooxerg/.config/astra/REVIT_NATIVE_QA_LAUNCH.md"
REVIT_VERIFIED_EXPLORER_HELPER = (
    r"C:\Users\Vlad\AppData\Local\Astra-Revit-Bridge\QA"
    r"\Invoke-VerifiedExplorerRevit2023.ps1"
)
REVIT_LAUNCH_POLICY = (
    "For Revit 2023 native QA, read the mandatory guide with group=opss "
    "read_text(" + REVIT_NATIVE_QA_GUIDE + "). Never launch Revit.exe "
    "directly via windows_App, Start-Process, Invoke-Item, ShellExecute, "
    "or a new explorer.exe. Validate a FRESH Explorer UIA window using "
    "the verified helper on Windows: " + REVIT_VERIFIED_EXPLORER_HELPER + ". "
    "Only an existing real Explorer window's Revit 2023 ListItem may "
    "InvokePattern.Invoke(), with explicit launch authorization. "
    "A correct Explorer parent does not prove Autodesk licensing succeeded. "
    "When desktop/UIA/licensing fails, stop and do not retry automatically."
)


def _enforce_revit_launch_policy(name: str, arguments: dict[str, Any]) -> None:
    """Prevent common direct Revit launches; not an OS permission boundary."""
    if name == "windows_App" and arguments.get("mode", "launch") in {
        "launch", "launch_executable",
    }:
        def program_component(value: object) -> str:
            text = str(value or "")
            # Discrete argv is not a shell command. Parent folder names such
            # as Astra-Revit-Bridge are not the executable Revit. Preserve
            # opaque/inline commands and inspect their entire payload.
            if (ntpath.isabs(text) and
                    not any(char in text for char in '\r\n\x00;|&`\"\'')):
                return ntpath.basename(text.rstrip('\\/'))
            return text

        if arguments.get("mode", "launch") == "launch_executable":
            argv = arguments.get("args")
            parts = [str(arguments.get("name") or ""),
                     program_component(arguments.get("executable"))]
            if isinstance(argv, list):
                parts.extend(program_component(arg) for arg in argv)
            else:
                parts.append(str(argv or ""))
            target = " ".join(parts)
        else:
            target = " ".join(str(arguments.get(key) or "")
                              for key in ("name", "executable", "args"))
        if re.search(r"(?i)\brevit(?:\s+20\d\d|\.exe)?\b", target):
            raise ValueError("DIRECT_REVIT_LAUNCH_FORBIDDEN: " + REVIT_LAUNCH_POLICY)
    if name == "windows_PowerShell":
        command = arguments.get("command", "")
        if not isinstance(command, str):
            return
        contains_revit = re.search(r"(?i)\brevit(?:\.exe|\s+2023\.lnk)\b", command)
        invokes_process = re.search(
            r"(?i)\b(?:Start-Process|Invoke-Item|InvokeVerb|ShellExecute|"
            r"Start-Job|Start-ThreadJob)\b"
            r"|&\s*['\"]?[^\n]*\brevit\.exe\b",
            command,
        )
        if contains_revit and invokes_process:
            raise ValueError("DIRECT_REVIT_LAUNCH_FORBIDDEN: " + REVIT_LAUNCH_POLICY)


INTERNAL_GATEWAY_TOOLS = frozenset({"windows_operation_submit", "windows_operation_status"})

WINDOWS_OPERATION_RESULT_TOOL = Tool(
    name="windows_operation_result",
    description="Retrieve only owned historical operation result by fence_id without exposing bearer tokens.",
    inputSchema={
        "type": "object",
        "properties": {
            "fence_id": {
                "type": "string",
                "description": "Public fence_id of the historical operation.",
                "minLength": 1,
            }
        },
        "required": ["fence_id"],
        "additionalProperties": False,
    },
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
)


def _visible_remote_tools(items: Sequence[Tool], group: ToolGroup) -> list[Tool]:
    if group not in REMOTE_GROUPS:
        return []
    return sorted(
        [
            item for item in items
            if item.name.startswith(group + "_")
            and item.name not in INTERNAL_GATEWAY_TOOLS
            and (group != "revit" or item.name in READONLY_REVIT_TOOLS)
        ],
        key=lambda item: item.name,
    )


def _render_catalog(
    items: Sequence[Tool],
    *,
    group: ToolGroup,
    search: str,
    offset: int,
    limit: int,
    include_schema: bool,
) -> dict[str, Any]:
    if offset < 0 or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError("Invalid catalog pagination")
    if include_schema and limit > MAX_SCHEMA_PAGE_SIZE:
        raise ValueError("Request no more than four full schemas at once")
    if len(search) > MAX_SEARCH:
        raise ValueError("Search is limited to 128 characters")

    query = search.casefold()
    filtered = [
        item for item in items
        if not query or query in item.name.casefold()
        or query in (item.description or "").casefold()
    ]
    selected = filtered[offset: offset + limit]
    result = []
    for item in selected:
        entry: dict[str, Any] = {
            "name": item.name,
            "description": (item.description or "")[:350],
            "required_arguments": item.input_schema.get("required", []),
        }
        if item.annotations is not None:
            # Preserve tool-specific safety hints even though the MCP client
            # sees just one generic dispatcher tool.
            entry["annotations"] = item.annotations.model_dump(
                by_alias=True, exclude_none=True
            )
        if include_schema:
            # Return exact schema. Do not truncate required fields or types.
            entry["input_schema"] = item.input_schema
        result.append(entry)
    return {
        "group": group,
        "total": len(filtered),
        "offset": offset,
        "next_offset": (offset + len(selected) if offset + len(selected) < len(filtered) else None),
        "tools": result,
        "schemas_included": include_schema,
    }


def _bounded_result(result: CallToolResult) -> ToolResult:
    if len(result.content) > 64:
        raise ValueError("Tool returned too many content blocks")
    total_text_bytes = 0
    total_media_bytes = 0
    for block in result.content:
        if block.type in {"image", "audio"}:
            data = getattr(block, "data", "")
            # Base64 length check; no image content gets written to text logs.
            total_media_bytes += len(data)
            if total_media_bytes > (MAX_IMAGE_RESULT_BYTES * 4 // 3) + 4:
                raise ValueError("Media result too large; request a smaller screenshot region/scale")
        else:
            total_text_bytes += len(block.model_dump_json().encode("utf-8"))
            if total_text_bytes > MAX_TEXT_RESULT_BYTES:
                raise ValueError("Tool result too large; request smaller structured output")
    structured = result.structured_content
    if structured is not None:
        if len(json.dumps(structured, ensure_ascii=False).encode("utf-8")) > MAX_TEXT_RESULT_BYTES:
            raise ValueError("Structured tool result too large; narrow the request")
    return ToolResult(
        content=result.content,
        structured_content=structured,
        is_error=result.is_error,
        meta=result.meta,
    )


def _windows_completion_unknown(name: str, result: CallToolResult) -> bool:
    """Keep the GUI fence if Windows-MCP reports an execution timeout.

    Windows-MCP can return an MCP-successful envelope with content
    'Response: Command execution timed out / Status Code: 1'. The target
    UI action may still have happened; an MCP success is not native proof.
    """
    if name != "windows_PowerShell":
        return False
    messages = [block.text for block in result.content if block.type == "text"]
    if isinstance(result.structured_content, dict):
        messages.extend(
            value for key in ("result", "text", "response")
            if isinstance((value := result.structured_content.get(key)), str)
        )
    pattern = re.compile(r"\s*Response: Command execution timed out\s+Status Code: 1\s*")
    return any(pattern.fullmatch(text) for text in messages)


def register_tool_groups(
    server: Any,
    *,
    transport_factory: Callable[[], Any] | None = None,
    local_opss_server: Any | None = None,
    workspace_manager: WorkspaceManager | None = None,
    workspace_state_path: Path | None = None,
    allow_legacy_windows: bool = False,
) -> None:
    """Register three stable MCP tools with optional private Opss dispatch."""

    manager = workspace_manager or WorkspaceManager(
        workspace_state_path or Path(os.environ.get(
            "CHATGPT_MCP_STATE_DIR", str(Path.home() / ".chatgpt-web-oauth-mcp")
        )) / "revit-workspaces.json",
        NodeWorkspaceBackend(transport_factory),
    )
    virtual = {tool.name: tool for tool in workspace_tools()}
    legacy = legacy_revit_tool_schemas()

    async def fetch(group: ToolGroup) -> list[Tool]:
        if group == "opss":
            if local_opss_server is None:
                return []
            return sorted(
                [tool.to_mcp_tool() for tool in await local_opss_server.list_tools()],
                key=lambda item: item.name,
            )
        if group == "revit":
            # No VM connection while listing. Keep the three old read-only
            # methods discoverable alongside virtual workspace commands.
            return sorted((*virtual.values(), *legacy.values()), key=lambda item: item.name)
        if transport_factory is None:
            return []
        async with Client(transport_factory(), timeout=20, init_timeout=5) as client:
            items = await client.list_tools()
        visible = _visible_remote_tools(items, group)
        if group == "windows":
            # Reserved coordination fields belong to the router, not Node.
            visible = [item.model_copy(deep=True) for item in visible]
            for item in visible:
                if item.name == "windows_PowerShell":
                    item.input_schema.setdefault("properties", {})["timeout"] = {
                        "type": "integer", "minimum": 1,
                        "maximum": WINDOWS_POWERSHELL_TIMEOUT_SECONDS,
                        "default": WINDOWS_POWERSHELL_TIMEOUT_SECONDS,
                        "description": "Bounded Windows execution in seconds; longer work needs a durable script and receipt polling.",
                    }
                if item.name not in READONLY_WINDOWS:
                    item.input_schema.setdefault("properties", {}).update({
                        "_workspace_id": {"type": "string", "description": "Required with active workspaces: owning workspace."},
                        "_lease_token": {"type": "string", "description": "Required with active workspaces: acquire_ui token."},
                    })
            visible.append(WINDOWS_OPERATION_RESULT_TOOL)
            visible = sorted(visible, key=lambda item: item.name)
        return visible

    @server.tool(
        name="tool_group_select",
        title="Choose Opss, Windows or Revit tools",
        description=(
            "Choose ONE of three tool groups for this chat: opss (default: code/builds/"
            "jobs on gip-core), windows (GUI/screenshots/start/update Revit on VM), "
            "revit (native Revit API on QA model). Loads a compact page of available "
            "tool names. Pass this same group to tool_group_catalog for schemas "
            "and tool_group_call to execute. Stateless across reconnects and chats."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def tool_group_select(
        group: Annotated[ToolGroup, Field(description="One group to inspect: opss, windows or revit.")],
        limit: Annotated[int, Field(description="Maximum number of tool names to preview (1..20).")] = 10,
    ) -> dict[str, Any]:
        if group not in ALL_GROUPS:
            raise ValueError("Choose opss, windows, or revit")
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValueError("Limit must be 1..20")
        if group == "opss" and local_opss_server is None:
            return {
                "selected": group,
                "groups": ALL_GROUPS,
                "native_tools": True,
                "message": "Opss tools are already available directly; use run_command, "
                           "read_text, git_*, job_* and the usual Opss tools.",
                "tools": [],
            }
        items = await fetch(group)
        if not items:
            raise ValueError(f"Group {group} is unavailable or publishes no permitted tools")
        page = _render_catalog(
            items, group=group, search="", offset=0, limit=limit, include_schema=False
        )
        return {
            "selected": group,
            "groups": ALL_GROUPS,
            "native_tools": False,
            "message": "Use tool_group_catalog(group=...) for schemas and "
                       "tool_group_call(group=..., name=...) to execute. "
                       "Group selection is explicit, not server state. "
                       "Opss work stays on gip-core.",
            **({"revit_native_qa_launch_policy": REVIT_LAUNCH_POLICY}
               if group in {"windows", "revit"} else {}),
            **page,
        }

    @server.tool(
        name="tool_group_catalog",
        title="Discover tools and exact argument schemas",
        description=(
            "Discover tools in an explicitly chosen group. Name/search and pagination return "
            "compact summaries; include_schema=true returns exact argument definitions "
            "(maximum four tools per page)."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    async def tool_group_catalog(
        group: Annotated[ToolGroup, Field(description="The group whose tools should be listed.")],
        search: Annotated[str, Field(description="Filter tools by name or description; max 128 characters.")] = "",
        offset: Annotated[int, Field(description="Zero-based pagination offset.")] = 0,
        limit: Annotated[int, Field(description="Page size (1..20, or 1..4 with full schemas).")] = 10,
        include_schema: Annotated[bool, Field(description="Include full input argument JSON schema for each returned tool.")] = False,
    ) -> dict[str, Any]:
        # Validate before making any network call.
        if offset < 0 or not 1 <= limit <= MAX_PAGE_SIZE or len(search) > MAX_SEARCH:
            raise ValueError("Invalid search, offset or limit")
        if include_schema and limit > MAX_SCHEMA_PAGE_SIZE:
            raise ValueError("Use limit <= 4 with include_schema=true")
        if group not in ALL_GROUPS:
            raise ValueError("Choose opss, windows or revit")
        if group == "opss" and local_opss_server is None:
            return {
                "group": "opss",
                "native_tools": True,
                "message": "Opss tools are already visible in the MCP tool catalog. "
                           "Call them directly, or select windows/revit for their tools.",
                "tools": [],
            }
        items = await fetch(group)
        return _render_catalog(
            items, group=group, search=search, offset=offset,
            limit=limit, include_schema=include_schema,
        )

    @server.tool(
        name="tool_group_call",
        title="Execute a tool in the chosen group",
        description=(
            "Invoke one exact tool from opss, windows, or revit. First "
            "call tool_group_select, then tool_group_catalog(group=..., include_schema=true) "
            "for argument names. Opss code/jobs run on gip-core; Revit calls run through Revit-MCP, NEVER through "
            "Windows PowerShell. Return images/text intact. Only use disposable QA "
            "models for operations that may affect the model."
        ),
        # A generic dispatcher may invoke write/destructive tools. Never
        # advertise it to the MCP client as a harmless read-only action.
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, openWorldHint=True
        ),
    )
    async def tool_group_call(
        group: Annotated[ToolGroup, Field(description="The group that owns the requested tool.")],
        name: Annotated[str, Field(description="Exact tool name obtained from tool_group_catalog.")],
        arguments: Annotated[dict[str, Any] | None, Field(description="JSON object arguments matching that tool's input schema.")] = None,
    ) -> ToolResult:
        if group not in ALL_GROUPS:
            raise ValueError("Choose opss, windows, or revit")
        if not name or len(name) > 128 or (
            group in REMOTE_GROUPS and not name.startswith(group + "_")
        ):
            raise ValueError(f"Tool is not in the selected {group} group")
        if group == "windows" and name in INTERNAL_GATEWAY_TOOLS:
            raise ValueError(f"Tool {name} is internal and cannot be directly invoked")
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        args = dict(arguments or {})
        if group == "windows":
            _enforce_revit_launch_policy(name, args)
            if name == "windows_PowerShell":
                timeout = args.get("timeout", WINDOWS_POWERSHELL_TIMEOUT_SECONDS)
                if type(timeout) is not int or not 1 <= timeout <= WINDOWS_POWERSHELL_TIMEOUT_SECONDS:
                    raise ValueError("WINDOWS_TIMEOUT_EXCEEDS_TRANSPORT_BUDGET: timeout must be 1..20 seconds; use a durable script for longer work")
                args["timeout"] = timeout
        max_bytes = MAX_OPSS_CALL_ARGS_BYTES if group == "opss" else MAX_CALL_ARGS_BYTES
        if len(json.dumps(args, ensure_ascii=False).encode("utf-8")) > max_bytes:
            raise ValueError("Tool arguments exceed group size limit")
        if group == "windows" and name == "windows_operation_result":
            Draft202012Validator(WINDOWS_OPERATION_RESULT_TOOL.input_schema).validate(args)
            fence_id = args.get("fence_id")
            if not isinstance(fence_id, str) or not fence_id:
                raise ValueError("fence_id is required")
            record = manager.get_windows_operation(fence_id, require_owner=True)
            if record is None:
                raise ValueError("OPERATION_NOT_FOUND: no historical operation matches this fence_id")
            if transport_factory is None:
                raise ValueError("Group windows is not connected")
            async with asyncio.timeout(24), Client(transport_factory(), timeout=5, init_timeout=4) as client:
                from .windows_operation_client import WindowsOperationClient
                op_client = WindowsOperationClient(client)
                receipt = await op_client.status(record["operation_token"], record["digest"])
                if receipt["state"] == "completed":
                    manager.complete_durable_operation(fence_id, receipt)
                    res = receipt.get("result")
                    if isinstance(res, dict):
                        res = CallToolResult.model_validate(res)
                    return _bounded_result(res)
                elif receipt["state"] == "running":
                    return ToolResult(
                        content=[TextContent(type="text", text=f"Operation {fence_id} is running.")],
                        meta={"fence_id": fence_id, "state": "running"},
                    )
                else:
                    return ToolResult(
                        content=[TextContent(type="text", text=f"Operation {fence_id} state is unknown.")],
                        is_error=True,
                        meta={"fence_id": fence_id, "state": "unknown"},
                    )
        if group == "revit" and name in virtual:
            async with asyncio.timeout(24):
                if name != "revit_workspace_ui_status":
                    await manager.reconcile_durable_operation()
                value = await manager.call(name, args)
            return ToolResult(structured_content=value)
        if group == "revit":
            if name not in READONLY_REVIT_TOOLS:
                manager.require_unscoped_allowed()
                raise ValueError("Revit method not approved for QA use")
        if group == "opss":
            if local_opss_server is None:
                raise ValueError("Opss native tools are already available in this profile")
            available = {tool.name for tool in await fetch("opss")}
            if name not in available:
                raise ValueError("Unknown or disabled Opss tool")
            return await local_opss_server.call_tool(name, args, run_middleware=False)
        if transport_factory is None:
            raise ValueError(f"Group {group} is not connected")
        async with asyncio.timeout(24), Client(transport_factory(), timeout=5, init_timeout=4) as client:
            all_remote = await client.list_tools()
            available = _visible_remote_tools(all_remote, group)
            if name not in {item.name for item in available}:
                raise ValueError("Tool not published in selected remote group")

            independent_read = group == "windows" and _headless_windows_read(name, args)
            if not independent_read:
                await manager.reconcile_durable_operation(client)
            if group == "revit":
                manager.require_unscoped_allowed()
            all_tool_names = {t.name for t in all_remote}
            has_durable = "windows_operation_submit" in all_tool_names and "windows_operation_status" in all_tool_names
            if group == "windows" and not independent_read and not has_durable and not allow_legacy_windows:
                raise ValueError("DURABLE_WINDOWS_ENDPOINTS_REQUIRED: Gateway does not support durable operations")

            if group == "windows" and not independent_read and has_durable:
                from .windows_operation_client import WindowsOperationClient, compute_request_digest
                clean_args = {k: v for k, v in args.items() if k not in {"_workspace_id", "_lease_token"}}
                upstream_tool = next(t for t in available if t.name == name)
                Draft202012Validator(upstream_tool.input_schema).validate(clean_args)
                digest = compute_request_digest(name, clean_args)
                args, fence = await manager.begin_remote(group, name, args, durable=True, request_digest=digest)
                fence_id = hashlib.sha256(fence.encode("utf-8")).hexdigest()[:24]
                op_client = WindowsOperationClient(client)
                try:
                    receipt = await op_client.submit_and_poll(fence, name, args, digest, poll_timeout=15.0)
                    if receipt["state"] == "completed":
                        manager.complete_durable_operation(fence_id, receipt)
                        res = receipt.get("result")
                        if isinstance(res, dict):
                            res = CallToolResult.model_validate(res)
                        return _bounded_result(res)
                    elif receipt["state"] == "running":
                        manager.uncertain_remote(fence)
                        return ToolResult(
                            content=[TextContent(type="text", text=f"Operation {fence_id} is pending. Use windows_operation_result(fence_id='{fence_id}') to retrieve the result.")],
                            meta={"pending": True, "fence_id": fence_id},
                        )
                    else:
                        manager.uncertain_remote(fence)
                        return ToolResult(
                            content=[TextContent(type="text", text=f"Operation {fence_id} state is unknown.")],
                            is_error=True,
                            meta={"fence_id": fence_id, "state": "unknown"},
                        )
                finally:
                    manager.uncertain_remote(fence)
            else:
                args, fence = await manager.begin_remote(group, name, args)
                try:
                    result = await client.call_tool(name, args, timeout=REMOTE_CALL_TIMEOUT_SECONDS, raise_on_error=False)
                    uncertain = _windows_completion_unknown(name, result)
                    if not result.is_error and not uncertain:
                        manager.complete_remote(fence)
                finally:
                    manager.uncertain_remote(fence)
        bounded = _bounded_result(result)
        if uncertain:
            return ToolResult(
                content=bounded.content,
                structured_content=bounded.structured_content,
                is_error=True,
                meta=bounded.meta,
            )
        return bounded
