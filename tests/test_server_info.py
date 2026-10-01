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
    assert payload["tool_profile"] == {"name": "full", "hidden_tools": []}
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
    assert payload["code_graph"] == {
        "enabled": config.CODE_GRAPH_ENABLED,
        "backend": "joern-docker",
        "query_runtime": "persistent-rest",
        "joern_version": config.JOERN_VERSION,
        "joern_image": config.JOERN_IMAGE,
        "build_timeout_seconds": config.JOERN_BUILD_TIMEOUT_SECONDS,
        "query_timeout_seconds": config.JOERN_QUERY_TIMEOUT_SECONDS,
        "query_server_start_timeout_seconds": config.JOERN_QUERY_SERVER_START_TIMEOUT_SECONDS,
        "query_server_max_containers": config.JOERN_QUERY_SERVER_MAX_CONTAINERS,
        "query_server_network": "none",
        "query_server_host_port_exposed": False,
        "cache_max_bytes": config.CODE_GRAPH_CACHE_MAX_BYTES,
        "cache_max_graphs": config.CODE_GRAPH_CACHE_MAX_GRAPHS,
        "identity_kind": "committed_git_tree",
        "query_requires_ready_cache": True,
        "working_tree_included": False,
    }

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
        "discovery_tool": "get_guide",
        "index_resource": "skill://chatgpt-web-oauth-mcp/index",
        "guide_tools": {
            "code-graph-use": "get_guide",
            "delegate-use": "get_guide",
            "file-use": "get_guide",
            "process-use": "get_guide",
            "runtime-use": "get_guide",
            "git-use": "get_guide",
        },
        "compatibility_tools": {
            "code-graph-use": "get_code_graph_use",
        },
        "guide_resources": {
            "code-graph-use": "skill://chatgpt-web-oauth-mcp/code-graph-use",
            "delegate-use": "skill://chatgpt-web-oauth-mcp/delegate-use",
            "file-use": "skill://chatgpt-web-oauth-mcp/file-use",
            "process-use": "skill://chatgpt-web-oauth-mcp/process-use",
            "runtime-use": "skill://chatgpt-web-oauth-mcp/runtime-use",
            "git-use": "skill://chatgpt-web-oauth-mcp/git-use",
        },
        "progressive_disclosure": True,
    }
    assert payload["resources"] == [
        "skill://chatgpt-web-oauth-mcp/code-graph-use",
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
        "code_graph_status",
        "code_graph_prepare",
        "code_graph_callers",
        "code_graph_callees",
        "code_graph_impact",
        "code_graph_diff_impact",
        "code_graph_path",
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
        "get_guide",
        "get_code_graph_use",
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
        "get_skill_index",
        "get_delegate_use",
        "get_file_use",
        "get_process_use",
        "get_runtime_use",
        "get_git_use",
    ]:
        assert removed not in tools, f"did not expect removed tool {removed}"

    assert {"codex", "claude", "antigravity", "pi"} <= set(payload["delegate_harnesses"])
    assert payload["delegate_default_harness"] == config.DELEGATE_DEFAULT_HARNESS
    assert payload["delegate_harnesses"]["claude"]["display_name"] == "Claude Code"
    assert payload["delegate_harnesses"]["antigravity"]["display_name"] == "Antigravity"
    assert payload["delegate_harnesses"]["antigravity"]["durable_execution"] is True
    assert payload["delegate_harnesses"]["antigravity"]["durability_backend"] == "job_registry"
    assert payload["delegate_harnesses"]["codex"]["durable_execution"] is False
    assert payload["delegate_runtime"]["status"] in {"ready", "shutting_down"}
    assert payload["delegate_runtime"]["durable"]["backend"] == "job_registry"
    assert "tasks" in payload["delegate_runtime"]
    assert "groups" in payload["delegate_runtime"]
    assert payload["health_monitoring"] == {
        "endpoint": "/internal/health",
        "enabled": bool(config.HEALTH_TOKEN),
        "session_idle_ttl_seconds": config.SESSION_IDLE_TTL_SECONDS,
        "session_ephemeral_idle_ttl_seconds": config.SESSION_EPHEMERAL_IDLE_TTL_SECONDS,
        "session_checkpoint_ttl_seconds": config.SESSION_CHECKPOINT_TTL_SECONDS,
        "session_active_window_seconds": config.SESSION_ACTIVE_WINDOW_SECONDS,
        "session_request_stall_seconds": config.SESSION_REQUEST_STALL_SECONDS,
        "session_orchestration_quiet_seconds": config.SESSION_ORCHESTRATION_QUIET_SECONDS,
        "session_limit": config.HEALTH_SESSION_LIMIT,
    }
    assert payload["tool_count"] == len(tools)
