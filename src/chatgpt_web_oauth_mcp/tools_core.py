from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from . import session
from .delegate_guidance import (
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
from .tmux_ops import TmuxClient, tmux_runtime_info
from .tool_context import LOCAL_STATE_TOOL, READ_ONLY_TOOL, ToolContext


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
        list_tools = getattr(mcp, "_list_tools")
        try:
            registered = await list_tools()
        except TypeError:
            # fastmcp 2.14 requires a context arg; None works for server-side listing.
            registered = await list_tools(None)
        tools = sorted(tool.name for tool in registered)
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
            "command_timeout_seconds": ctx.command_timeout,
            "auth": ctx.current_oauth_config().normalized_auth_mode,
            "debug_mcp_logging": ctx.debug_mcp_logging,
            "health_monitoring": {
                "endpoint": "/internal/health",
                "enabled": bool(ctx.global_value("HEALTH_TOKEN", "")),
                "session_idle_ttl_seconds": int(
                    ctx.global_value("SESSION_IDLE_TTL_SECONDS", 86400)
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
                "discovery_tool": "get_skill_index",
                "index_resource": SKILL_INDEX_URI,
                "guide_tools": {
                    "delegate-use": "get_delegate_use",
                    "file-use": "get_file_use",
                    "process-use": "get_process_use",
                    "runtime-use": "get_runtime_use",
                    "git-use": "get_git_use",
                },
                "guide_resources": {
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
        "env_snapshot": env_snapshot,
        "env_diff": env_diff,
    }
