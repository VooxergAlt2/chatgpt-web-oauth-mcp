from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field

from . import session
from .delegate_guidance import (
    CODE_GRAPH_USE_URI,
    DELEGATE_USE_URI,
    FILE_USE_URI,
    GIT_USE_URI,
    PROCESS_USE_URI,
    RUNTIME_USE_URI,
    SKILL_INDEX_URI,
)
from .envtools import env_diff as env_diff_impl
from .envtools import env_snapshot as env_snapshot_impl
from .pathing import resolve_cwd, resolve_path
from .session_checkpoints import POLL_REQUIRED, RESULT_REQUIRES_CONSUMPTION
from .session_continuation import (
    foreign_owned_result_ids,
    owns_result,
    refresh_session_owned_work,
    summarize_abandoned_checkpoint,
)
from .tmux_ops import TmuxClient, tmux_runtime_info
from .tool_context import LOCAL_STATE_TOOL, READ_ONLY_TOOL, ToolContext
from .tool_surface import tool_schema_footprint


def register_core_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register server metadata and cwd tools."""

    @mcp.tool(
        name="server_info",
        title="Server Info",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return server metadata: app name, host/port, workspace root, state dir, "
            "timeouts, auth mode, and registered tools/resources. Useful as a first "
            "call to confirm which bridge you are connected to and what it can do."
        ),
    )
    async def server_info() -> dict[str, object]:
        registered = await mcp.list_tools()
        tools = sorted(tool.name for tool in registered)
        footprint = tool_schema_footprint(registered)
        usage = ctx.tool_usage_store.snapshot() if ctx.tool_usage_store is not None else None
        footprint_rows = footprint.get("tools")
        usage_rows = usage.get("tools") if isinstance(usage, dict) else None
        transition_rows = usage.get("transitions") if isinstance(usage, dict) else None
        registered_resources = await mcp.list_resources()
        resource_uris = sorted(str(resource.uri) for resource in registered_resources)
        session_cwd = session.get_default_cwd()
        return {
            "success": True,
            "app_name": ctx.app_name,
            "host": ctx.host,
            "port": ctx.port,
            "workspace_root": str(ctx.workspace_root),
            "session_cwd": str(session_cwd) if session_cwd else None,
            "state_dir": str(ctx.state_dir),
            "tool_profile": {
                "name": str(ctx.global_value("TOOL_PROFILE", "full")),
                "hidden_tools": list(
                    ctx.global_value("TOOL_PROFILE_HIDDEN_TOOLS", ())
                ),
            },
            "command_timeout_seconds": ctx.command_timeout,
            "openai_foreground_timeout_seconds": ctx.openai_foreground_timeout,
            "auth": ctx.current_oauth_config().normalized_auth_mode,
            "debug_mcp_logging": ctx.debug_mcp_logging,
            "health_monitoring": {
                "endpoint": "/internal/health",
                "enabled": bool(ctx.global_value("HEALTH_TOKEN", "")),
                "session_idle_ttl_seconds": int(
                    ctx.global_value("SESSION_IDLE_TTL_SECONDS", 86400)
                ),
                "session_ephemeral_idle_ttl_seconds": int(
                    ctx.global_value("SESSION_EPHEMERAL_IDLE_TTL_SECONDS", 300)
                ),
                "session_checkpoint_ttl_seconds": int(
                    ctx.global_value("SESSION_CHECKPOINT_TTL_SECONDS", 86400)
                ),
                "session_active_window_seconds": int(
                    ctx.global_value("SESSION_ACTIVE_WINDOW_SECONDS", 600)
                ),
                "session_request_stall_seconds": int(
                    ctx.global_value("SESSION_REQUEST_STALL_SECONDS", 180)
                ),
                "session_orchestration_quiet_seconds": int(
                    ctx.global_value("SESSION_ORCHESTRATION_QUIET_SECONDS", 180)
                ),
                "session_limit": int(ctx.global_value("HEALTH_SESSION_LIMIT", 20)),
            },
            "codex_command": ctx.codex_command,
            "pi_command": ctx.pi_command,
            "delegate_harnesses": ctx.registry.harness_info(),
            "delegate_routing": ctx.registry.routing_guidance(),
            "delegate_runtime": ctx.registry.runtime_info(),
            "delegate_default_harness": ctx.delegate_default_harness,
            "codex_runtime": (
                {
                    **ctx.codex_runtime_manager.info(),
                    "default_sandbox": ctx.codex_runtime_default_sandbox,
                    "computer_use_approval_mode": ctx.codex_runtime_cua_approval_mode,
                    "computer_use_allowed_apps": sorted(ctx.codex_runtime_cua_allowed_apps),
                }
                if ctx.codex_runtime_manager is not None
                else {
                    "enabled": False,
                    "default_sandbox": ctx.codex_runtime_default_sandbox,
                    "computer_use_approval_mode": ctx.codex_runtime_cua_approval_mode,
                    "computer_use_allowed_apps": sorted(ctx.codex_runtime_cua_allowed_apps),
                }
            ),
            "tmux": tmux_runtime_info(
                binary=ctx.tmux_binary,
                socket_name=ctx.tmux_socket_name,
            ),
            "code_graph": {
                "enabled": bool(ctx.global_value("CODE_GRAPH_ENABLED", True)),
                "backend": "joern-docker",
                "query_runtime": "persistent-rest",
                "joern_version": str(ctx.global_value("JOERN_VERSION", "")),
                "joern_image": str(ctx.global_value("JOERN_IMAGE", "")),
                "build_timeout_seconds": int(
                    ctx.global_value("JOERN_BUILD_TIMEOUT_SECONDS", 1800)
                ),
                "query_timeout_seconds": int(
                    ctx.global_value("JOERN_QUERY_TIMEOUT_SECONDS", 20)
                ),
                "query_server_start_timeout_seconds": int(
                    ctx.global_value("JOERN_QUERY_SERVER_START_TIMEOUT_SECONDS", 120)
                ),
                "query_server_max_containers": int(
                    ctx.global_value("JOERN_QUERY_SERVER_MAX_CONTAINERS", 1)
                ),
                "query_server_network": "none",
                "query_server_host_port_exposed": False,
                "cache_max_bytes": int(
                    ctx.global_value("CODE_GRAPH_CACHE_MAX_BYTES", 5 * 1024 * 1024 * 1024)
                ),
                "cache_max_graphs": int(
                    ctx.global_value("CODE_GRAPH_CACHE_MAX_GRAPHS", 8)
                ),
                "identity_kind": "committed_git_tree",
                "query_requires_ready_cache": True,
                "working_tree_included": False,
            },
            "routing_contract": {
                "chatgpt_web_role": "architect_manager_reviewer",
                "codex_runtime_role": "persistent_runtime_and_connected_mcp_access",
                "execution_loop": {
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
                },
                "default_flow": [
                    "ChatGPT Web inspects and reasons with direct MCP tools.",
                    "Use delegate_* for bounded independent agent exploration/review or isolated implementation slices.",
                    "Use codex_runtime_* and codex_mcp_* when persistent Codex runtime access is needed.",
                    "Use direct file, process, and Git tools for deterministic local operations.",
                    "Always independently review delegate diffs, logs, and verification before accepting agent work.",
                ],
            },
            "skill_guidance": {
                "discovery_tool": "get_guide",
                "index_resource": SKILL_INDEX_URI,
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
                    "code-graph-use": CODE_GRAPH_USE_URI,
                    "delegate-use": DELEGATE_USE_URI,
                    "file-use": FILE_USE_URI,
                    "process-use": PROCESS_USE_URI,
                    "runtime-use": RUNTIME_USE_URI,
                    "git-use": GIT_USE_URI,
                },
                "progressive_disclosure": True,
            },
            "resources": resource_uris,
            "resource_count": len(resource_uris),
            "tools": tools,
            "tool_count": len(tools),
            "tool_surface": {
                "schema": {
                    "encoding": footprint["encoding"],
                    "tool_count": footprint["tool_count"],
                    "total_tokens": footprint["total_tokens"],
                    "total_bytes": footprint["total_bytes"],
                    "largest_tools": (
                        footprint_rows[:10] if isinstance(footprint_rows, list) else []
                    ),
                },
                "usage": (
                    {
                        "path": usage.get("path"),
                        "updated_at": usage.get("updated_at"),
                        "total_calls": usage.get("total_calls"),
                        "total_errors": usage.get("total_errors"),
                        "most_used_tools": (
                            usage_rows[:10] if isinstance(usage_rows, list) else []
                        ),
                        "top_transitions": (
                            transition_rows[:10]
                            if isinstance(transition_rows, list)
                            else []
                        ),
                        "arguments_recorded": False,
                        "payloads_recorded": False,
                        "session_ids_recorded": False,
                    }
                    if isinstance(usage, dict)
                    else None
                ),
            },
        }

    @mcp.tool(
        name="execution_state",
        title="Execution State",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return a bounded server-side execution-loop snapshot for durable jobs, delegated CLI agents, and "
            "tmux sessions scoped to the effective current working directory. Durable jobs are classified from verified process-group, "
            "CPU-time, output-growth, and repeated-observation evidence as ACTIVE, QUIET, STALLED_SUSPECTED, "
            "DEAD, TERMINAL, or UNKNOWN. Global activity is diagnostic only and never justifies waiting for the "
            "current workflow. state=NEXT_ACTION_REQUIRED means immediately invoke the next concrete tool or "
            "return a checkpoint/final/blocker response."
        ),
    )
    def execution_state(
        cwd: Annotated[
            str | None,
            Field(
                description=(
                    "Workflow working directory to scope activity to. Defaults to the session cwd, then the "
                    "workspace root. Jobs, delegates, and tmux sessions from other cwd values are diagnostic only."
                )
            ),
        ] = None,
    ) -> dict[str, object]:
        effective_cwd = resolve_cwd(cwd, ctx.workspace_root).resolve(strict=False)
        session_cwd = session.get_default_cwd()
        jobs = ctx.job_registry.list_jobs(
            state_dir=ctx.state_dir,
            status="running",
            offset=0,
            limit=200,
            max_tokens=ctx.tool_output_token_budget,
            exclude_job_ids=foreign_owned_result_ids(ctx, kind="job"),
        )
        tmux = TmuxClient(
            binary=ctx.tmux_binary,
            socket_name=ctx.tmux_socket_name,
            timeout=ctx.tmux_control_timeout,
        ).list_sessions(include_panes=True)
        delegates = ctx.registry.delegate_status(
            project_cwd=effective_cwd,
            limit=20,
            watch_seconds=0,
            max_tokens=ctx.tool_output_token_budget,
            exclude_delegate_ids=foreign_owned_result_ids(ctx, kind="delegate"),
        )

        jobs_ok = bool(jobs.get("success"))
        jobs_truncated = jobs_ok and bool(jobs.get("truncated"))
        jobs_observation_ok = jobs_ok and not jobs_truncated
        tmux_ok = bool(tmux.get("success"))
        delegates_ok = bool(delegates.get("success"))
        all_running_jobs = jobs.get("jobs", []) if jobs_ok else []
        all_tmux_sessions = tmux.get("sessions", []) if tmux_ok else []

        def same_cwd(raw_path: object) -> bool:
            if not isinstance(raw_path, str) or not raw_path:
                return False
            return Path(raw_path).expanduser().resolve(strict=False) == effective_cwd

        running_jobs = [job for job in all_running_jobs if same_cwd(job.get("cwd"))]

        def scoped_tmux_panes(item: object) -> list[dict[str, object]]:
            if not isinstance(item, dict):
                return []
            panes = item.get("panes", [])
            if not isinstance(panes, list):
                return []
            return [
                pane
                for pane in panes
                if isinstance(pane, dict) and same_cwd(pane.get("current_path"))
            ]

        tmux_sessions = [item for item in all_tmux_sessions if scoped_tmux_panes(item)]
        live_tmux_sessions = [
            item
            for item in tmux_sessions
            if any(not bool(pane.get("pane_dead")) for pane in scoped_tmux_panes(item))
        ]

        project_delegate = delegates.get("project") if delegates_ok else None
        active_delegates = (
            project_delegate.get("active", [])
            if isinstance(project_delegate, dict) and isinstance(project_delegate.get("active"), list)
            else []
        )
        delegate_activity_states = {
            str(item.get("activity_state"))
            for item in active_delegates
            if isinstance(item, dict)
        }

        job_activity: list[dict[str, object]] = []
        for job in running_jobs:
            snapshot = ctx.job_registry.job_activity_snapshot(
                job_id=str(job.get("job_id") or ""),
                state_dir=ctx.state_dir,
            )
            assessment = ctx.activity_tracker.assess(snapshot)
            job_activity.append(
                {
                    "job_id": snapshot.get("job_id") or job.get("job_id"),
                    "name": snapshot.get("name") or job.get("name"),
                    "status": snapshot.get("status") or job.get("status"),
                    "pid": snapshot.get("pid"),
                    "pgid": snapshot.get("pgid"),
                    "process_group_member_count": snapshot.get("process_group_member_count"),
                    "process_group_cpu_seconds": snapshot.get("process_group_cpu_seconds"),
                    "stdout_bytes": snapshot.get("stdout_bytes"),
                    "stderr_bytes": snapshot.get("stderr_bytes"),
                    "last_output_at": snapshot.get("last_output_at"),
                    **assessment,
                }
            )

        verdicts = {str(item.get("verdict")) for item in job_activity}
        delegate_stalled = "suspected_stalled" in delegate_activity_states
        delegate_active = "active" in delegate_activity_states
        delegate_quiet = "starting_or_quiet" in delegate_activity_states
        delegate_queued = "queued" in delegate_activity_states

        if "STALLED_SUSPECTED" in verdicts or delegate_stalled:
            state = (
                "STALLED_DELEGATE_REQUIRES_INSPECTION"
                if delegate_stalled
                else "STALLED_PROCESS_REQUIRES_INSPECTION"
            )
            activity_verdict = "STALLED_SUSPECTED"
            waiting_justified = False
            required_action = (
                "INSPECT_DELEGATE_STATUS_LOGS_OR_CONTINUE_INDEPENDENT_WORK"
                if delegate_stalled
                else "INSPECT_STALLED_PROCESS_OR_CONTINUE_INDEPENDENT_WORK"
            )
        elif "UNKNOWN" in verdicts:
            state = "ACTIVITY_UNKNOWN"
            activity_verdict = "UNKNOWN"
            waiting_justified = False
            required_action = "RESOLVE_ACTIVITY_UNCERTAINTY"
        elif "DEAD" in verdicts:
            state = "DEAD_PROCESS_REQUIRES_RECONCILIATION"
            activity_verdict = "DEAD"
            waiting_justified = False
            required_action = "RECHECK_JOB_STATUS_OR_CONTINUE"
        elif "ACTIVE" in verdicts or delegate_active:
            state = "ACTIVE_DELEGATE" if delegate_active else "ACTIVE_PROCESS"
            activity_verdict = "ACTIVE"
            waiting_justified = True
            required_action = (
                "POLL_DELEGATE_STATUS"
                if delegate_active
                else "POLL_OR_INSPECT_ACTIVE_PROCESS"
            )
        elif "QUIET" in verdicts or delegate_quiet:
            state = (
                "QUIET_DELEGATE_REQUIRES_RECHECK"
                if delegate_quiet
                else "QUIET_PROCESS_REQUIRES_RECHECK"
            )
            activity_verdict = "QUIET"
            waiting_justified = False
            required_action = (
                "RECHECK_DELEGATE_STATUS_OR_INSPECT_LOGS"
                if delegate_quiet
                else "RECHECK_OR_INSPECT_QUIET_PROCESS"
            )
        elif delegate_queued:
            state = "DELEGATE_QUEUED_REQUIRES_STATUS"
            activity_verdict = "QUEUED"
            waiting_justified = False
            required_action = "POLL_DELEGATE_STATUS"
        elif "TERMINAL" in verdicts:
            state = "TERMINAL_PROCESS_REQUIRES_NEXT_ACTION"
            activity_verdict = "TERMINAL"
            waiting_justified = False
            required_action = "CONTINUE_AFTER_TERMINAL_JOB"
        elif live_tmux_sessions:
            state = "INTERACTIVE_SESSION_REQUIRES_INSPECTION"
            activity_verdict = "INTERACTIVE"
            waiting_justified = False
            required_action = "INSPECT_TMUX_STATUS_AND_CAPTURE"
        elif not jobs_observation_ok or not tmux_ok or not delegates_ok:
            state = "ACTIVITY_UNKNOWN"
            activity_verdict = "UNKNOWN"
            waiting_justified = False
            required_action = "RESOLVE_ACTIVITY_UNCERTAINTY"
        else:
            state = "NEXT_ACTION_REQUIRED"
            activity_verdict = "IDLE"
            waiting_justified = False
            required_action = "INVOKE_NEXT_TOOL_OR_RETURN_CHECKPOINT"

        payload = {
            "success": jobs_observation_ok and tmux_ok and delegates_ok,
            "state": state,
            "activity_verdict": activity_verdict,
            "waiting_justified": waiting_justified,
            "required_action": required_action,
            "scope_cwd": str(effective_cwd),
            "scope_source": "explicit" if cwd else ("session" if session_cwd is not None else "workspace_root"),
            "running_jobs": running_jobs,
            "running_job_count": len(running_jobs),
            "job_activity": job_activity,
            "activity_policy": ctx.activity_tracker.policy(),
            "active_delegates": active_delegates,
            "active_delegate_count": len(active_delegates),
            "delegate_project_status": (
                project_delegate.get("status") if isinstance(project_delegate, dict) else None
            ),
            "tmux_sessions": tmux_sessions,
            "tmux_session_count": len(tmux_sessions),
            "live_tmux_session_count": len(live_tmux_sessions),
            "global_running_job_count": int(jobs.get("total", len(all_running_jobs))) if jobs_ok else 0,
            "global_tmux_session_count": len(all_tmux_sessions),
            "observation_errors": {
                "jobs": (
                    jobs.get("error")
                    if not jobs_ok
                    else (
                        {
                            "code": "job_list_truncated",
                            "message": "Running job registry snapshot was truncated; scoped activity may be omitted.",
                            "returned": len(all_running_jobs),
                            "total": jobs.get("total"),
                        }
                        if jobs_truncated
                        else None
                    )
                ),
                "delegates": delegates.get("error") if not delegates_ok else None,
                "tmux": tmux.get("error") if not tmux_ok else None,
            },
            "contract": (
                "Do not stop in reasoning after a tool result. Waiting requires verified observable progress. "
                "QUIET requires recheck or inspection, delegated work must be followed through delegate_status/logs, "
                "STALLED_SUSPECTED requires process/resource/log inspection or independent work, and NEXT_ACTION_REQUIRED requires another concrete tool call or a "
                "checkpoint/final/blocker response in the current turn."
            ),
        }
        session.registry.note_execution_state(
            required_action=required_action,
            state=state,
        )
        session_key = session.get_current_session_id()
        if session_key:
            checkpoint = ctx.checkpoint_store.get(session_key)
            if not isinstance(checkpoint, dict):
                checkpoint = {}
            runtime_jobs = {
                str(item.get("job_id")): {
                    "status": item.get("status"),
                    "name": item.get("name"),
                    "exit_code": item.get("exit_code"),
                    "success": item.get("success"),
                    "terminal": False,
                }
                for item in running_jobs
                if isinstance(item, dict) and item.get("job_id")
                and owns_result(
                    checkpoint,
                    kind="job",
                    result_id=str(item.get("job_id")),
                )
            }
            runtime_delegates = {
                str(item.get("delegate_id")): {
                    "status": item.get("status"),
                    "completed": item.get("completed"),
                    "success": item.get("success"),
                    "harness": item.get("harness"),
                    "activity_state": item.get("activity_state"),
                    "terminal": False,
                }
                for item in active_delegates
                if isinstance(item, dict) and item.get("delegate_id")
                and owns_result(
                    checkpoint,
                    kind="delegate",
                    result_id=str(item.get("delegate_id")),
                )
            }
            try:
                ctx.checkpoint_store.record_runtime(
                    session_key=session_key,
                    last_tool="execution_state",
                    cwd=str(effective_cwd),
                    jobs=runtime_jobs,
                    delegates=runtime_delegates,
                    next_action=required_action,
                )
            except (OSError, TypeError, ValueError) as exc:
                payload["resume_checkpoint_warning"] = (
                    "Automatic resume checkpoint could not be updated: "
                    f"{type(exc).__name__}: {exc}"
                )
        return payload

    @mcp.tool(
        name="set_default_cwd",
        title="Set Default CWD",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Set the session-wide default working directory used whenever a tool call "
            "omits `cwd`. Pass null (or omit path) to clear the override and fall back to "
            "the server's workspace root. Useful when running many commands in the same "
            "repo: set it once instead of passing `cwd` on every call."
        ),
    )
    def set_default_cwd(
        path: Annotated[
            str | None,
            Field(
                description=(
                    "Directory to use as the session default cwd. Pass null or omit to clear "
                    "the override and use the server workspace_root."
                )
            ),
        ] = None
    ) -> dict[str, object]:
        if not path:
            session.set_default_cwd(None)
            return {
                "success": True,
                "session_cwd": None,
                "workspace_root": str(ctx.workspace_root),
                "cleared": True,
            }
        target = resolve_path(path, ctx.workspace_root)
        if not target.exists():
            return {
                "success": False,
                "error": {
                    "code": "cwd_not_found",
                    "message": f"Path does not exist: {target}",
                },
                "path": str(target),
            }
        if not target.is_dir():
            return {
                "success": False,
                "error": {
                    "code": "cwd_not_directory",
                    "message": f"Path is not a directory: {target}",
                },
                "path": str(target),
            }
        session.set_default_cwd(target)
        return {
            "success": True,
            "session_cwd": str(target),
            "workspace_root": str(ctx.workspace_root),
            "cleared": False,
        }

    @mcp.tool(
        name="get_default_cwd",
        title="Get Default CWD",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return the currently active default working directory and whether it comes "
            "from the session override (set_default_cwd) or from the server's workspace root."
        ),
    )
    def get_default_cwd() -> dict[str, object]:
        session_cwd = session.get_default_cwd()
        effective = session_cwd if session_cwd is not None else ctx.workspace_root
        return {
            "success": True,
            "session_cwd": str(session_cwd) if session_cwd else None,
            "workspace_root": str(ctx.workspace_root),
            "effective_cwd": str(effective),
            "source": "session" if session_cwd else "workspace_root",
        }

    @mcp.tool(
        name="session_checkpoint",
        title="Session Checkpoint",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Persist the current logical chat checkpoint for reliable later resume. "
            "Store only the current semantic slice and concrete next action; checkpoints "
            "expire automatically after the configured TTL."
        ),
    )
    def session_checkpoint(
        goal: Annotated[str, Field(description="Overall goal currently being pursued.")],
        current_slice: Annotated[
            str,
            Field(description="Current bounded implementation/review slice."),
        ],
        next_action: Annotated[
            str,
            Field(description="Exact next concrete action when the chat resumes."),
        ],
        done_means: Annotated[
            list[str] | None,
            Field(description="Acceptance conditions for the current slice."),
        ] = None,
        job_ids: Annotated[
            list[str] | None,
            Field(description="Durable job ids whose results matter to this checkpoint."),
        ] = None,
        delegate_ids: Annotated[
            list[str] | None,
            Field(description="Delegate ids whose results matter to this checkpoint."),
        ] = None,
        notes: Annotated[
            str | None,
            Field(description="Optional concise architectural decisions or blockers."),
        ] = None,
        cwd: Annotated[
            str | None,
            Field(description="Project/worktree cwd. Defaults to the current session cwd."),
        ] = None,
    ) -> dict[str, object]:
        session_key = session.get_current_session_id()
        if not session_key:
            return {
                "success": False,
                "error": {
                    "code": "logical_session_unavailable",
                    "message": "No logical MCP session is available for checkpointing.",
                },
            }
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        payload = {
            "goal": goal.strip(),
            "current_slice": current_slice.strip(),
            "next_action": next_action.strip(),
            "done_means": list(done_means or []),
            "job_ids": list(dict.fromkeys(job_ids or [])),
            "delegate_ids": list(dict.fromkeys(delegate_ids or [])),
            "notes": notes.strip() if notes else None,
            "cwd": str(resolved_cwd),
        }
        saved = ctx.checkpoint_store.put(
            session_key=session_key,
            checkpoint=payload,
        )
        session.set_default_cwd(resolved_cwd)
        return {
            "success": True,
            "checkpoint": saved,
            "ttl_seconds": int(ctx.global_value("SESSION_CHECKPOINT_TTL_SECONDS", 86400)),
        }

    @mcp.tool(
        name="session_resume",
        title="Resume Session",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Restore the durable checkpoint for this logical chat and refresh every saved "
            "job/delegate status before returning the exact next action. Use this first when "
            "the user asks to continue/resume or asks where work stopped."
        ),
    )
    def session_resume() -> dict[str, object]:
        session_key = session.get_current_session_id()
        if not session_key:
            return {
                "success": False,
                "error": {
                    "code": "logical_session_unavailable",
                    "message": "No logical MCP session is available for resume.",
                },
            }
        checkpoint = ctx.checkpoint_store.get(session_key)
        if checkpoint is None:
            return {
                "success": True,
                "resumable": False,
                "checkpoint": None,
                "jobs": [],
                "delegates": [],
                "pending_results": [],
            }

        checkpoint_cwd = checkpoint.get("cwd")
        runtime = checkpoint.get("runtime")
        if not isinstance(runtime, dict):
            runtime = {}
        if not checkpoint_cwd:
            checkpoint_cwd = runtime.get("cwd")
        if isinstance(checkpoint_cwd, str) and checkpoint_cwd:
            candidate = Path(checkpoint_cwd)
            if candidate.is_dir():
                session.set_default_cwd(candidate)

        refreshed = refresh_session_owned_work(
            ctx,
            session_key=session_key,
        )
        checkpoint = ctx.checkpoint_store.get(session_key) or checkpoint
        runtime = checkpoint.get("runtime")
        if not isinstance(runtime, dict):
            runtime = {}

        jobs = refreshed.get("jobs")
        if not isinstance(jobs, list):
            jobs = []
        delegates = refreshed.get("delegates")
        if not isinstance(delegates, list):
            delegates = []
        pending_results = refreshed.get("pending_results")
        if not isinstance(pending_results, list):
            pending_results = []

        interrupted_delegate_ids: list[str] = []
        for item in delegates:
            snapshot = item.get("delegate") if isinstance(item, dict) else None
            if not isinstance(snapshot, dict):
                continue
            error = snapshot.get("error")
            if (
                isinstance(error, dict)
                and error.get("code") in {"server_restart", "server_shutdown"}
            ):
                delegate_id = str(snapshot.get("delegate_id") or "")
                if delegate_id and delegate_id not in interrupted_delegate_ids:
                    interrupted_delegate_ids.append(delegate_id)

        semantic_next_action = checkpoint.get("next_action")
        semantic_suffix = (
            f" Then continue the saved plan: {semantic_next_action}"
            if semantic_next_action
            else ""
        )
        runtime_jobs = runtime.get("jobs")
        if not isinstance(runtime_jobs, dict):
            runtime_jobs = {}
        runtime_delegates = runtime.get("delegates")
        if not isinstance(runtime_delegates, dict):
            runtime_delegates = {}
        owned_in_progress = any(
            isinstance(state, dict)
            and state.get("continuation_state") == POLL_REQUIRED
            for state in [*runtime_jobs.values(), *runtime_delegates.values()]
        )
        if interrupted_delegate_ids:
            next_action = (
                "Consume the interrupted delegate result(s), restart or replace them if still required, "
                f"and independently verify any partial work: {', '.join(interrupted_delegate_ids)}."
                f"{semantic_suffix}"
            )
            resume_state = "delegate_interrupted_by_server_restart"
        elif pending_results:
            pending_refs = ", ".join(
                f"{item.get('kind')}:{item.get('id')}"
                for item in pending_results
                if isinstance(item, dict)
            )
            next_action = (
                "Consume and verify the pending terminal result(s)"
                + (f" ({pending_refs})" if pending_refs else "")
                + " before continuing the conversation plan."
                + semantic_suffix
            )
            resume_state = "terminal_results_ready"
        elif owned_in_progress:
            next_action = (
                runtime.get("next_action")
                or "Poll or await the owned job/delegate work until terminal."
            )
            next_action = f"{next_action}{semantic_suffix}"
            resume_state = "owned_work_in_progress"
        elif semantic_next_action:
            next_action = semantic_next_action
            resume_state = "semantic_checkpoint"
        else:
            next_action = runtime.get("next_action")
            resume_state = "runtime_checkpoint"

        return {
            "success": True,
            "resumable": True,
            "resume_state": resume_state,
            "checkpoint": checkpoint,
            "runtime": runtime,
            "jobs": jobs,
            "delegates": delegates,
            "pending_results": pending_results,
            "next_action": next_action,
        }


    @mcp.tool(
        name="pending_results",
        title="Pending Results",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Reconcile background jobs/delegates owned by this logical chat and return terminal "
            "results that still require explicit consumption. Results remain durable until "
            "mark_result_consumed is called or the session checkpoint expires/closes."
        ),
    )
    def pending_results() -> dict[str, object]:
        session_key = session.get_current_session_id()
        if not session_key:
            return {
                "success": False,
                "error": {
                    "code": "logical_session_unavailable",
                    "message": "No logical MCP session is available for result lookup.",
                },
            }
        refreshed = refresh_session_owned_work(
            ctx,
            session_key=session_key,
        )
        return {
            "success": True,
            "resumable": bool(refreshed.get("resumable")),
            "pending_results": refreshed.get("pending_results", []),
        }

    @mcp.tool(
        name="await_job",
        title="Await Owned Job",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Wait for at most 30 seconds for one durable job already owned by this logical chat. "
            "The terminal result is placed in the durable result inbox and is not auto-consumed."
        ),
    )
    def await_job(
        job_id: Annotated[str, Field(description="Owned durable job identifier.")],
        wait_seconds: Annotated[
            float,
            Field(description="Maximum server-side wait window.", ge=0, le=30),
        ] = 25.0,
    ) -> dict[str, object]:
        session_key = session.get_current_session_id()
        checkpoint = ctx.checkpoint_store.get(session_key) if session_key else None
        if not session_key or checkpoint is None:
            return {
                "success": False,
                "error": {
                    "code": "logical_session_unavailable",
                    "message": "No resumable logical MCP session is available.",
                },
            }
        if not owns_result(checkpoint, kind="job", result_id=job_id):
            return {
                "success": False,
                "error": {
                    "code": "result_not_owned",
                    "message": "This durable job is not owned by the current logical session.",
                },
                "job_id": job_id,
            }
        refreshed = refresh_session_owned_work(
            ctx,
            session_key=session_key,
            kind="job",
            result_id=job_id,
            wait_seconds=wait_seconds,
        )
        state = ctx.checkpoint_store.owned_result(
            session_key,
            kind="job",
            result_id=job_id,
        )
        jobs = refreshed.get("jobs")
        result = jobs[0] if isinstance(jobs, list) and jobs else None
        return {
            "success": True,
            "job_id": job_id,
            "terminal": bool(isinstance(state, dict) and state.get("terminal")),
            "continuation_state": state.get("continuation_state") if isinstance(state, dict) else None,
            "result": result,
            "pending_results": refreshed.get("pending_results", []),
        }

    @mcp.tool(
        name="await_delegate",
        title="Await Owned Delegate",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Wait for at most 30 seconds for one delegate already owned by this logical chat. "
            "The terminal result is placed in the durable result inbox and is not auto-consumed."
        ),
    )
    def await_delegate(
        delegate_id: Annotated[str, Field(description="Owned delegate identifier.")],
        wait_seconds: Annotated[
            float,
            Field(description="Maximum server-side wait window.", ge=0, le=30),
        ] = 25.0,
    ) -> dict[str, object]:
        session_key = session.get_current_session_id()
        checkpoint = ctx.checkpoint_store.get(session_key) if session_key else None
        if not session_key or checkpoint is None:
            return {
                "success": False,
                "error": {
                    "code": "logical_session_unavailable",
                    "message": "No resumable logical MCP session is available.",
                },
            }
        if not owns_result(checkpoint, kind="delegate", result_id=delegate_id):
            return {
                "success": False,
                "error": {
                    "code": "result_not_owned",
                    "message": "This delegate is not owned by the current logical session.",
                },
                "delegate_id": delegate_id,
            }
        refreshed = refresh_session_owned_work(
            ctx,
            session_key=session_key,
            kind="delegate",
            result_id=delegate_id,
            wait_seconds=wait_seconds,
        )
        state = ctx.checkpoint_store.owned_result(
            session_key,
            kind="delegate",
            result_id=delegate_id,
        )
        delegates = refreshed.get("delegates")
        result = delegates[0] if isinstance(delegates, list) and delegates else None
        return {
            "success": True,
            "delegate_id": delegate_id,
            "terminal": bool(isinstance(state, dict) and state.get("terminal")),
            "continuation_state": state.get("continuation_state") if isinstance(state, dict) else None,
            "result": result,
            "pending_results": refreshed.get("pending_results", []),
        }

    @mcp.tool(
        name="mark_result_consumed",
        title="Mark Result Consumed",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Explicitly acknowledge that one terminal owned job/delegate result has been read "
            "and incorporated into the conversation plan. This is idempotent."
        ),
    )
    def mark_result_consumed(
        kind: Annotated[
            Literal["job", "delegate"],
            Field(description="Owned result kind."),
        ],
        result_id: Annotated[
            str,
            Field(description="Owned job_id or delegate_id."),
        ],
    ) -> dict[str, object]:
        session_key = session.get_current_session_id()
        checkpoint = ctx.checkpoint_store.get(session_key) if session_key else None
        if not session_key or checkpoint is None:
            return {
                "success": False,
                "error": {
                    "code": "logical_session_unavailable",
                    "message": "No resumable logical MCP session is available.",
                },
            }
        if not owns_result(checkpoint, kind=kind, result_id=result_id):
            return {
                "success": False,
                "error": {
                    "code": "result_not_owned",
                    "message": "This result is not owned by the current logical session.",
                },
                "kind": kind,
                "result_id": result_id,
            }

        refresh_session_owned_work(
            ctx,
            session_key=session_key,
            kind=kind,
            result_id=result_id,
            wait_seconds=0,
        )
        consumed = ctx.checkpoint_store.mark_result_consumed(
            session_key,
            kind=kind,
            result_id=result_id,
        )
        if not consumed.get("consumed"):
            return {
                "success": False,
                "error": {
                    "code": str(consumed.get("reason") or "result_not_terminal"),
                    "message": "The owned result is not terminal and cannot be consumed yet.",
                },
                "kind": kind,
                "result_id": result_id,
                "state": consumed.get("state"),
            }
        if kind == "delegate" and not bool(consumed.get("already_consumed")):
            try:
                ctx.registry.note_delegate_consumed(result_id)
            except (OSError, TypeError, ValueError):
                pass
        remaining = ctx.checkpoint_store.pending_results(session_key)
        if not remaining:
            session.registry.note_execution_state(
                required_action=None,
                state="RESULTS_CONSUMED",
                session_id=session_key,
            )
        return {
            "success": True,
            "kind": kind,
            "result_id": result_id,
            "already_consumed": bool(consumed.get("already_consumed")),
            "state": consumed.get("state"),
            "pending_results": remaining,
        }

    @mcp.tool(
        name="session_close",
        title="Close Session",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Explicitly abandon continuation ownership for the current logical chat. "
            "Removes its durable checkpoint and runtime session state immediately, reports "
            "any unread terminal results or in-progress owned work that were abandoned, "
            "and does not kill running durable jobs."
        ),
    )
    def session_close() -> dict[str, object]:
        session_key = session.get_current_session_id()
        if not session_key:
            return {
                "success": False,
                "error": {
                    "code": "logical_session_unavailable",
                    "message": "No logical MCP session is available to close.",
                },
            }
        removed_checkpoint = ctx.checkpoint_store.pop(session_key)
        abandonment = summarize_abandoned_checkpoint(removed_checkpoint)
        session.registry.close(session_key)
        return {
            "success": True,
            "closed": True,
            "checkpoint_removed": removed_checkpoint is not None,
            "running_jobs_untouched": True,
            **abandonment,
        }

    @mcp.tool(
        name="env_snapshot",
        title="Environment Snapshot",
        annotations=READ_ONLY_TOOL,
        description=(
            "Collect a small read-only environment snapshot for cwd: platform, Python runtime, "
            "git, Node/npm, Java, common dependency/config file hashes, and optionally bounded "
            "pip freeze output. Does not source shell profiles, activate venvs, install packages, "
            "or modify the local environment."
        ),
    )
    def env_snapshot(
        cwd: Annotated[
            str | None,
            Field(description="Working directory to inspect. Defaults to the session cwd or workspace root."),
        ] = None,
        include_packages: Annotated[
            bool,
            Field(description="When true, include bounded `python -m pip freeze` output for the server Python."),
        ] = False,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return env_snapshot_impl(cwd=resolved_cwd, include_packages=include_packages)

    @mcp.tool(
        name="env_diff",
        title="Environment Diff",
        annotations=READ_ONLY_TOOL,
        description=(
            "Compare two inline env_snapshot-like JSON objects and return changed nested keys "
            "using dot-path notation. This tool does not read snapshot files from disk."
        ),
    )
    def env_diff(
        left: Annotated[
            dict[str, Any],
            Field(description="Left environment snapshot object to compare."),
        ],
        right: Annotated[
            dict[str, Any],
            Field(description="Right environment snapshot object to compare."),
        ],
    ) -> dict[str, object]:
        return env_diff_impl(left=left, right=right)

    return {
        "server_info": server_info,
        "execution_state": execution_state,
        "set_default_cwd": set_default_cwd,
        "get_default_cwd": get_default_cwd,
        "session_checkpoint": session_checkpoint,
        "session_resume": session_resume,
        "pending_results": pending_results,
        "await_job": await_job,
        "await_delegate": await_delegate,
        "mark_result_consumed": mark_result_consumed,
        "session_close": session_close,
        "env_snapshot": env_snapshot,
        "env_diff": env_diff,
    }
