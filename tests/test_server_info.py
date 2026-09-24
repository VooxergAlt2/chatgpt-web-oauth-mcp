from __future__ import annotations

import asyncio

from chatgpt_web_oauth_mcp import config, server
from chatgpt_web_oauth_mcp.server import server_info


def _call() -> dict:
    fn = server_info.fn if hasattr(server_info, "fn") else server_info
    return asyncio.run(fn())


def test_server_info_reports_metadata_and_tools() -> None:
    assert server._tool_context.codex_runtime_default_sandbox == config.CODEX_RUNTIME_DEFAULT_SANDBOX
    assert server._tool_context.codex_runtime_cua_approval_mode == "interactive"
    assert server._tool_context.codex_runtime_cua_allowed_apps == frozenset()
    payload = _call()
    assert payload["success"] is True
    assert payload["app_name"] == "chatgpt-web-oauth-mcp"
    assert isinstance(payload["port"], int)
    assert isinstance(payload["workspace_root"], str)
    assert payload["auth"] in {"none", "shared_token", "oauth"}
    assert payload["command_timeout_seconds"] >= 1
    assert "delegate_timeout_seconds" not in payload
    assert "delegate_wait_timeout_seconds" not in payload
    assert "delegate_mode" not in payload

    runtime_info = payload["codex_runtime"]
    assert runtime_info["enabled"] is True
    assert runtime_info["process_status"] == "stopped"
    assert runtime_info["runtime_count"] >= 0
    assert runtime_info["binding_store"]["available"] is True
    assert runtime_info["default_sandbox"] == config.CODEX_RUNTIME_DEFAULT_SANDBOX
    assert runtime_info["computer_use_approval_mode"] == "interactive"
    assert runtime_info["computer_use_allowed_apps"] == []

    assert payload["routing_contract"]["chatgpt_web_role"] == "architect_manager_reviewer"
    assert payload["routing_contract"]["codex_runtime_role"] == "persistent_runtime_and_connected_mcp_access"
    assert payload["routing_contract"]["execution_loop"] == {
        "after_tool_result": "invoke_next_tool_or_return_checkpoint",
        "waiting_requires": "verified_observable_progress",
        "idle_state": "NEXT_ACTION_REQUIRED",
        "quiet_state": "QUIET_PROCESS_REQUIRES_RECHECK",
        "stalled_state": "STALLED_PROCESS_REQUIRES_INSPECTION",
        "recovery_tool": "execution_state",
        "activity_evidence": [
            "process_group_identity",
            "process_group_cpu_time_delta",
            "job_output_growth",
            "process_group_change",
        ],
    }
    assert payload["skill_guidance"] == {
        "discovery_tool": "get_skill_index",
        "index_resource": "skill://chatgpt-web-oauth-mcp/index",
        "guide_tools": {
            "delegate-use": "get_delegate_use",
            "file-use": "get_file_use",
            "process-use": "get_process_use",
            "runtime-use": "get_runtime_use",
            "git-use": "get_git_use",
        },
        "guide_resources": {
            "delegate-use": "skill://chatgpt-web-oauth-mcp/delegate-use",
            "file-use": "skill://chatgpt-web-oauth-mcp/file-use",
            "process-use": "skill://chatgpt-web-oauth-mcp/process-use",
            "runtime-use": "skill://chatgpt-web-oauth-mcp/runtime-use",
            "git-use": "skill://chatgpt-web-oauth-mcp/git-use",
        },
        "progressive_disclosure": True,
    }
    assert payload["resources"] == [
        "skill://chatgpt-web-oauth-mcp/delegate-use",
        "skill://chatgpt-web-oauth-mcp/file-use",
        "skill://chatgpt-web-oauth-mcp/git-use",
        "skill://chatgpt-web-oauth-mcp/index",
        "skill://chatgpt-web-oauth-mcp/process-use",
        "skill://chatgpt-web-oauth-mcp/runtime-use",
    ]
    assert payload["resource_count"] == len(payload["resources"])

    tools = payload["tools"]
    assert isinstance(tools, list)
    for name in [
        "server_info",
        "execution_state",
        "codex_runtime_open",
        "codex_runtime_list",
        "codex_runtime_acquire",
        "codex_runtime_resume",
        "codex_runtime_status",
        "codex_runtime_close",
        "codex_mcp_inventory",
        "codex_mcp_call",
        "delegate_task",
        "delegate_batch",
        "delegate_status",
        "delegate_cancel",
        "delegate_harnesses",
        "env_snapshot",
        "env_diff",
        "search",
        "read_text",
        "code_map_symbols",
        "code_map_references",
        "code_map_imports",
        "run_command",
        "apply_patch",
        "git_status",
        "git_show",
        "git_blame",
        "git_worktree_create",
        "git_worktree_list",
        "git_worktree_status",
        "git_worktree_remove",
        "tmux_list",
        "tmux_start",
        "tmux_status",
        "tmux_capture",
        "tmux_send",
        "tmux_kill",
        "get_skill_index",
        "get_delegate_use",
        "get_file_use",
        "get_process_use",
        "get_runtime_use",
        "get_git_use",
    ]:
        assert name in tools, f"expected {name} in tools list"

    for removed in [
        "codex_exec",
        "run_command_stream",
        "get_task",
        "wait_task",
        "cancel_task",
        "purge_tasks",
        "taskboard_create",
        "taskboard_delegate",
        "taskboard_status",
        "taskboard_collect_results",
        "list_skills",
    ]:
        assert removed not in tools, f"did not expect removed tool {removed}"

    assert {"codex", "claude", "antigravity", "pi"} <= set(payload["delegate_harnesses"])
    assert payload["delegate_default_harness"] == config.DELEGATE_DEFAULT_HARNESS
    assert payload["delegate_harnesses"]["claude"]["display_name"] == "Claude Code"
    assert payload["delegate_harnesses"]["antigravity"]["display_name"] == "Antigravity"
    assert payload["health_monitoring"] == {
        "endpoint": "/internal/health",
        "enabled": bool(config.HEALTH_TOKEN),
        "session_idle_ttl_seconds": config.SESSION_IDLE_TTL_SECONDS,
        "session_checkpoint_ttl_seconds": config.SESSION_CHECKPOINT_TTL_SECONDS,
        "session_active_window_seconds": config.SESSION_ACTIVE_WINDOW_SECONDS,
        "session_request_stall_seconds": config.SESSION_REQUEST_STALL_SECONDS,
        "session_orchestration_quiet_seconds": config.SESSION_ORCHESTRATION_QUIET_SECONDS,
        "session_limit": config.HEALTH_SESSION_LIMIT,
    }
    assert payload["tool_count"] == len(tools)
