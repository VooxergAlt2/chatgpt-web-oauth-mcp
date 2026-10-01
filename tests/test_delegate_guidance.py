from __future__ import annotations

import asyncio

from chatgpt_web_oauth_mcp.delegate_guidance import (
    CODE_GRAPH_USE_GUIDE,
    CODE_GRAPH_USE_URI,
    DELEGATE_USE_GUIDE,
    DELEGATE_USE_URI,
    FILE_USE_GUIDE,
    FILE_USE_URI,
    GIT_USE_GUIDE,
    GIT_USE_URI,
    PROCESS_USE_GUIDE,
    PROCESS_USE_URI,
    RUNTIME_USE_GUIDE,
    RUNTIME_USE_URI,
    SKILL_INDEX_URI,
    code_graph_use_payload,
    delegate_use_payload,
    file_use_payload,
    git_use_payload,
    process_use_payload,
    runtime_use_payload,
    skill_index_payload,
)


def test_skill_index_routes_agents_to_operating_guides() -> None:
    payload = skill_index_payload()

    assert payload["namespace"] == "chatgpt-web-oauth-mcp"
    assert payload["resource_uri"] == SKILL_INDEX_URI
    assert payload["discovery_tool"] == "get_guide"
    skills = {item["name"]: item for item in payload["skills"]}
    assert set(skills) == {
        "delegate-use",
        "file-use",
        "code-graph-use",
        "process-use",
        "runtime-use",
        "git-use",
    }
    for name, skill in skills.items():
        assert skill["guide_tool"] == "get_guide"
        assert skill["guide_args"] == {"name": name}

    required_tools = {
        tool
        for skill in skills.values()
        for tool in skill["required_before_tools"]
    }
    assert {
        "execution_state",
        "delegate_task",
        "delegate_batch",
        "delegate_status",
        "delegate_cancel",
        "delegate_harnesses",
        "list_files",
        "code_graph_prepare",
        "apply_patch",
        "run_command",
        "job_start",
        "tmux_start",
        "codex_runtime_acquire",
        "codex_runtime_list",
        "git_status",
        "git_worktree_remove",
    } <= required_tools


def test_delegate_use_guide_contains_harness_and_review_contracts() -> None:
    payload = delegate_use_payload()
    assert payload["success"] is True
    assert payload["resource_uri"] == DELEGATE_USE_URI
    assert payload["content"] == DELEGATE_USE_GUIDE
    for required in [
        "delegate_task",
        "delegate_batch",
        "delegate_status",
        "delegate_cancel",
        "delegate_harnesses",
        "Claude",
        "Antigravity",
        "independently review",
        "resume_from_delegate_id",
        "delegate_harnesses.runtime",
        "server_info.delegate_runtime",
        "antigravity2",
        "Quota thresholds are admission-only",
        "already-running or already-queued",
        "durable_execution=true",
        "recovered_from_disk=true",
    ]:
        assert required in DELEGATE_USE_GUIDE


def test_file_use_guide_contains_critical_operating_contracts() -> None:
    payload = file_use_payload()
    assert payload["success"] is True
    assert payload["resource_uri"] == FILE_USE_URI
    assert payload["content"] == FILE_USE_GUIDE
    for required in [
        "list_files",
        "next_offset",
        "encoding_error",
        "mode=hex",
        "apply_patch",
        "expected_revision",
        "revision_conflict",
        "dry_run=true",
        "rolled_back",
    ]:
        assert required in FILE_USE_GUIDE


def test_code_graph_use_guide_contains_semantic_and_safety_contracts() -> None:
    payload = code_graph_use_payload()
    assert payload["success"] is True
    assert payload["resource_uri"] == CODE_GRAPH_USE_URI
    assert payload["content"] == CODE_GRAPH_USE_GUIDE
    for required in [
        "code_graph_status",
        "code_graph_prepare",
        "code_graph_callers",
        "code_graph_callees",
        "code_graph_impact",
        "code_graph_diff_impact",
        "code_graph_path",
        "committed Git tree",
        "Dirty and untracked",
        "ambiguous",
        "full_name",
        "Query tools never auto-build",
        "Semantic query tools never cold-start Joern",
        "code_graph_runtime_not_ready",
        "query_runtime_ready=true",
        "bounded 120s",
        "network=none",
        "expose no host port",
        "manifest v2",
        "CPG SHA-256",
        "memoizes validation by bounded stat identity",
        "unchanged warm queries do not reread the full CPG",
        "actual Docker contract",
        "JOERN_QUERY_SERVER_MAX_CONTAINERS",
        "reconcile orphaned Joern servers",
        "cleaned up strictly on normal MCP lifespan shutdown",
        "runtime acceptance",
        "<operator>.* noise are filtered by default",
        "call_resolution_complete",
        "unresolved_call_sites",
        "methodFullName=<unknownFullName>",
        "not asserted edges",
        "dynamic dispatch",
        "use_merge_base=true",
        "base_graph_required",
        "module_scope_changes",
        "pagination_complete=true",
        "Canonical startup sequence is always",
        "query_ready=false",
        "await_job(job_id)",
        "query_ready=true",
        "do not search for a shell command or call Joern directly",
        "code_graph_prepare is the canonical graph/runtime launcher",
    ]:
        assert required in CODE_GRAPH_USE_GUIDE


def test_process_use_guide_contains_critical_operating_contracts() -> None:
    payload = process_use_payload()
    assert payload["success"] is True
    assert payload["resource_uri"] == PROCESS_USE_URI
    assert payload["content"] == PROCESS_USE_GUIDE
    for required in [
        "execution_state",
        "NEXT_ACTION_REQUIRED",
        "activity_verdict=QUIET",
        "activity_verdict=STALLED_SUSPECTED",
        "Never auto-kill",
        "run_command",
        "job_start",
        "job_output",
        "next_cursor",
        "tmux_start",
        "accepted_by_tmux=true",
        "force=true",
        "explicit user-approved",
        "shared foreground wall-clock budget",
        "900 seconds",
        "default of 300 seconds",
    ]:
        assert required in PROCESS_USE_GUIDE


def test_git_use_guide_contains_critical_operating_contracts() -> None:
    payload = git_use_payload()
    assert payload["success"] is True
    assert payload["resource_uri"] == GIT_USE_URI
    assert payload["content"] == GIT_USE_GUIDE
    for required in [
        "git_status",
        "git_diff(staged=true",
        "stage_all=true",
        "amend=true",
        "git_worktree_create",
        "git_worktree_remove(force=true)",
        "worktree_dirty",
    ]:
        assert required in GIT_USE_GUIDE


def test_runtime_use_guide_contains_reuse_and_gc_contracts() -> None:
    payload = runtime_use_payload()
    assert payload["success"] is True
    assert payload["resource_uri"] == RUNTIME_USE_URI
    assert payload["content"] == RUNTIME_USE_GUIDE
    for required in [
        "codex_runtime_acquire",
        "codex_runtime_list",
        "stable name",
        "8-hour",
        "LRU",
        "Capacity LRU never evicts a `ready` runtime",
        "runtime_limit_reached",
    ]:
        assert required in RUNTIME_USE_GUIDE


def test_skill_resources_share_the_same_authoritative_content() -> None:
    from chatgpt_web_oauth_mcp.server import mcp

    async def scenario() -> None:
        resources = await mcp.list_resources()
        resource_uris = {str(resource.uri) for resource in resources}
        assert {
            SKILL_INDEX_URI,
            CODE_GRAPH_USE_URI,
            DELEGATE_USE_URI,
            FILE_USE_URI,
            PROCESS_USE_URI,
            RUNTIME_USE_URI,
            GIT_USE_URI,
        } <= resource_uris

        for uri, expected in [
            (CODE_GRAPH_USE_URI, CODE_GRAPH_USE_GUIDE),
            (DELEGATE_USE_URI, DELEGATE_USE_GUIDE),
            (FILE_USE_URI, FILE_USE_GUIDE),
            (PROCESS_USE_URI, PROCESS_USE_GUIDE),
            (RUNTIME_USE_URI, RUNTIME_USE_GUIDE),
            (GIT_USE_URI, GIT_USE_GUIDE),
        ]:
            guide = await mcp.read_resource(uri)
            assert guide.contents[0].content == expected

    asyncio.run(scenario())
