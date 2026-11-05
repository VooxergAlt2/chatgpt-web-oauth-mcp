"""Server-owned native tool profiles for the pinned Antigravity ACP runtime.

This provider extension limits advertised built-ins. Sandbox permissions, scoped
gateway reads and independent writer verification remain separate boundaries.
"""
from __future__ import annotations

import hashlib
import json

from .acp_session_store import AcpStateError


# Stable, versioned IDs: changing a tool set requires a new profile ID.
_PROFILE_TOOLS = {
    "opss-proposal-v1": ("finish",),
    "opss-explore-v1": ("finish", "view_file"),
    "opss-explore-questions-v1": ("ask_question", "finish", "view_file"),
    "opss-code-v1": ("create_file", "edit_file", "finish", "view_file"),
    "opss-code-questions-v1": ("ask_question", "create_file", "edit_file", "finish", "view_file"),
}
_PROFILE_FIELDS = frozenset({"version", "id", "sha256", "enabled_tools"})


def _binding(profile_id: str) -> dict:
    payload = {"version": 1, "id": profile_id,
               "enabled_tools": list(_PROFILE_TOOLS[profile_id])}
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "sha256": hashlib.sha256(raw).hexdigest()}


def _validated_profile(value: dict) -> dict:
    # Return a fresh canonical value; never retain an externally mutable list.
    if (not isinstance(value, dict) or set(value) != _PROFILE_FIELDS
            or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("id"), str) or value["id"] not in _PROFILE_TOOLS
            or not isinstance(value.get("sha256"), str)
            or not isinstance(value.get("enabled_tools"), list)
            or any(not isinstance(item, str) for item in value["enabled_tools"])):
        raise AcpStateError("acp_tool_profile_invalid")
    expected = _binding(value["id"])
    if value != expected:
        raise AcpStateError("acp_tool_profile_invalid")
    return expected


def native_tool_profile(*, writer: bool, questions: bool, proposal: bool = False) -> dict:
    """Select one fixed profile from trusted worker capabilities, not user input."""
    if type(writer) is not bool or type(questions) is not bool or type(proposal) is not bool:
        raise AcpStateError("acp_tool_profile_invalid")
    if proposal:
        if writer:
            raise AcpStateError("acp_tool_profile_invalid")
        return _binding("opss-proposal-v1")
    role = "code" if writer else "explore"
    suffix = "-questions" if questions else ""
    return _binding(f"opss-{role}{suffix}-v1")


def bind_native_tool_profile(state: dict, profile: dict) -> bool:
    """Check/bind the profile under the caller's existing session lease.

    The caller must durably save a new binding before opening a provider
    connection. Binding survives setup failure or a crash before session_id.
    Checkpoint version 2 makes older workers reject these sessions on downgrade;
    ownership identity and the separate version-1 control mailbox are unchanged.
    """
    expected = _validated_profile(profile)
    if not isinstance(state, dict) or type(state.get("version")) is not int:
        raise AcpStateError("acp_tool_profile_invalid")
    if "native_tool_profile" not in state:
        if state["version"] != 1:
            raise AcpStateError("acp_tool_profile_invalid")
        if state.get("session_id"):
            # An old conversation may have a broader persisted native surface.
            # It must never be silently reconfigured while continuing its history.
            raise AcpStateError("acp_tool_profile_legacy_requires_new_session")
        state["native_tool_profile"] = expected
        state["version"] = 2
        return True
    if state["version"] != 2:
        raise AcpStateError("acp_tool_profile_invalid")
    observed = _validated_profile(state["native_tool_profile"])
    if observed != expected:
        raise AcpStateError("acp_tool_profile_mismatch")
    return False


def profile_sdk_kwargs(profile: dict) -> dict:
    """SDK 0.12.1 **kwargs become wire _meta; an _meta kwarg double-nests it."""
    value = _validated_profile(profile)
    return {"antigravity": {"enabledTools": list(value["enabled_tools"])}}
