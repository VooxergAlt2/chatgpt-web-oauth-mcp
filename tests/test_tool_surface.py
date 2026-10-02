from __future__ import annotations

import json
from pathlib import Path

import tiktoken
from fastmcp.tools.base import ToolResult

from chatgpt_web_oauth_mcp.tool_surface import (
    ToolUsageStore,
    _tool_result_success,
    tool_schema_footprint,
)


class _WireTool:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def model_dump(self, *, by_alias: bool, exclude_none: bool) -> dict[str, object]:
        assert by_alias is True
        assert exclude_none is True
        return dict(self.payload)


class _Tool:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def to_mcp_tool(self) -> _WireTool:
        return _WireTool(self.payload)


def test_tool_schema_footprint_measures_exact_wire_json() -> None:
    payloads = [
        {
            "name": "small",
            "description": "small tool",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "large",
            "description": "large " * 50,
            "inputSchema": {
                "type": "object",
                "properties": {"value": {"type": "string", "description": "value " * 20}},
            },
        },
    ]
    result = tool_schema_footprint([_Tool(item) for item in payloads])
    encoding = tiktoken.get_encoding("o200k_base")
    expected_tokens = 0
    expected_bytes = 0
    for payload in payloads:
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        expected_tokens += len(encoding.encode(rendered))
        expected_bytes += len(rendered.encode("utf-8"))

    assert result["tool_count"] == 2
    assert result["total_tokens"] == expected_tokens
    assert result["total_bytes"] == expected_bytes
    assert [item["name"] for item in result["tools"]] == ["large", "small"]


def test_tool_usage_store_records_only_aggregates(tmp_path: Path) -> None:
    path = tmp_path / "tool-usage.json"
    store = ToolUsageStore(lambda: path)

    store.record(tool_name="search", success=True, latency_ms=12.5)
    store.record(
        tool_name="read_text",
        success=False,
        latency_ms=7.5,
        previous_tool="search",
    )

    snapshot = store.snapshot()
    raw = path.read_text(encoding="utf-8")
    assert "arguments" not in raw
    assert "payload" not in raw
    assert "session_id" not in raw
    assert snapshot["total_calls"] == 2
    assert snapshot["total_errors"] == 1
    assert [item["name"] for item in snapshot["tools"]] == ["read_text", "search"]
    read_row = next(item for item in snapshot["tools"] if item["name"] == "read_text")
    assert read_row["calls"] == 1
    assert read_row["errors"] == 1
    assert read_row["avg_latency_ms"] == 7.5
    assert snapshot["transitions"] == [
        {"from": "search", "to": "read_text", "count": 1}
    ]


def test_tool_result_success_counts_structured_success_false_as_error() -> None:
    assert _tool_result_success(ToolResult(structured_content={"success": True})) is True
    assert _tool_result_success(ToolResult(structured_content={"success": False})) is False
    assert (
        _tool_result_success(
            ToolResult(structured_content={"success": True}, is_error=True)
        )
        is False
    )
