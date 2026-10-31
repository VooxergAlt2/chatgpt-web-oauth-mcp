"""Stateless, lazy tool groups for the Windows/Revit QA gateway.

Exactly three groups: opss (existing local tools), windows (remote UI),
and revit (remote Revit API). The MCP tools/list surface stays constant.
The model chooses its group explicitly on each call, receives the group's
schemas, and invokes exact tool names through a single dispatcher.

No server-wide switches or sticky session state: those break concurrent
chats, transports with per-request MCP session ids, and tools/list caches.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Literal

from fastmcp import Client
from fastmcp.tools import ToolResult
from mcp.types import CallToolResult, Tool, ToolAnnotations
from pydantic import Field

ToolGroup = Literal["opss", "windows", "revit"]
REMOTE_GROUPS = frozenset({"windows", "revit"})
ALL_GROUPS = ("opss", "windows", "revit")

# Independent defense in depth: the Windows gateway also restricts the
# Revit namespace, because its native localhost TCP endpoint can execute C#.
# Revisit only after the in-process authorization lease has native QA.
READONLY_REVIT_TOOLS = frozenset(
    {"revit_get_project_info", "revit_get_phases", "revit_get_selected_elements"}
)
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
        target = " ".join(
            str(arguments.get(key) or "") for key in ("name", "executable", "args")
        )
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


def _visible_remote_tools(items: Sequence[Tool], group: ToolGroup) -> list[Tool]:
    if group not in REMOTE_GROUPS:
        return []
    return sorted(
        [
            item for item in items
            if item.name.startswith(group + "_")
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


def register_tool_groups(
    server: Any,
    *,
    transport_factory: Callable[[], Any] | None = None,
    local_opss_server: Any | None = None,
) -> None:
    """Register three stable MCP tools with optional private Opss dispatch."""

    async def fetch(group: ToolGroup) -> list[Tool]:
        if group == "opss":
            if local_opss_server is None:
                return []
            return sorted(
                [tool.to_mcp_tool() for tool in await local_opss_server.list_tools()],
                key=lambda item: item.name,
            )
        if transport_factory is None:
            return []
        async with Client(transport_factory(), timeout=20, init_timeout=5) as client:
            items = await client.list_tools()
        return _visible_remote_tools(items, group)

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
        if group == "revit" and name not in READONLY_REVIT_TOOLS:
            raise ValueError("Revit method not approved for QA use")
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        args = arguments or {}
        if group == "windows":
            _enforce_revit_launch_policy(name, args)
        max_bytes = MAX_OPSS_CALL_ARGS_BYTES if group == "opss" else MAX_CALL_ARGS_BYTES
        if len(json.dumps(args, ensure_ascii=False).encode("utf-8")) > max_bytes:
            raise ValueError("Tool arguments exceed group size limit")
        if group == "opss":
            if local_opss_server is None:
                raise ValueError("Opss native tools are already available in this profile")
            available = {tool.name for tool in await fetch("opss")}
            if name not in available:
                raise ValueError("Unknown or disabled Opss tool")
            # Preserve existing implementation, session identity and safety.
            # Outer public middleware owns telemetry/continuation only once.
            return await local_opss_server.call_tool(name, args, run_middleware=False)
        if transport_factory is None:
            raise ValueError(f"Group {group} is not connected")
        async with Client(transport_factory(), timeout=30, init_timeout=5) as client:
            available = _visible_remote_tools(await client.list_tools(), group)
            if name not in {item.name for item in available}:
                raise ValueError("Tool not published in selected remote group")
            result = await client.call_tool(name, args, timeout=25, raise_on_error=False)
        return _bounded_result(result)
