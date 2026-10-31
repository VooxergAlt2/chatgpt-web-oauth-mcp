"""Central Node/native v1 contract. No PowerShell or raw TCP fallback."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Protocol

from fastmcp import Client

PROTOCOL_VERSION = 1
CAPABILITIES = frozenset({"document_context_v1", "workspace_queue_v1"})
IDENTITY_FIELDS = (
    "instance_id", "pid", "process_started_utc", "revit_version", "plugin_build_id", "port",
)


class WorkspaceBackend(Protocol):
    async def discover(self) -> dict[str, Any]: ...
    async def prepare(self, workspace_id: str, source_path: str) -> dict[str, Any]: ...
    async def dispatch(self, instance: dict[str, Any], operation: str,
                       payload: dict[str, Any]) -> dict[str, Any]: ...


def verified_identity(value: dict[str, Any]) -> dict[str, Any]:
    if type(value.get("protocol_version")) is not int or value.get("protocol_version") != PROTOCOL_VERSION or not CAPABILITIES.issubset(
        value.get("capabilities") or []
    ):
        raise ValueError("WORKSPACE_PROTOCOL_REQUIRED: native v1 document context and queue required")
    if any(value.get(key) in (None, "") for key in IDENTITY_FIELDS):
        raise ValueError("INSTANCE_IDENTITY_REQUIRED")
    if type(value["pid"]) is not int or value["pid"] <= 0 or type(value["port"]) is not int or not 1 <= value["port"] <= 65535:
        raise ValueError("INVALID_INSTANCE_IDENTITY")
    return {key: value[key] for key in IDENTITY_FIELDS}


class NodeWorkspaceBackend:
    """One bounded MCP call per operation; polling belongs to the caller.

    Node tools return direct JSON objects, in structuredContent (preferred) or
    a single JSON text block. hello returns the same identity shape as discover.
    bind/unbind return native request envelopes including request_id, even when
    asynchronous. Unknown/rejected responses are never retried automatically.
    """

    def __init__(self, transport_factory: Callable[[], Any] | None):
        self.transport_factory = transport_factory

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.transport_factory is None:
            raise ValueError("WORKSPACE_BACKEND_UNAVAILABLE")
        # The whole operation, including connect/init, stays below 25 seconds.
        async with asyncio.timeout(24):
            async with Client(self.transport_factory(), timeout=20, init_timeout=4) as client:
                result = await client.call_tool(name, arguments, timeout=20, raise_on_error=False)
        if result.is_error:
            raise ValueError("WORKSPACE_BACKEND_ERROR: " + str(result.content)[:500])
        value = result.structured_content
        if value is None and len(result.content) == 1 and result.content[0].type == "text":
            value = json.loads(result.content[0].text)
        if not isinstance(value, dict):
            raise ValueError("INVALID_WORKSPACE_BACKEND_RESULT")
        return value

    async def discover(self) -> dict[str, Any]:
        return await self._call("revit_workspace_discover", {})

    async def prepare(self, workspace_id: str, source_path: str) -> dict[str, Any]:
        return await self._call("revit_workspace_prepare", {
            "workspace_id": workspace_id, "source_path": source_path,
        })

    async def dispatch(self, instance: dict[str, Any], operation: str,
                       payload: dict[str, Any]) -> dict[str, Any]:
        result = await self._call("revit_workspace_dispatch", {
            "instance_id": instance["instance_id"], "port": instance["port"],
            "operation": operation, "payload": payload,
        })
        if operation == "hello":
            # Native hello identifies the process; port is the endpoint
            # verified by Node, never a port-file guess.
            result = dict(result)
            result.setdefault("port", instance["port"])
        return result
