from __future__ import annotations

import argparse
from contextlib import asynccontextmanager

import os
from typing import Any

import anyio
from fastmcp import FastMCP
import uvicorn

from .config import (
    APP_NAME,
    AUTH_MODE,
    AUTH_TOKEN,
    ANTIGRAVITY_COMMAND,
    ANTIGRAVITY_DEFAULT_MODEL,
    ANTIGRAVITY_DEFAULT_REASONING_EFFORT,
    ANTIGRAVITY_SKIP_PERMISSIONS,
    CLAUDE_BYPASS_PERMISSIONS,
    CLAUDE_COMMAND,
    CODEX_COMMAND,
    CODEX_RUNTIME_CUA_ALLOWED_APPS,
    CODEX_RUNTIME_CUA_APPROVAL_MODE,
    CODEX_RUNTIME_DEFAULT_SANDBOX,
    CODEX_RUNTIME_DEFAULT_TIMEOUT_MS,
    CODEX_RUNTIME_IDLE_TTL_SECONDS,
    CODEX_RUNTIME_MAX_CONCURRENCY,
    CODEX_RUNTIME_MAX_MESSAGE_BYTES,
    CODEX_RUNTIME_MAX_RUNTIMES,
    CODEX_RUNTIME_MAX_TIMEOUT_MS,
    CODEX_RUNTIME_OUTPUT_MAX_BYTES,
    CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS,
    COMMAND_TIMEOUT,
    OPENAI_FOREGROUND_TIMEOUT,
    DEBUG_MCP_LOGGING,
    DELEGATE_CANCEL_GRACE_SECONDS,
    DELEGATE_DEFAULT_HARNESS,
    DELEGATE_CODE_EXECUTION_TIMEOUT,
    DELEGATE_CODE_MAX_GLOBAL,
    DELEGATE_CODE_MAX_PER_PROJECT,
    DELEGATE_EXPLORE_EXECUTION_TIMEOUT,
    DELEGATE_EXPLORE_MAX_GLOBAL,
    DELEGATE_EXPLORE_MAX_PER_PROJECT,
    DELEGATE_MAX_TERMINAL_RECORDS,
    DELEGATE_QUEUE_LIMIT_GLOBAL,
    DELEGATE_QUEUE_LIMIT_PER_PROJECT,
    DELEGATE_RETENTION_SECONDS,
    DELEGATE_STATE_DIR,
    DELEGATE_TIMEOUT,
    DELEGATE_WAIT_TIMEOUT,
    GRACEFUL_SHUTDOWN_SECONDS,
    HEALTH_SESSION_LIMIT,
    HEALTH_TOKEN,
    HEALTH_USAGE_LIMITS_COMMAND_TIMEOUT_SECONDS,
    HEALTH_USAGE_LIMITS_ENABLED,
    HEALTH_USAGE_LIMITS_HTTP_TIMEOUT_SECONDS,
    HEALTH_USAGE_LIMITS_REFRESH_SECONDS,
    HOST,
    JOB_DEFAULT_TIMEOUT_SECONDS,
    JOB_LOG_MAX_BYTES,
    JOB_MAX_TERMINAL_RECORDS,
    JOB_MAX_TIMEOUT_SECONDS,
    JOB_OUTPUT_TOKEN_BUDGET,
    JOB_RETENTION_SECONDS,
    OAUTH_LOGIN_TOKEN,
    OAUTH_SCOPES,
    OAUTH_TOKEN_TTL_SECONDS,
    PORT,
    PI_COMMAND,
    PUBLIC_BASE_URL,
    QUOTA_PRIMING_ANTIGRAVITY_GEMINI_MODEL,
    QUOTA_PRIMING_ANTIGRAVITY_THIRD_PARTY_MODEL,
    QUOTA_PRIMING_CHECK_INTERVAL_SECONDS,
    QUOTA_PRIMING_CLAUDE_MODEL,
    QUOTA_PRIMING_COMMAND_TIMEOUT_SECONDS,
    QUOTA_PRIMING_ENABLED,
    QUOTA_PRIMING_MAX_ATTEMPTS_PER_CYCLE,
    QUOTA_PRIMING_POST_RESET_DELAY_SECONDS,
    QUOTA_PRIMING_RETRY_SECONDS,
    QUOTA_PRIMING_VERIFICATION_DELAY_SECONDS,
    QUOTA_PRIMING_VERIFICATION_PROBE_DELAY_SECONDS,
    READ_TOKEN_BUDGET,
    RIPGREP_BINARY,
    RUN_CAPTURE_MAX_BYTES,
    RUN_TOKEN_BUDGET,
    SESSION_ACTIVE_WINDOW_SECONDS,
    SESSION_CHECKPOINT_TTL_SECONDS,
    SESSION_EPHEMERAL_IDLE_TTL_SECONDS,
    SESSION_IDLE_TTL_SECONDS,
    SESSION_ORCHESTRATION_QUIET_SECONDS,
    SESSION_REQUEST_STALL_SECONDS,
    STATE_DIR,
    TMUX_BINARY,
    TMUX_CONTROL_TIMEOUT,
    TMUX_SOCKET_NAME,
    TOOL_OUTPUT_TOKEN_BUDGET,
    WORKSPACE_ROOT,
    ensure_runtime_directories,
)
from .activity import ActivityTracker
from .codex_runtime import CodexRuntimeManager
from .delegate_harnesses import AntigravityHarness, ClaudeHarness
from .executors import ExecutorRegistry
from .health import OpsHealthSnapshot
from .http_compat import build_http_compat_app
from .oauth import OAuthRuntimeConfig
from .quota_windows import QuotaWindowManager
from .session_checkpoints import SessionCheckpointStore
from .session_continuation import SessionContinuationMiddleware
from .shell import ForegroundProcessRegistry, JobRegistry
from .usage_limits import UsageLimitCollector
from .tool_context import ToolContext
from .tools_core import register_core_tools
from .tools_codex_runtime import register_codex_runtime_tools
from .tools_delegate import register_delegate_tools
from .tools_files import register_file_tools
from .tools_git_shell import register_git_shell_tools
from .tools_skills import register_skill_tools
from .tools_tmux import register_tmux_tools


# Bearer auth lives exclusively in the HTTP layer (http_compat.HTTPBearerAuthMiddleware)
# so unauthenticated clients can't even open an SSE session. The FastMCP
# protocol-layer middleware was redundant and has been removed.

job_registry = JobRegistry(
    retention_seconds=JOB_RETENTION_SECONDS,
    max_terminal_records=JOB_MAX_TERMINAL_RECORDS,
    default_timeout_seconds=JOB_DEFAULT_TIMEOUT_SECONDS,
    max_timeout_seconds=JOB_MAX_TIMEOUT_SECONDS,
    log_max_bytes=JOB_LOG_MAX_BYTES,
)
registry = ExecutorRegistry(
    codex_command=CODEX_COMMAND,
    pi_command=PI_COMMAND,
    default_harness=DELEGATE_DEFAULT_HARNESS,
    harnesses=[
        ClaudeHarness(
            command=CLAUDE_COMMAND or None,
            bypass_permissions=CLAUDE_BYPASS_PERMISSIONS,
        ),
        AntigravityHarness(
            command=ANTIGRAVITY_COMMAND or None,
            skip_permissions=ANTIGRAVITY_SKIP_PERMISSIONS,
            default_model=ANTIGRAVITY_DEFAULT_MODEL,
            default_reasoning_effort=ANTIGRAVITY_DEFAULT_REASONING_EFFORT,
        ),
    ],
    max_explore_per_project=DELEGATE_EXPLORE_MAX_PER_PROJECT,
    max_explore_global=DELEGATE_EXPLORE_MAX_GLOBAL,
    max_code_per_project=DELEGATE_CODE_MAX_PER_PROJECT,
    max_code_global=DELEGATE_CODE_MAX_GLOBAL,
    queue_limit_per_project=DELEGATE_QUEUE_LIMIT_PER_PROJECT,
    queue_limit_global=DELEGATE_QUEUE_LIMIT_GLOBAL,
    explore_execution_timeout_seconds=DELEGATE_EXPLORE_EXECUTION_TIMEOUT,
    code_execution_timeout_seconds=DELEGATE_CODE_EXECUTION_TIMEOUT,
    cancel_grace_seconds=DELEGATE_CANCEL_GRACE_SECONDS,
    durable_job_registry=job_registry,
    durable_state_dir=STATE_DIR,
    delegate_state_root=DELEGATE_STATE_DIR,
    delegate_retention_seconds=DELEGATE_RETENTION_SECONDS,
    max_terminal_delegate_records=DELEGATE_MAX_TERMINAL_RECORDS,
)
foreground_process_registry = ForegroundProcessRegistry()
activity_tracker = ActivityTracker()
checkpoint_store = SessionCheckpointStore(
    path=STATE_DIR / "session-checkpoints.json",
    ttl_seconds=SESSION_CHECKPOINT_TTL_SECONDS,
)
codex_runtime_manager = CodexRuntimeManager(
    state_dir=STATE_DIR,
    codex_command=CODEX_COMMAND,
    workspace_root=WORKSPACE_ROOT,
    max_concurrency=CODEX_RUNTIME_MAX_CONCURRENCY,
    max_runtimes=CODEX_RUNTIME_MAX_RUNTIMES,
    idle_ttl_seconds=CODEX_RUNTIME_IDLE_TTL_SECONDS,
    startup_timeout_seconds=CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS,
    default_timeout_ms=CODEX_RUNTIME_DEFAULT_TIMEOUT_MS,
    max_timeout_ms=CODEX_RUNTIME_MAX_TIMEOUT_MS,
    output_bytes_cap=CODEX_RUNTIME_OUTPUT_MAX_BYTES,
    max_message_bytes=CODEX_RUNTIME_MAX_MESSAGE_BYTES,
)
usage_limit_collector = UsageLimitCollector(
    antigravity_command=ANTIGRAVITY_COMMAND or None,
    codex_reader=codex_runtime_manager.adapter.account_rate_limits,
    refresh_interval_seconds=HEALTH_USAGE_LIMITS_REFRESH_SECONDS,
    command_timeout_seconds=HEALTH_USAGE_LIMITS_COMMAND_TIMEOUT_SECONDS,
    http_timeout_seconds=HEALTH_USAGE_LIMITS_HTTP_TIMEOUT_SECONDS,
)
quota_window_manager = QuotaWindowManager(
    usage_collector=usage_limit_collector,
    state_path=STATE_DIR / "quota-window-manager.json",
    antigravity_command=ANTIGRAVITY_COMMAND or None,
    claude_command=CLAUDE_COMMAND or None,
    codex_command=CODEX_COMMAND or None,
    enabled=QUOTA_PRIMING_ENABLED,
    check_interval_seconds=QUOTA_PRIMING_CHECK_INTERVAL_SECONDS,
    post_reset_delay_seconds=QUOTA_PRIMING_POST_RESET_DELAY_SECONDS,
    verification_delay_seconds=QUOTA_PRIMING_VERIFICATION_DELAY_SECONDS,
    verification_probe_delay_seconds=QUOTA_PRIMING_VERIFICATION_PROBE_DELAY_SECONDS,
    retry_seconds=QUOTA_PRIMING_RETRY_SECONDS,
    command_timeout_seconds=QUOTA_PRIMING_COMMAND_TIMEOUT_SECONDS,
    max_attempts_per_cycle=QUOTA_PRIMING_MAX_ATTEMPTS_PER_CYCLE,
    antigravity_gemini_model=QUOTA_PRIMING_ANTIGRAVITY_GEMINI_MODEL,
    antigravity_third_party_model=QUOTA_PRIMING_ANTIGRAVITY_THIRD_PARTY_MODEL,
    claude_model=QUOTA_PRIMING_CLAUDE_MODEL,
)
health_snapshot = OpsHealthSnapshot(
    registry=registry,
    job_registry=job_registry,
    activity_tracker=activity_tracker,
    state_dir=STATE_DIR,
    tool_output_token_budget=TOOL_OUTPUT_TOKEN_BUDGET,
    session_idle_ttl_seconds=SESSION_IDLE_TTL_SECONDS,
    session_ephemeral_idle_ttl_seconds=SESSION_EPHEMERAL_IDLE_TTL_SECONDS,
    session_active_window_seconds=SESSION_ACTIVE_WINDOW_SECONDS,
    session_request_stall_seconds=SESSION_REQUEST_STALL_SECONDS,
    session_orchestration_quiet_seconds=SESSION_ORCHESTRATION_QUIET_SECONDS,
    session_limit=HEALTH_SESSION_LIMIT,
    usage_limits_provider=(usage_limit_collector.snapshot if HEALTH_USAGE_LIMITS_ENABLED else None),
    quota_window_provider=quota_window_manager.snapshot,
)


@asynccontextmanager
async def _mcp_lifespan(_server: Any):
    try:
        await anyio.to_thread.run_sync(registry.recover_persisted_delegates)
        await anyio.to_thread.run_sync(
            lambda: registry.maintain_persisted_delegates(force=True)
        )
        await anyio.to_thread.run_sync(
            lambda: job_registry.maintain(state_dir=STATE_DIR, force=True)
        )
        if HEALTH_USAGE_LIMITS_ENABLED:
            usage_limit_collector.start()
        quota_window_manager.start()
        yield {}
    finally:
        quota_window_manager.stop()
        usage_limit_collector.stop()
        await anyio.to_thread.run_sync(registry.shutdown)
        await anyio.to_thread.run_sync(foreground_process_registry.shutdown)
        codex_runtime_manager.shutdown()


MCP_INSTRUCTIONS = (
    "Architecture: ChatGPT Web is the architect/manager/reviewer; this local MCP server exposes "
    "scoped local tools; codex_runtime_* provides a persistent Codex App Server runtime, "
    "uses command/exec for argv execution, and never starts a Codex LLM turn. "
    "Approval and form elicitation requests are bridged to the current MCP client when supported. "
    "Only an explicitly configured prototype policy auto-approves exact allowlisted Computer Use "
    "app-access requests; all other server requests remain interactive or fail closed. "
    "Use direct tools for deterministic repo inspection, planning, patching, commands, git checks, and verification. "
    "Use delegate_task/delegate_batch for bounded independent agent exploration, second-opinion review, or isolated "
    "implementation slices when another model adds value; discover capabilities with delegate_harnesses and load "
    "get_delegate_use before the first delegate workflow. Never treat an agent's success claim as acceptance: inspect "
    "its structured result and logs, review the actual diff, and run direct verification before declaring completion. "
    "Use search/read_text for focused or batched discovery and reading, apply_patch/write_file for edits, "
    "env_snapshot/env_diff for read-only runtime diagnostics. Before edits or reviews, use "
    "code_map_symbols to find definitions, code_map_references to estimate impact, and "
    "code_map_imports to inspect module boundaries. Use those results to identify candidate "
    "files_in_scope before detailed reads. code_map_* is lightweight and not for "
    "precise rename, type inference, or call graph analysis. "
    f"Use run_command for coherent bounded single or batched shell work expected to finish within the safe "
    f"foreground window. For ChatGPT/OpenAI sessions this deployment caps foreground work at "
    f"{OPENAI_FOREGROUND_TIMEOUT}s because the upstream command-response deadline is shorter than the local "
    f"900s hard ceiling; do not split a command solely to reduce wall-clock duration or to fit that window: "
    f"run the whole coherent command once "
    f"with job_start when it is expected to take longer. For sequential or parallel batches, timeout is one shared "
    f"foreground wall-clock budget across all child commands. Use job_start/job_list/job_status/job_output/"
    f"job_tail/job_kill when runtime is longer than the safe foreground window or the work must survive a client "
    f"disconnect. Durable jobs have a bounded execution timeout and log-output termination threshold. "
    f"job_list discovers records "
    "from the current state directory, job_output incrementally reads one stdout or stderr stream with "
    "a raw-byte cursor, and job_tail remains the backward-compatible last-N-lines API. Use tmux_* for "
    "persistent interactive TTY sessions, "
    "and git_* only inside a git repository. Use tmux_list/status/capture to observe a session and "
    "tmux_send for bounded text or key input; tmux capture output is a terminal snapshot, not a lossless log. "
    "Use codex_runtime_acquire with a stable logical name for recurring workers; use codex_runtime_list "
    "to discover reusable bindings, and open/resume/status/close only when their explicit lifecycle is needed. "
    "codex_mcp_inventory/codex_mcp_call for connected MCP access without starting a Codex LLM turn. "
    "If the user asks to continue/resume (including 'продолжи', 'продолжай', or asks where work stopped), "
    "call session_resume before repo/process rediscovery whenever a resumable checkpoint exists. "
    "Background jobs and delegates started by the current logical session are owned work. Terminal owned results "
    "remain in the durable result inbox until explicitly consumed; any later tool call may surface a "
    "session_continuation hint in result metadata. Use pending_results, await_job, or await_delegate to reconcile "
    "owned work, independently verify the result, then call mark_result_consumed only after incorporating it into "
    "the conversation plan. Never silently skip an unconsumed terminal result. "
    "If the user explicitly asks to close/end the chat session, call session_close. Before intentionally returning "
    "a nonterminal work checkpoint, call session_checkpoint with the current slice, exact next action, and relevant "
    "durable job/delegate ids. "
    "Execution-loop contract: after every tool result, immediately interpret the result and either invoke the "
    "next concrete tool or return a checkpoint/final/blocker response in the current turn. Never remain idle in "
    "reasoning merely because the larger task is unfinished. Waiting is justified only by verified observable "
    "progress. When activity is unclear, call execution_state. ACTIVE may justify polling; QUIET requires recheck "
    "or process/resource/log inspection instead of blind waiting; STALLED_SUSPECTED requires inspection or "
    "independent work; DEAD/TERMINAL and state=NEXT_ACTION_REQUIRED require the next tool call or response now. "
    "A ready Codex runtime is not evidence that task work is running. execution_state compares process-group "
    "identity, cumulative CPU-time, output growth, and process-group changes across observations; it never kills "
    "a process automatically. "
    "Call get_skill_index to discover progressive-disclosure operating guides, then load the matching "
    "get_delegate_use, get_file_use, get_process_use, get_runtime_use, or get_git_use "
    "guide before the first workflow in that tool family. No taskboard tools are exposed."
)

mcp = FastMCP(
    APP_NAME,
    instructions=MCP_INSTRUCTIONS,
    lifespan=_mcp_lifespan,
)


def _current_auth_token() -> str:
    # Resolved via module globals so tests that monkeypatch ``AUTH_TOKEN`` on
    # this module (and runtime overrides) are honored per-request.
    return globals().get("AUTH_TOKEN", "") or ""


def _current_oauth_config() -> OAuthRuntimeConfig:
    return OAuthRuntimeConfig(
        auth_mode=globals().get("AUTH_MODE", "") or "",
        auth_token=_current_auth_token(),
        public_base_url=globals().get("PUBLIC_BASE_URL", "") or "",
        state_dir=globals().get("STATE_DIR", STATE_DIR),
        oauth_login_token=globals().get("OAUTH_LOGIN_TOKEN", "") or "",
        oauth_scopes=tuple(globals().get("OAUTH_SCOPES", ("local-ops",)) or ("local-ops",)),
        oauth_token_ttl_seconds=int(globals().get("OAUTH_TOKEN_TTL_SECONDS", 86400) or 86400),
    )


def _current_debug_mcp_logging() -> bool:
    return bool(globals().get("DEBUG_MCP_LOGGING", False))


def _current_health_token() -> str:
    return str(globals().get("HEALTH_TOKEN", "") or "")


def _current_health_snapshot() -> dict[str, object]:
    return health_snapshot.snapshot()


def _global_value(name: str, default: Any = None) -> Any:
    return globals().get(name, default)


_tool_context = ToolContext(
    global_value=_global_value,
    current_oauth_config=_current_oauth_config,
)
mcp.add_middleware(SessionContinuationMiddleware(_tool_context))

_tool_exports: dict[str, object] = {}
_tool_exports.update(register_core_tools(mcp, _tool_context))
_tool_exports.update(register_codex_runtime_tools(mcp, _tool_context))
_tool_exports.update(register_delegate_tools(mcp, _tool_context))
_tool_exports.update(register_skill_tools(mcp))
_tool_exports.update(register_file_tools(mcp, _tool_context))
_tool_exports.update(register_git_shell_tools(mcp, _tool_context))
_tool_exports.update(register_tmux_tools(mcp, _tool_context))
globals().update(_tool_exports)


def build_http_app():
    streamable_app = mcp.http_app(
        path="/mcp",
        transport="streamable-http",
    )
    legacy_sse_app = mcp.http_app(
        path="/mcp",
        transport="sse",
    )
    return build_http_compat_app(
        streamable_app=streamable_app,
        legacy_sse_app=legacy_sse_app,
        app_name=APP_NAME,
        mcp_path="/mcp",
        get_auth_token=_current_auth_token,
        get_oauth_config=_current_oauth_config,
        get_debug_enabled=_current_debug_mcp_logging,
        get_health_token=_current_health_token,
        get_health_snapshot=_current_health_snapshot,
        session_request_stall_seconds=SESSION_REQUEST_STALL_SECONDS,
        command_timeout_seconds=float(globals().get("COMMAND_TIMEOUT", COMMAND_TIMEOUT)),
        openai_foreground_timeout_seconds=float(
            globals().get("OPENAI_FOREGROUND_TIMEOUT", OPENAI_FOREGROUND_TIMEOUT)
        ),
        cancel_foreground_owner=foreground_process_registry.cancel_owner,
        release_foreground_owner=foreground_process_registry.release_owner,
        instructions=MCP_INSTRUCTIONS,
    )


app = build_http_app()


class _ReadySignalServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, ready_fd: int | None) -> None:
        super().__init__(config)
        self._ready_fd = ready_fd

    def _emit_ready(self) -> None:
        if self._ready_fd is None:
            return
        os.write(self._ready_fd, b"ready\n")
        os.close(self._ready_fd)
        self._ready_fd = None

    def _close_ready_fd(self) -> None:
        if self._ready_fd is None:
            return
        os.close(self._ready_fd)
        self._ready_fd = None

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if not self.should_exit:
            self._emit_ready()

    async def serve(self, sockets=None) -> None:
        try:
            await super().serve(sockets=sockets)
        finally:
            self._close_ready_fd()


def _consume_ready_fd() -> int | None:
    raw_value = os.environ.pop("CHATGPT_MCP_READY_FD", "").strip()
    if not raw_value:
        return None
    return int(raw_value)


def build_uvicorn_server(*, fd: int | None = None, ready_fd: int | None = None) -> uvicorn.Server:
    http_app = build_http_app()
    config = uvicorn.Config(
        http_app,
        host=HOST,
        port=PORT,
        fd=fd,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
    )
    return _ReadySignalServer(config, ready_fd=ready_fd)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the chatgpt-web-oauth-mcp MCP server.")
    parser.add_argument("--fd", type=int, default=None, help="Inherited listening socket fd.")
    args = parser.parse_args(argv)

    ensure_runtime_directories()
    print(f"Starting {APP_NAME} on {HOST}:{PORT}")
    print(f"workspace_root={WORKSPACE_ROOT}")
    print(f"state_dir={STATE_DIR}")
    print("transport=streamable-http")
    print("mcp_path=/mcp")
    print(f"debug_mcp_logging={DEBUG_MCP_LOGGING}")
    print(f"graceful_shutdown_seconds={GRACEFUL_SHUTDOWN_SECONDS}")

    server = build_uvicorn_server(fd=args.fd, ready_fd=_consume_ready_fd())
    server.run()


if __name__ == "__main__":
    main()
