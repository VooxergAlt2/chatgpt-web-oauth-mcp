"""Process-wide configuration loaded from environment variables.

Semantic note on ``WORKSPACE_ROOT`` / ``DEFAULT_CWD``
-----------------------------------------------------
Despite the name "root", this value is **not a sandbox boundary**. The project
is designed to give an authorized MCP client arbitrary
local-shell capability; once a client passes the bearer token it has full shell
and full-filesystem access.

``WORKSPACE_ROOT`` is only used for two things:

1. **Relative-path anchor.** :func:`chatgpt_web_oauth_mcp.pathing.resolve_path`
   joins relative inputs onto it; absolute paths are returned as-is.
2. **Default ``cwd``.** :func:`chatgpt_web_oauth_mcp.pathing.resolve_cwd`
   falls back to it when neither the tool call nor the session-level override
   (``set_default_cwd``) provides a directory.

It therefore behaves like a *default working directory*, not a root. The
``DEFAULT_CWD`` alias below reflects that; ``WORKSPACE_ROOT`` is kept for
API compatibility. The environment variable name ``CHATGPT_MCP_WORKSPACE_ROOT``
is the canonical setting for this project.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .codex_runtime.models import SandboxMode, validate_cua_approval_mode, validate_sandbox
from .job_supervisor import (
    DEFAULT_JOB_LOG_MAX_BYTES,
    DEFAULT_JOB_TIMEOUT_SECONDS,
    MAX_JOB_TIMEOUT_SECONDS,
)
from .response_budget import DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET, resolve_token_budget
from .tool_profiles import normalize_tool_profile


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _positive_env_int(name: str, default: int) -> int:
    value = os.environ.get(name, str(default))
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer; got {value!r}.") from None
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer; got {value!r}.")
    return parsed


APP_NAME = "chatgpt-web-oauth-mcp"
HOST = os.environ.get("CHATGPT_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHATGPT_MCP_PORT", "8766"))
TOOL_PROFILE = normalize_tool_profile(os.environ.get("CHATGPT_MCP_TOOL_PROFILE", "full"))

# Default cwd for tool calls (see module docstring). Kept as WORKSPACE_ROOT
# for API compatibility; DEFAULT_CWD is the preferred name going forward.
WORKSPACE_ROOT = Path(
    os.environ.get("CHATGPT_MCP_WORKSPACE_ROOT", str(Path.home()))
).expanduser().resolve()
DEFAULT_CWD = WORKSPACE_ROOT

STATE_DIR = Path(
    os.environ.get("CHATGPT_MCP_STATE_DIR", str(Path.home() / ".chatgpt-web-oauth-mcp"))
).expanduser().resolve()
CODE_GRAPH_ENABLED = _env_flag("CHATGPT_MCP_CODE_GRAPH_ENABLED", default=True)
JOERN_DOCKER_BINARY = os.environ.get("CHATGPT_MCP_JOERN_DOCKER_BINARY", "docker").strip() or "docker"
JOERN_IMAGE = os.environ.get(
    "CHATGPT_MCP_JOERN_IMAGE",
    (
        "ghcr.io/joernio/joern@"
        "sha256:71a7af77e78d4a84cab0291d2fc1a1490bb27f60fbb64e4f0f1e3191e98b6bc3"
    ),
).strip()
JOERN_VERSION = os.environ.get("CHATGPT_MCP_JOERN_VERSION", "4.0.640").strip() or "4.0.640"
_joern_image_digest = JOERN_IMAGE.rsplit("@sha256:", 1)[-1].lower()
if (
    "@sha256:" not in JOERN_IMAGE
    or len(_joern_image_digest) != 64
    or any(ch not in "0123456789abcdef" for ch in _joern_image_digest)
):
    raise ValueError("CHATGPT_MCP_JOERN_IMAGE must be pinned by a full sha256 digest.")
JOERN_MEMORY_MB = _positive_env_int("CHATGPT_MCP_JOERN_MEMORY_MB", 8192)
JOERN_CPUS = _positive_env_int("CHATGPT_MCP_JOERN_CPUS", 4)
JOERN_PIDS_LIMIT = _positive_env_int("CHATGPT_MCP_JOERN_PIDS_LIMIT", 512)
JOERN_TMPFS_MB = _positive_env_int("CHATGPT_MCP_JOERN_TMPFS_MB", 2048)
JOERN_BUILD_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_JOERN_BUILD_TIMEOUT_SECONDS",
    1800,
)
JOERN_QUERY_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_JOERN_QUERY_TIMEOUT_SECONDS",
    20,
)
JOERN_QUERY_SERVER_START_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_JOERN_QUERY_SERVER_START_TIMEOUT_SECONDS",
    120,
)
JOERN_QUERY_SERVER_MAX_CONTAINERS = _positive_env_int(
    "CHATGPT_MCP_JOERN_QUERY_SERVER_MAX_CONTAINERS",
    1,
)
CODE_GRAPH_CACHE_MAX_BYTES = _positive_env_int(
    "CHATGPT_MCP_CODE_GRAPH_CACHE_MAX_BYTES",
    5 * 1024 * 1024 * 1024,
)
CODE_GRAPH_CACHE_MAX_GRAPHS = _positive_env_int(
    "CHATGPT_MCP_CODE_GRAPH_CACHE_MAX_GRAPHS",
    8,
)
CODE_GRAPH_STAGING_TTL_SECONDS = _positive_env_int(
    "CHATGPT_MCP_CODE_GRAPH_STAGING_TTL_SECONDS",
    24 * 60 * 60,
)
_delegate_state_override = os.environ.get("CHATGPT_MCP_DELEGATE_STATE_DIR", "").strip()
DELEGATE_STATE_DIR = Path(
    _delegate_state_override or str(STATE_DIR / "delegates")
).expanduser().resolve()
LEGACY_DELEGATE_STATE_DIRS = (
    ()
    if _delegate_state_override
    else (Path(tempfile.gettempdir()) / "chatgpt-web-oauth-mcp",)
)
AUTH_TOKEN = os.environ.get("CHATGPT_MCP_AUTH_TOKEN", "").strip()
AUTH_MODE = os.environ.get("CHATGPT_MCP_AUTH_MODE", "").strip().lower()
PUBLIC_BASE_URL = os.environ.get("CHATGPT_MCP_PUBLIC_BASE_URL", "").strip().rstrip("/")
OAUTH_LOGIN_TOKEN = os.environ.get("CHATGPT_MCP_OAUTH_LOGIN_TOKEN", "").strip()
OAUTH_SCOPES = tuple(
    scope
    for scope in os.environ.get("CHATGPT_MCP_OAUTH_SCOPES", "local-ops").split()
    if scope
)
OAUTH_TOKEN_TTL_SECONDS = int(os.environ.get("CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS", "86400"))
OAUTH_REFRESH_TOKEN_TTL_SECONDS = int(
    os.environ.get("CHATGPT_MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS", "2592000")
)
CODEX_COMMAND = os.environ.get("CHATGPT_MCP_CODEX_COMMAND", "codex").strip()
CODEX_RUNTIME_DEFAULT_SANDBOX: SandboxMode = validate_sandbox(
    os.environ.get("CHATGPT_MCP_CODEX_RUNTIME_DEFAULT_SANDBOX", "workspace-write").strip().lower()
)
CODEX_RUNTIME_CUA_APPROVAL_MODE = validate_cua_approval_mode(
    os.environ.get("CHATGPT_MCP_CODEX_RUNTIME_CUA_APPROVAL_MODE", "interactive").strip().lower()
)
CODEX_RUNTIME_CUA_ALLOWED_APPS = tuple(
    dict.fromkeys(
        app.strip()
        for app in os.environ.get("CHATGPT_MCP_CODEX_RUNTIME_CUA_ALLOWED_APPS", "").split(",")
        if app.strip()
    )
)
CODEX_RUNTIME_MAX_CONCURRENCY = _positive_env_int(
    "CHATGPT_MCP_CODEX_RUNTIME_MAX_CONCURRENCY",
    8,
)
CODEX_RUNTIME_MAX_RUNTIMES = _positive_env_int(
    "CHATGPT_MCP_CODEX_RUNTIME_MAX_RUNTIMES",
    64,
)
CODEX_RUNTIME_IDLE_TTL_SECONDS = _positive_env_int(
    "CHATGPT_MCP_CODEX_RUNTIME_IDLE_TTL_SECONDS",
    8 * 60 * 60,
)
CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS",
    15,
)
CODEX_RUNTIME_DEFAULT_TIMEOUT_MS = _positive_env_int(
    "CHATGPT_MCP_CODEX_RUNTIME_DEFAULT_TIMEOUT_MS",
    120000,
)
CODEX_RUNTIME_MAX_TIMEOUT_MS = _positive_env_int(
    "CHATGPT_MCP_CODEX_RUNTIME_MAX_TIMEOUT_MS",
    300000,
)
if CODEX_RUNTIME_DEFAULT_TIMEOUT_MS > CODEX_RUNTIME_MAX_TIMEOUT_MS:
    raise ValueError(
        "CHATGPT_MCP_CODEX_RUNTIME_DEFAULT_TIMEOUT_MS cannot exceed "
        "CHATGPT_MCP_CODEX_RUNTIME_MAX_TIMEOUT_MS."
    )
CODEX_RUNTIME_OUTPUT_MAX_BYTES = _positive_env_int(
    "CHATGPT_MCP_CODEX_RUNTIME_OUTPUT_MAX_BYTES",
    1024 * 1024,
)
CODEX_RUNTIME_MAX_MESSAGE_BYTES = _positive_env_int(
    "CHATGPT_MCP_CODEX_RUNTIME_MAX_MESSAGE_BYTES",
    8 * 1024 * 1024,
)
PI_COMMAND = os.environ.get("CHATGPT_MCP_PI_COMMAND", "pi").strip()
CLAUDE_COMMAND = os.environ.get("CHATGPT_MCP_CLAUDE_COMMAND", "claude").strip()
ANTIGRAVITY_COMMAND = os.environ.get("CHATGPT_MCP_ANTIGRAVITY_COMMAND", "agy").strip()
LOCAL_DELEGATE_ENABLED = _env_flag("CHATGPT_MCP_LOCAL_DELEGATE_ENABLED", False)
LOCAL_DELEGATE_ENDPOINT = (
    os.environ.get("CHATGPT_MCP_LOCAL_DELEGATE_ENDPOINT", "http://127.0.0.1:8081")
    .strip()
    .rstrip("/")
)
LOCAL_DELEGATE_MODEL = (
    os.environ.get("CHATGPT_MCP_LOCAL_DELEGATE_MODEL", "qwen80").strip()
    or "qwen80"
)
LOCAL_DELEGATE_HEALTH_TIMEOUT_MS = _positive_env_int(
    "CHATGPT_MCP_LOCAL_DELEGATE_HEALTH_TIMEOUT_MS",
    350,
)
LOCAL_DELEGATE_REQUEST_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_LOCAL_DELEGATE_REQUEST_TIMEOUT_SECONDS",
    120,
)
LOCAL_DELEGATE_MAX_TURNS = _positive_env_int(
    "CHATGPT_MCP_LOCAL_DELEGATE_MAX_TURNS",
    12,
)
LOCAL_DELEGATE_MAX_TOKENS = _positive_env_int(
    "CHATGPT_MCP_LOCAL_DELEGATE_MAX_TOKENS",
    1200,
)
LOCAL_DELEGATE_MAX_CONCURRENCY = _positive_env_int(
    "CHATGPT_MCP_LOCAL_DELEGATE_MAX_CONCURRENCY",
    4,
)
LOCAL_DELEGATE_ENABLE_THINKING = _env_flag(
    "CHATGPT_MCP_LOCAL_DELEGATE_ENABLE_THINKING",
    False,
)
LOCAL_DELEGATE_UNAVAILABLE_COOLDOWN_SECONDS = _positive_env_int(
    "CHATGPT_MCP_LOCAL_DELEGATE_UNAVAILABLE_COOLDOWN_SECONDS",
    60,
)
ANTIGRAVITY2_ENABLED = _env_flag("CHATGPT_MCP_ANTIGRAVITY2_ENABLED", False)
ANTIGRAVITY2_COMMAND = os.environ.get(
    "CHATGPT_MCP_ANTIGRAVITY2_COMMAND",
    ANTIGRAVITY_COMMAND or "agy",
).strip()
ANTIGRAVITY2_HOME = Path(
    os.environ.get(
        "CHATGPT_MCP_ANTIGRAVITY2_HOME",
        str(STATE_DIR / "antigravity2-home"),
    )
).expanduser().resolve()
ANTIGRAVITY_ELIGIBILITY_WATCHDOG_SCRIPT = Path(
    os.environ.get(
        "CHATGPT_MCP_ANTIGRAVITY_ELIGIBILITY_WATCHDOG_SCRIPT",
        str(Path.home() / ".local" / "bin" / "agy-eligibility-recovery.sh"),
    )
).expanduser().resolve()
ANTIGRAVITY_ELIGIBILITY_WATCHDOG_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_ANTIGRAVITY_ELIGIBILITY_WATCHDOG_TIMEOUT_SECONDS",
    120,
)
QUOTA_ADMISSION_POLICY_PATH = Path(
    os.environ.get(
        "CHATGPT_MCP_QUOTA_ADMISSION_POLICY_PATH",
        str(STATE_DIR / "delegate-quota-policy.json"),
    )
).expanduser().resolve()
HEALTH_USAGE_LIMITS_ENABLED = _env_flag("CHATGPT_MCP_HEALTH_USAGE_LIMITS_ENABLED", True)
HEALTH_USAGE_LIMITS_REFRESH_SECONDS = _positive_env_int(
    "CHATGPT_MCP_HEALTH_USAGE_LIMITS_REFRESH_SECONDS",
    300,
)
HEALTH_USAGE_LIMITS_COMMAND_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_HEALTH_USAGE_LIMITS_COMMAND_TIMEOUT_SECONDS",
    10,
)
HEALTH_USAGE_LIMITS_HTTP_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_HEALTH_USAGE_LIMITS_HTTP_TIMEOUT_SECONDS",
    5,
)
QUOTA_PRIMING_ENABLED = _env_flag("CHATGPT_MCP_QUOTA_PRIMING_ENABLED", True)
QUOTA_PRIMING_CHECK_INTERVAL_SECONDS = _positive_env_int(
    "CHATGPT_MCP_QUOTA_PRIMING_CHECK_INTERVAL_SECONDS",
    30,
)
QUOTA_PRIMING_POST_RESET_DELAY_SECONDS = _positive_env_int(
    "CHATGPT_MCP_QUOTA_PRIMING_POST_RESET_DELAY_SECONDS",
    120,
)
QUOTA_PRIMING_VERIFICATION_DELAY_SECONDS = _positive_env_int(
    "CHATGPT_MCP_QUOTA_PRIMING_VERIFICATION_DELAY_SECONDS",
    5,
)
QUOTA_PRIMING_VERIFICATION_PROBE_DELAY_SECONDS = _positive_env_int(
    "CHATGPT_MCP_QUOTA_PRIMING_VERIFICATION_PROBE_DELAY_SECONDS",
    3,
)
QUOTA_PRIMING_RETRY_SECONDS = _positive_env_int(
    "CHATGPT_MCP_QUOTA_PRIMING_RETRY_SECONDS",
    300,
)
QUOTA_PRIMING_COMMAND_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_QUOTA_PRIMING_COMMAND_TIMEOUT_SECONDS",
    90,
)
QUOTA_PRIMING_MAX_ATTEMPTS_PER_CYCLE = _positive_env_int(
    "CHATGPT_MCP_QUOTA_PRIMING_MAX_ATTEMPTS_PER_CYCLE",
    3,
)
QUOTA_PRIMING_ANTIGRAVITY_GEMINI_MODEL = (
    os.environ.get(
        "CHATGPT_MCP_QUOTA_PRIMING_ANTIGRAVITY_GEMINI_MODEL",
        "gemini-3.8-flash-low",
    ).strip()
    or "gemini-3.8-flash-low"
)
QUOTA_PRIMING_ANTIGRAVITY_THIRD_PARTY_MODEL = (
    os.environ.get(
        "CHATGPT_MCP_QUOTA_PRIMING_ANTIGRAVITY_THIRD_PARTY_MODEL",
        "gpt-oss-120b-medium",
    ).strip()
    or "gpt-oss-120b-medium"
)
QUOTA_PRIMING_CLAUDE_MODEL = (
    os.environ.get("CHATGPT_MCP_QUOTA_PRIMING_CLAUDE_MODEL", "haiku").strip()
    or "haiku"
)
CLAUDE_BYPASS_PERMISSIONS = _env_flag("CHATGPT_MCP_CLAUDE_BYPASS_PERMISSIONS", False)
ANTIGRAVITY_SKIP_PERMISSIONS = _env_flag("CHATGPT_MCP_ANTIGRAVITY_SKIP_PERMISSIONS", False)
ANTIGRAVITY_DEFAULT_MODEL = (
    os.environ.get("CHATGPT_MCP_ANTIGRAVITY_DEFAULT_MODEL", "gemini-3.8-flash").strip()
    or "gemini-3.8-flash"
)
ANTIGRAVITY_DEFAULT_REASONING_EFFORT = (
    os.environ.get("CHATGPT_MCP_ANTIGRAVITY_DEFAULT_REASONING_EFFORT", "high").strip().lower()
    or "high"
)
if ANTIGRAVITY_DEFAULT_REASONING_EFFORT not in {"low", "medium", "high"}:
    raise ValueError(
        "CHATGPT_MCP_ANTIGRAVITY_DEFAULT_REASONING_EFFORT must be low, medium, or high."
    )
DELEGATE_DEFAULT_HARNESS = (
    os.environ.get("CHATGPT_MCP_DELEGATE_DEFAULT_HARNESS", "codex").strip().lower()
    or "codex"
)
DELEGATE_AUTOMATIC_ROUTING = _env_flag("CHATGPT_MCP_DELEGATE_AUTOMATIC_ROUTING", False)
DELEGATE_PRIMARY_HARNESS = (
    os.environ.get("CHATGPT_MCP_DELEGATE_PRIMARY_HARNESS", DELEGATE_DEFAULT_HARNESS).strip().lower()
    or DELEGATE_DEFAULT_HARNESS
)
DELEGATE_FALLBACK_HARNESSES = tuple(
    dict.fromkeys(
        item.strip().lower()
        for item in os.environ.get("CHATGPT_MCP_DELEGATE_FALLBACK_HARNESSES", "").split(",")
        if item.strip()
    )
)
DELEGATE_ROUTING_UNAVAILABLE_COOLDOWN_SECONDS = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_ROUTING_UNAVAILABLE_COOLDOWN_SECONDS",
    15 * 60,
)
COMMAND_TIMEOUT = int(os.environ.get("CHATGPT_MCP_COMMAND_TIMEOUT", "300"))
OPENAI_FOREGROUND_TIMEOUT = _positive_env_int(
    "CHATGPT_MCP_OPENAI_FOREGROUND_TIMEOUT",
    30,
)
DELEGATE_TIMEOUT = _positive_env_int("CHATGPT_MCP_DELEGATE_TIMEOUT", 300)
DELEGATE_WAIT_TIMEOUT = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_WAIT_TIMEOUT",
    DELEGATE_TIMEOUT,
)
DELEGATE_EXPLORE_EXECUTION_TIMEOUT = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_EXPLORE_EXECUTION_TIMEOUT",
    900,
)
DELEGATE_CODE_EXECUTION_TIMEOUT = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_CODE_EXECUTION_TIMEOUT",
    3600,
)
DELEGATE_CANCEL_GRACE_SECONDS = float(
    os.environ.get("CHATGPT_MCP_DELEGATE_CANCEL_GRACE_SECONDS", "5")
)
DELEGATE_EXPLORE_MAX_PER_PROJECT = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_EXPLORE_MAX_PER_PROJECT",
    4,
)
DELEGATE_EXPLORE_MAX_GLOBAL = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_EXPLORE_MAX_GLOBAL",
    8,
)
DELEGATE_CODE_MAX_PER_PROJECT = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_CODE_MAX_PER_PROJECT",
    2,
)
DELEGATE_CODE_MAX_GLOBAL = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_CODE_MAX_GLOBAL",
    4,
)
DELEGATE_QUEUE_LIMIT_PER_PROJECT = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_QUEUE_LIMIT_PER_PROJECT",
    32,
)
DELEGATE_QUEUE_LIMIT_GLOBAL = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_QUEUE_LIMIT_GLOBAL",
    128,
)
JOB_RETENTION_SECONDS = _positive_env_int(
    "CHATGPT_MCP_JOB_RETENTION_SECONDS",
    7 * 86400,
)
JOB_MAX_TERMINAL_RECORDS = _positive_env_int(
    "CHATGPT_MCP_JOB_MAX_TERMINAL_RECORDS",
    500,
)
JOB_DEFAULT_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_JOB_DEFAULT_TIMEOUT_SECONDS",
    DEFAULT_JOB_TIMEOUT_SECONDS,
)
JOB_MAX_TIMEOUT_SECONDS = _positive_env_int(
    "CHATGPT_MCP_JOB_MAX_TIMEOUT_SECONDS",
    MAX_JOB_TIMEOUT_SECONDS,
)
if JOB_DEFAULT_TIMEOUT_SECONDS > JOB_MAX_TIMEOUT_SECONDS:
    raise ValueError(
        "CHATGPT_MCP_JOB_DEFAULT_TIMEOUT_SECONDS cannot exceed "
        "CHATGPT_MCP_JOB_MAX_TIMEOUT_SECONDS."
    )
JOB_LOG_MAX_BYTES = _positive_env_int(
    "CHATGPT_MCP_JOB_LOG_MAX_BYTES",
    DEFAULT_JOB_LOG_MAX_BYTES,
)
DELEGATE_RETENTION_SECONDS = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_RETENTION_SECONDS",
    7 * 86400,
)
DELEGATE_MAX_TERMINAL_RECORDS = _positive_env_int(
    "CHATGPT_MCP_DELEGATE_MAX_TERMINAL_RECORDS",
    1000,
)
TOOL_OUTPUT_TOKEN_BUDGET = resolve_token_budget(
    os.environ.get(
        "CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET",
        str(DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET),
    ),
    global_name="CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET",
)
READ_TOKEN_BUDGET = resolve_token_budget(
    TOOL_OUTPUT_TOKEN_BUDGET,
    os.environ.get("CHATGPT_MCP_READ_TOKEN_BUDGET"),
    global_name="CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET",
    tool_name="CHATGPT_MCP_READ_TOKEN_BUDGET",
)
RUN_TOKEN_BUDGET = resolve_token_budget(
    TOOL_OUTPUT_TOKEN_BUDGET,
    os.environ.get("CHATGPT_MCP_RUN_TOKEN_BUDGET"),
    global_name="CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET",
    tool_name="CHATGPT_MCP_RUN_TOKEN_BUDGET",
)
JOB_OUTPUT_TOKEN_BUDGET = resolve_token_budget(
    TOOL_OUTPUT_TOKEN_BUDGET,
    os.environ.get("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET"),
    global_name="CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET",
    tool_name="CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET",
)
RUN_CAPTURE_MAX_BYTES = _positive_env_int(
    "CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES",
    1024 * 1024,
)
RIPGREP_BINARY = os.environ.get("CHATGPT_MCP_RIPGREP_BINARY", "rg").strip() or "rg"
TMUX_BINARY = os.environ.get("CHATGPT_MCP_TMUX_BINARY", "tmux").strip() or "tmux"
TMUX_SOCKET_NAME = os.environ.get("CHATGPT_MCP_TMUX_SOCKET_NAME", "default").strip() or "default"
TMUX_CONTROL_TIMEOUT = int(os.environ.get("CHATGPT_MCP_TMUX_CONTROL_TIMEOUT", "10"))
DEBUG_MCP_LOGGING = _env_flag("CHATGPT_MCP_DEBUG_MCP_LOGGING", default=False)
HEALTH_TOKEN = os.environ.get("CHATGPT_MCP_HEALTH_TOKEN", "").strip()
SESSION_IDLE_TTL_SECONDS = _positive_env_int(
    "CHATGPT_MCP_SESSION_IDLE_TTL_SECONDS",
    24 * 60 * 60,
)
SESSION_EPHEMERAL_IDLE_TTL_SECONDS = _positive_env_int(
    "CHATGPT_MCP_SESSION_EPHEMERAL_IDLE_TTL_SECONDS",
    5 * 60,
)
SESSION_CHECKPOINT_TTL_SECONDS = _positive_env_int(
    "CHATGPT_MCP_SESSION_CHECKPOINT_TTL_SECONDS",
    24 * 60 * 60,
)
SESSION_ACTIVE_WINDOW_SECONDS = _positive_env_int(
    "CHATGPT_MCP_SESSION_ACTIVE_WINDOW_SECONDS",
    10 * 60,
)
SESSION_REQUEST_STALL_SECONDS = _positive_env_int(
    "CHATGPT_MCP_SESSION_REQUEST_STALL_SECONDS",
    180,
)
SESSION_ORCHESTRATION_QUIET_SECONDS = _positive_env_int(
    "CHATGPT_MCP_SESSION_ORCHESTRATION_QUIET_SECONDS",
    180,
)
HEALTH_SESSION_LIMIT = _positive_env_int(
    "CHATGPT_MCP_HEALTH_SESSION_LIMIT",
    20,
)
GRACEFUL_SHUTDOWN_SECONDS = int(
    os.environ.get("CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS", "30")
)


def ensure_runtime_directories() -> None:
    if not WORKSPACE_ROOT.exists():
        raise FileNotFoundError(f"Default cwd does not exist: {WORKSPACE_ROOT}")
    if not WORKSPACE_ROOT.is_dir():
        raise NotADirectoryError(f"Default cwd is not a directory: {WORKSPACE_ROOT}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    DELEGATE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    if ANTIGRAVITY2_ENABLED:
        ANTIGRAVITY2_HOME.mkdir(parents=True, exist_ok=True)
    for directory in (
        STATE_DIR,
        DELEGATE_STATE_DIR,
        *((ANTIGRAVITY2_HOME,) if ANTIGRAVITY2_ENABLED else ()),
    ):
        try:
            directory.chmod(0o700)
        except OSError:
            pass
