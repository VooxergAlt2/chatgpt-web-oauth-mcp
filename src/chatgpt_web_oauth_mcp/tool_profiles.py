from __future__ import annotations

from typing import Any


FULL_TOOL_PROFILE = "full"
LEAN_TOOL_PROFILE = "lean"
VALID_TOOL_PROFILES = frozenset({FULL_TOOL_PROFILE, LEAN_TOOL_PROFILE})

LEAN_HIDDEN_TOOL_NAMES = frozenset(
    {
        "codex_runtime_open",
        "codex_runtime_list",
        "codex_runtime_acquire",
        "codex_runtime_resume",
        "codex_runtime_status",
        "codex_runtime_close",
        "codex_mcp_inventory",
        "codex_mcp_call",
        "tmux_list",
        "tmux_start",
        "tmux_status",
        "tmux_capture",
        "tmux_send",
        "tmux_kill",
    }
)


def normalize_tool_profile(value: str | None) -> str:
    normalized = str(value or FULL_TOOL_PROFILE).strip().lower() or FULL_TOOL_PROFILE
    if normalized not in VALID_TOOL_PROFILES:
        allowed = ", ".join(sorted(VALID_TOOL_PROFILES))
        raise ValueError(
            f"CHATGPT_MCP_TOOL_PROFILE must be one of: {allowed}; got {value!r}."
        )
    return normalized


def hidden_tool_names(profile: str) -> frozenset[str]:
    normalized = normalize_tool_profile(profile)
    if normalized == LEAN_TOOL_PROFILE:
        return LEAN_HIDDEN_TOOL_NAMES
    return frozenset()


def apply_tool_profile(mcp: Any, profile: str) -> dict[str, object]:
    normalized = normalize_tool_profile(profile)
    hidden = hidden_tool_names(normalized)
    if hidden:
        mcp.disable(names=set(hidden), components={"tool"})
    return {
        "name": normalized,
        "hidden_tools": sorted(hidden),
    }
