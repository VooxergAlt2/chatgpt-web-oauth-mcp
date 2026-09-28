from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from collections import deque
from pathlib import Path, PureWindowsPath

from .delegate_harnesses import (
    CodexHarness,
    DelegateHarness,
    PiHarness,
    command_available,
)
from .delegate_models import (
    TERMINAL_TASK_STATES,
    DelegateGroup,
    DelegateLogPaths,
    DelegateTask,
    ProjectIdentity,
    TaskKind,
)
from .delegate_process import (
    TIMEOUT_EXIT_CODE,
    DelegateProcessRunner,
    Invocation,
    extract_structured_output,
    harness_display_name,
    log_read_hint,
    write_private_json,
    write_private_text,
)
from .delegate_project import ProjectIdentityResolver
from .delegate_scheduler import (
    DelegateQueueFullError,
    DelegateScheduler,
    DelegateSchedulerShuttingDownError,
)
from .delegate_telemetry import DelegateTelemetryStore
from .quota_admission import DelegateQuotaAdmissionGate
from .job_supervisor import (
    process_group_exists,
    process_group_matches_snapshot,
    process_identity_matches,
    snapshot_process_group,
)
from .process_env import sanitized_child_env
from .response_budget import (
    DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
    ResponseBudget,
    with_budget_metadata,
)
from .state_io import (
    ensure_private_directory,
    ensure_private_file,
    interprocess_file_lock,
)


ALLOWED_COMMIT_MODES = {"allowed", "required", "forbidden"}
ALLOWED_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
ALLOWED_TASK_KINDS = {"explore", "code"}
DEFAULT_MODEL = "default"
DEFAULT_REASONING_EFFORT = "default"
DEFAULT_EXPLORE_MODEL = "gpt-5.6-luna"
DEFAULT_EXPLORE_REASONING_EFFORT = "low"
DEFAULT_CODE_MODEL = "gpt-5.6-sol"
DEFAULT_CODE_REASONING_EFFORT = "xhigh"
DEFAULT_DELEGATE_WAIT_SECONDS = 300.0
DEFAULT_EXPLORE_EXECUTION_TIMEOUT_SECONDS = 900
DEFAULT_CODE_EXECUTION_TIMEOUT_SECONDS = 3600
DEFAULT_CANCEL_GRACE_SECONDS = 5.0
DELEGATE_STALL_HINT_SECONDS = 180.0
DEFAULT_DELEGATE_HISTORY_LIMIT = 20
DEFAULT_DELEGATE_SCHEDULER_TERMINAL_LIMIT = 16
DEFAULT_DELEGATE_STATUS_POLL_SECONDS = 5.0
MAX_DELEGATE_STATUS_WATCH_SECONDS = 300.0
_DELEGATE_MAINTENANCE_INTERVAL_SECONDS = 300.0
IS_WINDOWS = os.name == "nt"
_PERSISTED_DELEGATE_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_PERSISTED_GROUP_ID_RE = re.compile(r"^grp-[0-9a-f]{12}$")
_PERSISTED_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "timed_out"}


def _split_command(command: str) -> list[str]:
    return shlex.split(command)


def _binary_name(binary: str) -> str:
    if IS_WINDOWS:
        return PureWindowsPath(binary).stem.lower()
    return Path(binary).stem.lower()


def _resolve_delegate_command_parts(command: str) -> list[str]:
    parts = _split_command(command)
    if not IS_WINDOWS or not parts:
        return parts
    if _binary_name(parts[0]) != "codex":
        return parts
    resolved = shutil.which(parts[0])
    if resolved:
        parts[0] = resolved
    return parts


def _command_available(command: str | None) -> bool:
    return command_available(command)


def _normalize_reasoning_effort(reasoning_effort: str | None) -> str | None:
    normalized = (reasoning_effort or "").strip().lower()
    if not normalized or normalized == DEFAULT_REASONING_EFFORT:
        return None
    return normalized


def _normalize_model(model: str | None) -> str | None:
    normalized = (model or "").strip()
    if not normalized or normalized.lower() == DEFAULT_MODEL:
        return None
    return normalized


def _extract_structured_output(text: str) -> object | None:
    return extract_structured_output(text)


def _default_delegate_state_root() -> Path:
    configured = os.environ.get("CHATGPT_MCP_DELEGATE_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    state_dir = Path(
        os.environ.get(
            "CHATGPT_MCP_STATE_DIR",
            str(Path.home() / ".chatgpt-web-oauth-mcp"),
        )
    ).expanduser().resolve()
    return state_dir / "delegates"


def _delegate_log_root_for_harness(
    harness: str,
    *,
    state_root: Path | None = None,
) -> Path:
    safe_name = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in harness.lower()
    ).strip("-") or "cli"
    root = state_root if state_root is not None else _default_delegate_state_root()
    if safe_name == "codex":
        return root / "codex-delegates"
    return root / f"{safe_name}-delegates"


def _create_delegate_logs(
    delegate_id: str,
    *,
    harness: str = "codex",
    state_root: Path | None = None,
) -> DelegateLogPaths:
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_dir = _delegate_log_root_for_harness(
        harness,
        state_root=state_root,
    ) / f"{timestamp}-{delegate_id}"
    log_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    ensure_private_directory(log_dir)
    return DelegateLogPaths(
        log_dir=log_dir,
        prompt=log_dir / "prompt.txt",
        stdout=log_dir / "stdout.log",
        stderr=log_dir / "stderr.log",
        metadata=log_dir / "metadata.json",
    )


def _format_epoch_seconds(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _delegate_request_fingerprint(
    *,
    task: str | None,
    goal: str | None,
    task_id: str | None,
    cwd: Path,
    harness: str,
    kind: str,
    group_id: str | None,
    files_in_scope: list[str] | None,
    out_of_scope: list[str] | None,
    context_files: list[str] | None,
    acceptance_criteria: list[str] | None,
    done_means: list[str] | None,
    verification_commands: list[str] | None,
    commit_mode: str,
    model: str,
    reasoning_effort: str,
    output_schema: dict[str, object] | None,
    parse_structured_output: bool,
    depends_on_group_ids: list[str] | None,
    resume_conversation_id: str | None = None,
) -> str:
    payload = {
        "task": task or "",
        "goal": goal or "",
        "task_id": task_id or "",
        "cwd": str(cwd),
        "harness": harness,
        "kind": kind,
        "group_id": group_id or "",
        "files_in_scope": files_in_scope or [],
        "out_of_scope": out_of_scope or [],
        "context_files": context_files or [],
        "acceptance_criteria": acceptance_criteria or [],
        "done_means": done_means or [],
        "verification_commands": verification_commands or [],
        "commit_mode": commit_mode,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "output_schema": output_schema or None,
        "parse_structured_output": parse_structured_output,
        "depends_on_group_ids": depends_on_group_ids or [],
        "resume_conversation_id": resume_conversation_id or "",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _status_entry_lifecycle_signature(entry: dict[str, object] | None) -> tuple[object, ...]:
    if not entry:
        return ("none",)
    error = entry.get("error")
    error_outcome: object = error
    if isinstance(error, dict):
        error_outcome = (error.get("code"), error.get("message"))
    counts = entry.get("counts")
    count_signature: tuple[object, ...] = ()
    if isinstance(counts, dict):
        count_signature = tuple(
            counts.get(key)
            for key in ("queued", "running", "succeeded", "failed", "cancelled", "timed_out")
        )
    return (
        entry.get("delegate_id") or entry.get("group_id"),
        entry.get("status"),
        entry.get("completed"),
        entry.get("in_progress"),
        entry.get("success"),
        entry.get("exit_code"),
        entry.get("timed_out"),
        entry.get("activity_state"),
        entry.get("stdout_bytes"),
        entry.get("stderr_bytes"),
        error_outcome,
        *count_signature,
    )


def _status_response_lifecycle_signature(payload: dict[str, object]) -> tuple[object, ...]:
    for key in ("delegate", "group", "project"):
        entry = payload.get(key)
        if isinstance(entry, dict):
            return (key, *_status_entry_lifecycle_signature(entry))
    latest = payload.get("active") or payload.get("latest")
    return ("list", *_status_entry_lifecycle_signature(latest if isinstance(latest, dict) else None))


def _status_focus_entry(payload: dict[str, object]) -> dict[str, object] | None:
    for key in ("delegate", "group", "project", "active", "latest"):
        entry = payload.get(key)
        if isinstance(entry, dict):
            return entry
    return None


class ExecutorRegistry:
    """Compatibility facade over project-scoped delegate scheduling."""

    def __init__(
        self,
        *,
        codex_command: str | None = None,
        pi_command: str | None = None,
        default_harness: str = "codex",
        harnesses: list[DelegateHarness] | tuple[DelegateHarness, ...] | None = None,
        max_explore_per_project: int = 4,
        max_explore_global: int = 8,
        max_code_per_project: int = 1,
        max_code_global: int = 4,
        queue_limit_per_project: int = 32,
        queue_limit_global: int = 128,
        explore_execution_timeout_seconds: int = DEFAULT_EXPLORE_EXECUTION_TIMEOUT_SECONDS,
        code_execution_timeout_seconds: int = DEFAULT_CODE_EXECUTION_TIMEOUT_SECONDS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
        allow_unsafe_explore_command: bool = False,
        durable_job_registry: object | None = None,
        durable_state_dir: Path | None = None,
        durable_harnesses: tuple[str, ...] = ("antigravity", "antigravity2"),
        delegate_state_root: Path | None = None,
        legacy_delegate_state_roots: tuple[Path, ...] = (),
        telemetry_state_path: Path | None = None,
        quota_admission_gate: DelegateQuotaAdmissionGate | None = None,
        delegate_retention_seconds: float = 7 * 86400,
        max_terminal_delegate_records: int = 1000,
    ) -> None:
        self.codex_command = codex_command
        self.pi_command = pi_command
        self.default_harness = default_harness.strip().lower() or "codex"
        configured_harnesses: dict[str, DelegateHarness] = {
            "codex": CodexHarness(
                command=codex_command,
                allow_unsafe_explore_command=allow_unsafe_explore_command,
            ),
        }
        if pi_command is not None:
            configured_harnesses["pi"] = PiHarness(command=pi_command)
        for adapter in harnesses or ():
            configured_harnesses[adapter.name.strip().lower()] = adapter
        self.harnesses = configured_harnesses
        self.explore_execution_timeout_seconds = max(1, int(explore_execution_timeout_seconds))
        self.code_execution_timeout_seconds = max(1, int(code_execution_timeout_seconds))
        self.cancel_grace_seconds = max(0.0, float(cancel_grace_seconds))
        self.allow_unsafe_explore_command = bool(allow_unsafe_explore_command)
        self.durable_job_registry = durable_job_registry
        self.durable_state_dir = durable_state_dir
        self.delegate_state_root = (
            Path(delegate_state_root).expanduser().resolve()
            if delegate_state_root is not None
            else _default_delegate_state_root()
        )
        self.legacy_delegate_state_roots = tuple(
            root
            for root in dict.fromkeys(
                Path(item).expanduser().resolve()
                for item in legacy_delegate_state_roots
            )
            if root != self.delegate_state_root
        )
        self.delegate_retention_seconds = max(60.0, float(delegate_retention_seconds))
        self.max_terminal_delegate_records = max(1, int(max_terminal_delegate_records))
        self._maintenance_lock = threading.Lock()
        self._last_delegate_maintenance = 0.0
        self._last_delegate_migration: dict[str, object] | None = None
        self.telemetry = (
            DelegateTelemetryStore(telemetry_state_path)
            if telemetry_state_path is not None
            else None
        )
        self.quota_admission_gate = quota_admission_gate
        self.durable_harnesses = frozenset(
            item.strip().lower() for item in durable_harnesses if item.strip()
        )
        self.project_resolver = ProjectIdentityResolver()
        self._process_runner = DelegateProcessRunner(
            popen_factory=lambda *args, **kwargs: subprocess.Popen(*args, **kwargs)
        )
        self._history: deque[dict[str, object]] = deque(maxlen=DEFAULT_DELEGATE_HISTORY_LIMIT)
        self._group_history: deque[dict[str, object]] = deque(maxlen=DEFAULT_DELEGATE_HISTORY_LIMIT)
        self._last_scheduler_prune: dict[str, int] | None = None
        self._persisted_delegate_paths: dict[str, Path] = {}
        self._last_recovery_summary: dict[str, object] | None = None
        self._submitted_seq = 0
        self.scheduler = DelegateScheduler(
            runner=self._run_scheduled_task,
            terminator=self._terminate_task,
            on_terminal=self._on_task_terminal,
            cancelled_result_factory=self._cancelled_result,
            max_explore_per_project=max_explore_per_project,
            max_explore_global=max_explore_global,
            max_code_per_project=max_code_per_project,
            max_code_global=max_code_global,
            queue_limit_per_project=queue_limit_per_project,
            queue_limit_global=queue_limit_global,
        )
        # Kept for callers/tests that used the old registry synchronization hook.
        self._lock = self.scheduler.lock

    def harness_info(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for name in sorted(self.harnesses):
            _, adapter = self._resolve_harness(name)
            if adapter is not None:
                info = adapter.info()
                if self.quota_admission_gate is not None and bool(info.get("available")):
                    defaults = adapter.task_defaults("explore")
                    quota = self.quota_admission_gate.decision(
                        harness=name,
                        model=defaults.model,
                    )
                    info["quota_admission"] = quota
                durable = (
                    os.name == "posix"
                    and self.durable_job_registry is not None
                    and self.durable_state_dir is not None
                    and name in self.durable_harnesses
                )
                info["durable_execution"] = durable
                info["durability_backend"] = "job_registry" if durable else None
                result[name] = info
        return result

    def routing_guidance(self) -> dict[str, object]:
        harnesses = self.harness_info()

        def available(name: str, *, read_only: bool = False) -> bool:
            info = harnesses.get(name)
            if not isinstance(info, dict):
                return False
            quota = info.get("quota_admission")
            if isinstance(quota, dict) and quota.get("allowed") is False:
                return False
            if read_only:
                return bool(info.get("explore_available")) and bool(
                    info.get("read_only_supported")
                )
            return bool(info.get("available"))

        def choose(preferred: str, *, read_only: bool = False) -> str | None:
            if available(preferred, read_only=read_only):
                return preferred
            if available(self.default_harness, read_only=read_only):
                return self.default_harness
            return next(
                (
                    name
                    for name in sorted(harnesses)
                    if available(name, read_only=read_only)
                ),
                None,
            )

        explore_harness = choose("codex", read_only=True)
        review_harness = choose("antigravity", read_only=True)
        code_harness = choose("codex")
        explore_reason = (
            "cheap bounded repository discovery"
            if explore_harness == "codex"
            else (
                "available read-only fallback for bounded repository discovery"
                if explore_harness is not None
                else "no compatible read-only delegate harness available"
            )
        )
        return {
            "automatic_routing": False,
            "explicit_harness_override_preserved": True,
            "principles": [
                "Use direct MCP tools for deterministic local inspection or commands.",
                "Delegate one bounded slice only when an independent agent adds value.",
                "Prefer read-only exploration before code when implementation scope is uncertain.",
                "Use telemetry as operator evidence, not as an automatic model score.",
            ],
            "profiles": {
                "bounded_explore": {
                    "kind": "explore",
                    "preferred_harness": explore_harness,
                    "reason": explore_reason,
                },
                "independent_review": {
                    "kind": "explore",
                    "preferred_harness": review_harness,
                    "reason": "independent second-pass review or broad synthesis",
                },
                "implementation": {
                    "kind": "code",
                    "preferred_harness": code_harness,
                    "reason": "one project-scoped writer slice",
                },
            },
            "continuation": {
                "prefer_resume_for_same_review": (
                    available("antigravity", read_only=True)
                    or available("antigravity2", read_only=True)
                ),
                "resume_parameter": "resume_from_delegate_id",
            },
        }

    @staticmethod
    def _project_prompt_context(
        project: ProjectIdentity,
        *,
        cwd: Path,
    ) -> list[str]:
        if project.git_common_dir is None:
            return [f"Project root: {project.project_root}"]
        lines = [
            f"Project root: {project.project_root}",
            "Repository baseline: inspect the current working tree before writing.",
        ]
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), "rev-parse", "--verify", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                env=sanitized_child_env(),
                close_fds=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        if completed is not None and completed.returncode == 0:
            head = completed.stdout.strip()
            if re.fullmatch(r"[0-9a-fA-F]{40,64}", head):
                lines.append(f"Git HEAD at submission: {head[:12]}")
        return lines

    def runtime_info(self) -> dict[str, object]:
        """Return bounded operator-facing delegate scheduler/recovery state."""

        with self._lock:
            tasks = list(self.scheduler.tasks.values())
            groups = list(self.scheduler.groups.values())
            task_counts = self.scheduler.task_counts(tasks)
            group_counts: dict[str, int] = {"total": len(groups)}
            for group in groups:
                group_counts[group.state] = group_counts.get(group.state, 0) + 1
            durable_tasks = [task for task in tasks if task.durable_job_id]
            recovery = (
                dict(self._last_recovery_summary)
                if self._last_recovery_summary is not None
                else None
            )
            last_prune = (
                dict(self._last_scheduler_prune)
                if self._last_scheduler_prune is not None
                else None
            )
            result: dict[str, object] = {
                "status": "shutting_down" if self.scheduler.is_shutting_down else "ready",
                "tasks": task_counts,
                "groups": group_counts,
                "active_projects": sum(
                    1
                    for lane in self.scheduler.lanes.values()
                    if lane.pending or lane.active_explores or lane.active_code is not None
                ),
                "durable": {
                    "enabled_harnesses": sorted(self.durable_harnesses),
                    "backend": (
                        "job_registry"
                        if self.durable_job_registry is not None
                        and self.durable_state_dir is not None
                        else None
                    ),
                    "state_dir": (
                        str(self.durable_state_dir)
                        if self.durable_state_dir is not None
                        else None
                    ),
                    "tracked": len(durable_tasks),
                    "running": sum(
                        1 for task in durable_tasks if task.state == "running"
                    ),
                    "recovered_running": sum(
                        1
                        for task in durable_tasks
                        if task.state == "running" and task.recovered_from_disk
                    ),
                },
                "recovered_tasks": sum(
                    1 for task in tasks if task.recovered_from_disk
                ),
                "persisted_delegate_records": len(self._persisted_delegate_paths),
                "memory": {
                    "scheduler_tasks": len(tasks),
                    "scheduler_groups": len(groups),
                    "scheduler_lanes": len(self.scheduler.lanes),
                    "task_history": len(self._history),
                    "group_history": len(self._group_history),
                    "history_limit": DEFAULT_DELEGATE_HISTORY_LIMIT,
                    "scheduler_terminal_limit": DEFAULT_DELEGATE_SCHEDULER_TERMINAL_LIMIT,
                    "last_prune": last_prune,
                },
                "state": {
                    "root": str(self.delegate_state_root),
                    "legacy_roots": [
                        str(root) for root in self.legacy_delegate_state_roots
                    ],
                    "last_migration": (
                        dict(self._last_delegate_migration)
                        if self._last_delegate_migration is not None
                        else None
                    ),
                },
                "last_recovery": recovery,
            }
        result["telemetry"] = (
            self.telemetry.snapshot()
            if self.telemetry is not None
            else {"enabled": False}
        )
        result["quota_admission"] = (
            self.quota_admission_gate.snapshot()
            if self.quota_admission_gate is not None
            else {"enabled": False}
        )
        return result

    def note_delegate_consumed(self, delegate_id: str) -> bool:
        if self.telemetry is None:
            return False
        return self.telemetry.mark_consumed(delegate_id)

    def _record_terminal_telemetry(
        self,
        snapshot: dict[str, object],
        *,
        metadata_path: Path | None = None,
        completed_at_epoch: float | None = None,
    ) -> None:
        if self.telemetry is None:
            return
        timestamp = completed_at_epoch
        if timestamp is None:
            timestamp = self._telemetry_completed_at_epoch(
                snapshot,
                metadata_path=metadata_path,
            )
        try:
            self.telemetry.record_terminal(
                snapshot,
                completed_at_epoch=timestamp,
            )
        except (OSError, TypeError, ValueError):
            pass

    @staticmethod
    def _telemetry_completed_at_epoch(
        snapshot: dict[str, object],
        *,
        metadata_path: Path | None,
    ) -> float | None:
        source_mtime = snapshot.get("state_source_metadata_mtime_epoch")
        if isinstance(source_mtime, (int, float)) and not isinstance(
            source_mtime,
            bool,
        ):
            return float(source_mtime)
        completed_at = snapshot.get("completed_at_epoch")
        if isinstance(completed_at, (int, float)) and not isinstance(
            completed_at,
            bool,
        ):
            return float(completed_at)
        if metadata_path is None:
            return None
        try:
            return metadata_path.stat().st_mtime
        except OSError:
            return None

    @staticmethod
    def _should_backfill_terminal_telemetry(
        snapshot: dict[str, object],
    ) -> bool:
        return not bool(snapshot.get("state_migrated_from"))

    def _delegate_scan_roots(self) -> list[Path]:
        roots: list[Path] = []
        for state_root in (
            self.delegate_state_root,
            *self.legacy_delegate_state_roots,
        ):
            for harness in sorted(self.harnesses):
                root = _delegate_log_root_for_harness(
                    harness,
                    state_root=state_root,
                )
                if root not in roots:
                    roots.append(root)
        return roots

    @staticmethod
    def _rewrite_migrated_log_paths(
        payload: dict[str, object],
        *,
        source_dir: Path,
        target_dir: Path,
        target_metadata: Path,
    ) -> dict[str, object]:
        migrated = dict(payload)
        logs = migrated.get("logs")
        if isinstance(logs, dict):
            rewritten = dict(logs)
            for key, value in logs.items():
                if not isinstance(value, str) or not value:
                    continue
                value_path = Path(value)
                if value_path.is_absolute():
                    try:
                        relative = value_path.relative_to(source_dir)
                    except ValueError:
                        try:
                            relative = value_path.resolve().relative_to(
                                source_dir.resolve()
                            )
                        except (OSError, ValueError):
                            continue
                else:
                    if ".." in value_path.parts:
                        continue
                    relative = value_path
                rewritten[key] = str(target_dir / relative)
            rewritten["log_dir"] = str(target_dir)
            rewritten["metadata"] = str(target_metadata)
            migrated["logs"] = rewritten
        migrated.pop("log_read_hint", None)
        migrated["state_migrated_from"] = str(source_dir)
        migrated["state_migrated_at_epoch"] = time.time()
        return migrated

    def migrate_legacy_persisted_delegates(self) -> dict[str, object]:
        """Move recoverable legacy delegate records into canonical state."""

        ensure_private_directory(self.delegate_state_root)
        with interprocess_file_lock(
            self.delegate_state_root / ".migration.lock"
        ):
            return self._migrate_legacy_persisted_delegates_locked()

    def _migrate_legacy_persisted_delegates_locked(self) -> dict[str, object]:
        migrated = 0
        unattributed_removed = 0
        duplicates_reconciled = 0
        conflicts = 0
        skipped = 0
        errors = 0

        for legacy_state_root in self.legacy_delegate_state_roots:
            for harness in sorted(self.harnesses):
                source_root = _delegate_log_root_for_harness(
                    harness,
                    state_root=legacy_state_root,
                )
                target_root = _delegate_log_root_for_harness(
                    harness,
                    state_root=self.delegate_state_root,
                )
                if not source_root.is_dir() or source_root.is_symlink():
                    continue
                ensure_private_directory(target_root)
                try:
                    metadata_paths = list(source_root.glob("*/metadata.json"))
                except OSError:
                    errors += 1
                    continue

                for metadata_path in metadata_paths:
                    payload = self._read_persisted_delegate_metadata(metadata_path)
                    if payload is None:
                        skipped += 1
                        continue
                    delegate_id = str(payload.get("delegate_id") or "")
                    if not _PERSISTED_DELEGATE_ID_RE.fullmatch(delegate_id):
                        skipped += 1
                        continue

                    status = str(payload.get("status") or "").lower()
                    terminal = (
                        status in _PERSISTED_TERMINAL_STATES
                        or bool(payload.get("completed"))
                    )
                    if not terminal:
                        durable = payload.get("durable") is True and isinstance(
                            payload.get("durable_job_id"),
                            str,
                        )
                        attributed = isinstance(
                            payload.get("owner_pid"),
                            int,
                        ) and isinstance(
                            payload.get("owner_process_identity"),
                            str,
                        )
                        if not durable and not attributed:
                            try:
                                log_dir = metadata_path.parent
                                if (
                                    not log_dir.is_symlink()
                                    and log_dir.parent.resolve() == source_root.resolve()
                                ):
                                    shutil.rmtree(log_dir)
                                    unattributed_removed += 1
                            except OSError:
                                errors += 1
                        continue

                    source_dir = metadata_path.parent
                    target_dir = target_root / source_dir.name
                    target_metadata = target_dir / "metadata.json"
                    if target_dir.exists():
                        target_payload = self._read_persisted_delegate_metadata(
                            target_metadata
                        )
                        target_status = (
                            str(target_payload.get("status") or "").lower()
                            if isinstance(target_payload, dict)
                            else ""
                        )
                        if (
                            isinstance(target_payload, dict)
                            and str(target_payload.get("delegate_id") or "")
                            == delegate_id
                            and (
                                target_status in _PERSISTED_TERMINAL_STATES
                                or bool(target_payload.get("completed"))
                            )
                        ):
                            try:
                                if (
                                    not source_dir.is_symlink()
                                    and source_dir.parent.resolve()
                                    == source_root.resolve()
                                ):
                                    shutil.rmtree(source_dir)
                                    duplicates_reconciled += 1
                            except OSError:
                                errors += 1
                        else:
                            conflicts += 1
                        continue

                    staging_dir = target_root / (
                        f".{source_dir.name}.migrating-{uuid.uuid4().hex}"
                    )
                    try:
                        if (
                            source_dir.is_symlink()
                            or source_dir.parent.resolve() != source_root.resolve()
                        ):
                            skipped += 1
                            continue
                        for stale in target_root.glob(
                            f".{source_dir.name}.migrating-*"
                        ):
                            if stale.is_dir() and not stale.is_symlink():
                                shutil.rmtree(stale)
                        shutil.copytree(source_dir, staging_dir)
                        migrated_payload = self._rewrite_migrated_log_paths(
                            payload,
                            source_dir=source_dir,
                            target_dir=target_dir,
                            target_metadata=target_metadata,
                        )
                        try:
                            migrated_payload["state_source_metadata_mtime_epoch"] = (
                                metadata_path.stat().st_mtime
                            )
                        except OSError:
                            pass
                        write_private_json(
                            staging_dir / "metadata.json",
                            migrated_payload,
                        )
                        staging_dir.rename(target_dir)
                        shutil.rmtree(source_dir)
                        migrated += 1
                    except OSError:
                        errors += 1
                        try:
                            if staging_dir.exists():
                                shutil.rmtree(staging_dir)
                        except OSError:
                            pass

        summary: dict[str, object] = {
            "success": errors == 0,
            "migrated_terminal": migrated,
            "unattributed_removed": unattributed_removed,
            "duplicates_reconciled": duplicates_reconciled,
            "conflicts": conflicts,
            "skipped": skipped,
            "errors": errors,
            "canonical_root": str(self.delegate_state_root),
            "legacy_roots": [
                str(root) for root in self.legacy_delegate_state_roots
            ],
        }
        with self._lock:
            self._last_delegate_migration = dict(summary)
        return summary

    def maintain_persisted_delegates(self, *, force: bool = False) -> dict[str, object]:
        """Prune terminal delegate records across canonical and legacy roots."""

        now_monotonic = time.monotonic()
        with self._maintenance_lock:
            if (
                not force
                and now_monotonic - self._last_delegate_maintenance
                < _DELEGATE_MAINTENANCE_INTERVAL_SECONDS
            ):
                return {"success": True, "skipped": True, "reason": "maintenance_throttled"}
            self._last_delegate_maintenance = now_monotonic

        cutoff = time.time() - self.delegate_retention_seconds
        removed_delegate_records: list[tuple[str, Path]] = []
        removed = 0
        errors = 0
        terminal_records = 0
        per_harness: dict[str, dict[str, int]] = {}
        for harness in sorted(self.harnesses):
            roots = [
                _delegate_log_root_for_harness(
                    harness,
                    state_root=state_root,
                )
                for state_root in (
                    self.delegate_state_root,
                    *self.legacy_delegate_state_roots,
                )
            ]
            terminal: list[tuple[float, Path, Path, str]] = []
            for root in roots:
                if not root.is_dir() or root.is_symlink():
                    continue
                try:
                    metadata_paths = list(root.glob("*/metadata.json"))
                except OSError:
                    metadata_paths = []
                for metadata_path in metadata_paths:
                    payload = self._read_persisted_delegate_metadata(metadata_path)
                    if payload is None:
                        continue
                    status = str(payload.get("status") or "").lower()
                    if status not in _PERSISTED_TERMINAL_STATES and not bool(payload.get("completed")):
                        continue
                    delegate_id = str(payload.get("delegate_id") or "")
                    if not _PERSISTED_DELEGATE_ID_RE.fullmatch(delegate_id):
                        continue
                    completed_at = self._telemetry_completed_at_epoch(
                        payload,
                        metadata_path=metadata_path,
                    )
                    if completed_at is None:
                        continue
                    terminal.append(
                        (completed_at, root, metadata_path.parent, delegate_id)
                    )

            terminal.sort(key=lambda item: item[0], reverse=True)
            terminal_records += len(terminal)
            harness_removed = 0
            for index, (completed_at, root, log_dir, delegate_id) in enumerate(terminal):
                if (
                    completed_at >= cutoff
                    and index < self.max_terminal_delegate_records
                ):
                    continue
                try:
                    if log_dir.is_symlink() or log_dir.parent.resolve() != root.resolve():
                        errors += 1
                        continue
                    shutil.rmtree(log_dir)
                    removed += 1
                    harness_removed += 1
                    removed_delegate_records.append((delegate_id, log_dir / "metadata.json"))
                except OSError:
                    errors += 1
            per_harness[harness] = {
                "terminal_records": len(terminal),
                "removed": harness_removed,
                "retained_terminal_records": len(terminal) - harness_removed,
            }

        if removed_delegate_records:
            with self._lock:
                for delegate_id, metadata_path in removed_delegate_records:
                    if self._persisted_delegate_paths.get(delegate_id) == metadata_path:
                        self._persisted_delegate_paths.pop(delegate_id, None)
        return {
            "success": errors == 0,
            "removed": removed,
            "terminal_records": terminal_records,
            "errors": errors,
            "retention_seconds": self.delegate_retention_seconds,
            "max_terminal_records_per_harness": self.max_terminal_delegate_records,
            "harnesses": per_harness,
        }

    def shutdown(self, *, wait_seconds: float = 10.0) -> dict[str, object]:
        """Stop scheduling while leaving canonical durable jobs alive."""

        return self.scheduler.shutdown(
            reason="server_shutdown",
            wait_seconds=wait_seconds,
            preserve_running=lambda task: bool(task.durable_job_id),
        )

    def _durable_enabled_for(self, task: DelegateTask) -> bool:
        return (
            os.name == "posix"
            and self.durable_job_registry is not None
            and self.durable_state_dir is not None
            and task.harness in self.durable_harnesses
        )

    def _persist_durable_termination_intent(
        self,
        metadata_path: Path,
        *,
        reason: str,
    ) -> None:
        payload = self._read_persisted_delegate_metadata(metadata_path)
        if not isinstance(payload, dict) or payload.get("durable") is not True:
            return
        payload["durable_termination_reason"] = reason
        if reason != "timed_out":
            payload["cancel_reason"] = reason
        try:
            write_private_json(metadata_path, payload)
        except OSError:
            pass

    def _terminate_task(self, task: DelegateTask) -> None:
        if (
            task.durable_job_id
            and self.durable_job_registry is not None
            and self.durable_state_dir is not None
        ):
            self._persist_durable_termination_intent(
                task.log_paths.metadata,
                reason=task.cancel_reason or "cancelled",
            )
            self.durable_job_registry.kill_job(
                job_id=task.durable_job_id,
                state_dir=self.durable_state_dir,
                signal_name="TERM",
            )
            return
        self._process_runner.cancel(task)

    def _scan_persisted_delegate_metadata(
        self,
        scan_roots: list[Path],
    ) -> list[tuple[Path, dict[str, object] | None]]:
        records: list[tuple[Path, dict[str, object] | None]] = []
        for root in scan_roots:
            try:
                metadata_paths = list(root.glob("*/metadata.json"))
            except OSError:
                continue
            records.extend(
                (metadata_path, self._read_persisted_delegate_metadata(metadata_path))
                for metadata_path in metadata_paths
            )
        return records

    def _restore_persisted_group_shells(
        self,
        records: list[tuple[Path, dict[str, object] | None]],
    ) -> int:
        grouped: dict[str, list[tuple[Path, dict[str, object]]]] = {}
        seen_delegate_ids: set[str] = set()
        for metadata_path, payload in records:
            if payload is None:
                continue
            harness = str(payload.get("harness") or "").strip().lower()
            group_id = str(payload.get("group_id") or "").strip()
            delegate_id = str(payload.get("delegate_id") or "")
            if (
                not _PERSISTED_GROUP_ID_RE.fullmatch(group_id)
                or harness not in self.durable_harnesses
                or not _PERSISTED_DELEGATE_ID_RE.fullmatch(delegate_id)
                or delegate_id in seen_delegate_ids
            ):
                continue
            seen_delegate_ids.add(delegate_id)
            grouped.setdefault(group_id, []).append((metadata_path, payload))

        restored = 0
        for group_id, records in grouped.items():
            if not any(payload.get("durable") is True for _path, payload in records):
                continue
            harnesses = {
                str(payload.get("harness") or "").strip().lower()
                for _path, payload in records
            }
            project_keys = {
                str((payload.get("project") or {}).get("project_key") or "")
                for _path, payload in records
                if isinstance(payload.get("project"), dict)
            }
            if len(harnesses) != 1 or len(project_keys) != 1 or "" in project_keys:
                continue
            first_payload = records[0][1]
            project_payload = first_payload.get("project")
            if not isinstance(project_payload, dict):
                continue
            try:
                project = ProjectIdentity(
                    project_key=str(project_payload["project_key"]),
                    project_root=Path(str(project_payload["root"])),
                    git_common_dir=(
                        Path(str(project_payload["git_common_dir"]))
                        if project_payload.get("git_common_dir")
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError):
                continue

            def sort_key(item: tuple[Path, dict[str, object]]) -> tuple[float, str]:
                payload = item[1]
                try:
                    submitted = float(payload.get("submitted_at_epoch") or 0.0)
                except (TypeError, ValueError):
                    submitted = 0.0
                return submitted, str(payload.get("delegate_id") or "")

            ordered = sorted(records, key=sort_key)
            child_ids = [
                str(payload.get("delegate_id") or "")
                for _path, payload in ordered
            ]
            submitted_values: list[float] = []
            for _path, payload in ordered:
                try:
                    submitted_values.append(
                        float(payload.get("submitted_at_epoch") or 0.0)
                    )
                except (TypeError, ValueError):
                    pass
            submitted_at = min(
                (value for value in submitted_values if value > 0),
                default=time.time(),
            )
            max_concurrency_values = {
                value
                for _path, payload in ordered
                if isinstance((value := payload.get("group_max_concurrency")), int)
                and not isinstance(value, bool)
                and value > 0
            }
            if len(max_concurrency_values) > 1:
                continue
            logical_session_ids = {
                str(payload.get("logical_session_id") or "")
                for _path, payload in ordered
            }
            if len(logical_session_ids) > 1:
                continue
            logical_session_id = next(iter(logical_session_ids)) or None
            group = DelegateGroup(
                group_id=group_id,
                harness=str(first_payload.get("harness") or ""),
                project=project,
                kind="explore_batch",
                child_ids=child_ids,
                submitted_at=submitted_at,
                logical_session_id=logical_session_id,
                max_concurrency=(
                    next(iter(max_concurrency_values))
                    if max_concurrency_values
                    else None
                ),
            )
            try:
                group_added = self.scheduler.restore_group(group)
            except DelegateSchedulerShuttingDownError:
                continue
            if group_added:
                restored += 1
        return restored

    def _restore_persisted_group_terminal_task(
        self,
        payload: dict[str, object],
        metadata_path: Path,
    ) -> bool:
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id or self.scheduler.get_group(group_id) is None:
            return False
        task = self._task_from_persisted_durable(payload, metadata_path)
        if task is None:
            return False
        result = self._normalize_persisted_delegate(payload, metadata_path)
        result["detached_from_scheduler"] = False
        return self.scheduler.restore_terminal_task(task, result=result)

    def recover_persisted_delegates(
        self,
        *,
        roots: list[Path] | tuple[Path, ...] | None = None,
    ) -> dict[str, object]:
        """Recover terminal delegate metadata and reap verified crash orphans."""

        scan_roots = list(roots) if roots is not None else self._delegate_scan_roots()
        scanned_records = self._scan_persisted_delegate_metadata(scan_roots)
        groups_restored = self._restore_persisted_group_shells(scanned_records)
        group_terminal_restored = 0
        scanned = 0
        terminal_loaded = 0
        interrupted = 0
        termination_signalled = 0
        groups_gone = 0
        skipped = 0
        live_owner_skipped = 0
        unattributed_skipped = 0
        durable_running = 0
        durable_adopted = 0
        durable_already_attached = 0
        durable_unattached = 0
        duplicate_delegate_ids_skipped = 0
        telemetry_legacy_skipped = 0
        seen_delegate_ids: set[str] = set()
        telemetry_backfill: list[tuple[dict[str, object], float | None]] = []

        for root in scan_roots:
            for metadata_path, payload in (
                item
                for item in scanned_records
                if item[0].parent.parent == root
            ):
                scanned += 1
                if payload is None:
                    skipped += 1
                    continue
                delegate_id = str(payload.get("delegate_id") or "")
                if not _PERSISTED_DELEGATE_ID_RE.fullmatch(delegate_id):
                    skipped += 1
                    continue
                if delegate_id in seen_delegate_ids:
                    duplicate_delegate_ids_skipped += 1
                    continue
                seen_delegate_ids.add(delegate_id)
                status = str(payload.get("status") or "").lower()
                if status in _PERSISTED_TERMINAL_STATES or bool(payload.get("completed")):
                    self._persisted_delegate_paths[delegate_id] = metadata_path
                    if self._should_backfill_terminal_telemetry(payload):
                        completed_at = self._telemetry_completed_at_epoch(
                            payload,
                            metadata_path=metadata_path,
                        )
                        telemetry_backfill.append((payload, completed_at))
                    else:
                        telemetry_legacy_skipped += 1
                    if self._restore_persisted_group_terminal_task(
                        payload,
                        metadata_path,
                    ):
                        group_terminal_restored += 1
                    terminal_loaded += 1
                    continue
                if payload.get("durable") is True and isinstance(
                    payload.get("durable_job_id"), str
                ):
                    self._persisted_delegate_paths[delegate_id] = metadata_path
                    durable_snapshot = self._status_from_persisted_durable_delegate(
                        payload,
                        metadata_path,
                    )
                    if durable_snapshot is not None and durable_snapshot.get("completed"):
                        refreshed_payload = self._read_persisted_delegate_metadata(
                            metadata_path
                        )
                        if (
                            refreshed_payload is not None
                            and self._restore_persisted_group_terminal_task(
                                refreshed_payload,
                                metadata_path,
                            )
                        ):
                            group_terminal_restored += 1
                        if refreshed_payload is not None:
                            if self._should_backfill_terminal_telemetry(
                                refreshed_payload
                            ):
                                completed_at = self._telemetry_completed_at_epoch(
                                    refreshed_payload,
                                    metadata_path=metadata_path,
                                )
                                telemetry_backfill.append(
                                    (refreshed_payload, completed_at)
                                )
                            else:
                                telemetry_legacy_skipped += 1
                        terminal_loaded += 1
                    else:
                        durable_running += 1
                        if self.scheduler.get_task(delegate_id) is not None:
                            durable_already_attached += 1
                        elif self._adopt_persisted_durable_delegate(
                            payload,
                            metadata_path,
                        ):
                            durable_adopted += 1
                        else:
                            durable_unattached += 1
                    continue

                owner_pid = payload.get("owner_pid")
                owner_identity = payload.get("owner_process_identity")
                if not isinstance(owner_pid, int) or not isinstance(owner_identity, str):
                    unattributed_skipped += 1
                    continue
                owner_match = process_identity_matches(owner_pid, owner_identity)
                if owner_match is True or owner_match is None:
                    # Rolling reload starts the replacement server before the
                    # previous server is drained. Keep the metadata path so
                    # direct status/session resume can observe the old owner
                    # finishing after this startup scan.
                    self._persisted_delegate_paths[delegate_id] = metadata_path
                    live_owner_skipped += 1
                    continue

                recovered, signalled, group_gone = self._recover_interrupted_delegate(
                    payload,
                    metadata_path,
                )
                self._persisted_delegate_paths[delegate_id] = metadata_path
                refreshed_payload = self._read_persisted_delegate_metadata(metadata_path)
                if (
                    refreshed_payload is not None
                    and self._restore_persisted_group_terminal_task(
                        refreshed_payload,
                        metadata_path,
                    )
                ):
                    group_terminal_restored += 1
                if refreshed_payload is not None:
                    if self._should_backfill_terminal_telemetry(
                        refreshed_payload
                    ):
                        completed_at = self._telemetry_completed_at_epoch(
                            refreshed_payload,
                            metadata_path=metadata_path,
                        )
                        telemetry_backfill.append(
                            (refreshed_payload, completed_at)
                        )
                    else:
                        telemetry_legacy_skipped += 1
                interrupted += 1
                if signalled:
                    termination_signalled += 1
                if group_gone:
                    groups_gone += 1

        telemetry_backfilled = 0
        if self.telemetry is not None and telemetry_backfill:
            try:
                telemetry_backfilled = self.telemetry.record_terminals_batch(
                    telemetry_backfill,
                    skip_existing=True,
                )
            except (OSError, TypeError, ValueError):
                telemetry_backfilled = 0

        recorded_at = time.time()
        summary: dict[str, object] = {
            "success": True,
            "recorded_at": _format_epoch_seconds(recorded_at),
            "recorded_at_epoch": recorded_at,
            "scanned": scanned,
            "terminal_loaded": terminal_loaded,
            "interrupted": interrupted,
            "orphan_groups_signalled": termination_signalled,
            "orphan_groups_gone": groups_gone,
            "skipped": skipped,
            "live_owner_skipped": live_owner_skipped,
            "unattributed_skipped": unattributed_skipped,
            "durable_running": durable_running,
            "durable_adopted": durable_adopted,
            "durable_already_attached": durable_already_attached,
            "durable_unattached": durable_unattached,
            "duplicate_delegate_ids_skipped": duplicate_delegate_ids_skipped,
            "telemetry_backfilled": telemetry_backfilled,
            "telemetry_legacy_skipped": telemetry_legacy_skipped,
            "groups_restored": groups_restored,
            "group_terminal_restored": group_terminal_restored,
        }
        with self._lock:
            self._last_recovery_summary = dict(summary)
        return summary

    @staticmethod
    def _read_persisted_delegate_metadata(
        metadata_path: Path,
    ) -> dict[str, object] | None:
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _normalize_persisted_delegate(
        payload: dict[str, object],
        metadata_path: Path,
        *,
        terminal: bool = True,
    ) -> dict[str, object]:
        result = dict(payload)
        result["completed"] = terminal
        result["in_progress"] = not terminal
        result["recovered_from_disk"] = True
        result["detached_from_scheduler"] = True
        logs = result.get("logs")
        if not isinstance(logs, dict):
            log_dir = metadata_path.parent
            logs = {
                "log_dir": str(log_dir),
                "prompt": str(log_dir / "prompt.txt"),
                "stdout": str(log_dir / "stdout.log"),
                "stderr": str(log_dir / "stderr.log"),
                "metadata": str(metadata_path),
            }
            result["logs"] = logs
        result["log_read_hint"] = {
            "tool": "read_text",
            "paths": [
                str(logs.get("stdout") or metadata_path.parent / "stdout.log"),
                str(logs.get("stderr") or metadata_path.parent / "stderr.log"),
                str(logs.get("metadata") or metadata_path),
            ],
            "message": (
                "Recovered delegate metadata is bounded. Read stdout/stderr "
                "for the delegate process output."
            ),
        }
        return result

    def _status_from_persisted_delegate(
        self,
        metadata_path: Path,
    ) -> dict[str, object] | None:
        payload = self._read_persisted_delegate_metadata(metadata_path)
        if payload is None:
            return None
        status = str(payload.get("status") or "").lower()
        if status in _PERSISTED_TERMINAL_STATES or bool(payload.get("completed")):
            return self._normalize_persisted_delegate(payload, metadata_path)
        if payload.get("durable") is True and isinstance(
            payload.get("durable_job_id"), str
        ):
            return self._status_from_persisted_durable_delegate(payload, metadata_path)

        owner_pid = payload.get("owner_pid")
        owner_identity = payload.get("owner_process_identity")
        if not isinstance(owner_pid, int) or not isinstance(owner_identity, str):
            return None
        owner_match = process_identity_matches(owner_pid, owner_identity)
        if owner_match is False:
            recovered, _signalled, _group_gone = self._recover_interrupted_delegate(
                payload,
                metadata_path,
            )
            return recovered

        # True means a rolling-reload predecessor still owns the task. None
        # is intentionally fail-closed: identity could not be verified, so
        # never signal the process and preserve the nonterminal observation.
        return self._normalize_persisted_delegate(
            payload,
            metadata_path,
            terminal=False,
        )

    def _status_from_persisted_durable_delegate(
        self,
        payload: dict[str, object],
        metadata_path: Path,
    ) -> dict[str, object] | None:
        if self.durable_job_registry is None or self.durable_state_dir is None:
            return self._normalize_persisted_delegate(
                payload,
                metadata_path,
                terminal=False,
            )
        job_id = str(payload.get("durable_job_id") or "")
        if not job_id:
            return None
        job_status = self.durable_job_registry.job_status(
            job_id=job_id,
            state_dir=self.durable_state_dir,
        )
        if job_status.get("success") is False:
            failed = dict(payload)
            failed.update(
                {
                    "success": False,
                    "status": "failed",
                    "completed": True,
                    "in_progress": False,
                    "error": {
                        "code": "durable_job_status_failed",
                        "message": str(
                            (job_status.get("error") or {}).get("message")
                            if isinstance(job_status.get("error"), dict)
                            else "Failed to read durable delegate job status."
                        ),
                    },
                }
            )
            write_private_json(metadata_path, failed)
            return self._normalize_persisted_delegate(failed, metadata_path)

        job_state = str(job_status.get("status") or "")
        execution_timeout = int(payload.get("execution_timeout_seconds") or 0)
        timed_out = (
            job_state == "running"
            and execution_timeout > 0
            and float(job_status.get("elapsed_seconds") or 0.0) >= execution_timeout
        )
        if timed_out:
            self._persist_durable_termination_intent(
                metadata_path,
                reason="timed_out",
            )
            self.durable_job_registry.kill_job(
                job_id=job_id,
                state_dir=self.durable_state_dir,
                signal_name="TERM",
            )
            job_status = self.durable_job_registry.job_status(
                job_id=job_id,
                state_dir=self.durable_state_dir,
            )
            job_state = str(job_status.get("status") or "")

        if job_state not in {
            "succeeded",
            "failed",
            "killed",
            "interrupted",
            "timed_out",
        }:
            running = dict(payload)
            running.update(
                {
                    "status": "running",
                    "completed": False,
                    "in_progress": True,
                    "durable_job_status": job_state or "running",
                    "elapsed_seconds": job_status.get("elapsed_seconds"),
                    "pid": job_status.get("pid"),
                    "last_output_at": job_status.get("last_output_at"),
                }
            )
            return self._normalize_persisted_delegate(
                running,
                metadata_path,
                terminal=False,
            )

        task = self._task_from_persisted_durable(
            payload,
            metadata_path,
            job_status=job_status,
        )
        if task is None:
            return None
        invocation = self._build_invocation_for_task(task)
        before_hex = payload.get("readonly_git_status_before_hex")
        before_status: bytes | None = None
        if isinstance(before_hex, str) and before_hex:
            try:
                before_status = bytes.fromhex(before_hex)
            except ValueError:
                before_status = None
        if payload.get("cancel_reason"):
            task.cancel_requested = True
            task.cancel_reason = str(payload.get("cancel_reason"))
        return self._finalize_durable_delegate(
            task,
            invocation=invocation,
            before_status=before_status,
            job_status=job_status,
            timed_out=timed_out or job_state == "timed_out",
        )

    @staticmethod
    def _persisted_readonly_before_status(
        payload: dict[str, object],
    ) -> bytes | None:
        before_hex = payload.get("readonly_git_status_before_hex")
        if not isinstance(before_hex, str) or not before_hex:
            return None
        try:
            return bytes.fromhex(before_hex)
        except ValueError:
            return None

    def _adopt_persisted_durable_delegate(
        self,
        payload: dict[str, object],
        metadata_path: Path,
    ) -> bool:
        if self.durable_job_registry is None or self.durable_state_dir is None:
            return False
        job_id = str(payload.get("durable_job_id") or "")
        if not job_id:
            return False
        job_status = self.durable_job_registry.job_status(
            job_id=job_id,
            state_dir=self.durable_state_dir,
        )
        if (
            job_status.get("success") is False
            or str(job_status.get("status") or "")
            in {"succeeded", "failed", "killed", "interrupted", "timed_out"}
        ):
            return False
        task = self._task_from_persisted_durable(
            payload,
            metadata_path,
            job_status=job_status,
        )
        if task is None:
            return False
        try:
            invocation = self._build_invocation_for_task(task)
        except OSError:
            return False
        before_status = self._persisted_readonly_before_status(payload)
        if payload.get("cancel_reason"):
            task.cancel_requested = True
            task.cancel_reason = str(payload.get("cancel_reason"))
        elapsed_seconds = max(0.0, float(job_status.get("elapsed_seconds") or 0.0))
        task.started_monotonic = time.monotonic() - elapsed_seconds
        self._submitted_seq += 1
        task.submitted_seq = self._submitted_seq
        try:
            return self.scheduler.adopt_running_task(
                task,
                runner=lambda recovered_task: self._wait_for_durable_delegate_job(
                    recovered_task,
                    invocation=invocation,
                    before_status=before_status,
                    initial_status=job_status,
                ),
            )
        except (DelegateSchedulerShuttingDownError, ValueError):
            return False

    def _task_from_persisted_durable(
        self,
        payload: dict[str, object],
        metadata_path: Path,
        *,
        job_status: dict[str, object] | None = None,
    ) -> DelegateTask | None:
        project_payload = payload.get("project")
        logs_payload = payload.get("logs")
        if not isinstance(project_payload, dict) or not isinstance(logs_payload, dict):
            return None
        try:
            project = ProjectIdentity(
                project_key=str(project_payload["project_key"]),
                project_root=Path(str(project_payload["root"])),
                git_common_dir=(
                    Path(str(project_payload["git_common_dir"]))
                    if project_payload.get("git_common_dir")
                    else None
                ),
            )
            prompt_path = metadata_path.parent / "prompt.txt"
            stdout_path = (
                Path(str(job_status["stdout_log"]))
                if isinstance(job_status, dict) and job_status.get("stdout_log")
                else Path(str(logs_payload["stdout"]))
            )
            stderr_path = (
                Path(str(job_status["stderr_log"]))
                if isinstance(job_status, dict) and job_status.get("stderr_log")
                else Path(str(logs_payload["stderr"]))
            )
            log_paths = DelegateLogPaths(
                log_dir=metadata_path.parent,
                prompt=prompt_path,
                stdout=stdout_path,
                stderr=stderr_path,
                metadata=metadata_path,
            )
        except (KeyError, TypeError, ValueError):
            return None
        try:
            prompt = log_paths.prompt.read_text(encoding="utf-8")
        except OSError:
            prompt = ""
        dependencies = payload.get("depends_on_group_ids")
        task = DelegateTask(
            delegate_id=str(payload.get("delegate_id") or ""),
            harness=str(payload.get("harness") or ""),
            project=project,
            kind=str(payload.get("kind") or "code"),  # type: ignore[arg-type]
            cwd=Path(str(payload.get("cwd") or project.project_root)),
            task=None,
            goal=None,
            task_id=str(payload["task_id"]) if payload.get("task_id") else None,
            group_id=str(payload["group_id"]) if payload.get("group_id") else None,
            model=str(payload.get("model") or DEFAULT_MODEL),
            reasoning_effort=str(payload.get("reasoning_effort") or DEFAULT_REASONING_EFFORT),
            sandbox_mode=str(payload.get("sandbox_mode") or ""),
            commit_mode=str(payload.get("commit_mode") or "forbidden"),
            execution_timeout_seconds=int(payload.get("execution_timeout_seconds") or 1),
            cancel_grace_seconds=self.cancel_grace_seconds,
            request_fingerprint=str(payload.get("request_fingerprint") or ""),
            prompt=prompt,
            log_paths=log_paths,
            output_schema=payload.get("output_schema") if isinstance(payload.get("output_schema"), dict) else None,
            parse_structured_output=bool(payload.get("parse_structured_output")),
            depends_on_group_ids=tuple(
                str(item)
                for item in dependencies
                if isinstance(item, str)
            ) if isinstance(dependencies, list) else (),
            resume_from_delegate_id=(
                str(payload["resume_from_delegate_id"])
                if payload.get("resume_from_delegate_id")
                else None
            ),
            resume_conversation_id=(
                str(payload["resume_conversation_id"])
                if payload.get("resume_conversation_id")
                else None
            ),
            logical_session_id=(
                str(payload["logical_session_id"])
                if payload.get("logical_session_id")
                else None
            ),
            submitted_at=float(payload.get("submitted_at_epoch") or time.time()),
        )
        task.started_at = float(payload.get("started_at_epoch") or task.submitted_at)
        task.durable_job_id = str(payload.get("durable_job_id") or "")
        raw_group_max_concurrency = payload.get("group_max_concurrency")
        if (
            isinstance(raw_group_max_concurrency, int)
            and not isinstance(raw_group_max_concurrency, bool)
            and raw_group_max_concurrency > 0
        ):
            task.group_max_concurrency = raw_group_max_concurrency
        task.recovered_from_disk = True
        task.state = "running"
        return task

    def _recover_interrupted_delegate(
        self,
        payload: dict[str, object],
        metadata_path: Path,
    ) -> tuple[dict[str, object], bool, bool]:
        pid = payload.get("pid")
        pgid = payload.get("pgid")
        expected_identity = payload.get("process_identity")
        identity_match = process_identity_matches(
            pid if isinstance(pid, int) else None,
            expected_identity,
        )
        termination_signalled = False
        group_gone = False
        termination_signal: int | None = None
        recovery_action = "process_missing_or_identity_mismatch"

        if (
            os.name == "posix"
            and identity_match is True
            and isinstance(pid, int)
            and isinstance(pgid, int)
            and pid > 0
            and pgid > 0
            and pgid == pid
        ):
            try:
                current_pgid = os.getpgid(pid)
            except OSError:
                current_pgid = None
            if current_pgid == pgid:
                expected_group = snapshot_process_group(pgid)
                if expected_group.get(pid) == expected_identity:
                    try:
                        os.killpg(pgid, signal.SIGTERM)
                        termination_signalled = True
                        termination_signal = signal.SIGTERM
                        recovery_action = "term_sent_to_verified_process_group"
                    except (OSError, ProcessLookupError):
                        recovery_action = "verified_process_group_already_gone"
                    deadline = time.monotonic() + min(
                        max(self.cancel_grace_seconds, 0.0),
                        2.0,
                    )
                    while process_group_exists(pgid) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    group_gone = not process_group_exists(pgid)
                    if group_gone and termination_signalled:
                        recovery_action = "terminated_verified_process_group"
                    if (
                        not group_gone
                        and process_group_matches_snapshot(pgid, expected_group)
                    ):
                        try:
                            os.killpg(
                                pgid,
                                getattr(signal, "SIGKILL", signal.SIGTERM),
                            )
                            termination_signalled = True
                            termination_signal = getattr(
                                signal,
                                "SIGKILL",
                                signal.SIGTERM,
                            )
                            recovery_action = "kill_sent_to_verified_process_group"
                        except (OSError, ProcessLookupError):
                            pass
                        kill_deadline = time.monotonic() + 0.5
                        while (
                            process_group_exists(pgid)
                            and time.monotonic() < kill_deadline
                        ):
                            time.sleep(0.01)
                        group_gone = not process_group_exists(pgid)
                        if group_gone:
                            recovery_action = "killed_verified_process_group"
                    elif not group_gone and termination_signalled:
                        recovery_action = "process_group_changed_after_term"
            else:
                recovery_action = "process_group_mismatch"
        elif (
            identity_match is True
            and isinstance(pid, int)
            and isinstance(pgid, int)
            and pid > 0
            and pgid > 0
            and pgid != pid
        ):
            recovery_action = "process_group_leader_mismatch"
        elif identity_match is None:
            recovery_action = "process_identity_unverifiable"

        recovered = dict(payload)
        recovered.update(
            {
                "success": False,
                "status": "cancelled",
                "completed": True,
                "in_progress": False,
                "timed_out": False,
                "wait_timed_out": False,
                "exit_code": -termination_signal if termination_signal else -1,
                "completed_at_epoch": time.time(),
                "recovered_from_disk": True,
                "recovery": {
                    "action": recovery_action,
                    "process_identity_match": identity_match,
                    "termination_signalled": termination_signalled,
                    "orphan_group_gone": group_gone,
                },
                "error": {
                    "code": "server_restart",
                    "message": (
                        "The non-durable delegate was interrupted by an MCP "
                        "server restart before a terminal result was recorded."
                    ),
                },
                "summary": (
                    "Delegate cancelled during startup recovery after an MCP "
                    "server restart."
                ),
            }
        )
        normalized = self._normalize_persisted_delegate(recovered, metadata_path)
        try:
            write_private_json(metadata_path, normalized)
        except OSError:
            pass
        return normalized, termination_signalled, group_gone

    def _resolve_harness(self, harness: str | None) -> tuple[str, DelegateHarness | None]:
        name = (harness or self.default_harness).strip().lower()
        adapter = self.harnesses.get(name)
        if name == "codex" and (
            adapter is None or adapter.command != self.codex_command
        ):
            adapter = CodexHarness(
                command=self.codex_command,
                allow_unsafe_explore_command=self.allow_unsafe_explore_command,
            )
            self.harnesses[name] = adapter
        elif name == "pi" and self.pi_command is not None and (
            adapter is None or adapter.command != self.pi_command
        ):
            adapter = PiHarness(command=self.pi_command)
            self.harnesses[name] = adapter
        return name, adapter

    def _resume_source_snapshot(
        self,
        delegate_id: str,
    ) -> dict[str, object] | None:
        task = self.scheduler.get_task(delegate_id)
        if task is not None:
            return self._task_snapshot(task)
        with self._lock:
            for item in reversed(self._history):
                if str(item.get("delegate_id") or "") == delegate_id:
                    return dict(item)
            metadata_path = self._persisted_delegate_paths.get(delegate_id)
        if metadata_path is None:
            return None
        payload = self._read_persisted_delegate_metadata(metadata_path)
        if payload is None:
            return None
        return self._normalize_persisted_delegate(payload, metadata_path)

    def _resolve_resume_conversation(
        self,
        *,
        resume_from_delegate_id: str | None,
        harness: str,
        project: ProjectIdentity,
        cwd: Path,
        timeout: int,
    ) -> tuple[str | None, dict[str, object] | None]:
        source_id = (resume_from_delegate_id or "").strip()
        if not source_id:
            return None, None
        if not _PERSISTED_DELEGATE_ID_RE.fullmatch(source_id):
            return None, self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="invalid_resume_delegate_id",
                message="resume_from_delegate_id must be an exact delegate identifier.",
                harness=harness,
            )
        if harness not in {"antigravity", "antigravity2"}:
            return None, self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="delegate_resume_unsupported",
                message="Explicit conversation resume is supported only for Antigravity account delegates.",
                harness=harness,
            )
        source = self._resume_source_snapshot(source_id)
        if source is None:
            return None, self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="delegate_resume_not_found",
                message=f"Resume source delegate not found: {source_id}",
                harness=harness,
            )
        if not bool(source.get("completed")):
            return None, self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="delegate_resume_not_terminal",
                message="Resume source delegate must be terminal before its conversation can be continued.",
                harness=harness,
            )
        if str(source.get("harness") or "") != harness:
            return None, self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="delegate_resume_harness_mismatch",
                message="Resume source delegate belongs to a different Antigravity account.",
                harness=harness,
            )
        source_project = source.get("project")
        if (
            not isinstance(source_project, dict)
            or str(source_project.get("project_key") or "") != project.project_key
        ):
            return None, self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="delegate_resume_project_mismatch",
                message="Resume source delegate belongs to a different project identity.",
                harness=harness,
            )
        harness_metadata = source.get("harness_metadata")
        conversation_id = (
            str(harness_metadata.get("conversation_id") or "").strip()
            if isinstance(harness_metadata, dict)
            else ""
        )
        try:
            normalized_conversation_id = str(uuid.UUID(conversation_id))
        except (ValueError, AttributeError):
            return None, self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="delegate_resume_conversation_unavailable",
                message="Resume source delegate has no valid Antigravity conversation_id.",
                harness=harness,
            )
        return normalized_conversation_id, None

    @property
    def _active(self) -> DelegateTask | None:
        active = self.scheduler.nonterminal_tasks()
        return active[0] if len(active) == 1 else None

    def delegate_ownership_scope(
        self,
        *,
        logical_session_id: str,
        delegate_id: str | None = None,
        group_id: str | None = None,
    ) -> str:
        if bool(delegate_id) == bool(group_id):
            raise ValueError("Provide exactly one of delegate_id or group_id.")
        owner: str | None = None
        if delegate_id:
            task = self.scheduler.get_task(delegate_id.strip())
            if task is not None:
                owner = task.logical_session_id
        else:
            group = self.scheduler.get_group((group_id or "").strip())
            if group is not None:
                owner = group.logical_session_id
        if not owner:
            return "unowned"
        return "owned_here" if owner == logical_session_id else "owned_elsewhere"

    def foreign_delegate_ids(self, *, logical_session_id: str) -> set[str]:
        return {
            task.delegate_id
            for task in self.scheduler.tasks.values()
            if task.logical_session_id
            and task.logical_session_id != logical_session_id
        }

    def run_delegate(
        self,
        *,
        task: str | None,
        goal: str | None = None,
        task_id: str | None = None,
        cwd: Path,
        timeout: int | None = None,
        wait_seconds: float = DEFAULT_DELEGATE_WAIT_SECONDS,
        execution_timeout_seconds: int | None = None,
        harness: str | None = None,
        kind: TaskKind = "code",
        group_id: str | None = None,
        depends_on_group_ids: list[str] | None = None,
        files_in_scope: list[str] | None = None,
        out_of_scope: list[str] | None = None,
        context_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        done_means: list[str] | None = None,
        verification_commands: list[str] | None = None,
        commit_mode: str = "allowed",
        model: str | None = None,
        reasoning_effort: str | None = None,
        output_schema: dict[str, object] | None = None,
        parse_structured_output: bool = True,
        resume_from_delegate_id: str | None = None,
        logical_session_id: str | None = None,
        before_submit: Callable[[], dict[str, object] | None] | None = None,
    ) -> dict[str, object]:
        if self.scheduler.is_shutting_down:
            return self._argument_error(
                cwd=cwd,
                timeout=int(timeout or execution_timeout_seconds or 0),
                code="server_shutdown",
                message="delegate scheduler is shutting down",
                harness=(harness or self.default_harness).strip().lower(),
            )
        harness_name, adapter = self._resolve_harness(harness)
        if adapter is None:
            return self._argument_error(
                cwd=cwd,
                timeout=int(timeout or execution_timeout_seconds or 0),
                code="unsupported_delegate_harness",
                message=f"Unsupported delegate harness: {harness_name}",
                details={"available_harnesses": sorted(self.harnesses)},
                harness=harness_name,
            )
        normalized_task = (task or "").strip()
        normalized_goal = (goal or "").strip()
        normalized_task_id = (task_id or "").strip() or None
        if not normalized_task and not normalized_goal:
            active = self.scheduler.nonterminal_tasks()
            if len(active) == 1:
                if (
                    active[0].logical_session_id != logical_session_id
                    and (
                        active[0].logical_session_id is not None
                        or logical_session_id is not None
                    )
                ):
                    return self._argument_error(
                        cwd=cwd,
                        timeout=int(timeout or 0),
                        code="delegate_owned_by_another_session",
                        message="Active delegate belongs to a different logical session.",
                    )
                return self._wait_for_task(active[0], wait_seconds=wait_seconds, attached=True)
            if len(active) > 1:
                return self._argument_error(
                    cwd=cwd,
                    timeout=int(timeout or 0),
                    code="ambiguous_delegate",
                    message="Multiple delegates are active; provide delegate_id or group_id.",
                )
            return self._argument_error(
                cwd=cwd,
                timeout=int(timeout or 0),
                code="missing_task_or_goal",
                message="delegate_task requires task or goal when no delegate is already running.",
            )

        validation = self._validate_task_arguments(
            cwd=cwd,
            kind=kind,
            commit_mode=commit_mode,
            reasoning_effort=reasoning_effort,
            timeout=int(timeout or execution_timeout_seconds or 0),
        )
        if validation is not None:
            return validation
        if kind == "explore" and not adapter.supports_read_only():
            return self._argument_error(
                cwd=cwd,
                timeout=int(timeout or execution_timeout_seconds or 0),
                code="readonly_sandbox_unavailable",
                message=(
                    f"Explore tasks require harness {harness_name!r} to enforce read-only execution."
                ),
                harness=harness_name,
            )
        project = self.project_resolver.resolve(cwd)
        dependencies = tuple(dict.fromkeys(depends_on_group_ids or []))
        dependency_error = self._validate_dependencies(project, dependencies)
        if dependency_error is not None:
            return dependency_error

        effective_model, effective_reasoning, effective_commit, sandbox_mode = self._task_defaults(
            adapter=adapter,
            kind=kind,
            model=model,
            reasoning_effort=reasoning_effort,
            commit_mode=commit_mode,
        )
        execution_timeout = int(
            execution_timeout_seconds
            or timeout
            or (
                self.explore_execution_timeout_seconds
                if kind == "explore"
                else self.code_execution_timeout_seconds
            )
        )
        resume_conversation_id, resume_error = self._resolve_resume_conversation(
            resume_from_delegate_id=resume_from_delegate_id,
            harness=harness_name,
            project=project,
            cwd=cwd,
            timeout=execution_timeout,
        )
        if resume_error is not None:
            return resume_error
        fingerprint = _delegate_request_fingerprint(
            task=normalized_task or None,
            goal=normalized_goal or None,
            task_id=normalized_task_id,
            cwd=cwd,
            harness=harness_name,
            kind=kind,
            group_id=group_id,
            files_in_scope=files_in_scope,
            out_of_scope=out_of_scope,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            done_means=done_means,
            verification_commands=verification_commands,
            commit_mode=effective_commit,
            model=effective_model,
            reasoning_effort=effective_reasoning,
            output_schema=output_schema,
            parse_structured_output=parse_structured_output,
            depends_on_group_ids=list(dependencies),
            resume_conversation_id=resume_conversation_id,
        )
        preexisting: DelegateTask | None = None
        with self._lock:
            preexisting = next(
                (
                    item
                    for item in self.scheduler.tasks.values()
                    if not item.is_terminal
                    and item.request_fingerprint == fingerprint
                ),
                None,
            )
            if (
                preexisting is not None
                and preexisting.logical_session_id != logical_session_id
                and (
                    preexisting.logical_session_id is not None
                    or logical_session_id is not None
                )
            ):
                return self._argument_error(
                    cwd=cwd,
                    timeout=execution_timeout,
                    code="delegate_owned_by_another_session",
                    message="Matching active delegate belongs to a different logical session.",
                )
        if preexisting is not None:
            return self._wait_for_task(
                preexisting,
                wait_seconds=wait_seconds,
                attached=True,
            )

        if not _command_available(adapter.command_for(kind)):
            return self._argument_error(
                cwd=cwd,
                timeout=execution_timeout,
                code=(
                    "codex_unavailable"
                    if harness_name == "codex"
                    else "delegate_harness_unavailable"
                ),
                message=f"Delegate harness command is not available: {harness_name}",
                details={
                    "harness": harness_name,
                    "command": adapter.command_for(kind),
                },
                harness=harness_name,
            )

        quota_admission_error: dict[str, object] | None = None
        if self.quota_admission_gate is not None:
            quota = self.quota_admission_gate.decision(
                harness=harness_name,
                model=effective_model,
                fresh=True,
            )
            if quota.get("allowed") is False:
                quota_admission_error = self._argument_error(
                    cwd=cwd,
                    timeout=execution_timeout,
                    code="delegate_quota_blocked",
                    message=(
                        f"Delegate harness {harness_name!r} is blocked by the configured quota threshold."
                    ),
                    details=quota,
                    harness=harness_name,
                )

        matching: DelegateTask | None = None
        with self._lock:
            target_group = None
            if group_id:
                target_group = self.scheduler.groups.get(group_id)
                if target_group is None:
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="group_not_found",
                        message=f"Delegate group not found: {group_id}",
                    )
                if (
                    kind != "explore"
                    or target_group.project.project_key != project.project_key
                    or target_group.harness != harness_name
                ):
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="invalid_delegate_group",
                        message="Only explore tasks using the group's project and harness may join it.",
                    )
                if target_group.completed_event.is_set():
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="delegate_group_completed",
                        message=f"Delegate group is already complete: {group_id}",
                    )
                if (
                    target_group.logical_session_id != logical_session_id
                    and (
                        target_group.logical_session_id is not None
                        or logical_session_id is not None
                    )
                ):
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="delegate_owned_by_another_session",
                        message="Delegate group belongs to a different logical session.",
                    )
            matching = next(
                (
                    item
                    for item in self.scheduler.tasks.values()
                    if not item.is_terminal and item.request_fingerprint == fingerprint
                ),
                None,
            )
            if (
                matching is not None
                and matching.logical_session_id != logical_session_id
                and (
                    matching.logical_session_id is not None
                    or logical_session_id is not None
                )
            ):
                return self._argument_error(
                    cwd=cwd,
                    timeout=execution_timeout,
                    code="delegate_owned_by_another_session",
                    message="Matching active delegate belongs to a different logical session.",
                )
            if matching is None:
                if quota_admission_error is not None:
                    return quota_admission_error
                if before_submit is not None:
                    admission_error = before_submit()
                    if admission_error is not None:
                        return admission_error
                delegate = self._make_task(
                    harness=harness_name,
                    project=project,
                    cwd=cwd,
                    kind=kind,
                    task=normalized_task or None,
                    goal=normalized_goal or None,
                    task_id=normalized_task_id,
                    group_id=group_id,
                    model=effective_model,
                    reasoning_effort=effective_reasoning,
                    sandbox_mode=sandbox_mode,
                    commit_mode=effective_commit,
                    execution_timeout_seconds=execution_timeout,
                    depends_on_group_ids=dependencies,
                    files_in_scope=files_in_scope or [],
                    out_of_scope=out_of_scope or [],
                    context_files=context_files or [],
                    acceptance_criteria=acceptance_criteria or [],
                    done_means=done_means or [],
                    verification_commands=verification_commands or [],
                    output_schema=output_schema,
                    parse_structured_output=parse_structured_output,
                    request_fingerprint=fingerprint,
                    resume_from_delegate_id=(
                        (resume_from_delegate_id or "").strip() or None
                    ),
                    resume_conversation_id=resume_conversation_id,
                    logical_session_id=logical_session_id,
                )
                if target_group is not None:
                    delegate.group_max_concurrency = target_group.max_concurrency
                    target_group.child_ids.append(delegate.delegate_id)
                try:
                    self.scheduler.submit_task(delegate)
                except (DelegateQueueFullError, DelegateSchedulerShuttingDownError) as exc:
                    if target_group is not None:
                        target_group.child_ids.remove(delegate.delegate_id)
                    if isinstance(exc, DelegateSchedulerShuttingDownError):
                        return self._argument_error(
                            cwd=cwd,
                            timeout=execution_timeout,
                            code="server_shutdown",
                            message=str(exc),
                        )
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="delegate_queue_full",
                        message=str(exc),
                        details={"scope": exc.scope, "limit": exc.limit},
                    )
        if matching is not None:
            return self._wait_for_task(matching, wait_seconds=wait_seconds, attached=True)
        return self._wait_for_task(delegate, wait_seconds=wait_seconds, attached=False)

    def run_codex(self, **kwargs: object) -> dict[str, object]:
        """Backward-compatible entry point pinned to the Codex harness."""
        kwargs["harness"] = "codex"
        return self.run_delegate(**kwargs)  # type: ignore[arg-type]

    def run_delegate_batch(
        self,
        *,
        tasks: list[dict[str, object]],
        cwd: Path,
        harness: str | None = None,
        max_concurrency: int | None = None,
        wait_seconds: float = DEFAULT_DELEGATE_WAIT_SECONDS,
        execution_timeout_seconds: int | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        logical_session_id: str | None = None,
    ) -> dict[str, object]:
        if self.scheduler.is_shutting_down:
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="server_shutdown",
                message="delegate scheduler is shutting down",
                harness=(harness or self.default_harness).strip().lower(),
            )
        harness_name, adapter = self._resolve_harness(harness)
        if adapter is None:
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="unsupported_delegate_harness",
                message=f"Unsupported delegate harness: {harness_name}",
                details={"available_harnesses": sorted(self.harnesses)},
                harness=harness_name,
            )
        if not tasks:
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="empty_delegate_batch",
                message="delegate_batch requires at least one exploration task.",
            )
        if max_concurrency is not None and max_concurrency <= 0:
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="invalid_max_concurrency",
                message="max_concurrency must be a positive integer.",
            )
        normalized_batch_reasoning = _normalize_reasoning_effort(reasoning_effort)
        if (
            normalized_batch_reasoning is not None
            and normalized_batch_reasoning not in ALLOWED_REASONING_EFFORTS
        ):
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="unsupported_reasoning_effort",
                message=f"Unsupported reasoning_effort: {reasoning_effort}",
                details={
                    "allowed_reasoning_efforts": [
                        DEFAULT_REASONING_EFFORT,
                        *ALLOWED_REASONING_EFFORTS,
                    ]
                },
            )
        cwd_error = self._cwd_error(cwd)
        if cwd_error is not None:
            return cwd_error
        if not _command_available(adapter.command_for("explore")):
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code=(
                    "codex_unavailable"
                    if harness_name == "codex"
                    else "delegate_harness_unavailable"
                ),
                message=f"Delegate harness command is not available: {harness_name}",
                details={
                    "harness": harness_name,
                    "command": adapter.command_for("explore"),
                },
                harness=harness_name,
            )
        if not adapter.supports_read_only():
            return self._argument_error(
                cwd=cwd,
                timeout=int(execution_timeout_seconds or self.explore_execution_timeout_seconds),
                code="readonly_sandbox_unavailable",
                message=(
                    f"Explore batches require harness {harness_name!r} to enforce read-only execution."
                ),
                harness=harness_name,
            )
        project = self.project_resolver.resolve(cwd)
        effective_model, effective_reasoning, commit_mode, sandbox_mode = self._task_defaults(
            adapter=adapter,
            kind="explore",
            model=model,
            reasoning_effort=reasoning_effort,
            commit_mode="forbidden",
        )
        execution_timeout = int(
            execution_timeout_seconds or self.explore_execution_timeout_seconds
        )
        group_id = f"grp-{uuid.uuid4().hex[:12]}"
        prepared_children: list[dict[str, object]] = []
        quota_refreshed = False
        for index, spec in enumerate(tasks):
            task_text = str(spec.get("task") or "").strip()
            goal_text = str(spec.get("goal") or "").strip()
            if not task_text and not goal_text:
                return self._argument_error(
                    cwd=cwd,
                    timeout=execution_timeout,
                    code="invalid_batch_task",
                    message=f"Batch task at index {index} requires task or goal.",
                    details={"index": index},
                )
            child_reasoning = (
                str(spec.get("reasoning_effort"))
                if spec.get("reasoning_effort") is not None
                else effective_reasoning
            )
            normalized_child_reasoning = _normalize_reasoning_effort(child_reasoning)
            if (
                normalized_child_reasoning is not None
                and normalized_child_reasoning not in ALLOWED_REASONING_EFFORTS
            ):
                return self._argument_error(
                    cwd=cwd,
                    timeout=execution_timeout,
                    code="unsupported_reasoning_effort",
                    message=f"Unsupported reasoning_effort in batch task {index}: {child_reasoning}",
                    details={"index": index},
                )
            task_model, task_reasoning, _, _ = self._task_defaults(
                adapter=adapter,
                kind="explore",
                model=str(spec.get("model")) if spec.get("model") is not None else effective_model,
                reasoning_effort=child_reasoning,
                commit_mode="forbidden",
            )
            if self.quota_admission_gate is not None:
                quota = self.quota_admission_gate.decision(
                    harness=harness_name,
                    model=task_model,
                    fresh=not quota_refreshed,
                )
                quota_refreshed = True
                if quota.get("allowed") is False:
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="delegate_quota_blocked",
                        message=(
                            f"Delegate harness {harness_name!r} is blocked by "
                            "the configured quota threshold."
                        ),
                        details=quota,
                        harness=harness_name,
                    )
            task_id = str(spec.get("task_id") or "").strip() or None
            scopes = self._batch_lists(spec)
            output_schema = (
                spec.get("output_schema")
                if isinstance(spec.get("output_schema"), dict)
                else None
            )
            parse_structured_output = bool(
                spec.get("parse_structured_output", True)
            )
            fingerprint = _delegate_request_fingerprint(
                task=task_text or None,
                goal=goal_text or None,
                task_id=task_id,
                cwd=cwd,
                harness=harness_name,
                kind="explore",
                group_id=group_id,
                files_in_scope=scopes["files_in_scope"],
                out_of_scope=scopes["out_of_scope"],
                context_files=scopes["context_files"],
                acceptance_criteria=scopes["acceptance_criteria"],
                done_means=scopes["done_means"],
                verification_commands=scopes["verification_commands"],
                commit_mode=commit_mode,
                model=task_model,
                reasoning_effort=task_reasoning,
                output_schema=output_schema,
                parse_structured_output=parse_structured_output,
                depends_on_group_ids=None,
                resume_conversation_id=None,
            )
            prepared_children.append(
                {
                    "task": task_text or None,
                    "goal": goal_text or None,
                    "task_id": task_id,
                    "model": task_model,
                    "reasoning_effort": task_reasoning,
                    "scopes": scopes,
                    "output_schema": output_schema,
                    "parse_structured_output": parse_structured_output,
                    "request_fingerprint": fingerprint,
                }
            )

        with self._lock:
            children: list[DelegateTask] = []
            for prepared in prepared_children:
                scopes = prepared["scopes"]
                assert isinstance(scopes, dict)
                children.append(
                    self._make_task(
                        harness=harness_name,
                        project=project,
                        cwd=cwd,
                        kind="explore",
                        task=(
                            prepared["task"]
                            if isinstance(prepared["task"], str)
                            else None
                        ),
                        goal=(
                            prepared["goal"]
                            if isinstance(prepared["goal"], str)
                            else None
                        ),
                        task_id=(
                            prepared["task_id"]
                            if isinstance(prepared["task_id"], str)
                            else None
                        ),
                        group_id=group_id,
                        model=str(prepared["model"]),
                        reasoning_effort=str(prepared["reasoning_effort"]),
                        sandbox_mode=sandbox_mode,
                        commit_mode=commit_mode,
                        execution_timeout_seconds=execution_timeout,
                        depends_on_group_ids=(),
                        files_in_scope=list(scopes["files_in_scope"]),
                        out_of_scope=list(scopes["out_of_scope"]),
                        context_files=list(scopes["context_files"]),
                        acceptance_criteria=list(scopes["acceptance_criteria"]),
                        done_means=list(scopes["done_means"]),
                        verification_commands=list(
                            scopes["verification_commands"]
                        ),
                        output_schema=(
                            prepared["output_schema"]
                            if isinstance(prepared["output_schema"], dict)
                            else None
                        ),
                        parse_structured_output=bool(
                            prepared["parse_structured_output"]
                        ),
                        request_fingerprint=str(
                            prepared["request_fingerprint"]
                        ),
                        logical_session_id=logical_session_id,
                    )
                )
            group = DelegateGroup(
                group_id=group_id,
                harness=harness_name,
                project=project,
                kind="explore_batch",
                child_ids=[child.delegate_id for child in children],
                submitted_at=time.time(),
                logical_session_id=logical_session_id,
                max_concurrency=(
                    min(max_concurrency, self.scheduler.max_explore_per_project)
                    if max_concurrency is not None
                    else None
                ),
            )
            for child in children:
                child.group_max_concurrency = group.max_concurrency
            try:
                self.scheduler.submit_group(group, children)
            except (DelegateQueueFullError, DelegateSchedulerShuttingDownError) as exc:
                if isinstance(exc, DelegateSchedulerShuttingDownError):
                    return self._argument_error(
                        cwd=cwd,
                        timeout=execution_timeout,
                        code="server_shutdown",
                        message=str(exc),
                        harness=harness_name,
                    )
                return self._argument_error(
                    cwd=cwd,
                    timeout=execution_timeout,
                    code="delegate_queue_full",
                    message=str(exc),
                    details={"scope": exc.scope, "limit": exc.limit},
                )
        group.completed_event.wait(timeout=max(0.0, float(wait_seconds)))
        return self._group_snapshot(group, include_results=group.completed_event.is_set())

    def run_codex_batch(self, **kwargs: object) -> dict[str, object]:
        """Backward-compatible batch entry point pinned to the Codex harness."""
        kwargs["harness"] = "codex"
        return self.run_delegate_batch(**kwargs)  # type: ignore[arg-type]

    def delegate_cancel(
        self,
        *,
        delegate_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, object]:
        if bool(delegate_id) == bool(group_id):
            return {
                "success": False,
                "error": {
                    "code": "invalid_cancel_filter",
                    "message": "Provide exactly one of delegate_id or group_id.",
                },
            }
        if delegate_id:
            normalized_delegate_id = delegate_id.strip()
            task = self.scheduler.cancel_task(normalized_delegate_id)
            if task is None:
                with self._lock:
                    historical = next(
                        (
                            dict(item)
                            for item in reversed(self._history)
                            if str(item.get("delegate_id") or "") == normalized_delegate_id
                        ),
                        None,
                    )
                if historical is not None:
                    return {"success": True, "delegate": historical}
                persisted_path = self._persisted_delegate_paths.get(normalized_delegate_id)
                if persisted_path is not None:
                    payload = self._read_persisted_delegate_metadata(persisted_path)
                    if (
                        isinstance(payload, dict)
                        and payload.get("durable") is True
                        and isinstance(payload.get("durable_job_id"), str)
                        and self.durable_job_registry is not None
                        and self.durable_state_dir is not None
                    ):
                        self._persist_durable_termination_intent(
                            persisted_path,
                            reason="cancelled",
                        )
                        self.durable_job_registry.kill_job(
                            job_id=str(payload["durable_job_id"]),
                            state_dir=self.durable_state_dir,
                            signal_name="TERM",
                        )
                        snapshot = self._status_from_persisted_delegate(persisted_path)
                        if snapshot is not None:
                            return {"success": True, "delegate": snapshot}
                return self._not_found("delegate", normalized_delegate_id)
            if not task.is_terminal:
                task.completed_event.wait(timeout=task.cancel_grace_seconds + 1)
            return {"success": True, "delegate": self._task_snapshot(task)}
        normalized_group_id = (group_id or "").strip()
        group = self.scheduler.cancel_group(normalized_group_id)
        if group is None:
            with self._lock:
                historical_group = next(
                    (
                        dict(item)
                        for item in reversed(self._group_history)
                        if str(item.get("group_id") or "") == normalized_group_id
                    ),
                    None,
                )
            if historical_group is not None:
                return {"success": True, "group": historical_group}
            return self._not_found("group", normalized_group_id)
        group.completed_event.wait(timeout=self.cancel_grace_seconds + 1)
        return {"success": True, "group": self._group_snapshot(group, include_results=True)}

    def delegate_status(
        self,
        *,
        delegate_id: str | None = None,
        group_id: str | None = None,
        project_cwd: str | Path | None = None,
        limit: int = 10,
        offset: int = 0,
        watch_seconds: float = 0.0,
        poll_seconds: float = DEFAULT_DELEGATE_STATUS_POLL_SECONDS,
        max_tokens: int = DEFAULT_TOOL_OUTPUT_TOKEN_BUDGET,
        exclude_delegate_ids: set[str] | frozenset[str] | None = None,
    ) -> dict[str, object]:
        selected = sum(bool(value) for value in (delegate_id, group_id, project_cwd))
        if selected > 1:
            return {
                "success": False,
                "error": {
                    "code": "invalid_status_filter",
                    "message": "Choose at most one of delegate_id, group_id, or project_cwd.",
                },
            }
        watch_seconds = max(0.0, min(float(watch_seconds), MAX_DELEGATE_STATUS_WATCH_SECONDS))
        poll_seconds = max(0.1, min(float(poll_seconds), 60.0))

        excluded_delegate_ids = frozenset(exclude_delegate_ids or ())

        def status_once() -> dict[str, object]:
            if group_id or project_cwd or offset or excluded_delegate_ids:
                return self._delegate_status_once(
                    delegate_id=delegate_id,
                    group_id=group_id,
                    project_cwd=project_cwd,
                    limit=limit,
                    offset=offset,
                    exclude_delegate_ids=excluded_delegate_ids,
                )
            # Preserve the old private hook signature used by local callers.
            return self._delegate_status_once(delegate_id=delegate_id, limit=limit)

        initial = status_once()
        if watch_seconds <= 0:
            return self._budget_delegate_status(initial, max_tokens=max_tokens, offset=offset)
        started_at = time.monotonic()
        if initial.get("success") is False:
            initial["watch"] = self._watch_payload(
                started_at, watch_seconds, poll_seconds, error_returned=True
            )
            return self._budget_delegate_status(initial, max_tokens=max_tokens, offset=offset)
        focus = _status_focus_entry(initial)
        if focus and focus.get("completed"):
            initial["watch"] = self._watch_payload(
                started_at, watch_seconds, poll_seconds, already_terminal=True
            )
            return self._budget_delegate_status(initial, max_tokens=max_tokens, offset=offset)
        signature = _status_response_lifecycle_signature(initial)
        deadline = started_at + watch_seconds
        current = initial
        while time.monotonic() < deadline:
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
            current = status_once()
            if _status_response_lifecycle_signature(current) != signature:
                current["watch"] = self._watch_payload(
                    started_at, watch_seconds, poll_seconds, status_changed=True
                )
                return self._budget_delegate_status(current, max_tokens=max_tokens, offset=offset)
        current["watch"] = self._watch_payload(
            started_at, watch_seconds, poll_seconds, timed_out=True
        )
        return self._budget_delegate_status(current, max_tokens=max_tokens, offset=offset)

    def _delegate_status_once(
        self,
        *,
        delegate_id: str | None,
        limit: int,
        group_id: str | None = None,
        project_cwd: str | Path | None = None,
        offset: int = 0,
        exclude_delegate_ids: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        limit = max(1, min(int(limit), DEFAULT_DELEGATE_HISTORY_LIMIT))
        offset = max(0, int(offset))
        if delegate_id:
            normalized_delegate_id = delegate_id.strip()
            if normalized_delegate_id in exclude_delegate_ids:
                return self._not_found("delegate", normalized_delegate_id)
            task = self.scheduler.get_task(normalized_delegate_id)
            if task is None:
                with self._lock:
                    historical = next(
                        (
                            dict(item)
                            for item in reversed(self._history)
                            if str(item.get("delegate_id") or "") == normalized_delegate_id
                        ),
                        None,
                    )
                    persisted_path = self._persisted_delegate_paths.get(normalized_delegate_id)
                if historical is not None:
                    return {"success": True, "delegate": historical}
                if persisted_path is not None:
                    persisted = self._status_from_persisted_delegate(persisted_path)
                    if persisted is not None:
                        return {
                            "success": True,
                            "delegate": persisted,
                        }
                return self._not_found("delegate", normalized_delegate_id)
            return {"success": True, "delegate": self._task_snapshot(task)}
        if group_id:
            normalized_group_id = group_id.strip()
            group = self.scheduler.get_group(normalized_group_id)
            if group is None:
                with self._lock:
                    historical_group = next(
                        (
                            dict(item)
                            for item in reversed(self._group_history)
                            if str(item.get("group_id") or "") == normalized_group_id
                        ),
                        None,
                    )
                if historical_group is not None:
                    return {"success": True, "group": historical_group}
                return self._not_found("group", normalized_group_id)
            return {"success": True, "group": self._group_snapshot(group, include_results=False)}
        if project_cwd:
            project = self.project_resolver.resolve(Path(project_cwd))
            tasks = [
                task
                for task in self.scheduler.tasks_for_project(project.project_key)
                if task.delegate_id not in exclude_delegate_ids
            ]
            counts = self.scheduler.task_counts(tasks)
            active = [self._task_snapshot(task) for task in tasks if not task.is_terminal]
            return {
                "success": True,
                "project": {
                    **project.as_payload(),
                    "status": "running" if active else "idle",
                    "completed": not active,
                    "in_progress": bool(active),
                    "counts": counts,
                    "active": active,
                },
            }

        active_tasks = sorted(
            (
                item
                for item in self.scheduler.nonterminal_tasks()
                if item.delegate_id not in exclude_delegate_ids
            ),
            key=lambda item: item.submitted_seq,
        )
        active_snapshots = [self._task_snapshot(task) for task in active_tasks]
        with self._lock:
            history = [
                item
                for item in reversed(self._history)
                if str(item.get("delegate_id") or "") not in exclude_delegate_ids
            ]
        all_recent = [*active_snapshots]
        seen = {str(item.get("delegate_id")) for item in all_recent}
        all_recent.extend(
            item for item in history if str(item.get("delegate_id")) not in seen
        )
        recent = all_recent[offset : offset + limit]
        return {
            "success": True,
            "active": active_snapshots[0] if active_snapshots else None,
            "active_delegates": active_snapshots,
            "latest": all_recent[0] if all_recent else None,
            "recent": recent,
            "history_limit": DEFAULT_DELEGATE_HISTORY_LIMIT,
            "truncated": offset + len(recent) < len(all_recent),
            "next_offset": offset + len(recent) if offset + len(recent) < len(all_recent) else None,
        }

    def _make_task(
        self,
        *,
        harness: str,
        project: ProjectIdentity,
        cwd: Path,
        kind: TaskKind,
        task: str | None,
        goal: str | None,
        task_id: str | None,
        group_id: str | None,
        model: str,
        reasoning_effort: str,
        sandbox_mode: str,
        commit_mode: str,
        execution_timeout_seconds: int,
        depends_on_group_ids: tuple[str, ...],
        files_in_scope: list[str],
        out_of_scope: list[str],
        context_files: list[str],
        acceptance_criteria: list[str],
        done_means: list[str],
        verification_commands: list[str],
        output_schema: dict[str, object] | None,
        parse_structured_output: bool,
        request_fingerprint: str,
        resume_from_delegate_id: str | None = None,
        resume_conversation_id: str | None = None,
        logical_session_id: str | None = None,
    ) -> DelegateTask:
        self._submitted_seq += 1
        delegate_id = uuid.uuid4().hex[:12]
        log_paths = _create_delegate_logs(
            delegate_id,
            harness=harness,
            state_root=self.delegate_state_root,
        )
        prompt = self._build_prompt(
            harness=harness,
            task=task,
            goal=goal,
            task_id=task_id,
            project_context=self._project_prompt_context(project, cwd=cwd),
            files_in_scope=files_in_scope,
            out_of_scope=out_of_scope,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            done_means=done_means,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
            kind=kind,
        )
        write_private_text(log_paths.prompt, prompt)
        ensure_private_file(log_paths.stdout)
        ensure_private_file(log_paths.stderr)
        delegate = DelegateTask(
            delegate_id=delegate_id,
            harness=harness,
            project=project,
            kind=kind,
            cwd=cwd,
            task=task,
            goal=goal,
            task_id=task_id,
            group_id=group_id,
            model=model,
            reasoning_effort=reasoning_effort,
            sandbox_mode=sandbox_mode,
            commit_mode=commit_mode,
            execution_timeout_seconds=execution_timeout_seconds,
            cancel_grace_seconds=self.cancel_grace_seconds,
            request_fingerprint=request_fingerprint,
            prompt=prompt,
            log_paths=log_paths,
            output_schema=output_schema,
            parse_structured_output=parse_structured_output,
            depends_on_group_ids=depends_on_group_ids,
            resume_from_delegate_id=resume_from_delegate_id,
            resume_conversation_id=resume_conversation_id,
            logical_session_id=logical_session_id,
            submitted_seq=self._submitted_seq,
        )
        write_private_json(
            log_paths.metadata,
            {
                "delegate_id": delegate_id,
                "executor": harness,
                "harness": harness,
                "group_id": group_id,
                "status": "queued",
                "kind": kind,
                "lane": delegate.lane,
                "cwd": str(cwd),
                "project": project.as_payload(),
                "sandbox_mode": sandbox_mode,
                "commit_mode": commit_mode,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "execution_timeout_seconds": execution_timeout_seconds,
                "task_id": task_id,
                "request_fingerprint": request_fingerprint,
                "depends_on_group_ids": list(depends_on_group_ids),
                "resume_from_delegate_id": resume_from_delegate_id,
                "resume_conversation_id": resume_conversation_id,
                "logical_session_id": logical_session_id,
                "submitted_at_epoch": delegate.submitted_at,
            },
        )
        return delegate

    def _run_scheduled_task(self, task: DelegateTask) -> dict[str, object]:
        return self._start_codex_delegate_impl(delegate_task=task)

    def _start_codex_delegate_impl(self, *, delegate_task: DelegateTask) -> dict[str, object]:
        """Legacy hook retained for callers that instrument delegate execution."""
        return self._start_delegate_impl(delegate_task=delegate_task)

    def _start_delegate_impl(self, *, delegate_task: DelegateTask) -> dict[str, object]:
        if self._durable_enabled_for(delegate_task):
            return self._run_durable_delegate(delegate_task)
        return self._process_runner.run(
            delegate_task,
            invocation_builder=self._build_invocation_for_task,
        )

    def _run_durable_delegate(self, task: DelegateTask) -> dict[str, object]:
        assert self.durable_job_registry is not None
        assert self.durable_state_dir is not None
        invocation = self._build_invocation_for_task(task)
        if invocation.use_shell or not isinstance(invocation.args, list):
            return self._process_runner.run(
                task,
                invocation_builder=lambda _task: invocation,
            )

        before_status = self._process_runner._git_status(task) if task.kind == "explore" else None
        if (
            task.kind == "explore"
            and task.project.git_common_dir is not None
            and before_status is None
            and not invocation.read_only_enforced
        ):
            return self._process_runner.run(
                task,
                invocation_builder=lambda _task: invocation,
            )

        command = (
            f"{shlex.join(invocation.args)} < "
            f"{shlex.quote(str(task.log_paths.prompt))}"
        )
        started = self.durable_job_registry.start_job(
            command=command,
            cwd=task.cwd,
            state_dir=self.durable_state_dir,
            name=f"delegate:{task.delegate_id}:{task.harness}",
            timeout_seconds=task.execution_timeout_seconds,
            env=invocation.env_overrides,
        )
        if started.get("success") is False:
            return self._process_runner._result(
                task,
                status="failed",
                exit_code=TIMEOUT_EXIT_CODE,
                error={
                    "code": "durable_job_start_failed",
                    "message": str(
                        (started.get("error") or {}).get("message")
                        if isinstance(started.get("error"), dict)
                        else "Failed to start durable delegate job."
                    ),
                },
                structured_output=None,
                duration_seconds=0.0,
            )

        job_id = str(started.get("job_id") or "")
        if not job_id:
            return self._process_runner._result(
                task,
                status="failed",
                exit_code=TIMEOUT_EXIT_CODE,
                error={
                    "code": "durable_job_start_failed",
                    "message": "Durable job started without a job_id.",
                },
                structured_output=None,
                duration_seconds=0.0,
            )
        task.durable_job_id = job_id
        task.log_paths = DelegateLogPaths(
            log_dir=task.log_paths.log_dir,
            prompt=task.log_paths.prompt,
            stdout=Path(str(started["stdout_log"])),
            stderr=Path(str(started["stderr_log"])),
            metadata=task.log_paths.metadata,
        )
        self._write_durable_delegate_metadata(
            task,
            invocation=invocation,
            before_status=before_status,
            job_status=started,
        )
        if task.cancel_requested:
            self._terminate_task(task)

        return self._wait_for_durable_delegate_job(
            task,
            invocation=invocation,
            before_status=before_status,
            initial_status=started,
        )

    def _wait_for_durable_delegate_job(
        self,
        task: DelegateTask,
        *,
        invocation: Invocation,
        before_status: bytes | None,
        initial_status: dict[str, object] | None = None,
    ) -> dict[str, object]:
        assert self.durable_job_registry is not None
        assert self.durable_state_dir is not None
        job_id = task.durable_job_id or ""
        if not job_id:
            return self._process_runner._result(
                task,
                status="failed",
                exit_code=TIMEOUT_EXIT_CODE,
                error={
                    "code": "durable_job_missing",
                    "message": "Durable delegate has no durable job id.",
                },
                structured_output=None,
                duration_seconds=0.0,
            )

        status = initial_status or self.durable_job_registry.job_status(
            job_id=job_id,
            state_dir=self.durable_state_dir,
        )
        timed_out = False
        while str(status.get("status") or "") not in {
            "succeeded",
            "failed",
            "killed",
            "interrupted",
            "timed_out",
        }:
            if (
                not timed_out
                and float(status.get("elapsed_seconds") or 0.0)
                >= task.execution_timeout_seconds
            ):
                timed_out = True
                self._persist_durable_termination_intent(
                    task.log_paths.metadata,
                    reason="timed_out",
                )
                self.durable_job_registry.kill_job(
                    job_id=job_id,
                    state_dir=self.durable_state_dir,
                    signal_name="TERM",
                )
            time.sleep(0.05)
            status = self.durable_job_registry.job_status(
                job_id=job_id,
                state_dir=self.durable_state_dir,
            )
            if status.get("success") is False:
                break

        return self._finalize_durable_delegate(
            task,
            invocation=invocation,
            before_status=before_status,
            job_status=status,
            timed_out=timed_out or str(status.get("status") or "") == "timed_out",
        )

    def _write_durable_delegate_metadata(
        self,
        task: DelegateTask,
        *,
        invocation: Invocation,
        before_status: bytes | None,
        job_status: dict[str, object],
    ) -> None:
        write_private_json(
            task.log_paths.metadata,
            {
                "delegate_id": task.delegate_id,
                "executor": task.harness,
                "harness": task.harness,
                "group_id": task.group_id,
                "group_max_concurrency": task.group_max_concurrency,
                "status": "running",
                "completed": False,
                "in_progress": True,
                "kind": task.kind,
                "lane": task.lane,
                "cwd": str(task.cwd),
                "project": task.project.as_payload(),
                "sandbox_mode": task.sandbox_mode,
                "commit_mode": task.commit_mode,
                "model": task.model,
                "reasoning_effort": task.reasoning_effort,
                "execution_timeout_seconds": task.execution_timeout_seconds,
                "task_id": task.task_id,
                "request_fingerprint": task.request_fingerprint,
                "depends_on_group_ids": list(task.depends_on_group_ids),
                "resume_from_delegate_id": task.resume_from_delegate_id,
                "resume_conversation_id": task.resume_conversation_id,
                "submitted_at_epoch": task.submitted_at,
                "started_at_epoch": task.started_at,
                "output_schema": task.output_schema,
                "parse_structured_output": task.parse_structured_output,
                "command_kind": "argv",
                "read_only_enforced": invocation.read_only_enforced,
                "readonly_git_status_before_hex": (
                    before_status.hex() if before_status is not None else None
                ),
                "durable": True,
                "durable_job_id": task.durable_job_id,
                "durable_job_status": job_status.get("status"),
                "logs": task.log_paths.as_payload(),
            },
        )

    def _finalize_durable_delegate(
        self,
        task: DelegateTask,
        *,
        invocation: Invocation,
        before_status: bytes | None,
        job_status: dict[str, object],
        timed_out: bool,
    ) -> dict[str, object]:
        persisted = self._read_persisted_delegate_metadata(task.log_paths.metadata)
        termination_reason = (
            str(persisted.get("durable_termination_reason"))
            if isinstance(persisted, dict)
            and isinstance(persisted.get("durable_termination_reason"), str)
            else None
        )
        effective_timed_out = timed_out or termination_reason == "timed_out"
        if job_status.get("success") is False:
            result = self._process_runner._result(
                task,
                status="failed",
                exit_code=TIMEOUT_EXIT_CODE,
                error={
                    "code": "durable_job_status_failed",
                    "message": str(
                        (job_status.get("error") or {}).get("message")
                        if isinstance(job_status.get("error"), dict)
                        else "Failed to read durable delegate job status."
                    ),
                },
                structured_output=None,
                duration_seconds=float(job_status.get("elapsed_seconds") or 0.0),
            )
        else:
            try:
                stdout_raw = task.log_paths.stdout.read_bytes()
            except OSError:
                stdout_raw = b""
            try:
                stderr_raw = task.log_paths.stderr.read_bytes()
            except OSError:
                stderr_raw = b""
            cancellation_error = None
            persisted_cancel_reason = (
                termination_reason
                if termination_reason and termination_reason != "timed_out"
                else None
            )
            if (
                persisted_cancel_reason is not None
                or task.cancel_requested
                or (
                    str(job_status.get("status") or "") == "killed"
                    and not effective_timed_out
                )
            ):
                cancellation_error = {
                    "code": persisted_cancel_reason or task.cancel_reason or "cancelled",
                    "message": f"{harness_display_name(task.harness)} delegate was cancelled.",
                }
            result = self._process_runner.finalize_completed(
                task,
                invocation=invocation,
                stdout_raw=stdout_raw,
                stderr_raw=stderr_raw,
                exit_code=int(job_status.get("exit_code") or 0),
                duration_seconds=float(job_status.get("elapsed_seconds") or 0.0),
                before_status=before_status,
                timed_out=effective_timed_out,
                cancellation_error=cancellation_error,
            )
        result["durable"] = True
        result["durable_job_id"] = task.durable_job_id
        result["recovered_from_disk"] = task.recovered_from_disk
        if termination_reason is not None:
            result["durable_termination_reason"] = termination_reason
        self._process_runner._write_final_metadata(task, result)
        return result

    def _build_invocation_for_task(self, task: DelegateTask) -> Invocation:
        adapter = self.harnesses.get(task.harness)
        if adapter is None:
            raise OSError(f"Delegate harness is no longer configured: {task.harness}")
        return adapter.build_invocation(task)

    def _build_invocation(
        self,
        *,
        command: str,
        task: str | None,
        goal: str | None,
        task_id: str | None = None,
        cwd: Path,
        files_in_scope: list[str] | None = None,
        out_of_scope: list[str] | None = None,
        context_files: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        done_means: list[str] | None = None,
        verification_commands: list[str] | None = None,
        commit_mode: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        kind: TaskKind = "code",
        prompt: str | None = None,
        is_git: bool | None = None,
    ) -> Invocation:
        effective_prompt = prompt or self._build_prompt(
            task=task,
            goal=goal,
            task_id=task_id,
            files_in_scope=files_in_scope or [],
            out_of_scope=out_of_scope or [],
            context_files=context_files or [],
            acceptance_criteria=acceptance_criteria or [],
            done_means=done_means or [],
            verification_commands=verification_commands or [],
            commit_mode="forbidden" if kind == "explore" else commit_mode,
            kind=kind,
        )
        return self._build_invocation_from_prompt(
            command=command,
            cwd=cwd,
            prompt=effective_prompt,
            model=_normalize_model(model),
            reasoning_effort=_normalize_reasoning_effort(reasoning_effort),
            kind=kind,
            is_git=is_git,
        )

    def _build_invocation_from_prompt(
        self,
        *,
        command: str,
        cwd: Path,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        kind: TaskKind = "code",
        is_git: bool | None = None,
    ) -> Invocation:
        parts = _resolve_delegate_command_parts(command)
        if parts and _binary_name(parts[0]) == "codex":
            args = [*parts, "exec"]
            if model:
                args.extend(["--model", model])
            if reasoning_effort:
                args.extend(["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"])
            if kind == "explore":
                args.extend(["--sandbox", "read-only", "--ephemeral"])
            else:
                args.append("--dangerously-bypass-approvals-and-sandbox")
            args.extend(["-C", str(cwd)])
            git_repo = (cwd / ".git").exists() if is_git is None else is_git
            if not git_repo:
                args.append("--skip-git-repo-check")
            args.append("-")
            return Invocation(args=args, use_shell=False, stdin=prompt.encode("utf-8"))
        return Invocation(args=command, use_shell=True)

    def _build_prompt(
        self,
        *,
        harness: str = "codex",
        task: str | None,
        goal: str | None,
        task_id: str | None = None,
        project_context: list[str] | None = None,
        files_in_scope: list[str] | None = None,
        out_of_scope: list[str] | None = None,
        context_files: list[str],
        acceptance_criteria: list[str],
        done_means: list[str] | None = None,
        verification_commands: list[str],
        commit_mode: str,
        kind: TaskKind = "code",
    ) -> str:
        executor_contract = (
            "- Codex is the local executor for exactly one bounded execution slice."
            if harness == "codex"
            else f"- The {harness} CLI harness is the local executor for exactly one bounded execution slice."
        )
        lines: list[str] = [
            "Architecture contract:",
            "- ChatGPT Web is the architect/manager/reviewer.",
            executor_contract,
            "- Execute only the scoped task below; do not expand into a broad planning or research loop.",
            "- If the task is too broad or underspecified, stop and report blocked with the smallest useful next execution prompt.",
            "",
        ]
        if kind == "explore":
            lines.extend(
                [
                    "Read-only contract:",
                    "- This is a read-only exploration task.",
                    "- Do not create, modify, delete, rename, stage, commit, or format files.",
                    "- Do not run commands whose purpose is to mutate the repository.",
                    "- Return findings and evidence only.",
                    "",
                ]
            )
        if task_id:
            lines.extend(["Task ID:", task_id, ""])
        if goal:
            lines.extend(["Goal:", goal, ""])
        if task:
            lines.extend(["Task:", task, ""])
        if project_context:
            lines.extend(["Project context:"])
            lines.extend(f"- {item}" for item in project_context)
            lines.append("")
        for title, values in (
            ("Files in scope:", files_in_scope or []),
            ("Out of scope:", out_of_scope or []),
            ("Acceptance criteria:", acceptance_criteria),
            ("Done means:", done_means or []),
            ("Verification commands:", verification_commands),
        ):
            if values:
                lines.append(title)
                lines.extend(f"- {item}" for item in values)
                lines.append("")
        lines.append(f"Commit mode: {'forbidden' if kind == 'explore' else commit_mode}")
        if context_files:
            lines.extend(["", "Context files:"])
            lines.extend(f"- {path}" for path in context_files)
        lines.extend(
            [
                "",
                "Progress logging contract:",
                "- Print concise progress updates to stderr as work advances, especially before long-running commands.",
                "- Keep stdout quiet unless emitting the compact final manifest or requested structured JSON.",
                "- The MCP server persists stdout/stderr to local delegate logs, and the caller may inspect them with read_text while status=running.",
                "",
                "Output contract:",
            ]
        )
        if harness in {"claude", "antigravity", "antigravity2"}:
            lines.extend(
                [
                    "- A native JSON Schema is supplied by the harness.",
                    "- Return exactly one JSON object matching that schema as the final answer.",
                    "- Do not wrap the final JSON in markdown and do not add prose before or after it.",
                    "- Populate every required field; use empty arrays or null where appropriate.",
                ]
            )
        else:
            lines.append(
                "- Return a compact execution manifest: status, files changed, commands run, verification result, deviations or blockers."
            )
        lines.extend(
            [
                "- Do not claim done unless the acceptance criteria passed locally, or clearly state which checks were not run.",
                "- Suggest at most one next small execution prompt if more work remains.",
            ]
        )
        return "\n".join(lines)

    def _task_defaults(
        self,
        *,
        adapter: DelegateHarness,
        kind: TaskKind,
        model: str | None,
        reasoning_effort: str | None,
        commit_mode: str,
    ) -> tuple[str, str, str, str]:
        normalized_model = _normalize_model(model)
        normalized_reasoning = _normalize_reasoning_effort(reasoning_effort)
        defaults = adapter.task_defaults(kind)
        if kind == "explore":
            return (
                normalized_model or defaults.model,
                normalized_reasoning or defaults.reasoning_effort,
                "forbidden",
                defaults.sandbox_mode,
            )
        return (
            normalized_model or defaults.model,
            normalized_reasoning or defaults.reasoning_effort,
            commit_mode,
            defaults.sandbox_mode,
        )

    def _explore_command_is_sandboxed(self, harness: str = "codex") -> bool:
        adapter = self.harnesses.get(harness)
        return bool(adapter and adapter.supports_read_only())

    def _task_snapshot(self, task: DelegateTask) -> dict[str, object]:
        if task.result is not None and task.is_terminal:
            return dict(task.result)
        with task.output_lock:
            stdout_bytes = task.stdout_bytes
            stderr_bytes = task.stderr_bytes
            last_output_at = task.last_output_at
            process = task.process
        durable_job_status: dict[str, object] | None = None
        if (
            task.durable_job_id
            and self.durable_job_registry is not None
            and self.durable_state_dir is not None
        ):
            candidate = self.durable_job_registry.job_status(
                job_id=task.durable_job_id,
                state_dir=self.durable_state_dir,
            )
            if candidate.get("success") is not False:
                durable_job_status = candidate
        now = time.monotonic()
        reference = last_output_at or task.started_monotonic
        quiet_seconds = round(now - reference, 3) if reference is not None else None
        activity_state = "queued" if task.state == "queued" else "starting_or_quiet"
        if task.state == "running" and last_output_at is not None:
            activity_state = "active"
        if quiet_seconds is not None and quiet_seconds >= DELEGATE_STALL_HINT_SECONDS:
            activity_state = "suspected_stalled"
        payload: dict[str, object] = {
            "success": True,
            "status": task.state,
            "completed": False,
            "in_progress": True,
            "executor": task.harness,
            "harness": task.harness,
            "cwd": str(task.cwd),
            "delegate_id": task.delegate_id,
            "task_id": task.task_id,
            "group_id": task.group_id,
            "group_max_concurrency": task.group_max_concurrency,
            "request_fingerprint": task.request_fingerprint,
            "resume_from_delegate_id": task.resume_from_delegate_id,
            "kind": task.kind,
            "lane": task.lane,
            "concurrency_scope": "project",
            "serial": task.serial,
            "sandbox_mode": task.sandbox_mode,
            "commit_mode": task.commit_mode,
            "project": task.project.as_payload(),
            "model": task.model,
            "reasoning_effort": task.reasoning_effort,
            "submitted_at": _format_epoch_seconds(task.submitted_at),
            "submitted_at_epoch": task.submitted_at,
            "started_at": _format_epoch_seconds(task.started_at) if task.started_at else None,
            "started_at_epoch": task.started_at,
            "elapsed_seconds": (
                round(float(durable_job_status.get("elapsed_seconds") or 0.0), 3)
                if durable_job_status is not None
                else round(now - (task.started_monotonic or now), 3)
            ),
            "pid": (
                durable_job_status.get("pid")
                if durable_job_status is not None
                else getattr(process, "pid", None)
            ),
            "durable": bool(task.durable_job_id),
            "durable_job_id": task.durable_job_id,
            "durable_job_status": (
                durable_job_status.get("status")
                if durable_job_status is not None
                else None
            ),
            "recovered_from_disk": task.recovered_from_disk,
            "activity_state": activity_state,
            "last_output_seconds_ago": quiet_seconds,
            "stdout_bytes": stdout_bytes,
            "stderr_bytes": stderr_bytes,
            "logs": task.log_paths.as_payload(),
            "log_read_hint": log_read_hint(task),
            "output_omitted": True,
            "timed_out": False,
            "wait_timed_out": False,
            "timeout": task.execution_timeout_seconds,
            "execution_timeout_seconds": task.execution_timeout_seconds,
            "depends_on_group_ids": list(task.depends_on_group_ids),
        }
        return payload

    def _wait_for_task(
        self,
        task: DelegateTask,
        *,
        wait_seconds: float,
        attached: bool,
    ) -> dict[str, object]:
        task.completed_event.wait(timeout=max(0.0, float(wait_seconds)))
        if task.result is not None and task.is_terminal:
            return task.result
        payload = self._task_snapshot(task)
        payload.update(
            {
                "attached_to_running_delegate": attached,
                "wait_seconds": round(max(0.0, float(wait_seconds)), 3),
                "wait_timed_out": True,
                "message": "CLI delegate has not completed. Use delegate_status with delegate_id.",
                "next": "call delegate_status with this delegate_id",
            }
        )
        return payload

    def _group_snapshot(
        self,
        group: DelegateGroup,
        *,
        include_results: bool,
    ) -> dict[str, object]:
        children = [self.scheduler.get_task(child_id) for child_id in group.child_ids]
        child_tasks = [child for child in children if child is not None]
        counts = self.scheduler.task_counts(child_tasks)
        completed = group.completed_event.is_set()
        child_payloads = [
            {
                "delegate_id": child.delegate_id,
                "task_id": child.task_id,
                "status": child.state,
                "completed": child.is_terminal,
                **(
                    {"result": self._task_snapshot(child)}
                    if include_results and child.is_terminal
                    else {}
                ),
            }
            for child in child_tasks
        ]
        payload: dict[str, object] = {
            "success": group.state == "succeeded" if completed else True,
            "status": group.state,
            "in_progress": not completed,
            "completed": completed,
            "group_id": group.group_id,
            "executor": group.harness,
            "harness": group.harness,
            "kind": group.kind,
            "project": group.project.as_payload(),
            "max_concurrency": group.max_concurrency,
            "counts": counts,
            "children": child_payloads,
            "results_ready": completed,
        }
        if completed and group.state == "failed":
            payload["error"] = {
                "code": "delegate_group_failed",
                "message": "One or more exploration delegates did not succeed.",
            }
        return payload

    def _on_task_terminal(self, task: DelegateTask) -> None:
        snapshot = self._task_snapshot(task)
        self._record_terminal_telemetry(
            snapshot,
            completed_at_epoch=task.completed_at,
        )
        if self.quota_admission_gate is not None:
            self.quota_admission_gate.note_terminal(snapshot)
        group_snapshot: dict[str, object] | None = None
        if task.group_id:
            group = self.scheduler.get_group(task.group_id)
            if group is not None and group.completed_event.is_set():
                group_snapshot = self._group_snapshot(group, include_results=False)
        with self._lock:
            maxlen = self._history.maxlen or DEFAULT_DELEGATE_HISTORY_LIMIT
            self._history = deque(
                (
                    item
                    for item in self._history
                    if item.get("delegate_id") != task.delegate_id
                ),
                maxlen=maxlen,
            )
            self._history.append(snapshot)
            if group_snapshot is not None:
                group_maxlen = self._group_history.maxlen or DEFAULT_DELEGATE_HISTORY_LIMIT
                self._group_history = deque(
                    (
                        item
                        for item in self._group_history
                        if item.get("group_id") != task.group_id
                    ),
                    maxlen=group_maxlen,
                )
                self._group_history.append(group_snapshot)
        prune = self.scheduler.prune_terminal(
            max_terminal_tasks=DEFAULT_DELEGATE_SCHEDULER_TERMINAL_LIMIT,
            max_terminal_groups=DEFAULT_DELEGATE_SCHEDULER_TERMINAL_LIMIT,
        )
        with self._lock:
            self._last_scheduler_prune = prune
        self.maintain_persisted_delegates()

    def _cancelled_result(self, task: DelegateTask) -> dict[str, object]:
        error_code = task.cancel_reason or "cancelled"
        message = (
            f"Queued {harness_display_name(task.harness)} delegate was interrupted by MCP server shutdown."
            if error_code == "server_shutdown"
            else f"Queued {harness_display_name(task.harness)} delegate was cancelled."
        )
        result = self._terminal_without_process(
            task,
            status="cancelled",
            error={
                "code": error_code,
                "message": message,
            },
        )
        write_private_json(task.log_paths.metadata, result)
        return result

    def _terminal_without_process(
        self,
        task: DelegateTask,
        *,
        status: str,
        error: dict[str, object],
    ) -> dict[str, object]:
        return {
            "success": False,
            "status": status,
            "completed": True,
            "in_progress": False,
            "executor": task.harness,
            "harness": task.harness,
            "cwd": str(task.cwd),
            "delegate_id": task.delegate_id,
            "group_id": task.group_id,
            "group_max_concurrency": task.group_max_concurrency,
            "task_id": task.task_id,
            "kind": task.kind,
            "lane": task.lane,
            "concurrency_scope": "project",
            "serial": task.serial,
            "sandbox_mode": task.sandbox_mode,
            "commit_mode": task.commit_mode,
            "project": task.project.as_payload(),
            "logs": task.log_paths.as_payload(),
            "log_read_hint": log_read_hint(task),
            "exit_code": TIMEOUT_EXIT_CODE,
            "summary": error["message"],
            "output_omitted": True,
            "timed_out": False,
            "wait_timed_out": False,
            "timeout": task.execution_timeout_seconds,
            "execution_timeout_seconds": task.execution_timeout_seconds,
            "structured_output": None,
            "output_schema": task.output_schema,
            "request_fingerprint": task.request_fingerprint,
            "model": task.model,
            "reasoning_effort": task.reasoning_effort,
            "error": error,
        }

    def _validate_task_arguments(
        self,
        *,
        cwd: Path,
        kind: str,
        commit_mode: str,
        reasoning_effort: str | None,
        timeout: int,
    ) -> dict[str, object] | None:
        if kind not in ALLOWED_TASK_KINDS:
            return self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="unsupported_delegate_kind",
                message=f"Unsupported delegate kind: {kind}",
                details={"allowed_kinds": sorted(ALLOWED_TASK_KINDS)},
            )
        if commit_mode not in ALLOWED_COMMIT_MODES:
            return self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="unsupported_commit_mode",
                message=f"Unsupported commit_mode: {commit_mode}",
                details={"allowed_commit_modes": sorted(ALLOWED_COMMIT_MODES)},
            )
        normalized_reasoning = _normalize_reasoning_effort(reasoning_effort)
        if normalized_reasoning is not None and normalized_reasoning not in ALLOWED_REASONING_EFFORTS:
            return self._argument_error(
                cwd=cwd,
                timeout=timeout,
                code="unsupported_reasoning_effort",
                message=f"Unsupported reasoning_effort: {reasoning_effort}",
                details={
                    "allowed_reasoning_efforts": [
                        DEFAULT_REASONING_EFFORT,
                        *ALLOWED_REASONING_EFFORTS,
                    ]
                },
            )
        return self._cwd_error(cwd)

    def _validate_dependencies(
        self,
        project: ProjectIdentity,
        dependencies: tuple[str, ...],
    ) -> dict[str, object] | None:
        for group_id in dependencies:
            group = self.scheduler.get_group(group_id)
            if group is None:
                return self._argument_error(
                    cwd=project.project_root,
                    timeout=0,
                    code="dependency_group_not_found",
                    message=f"Dependency group not found: {group_id}",
                )
        return None

    def _cwd_error(self, cwd: Path) -> dict[str, object] | None:
        if not cwd.exists():
            return self._argument_error(
                cwd=cwd,
                timeout=0,
                code="cwd_not_found",
                message=f"Working directory not found: {cwd}",
            )
        if not cwd.is_dir():
            return self._argument_error(
                cwd=cwd,
                timeout=0,
                code="cwd_not_directory",
                message=f"Working directory is not a directory: {cwd}",
            )
        return None

    def _argument_error(
        self,
        *,
        cwd: Path,
        timeout: int,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
        harness: str | None = None,
    ) -> dict[str, object]:
        error: dict[str, object] = {"code": code, "message": message}
        if details:
            error.update(details)
        return {
            "success": False,
            "status": "failed",
            "completed": True,
            "in_progress": False,
            "error": error,
            "cwd": str(cwd),
            "executor": harness or self.default_harness,
            "harness": harness or self.default_harness,
            "exit_code": TIMEOUT_EXIT_CODE,
            "summary": message,
            "timed_out": False,
            "wait_timed_out": False,
            "timeout": timeout,
            "serial": True,
        }

    def _not_found(self, kind: str, identifier: str) -> dict[str, object]:
        code = f"{kind}_not_found"
        return {
            "success": False,
            "error": {"code": code, "message": f"{kind.title()} not found: {identifier}"},
        }

    def _batch_lists(self, spec: dict[str, object]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for key in (
            "files_in_scope",
            "out_of_scope",
            "context_files",
            "acceptance_criteria",
            "done_means",
            "verification_commands",
        ):
            value = spec.get(key)
            result[key] = [str(item) for item in value] if isinstance(value, list) else []
        return result

    def _budget_delegate_status(
        self,
        payload: dict[str, object],
        *,
        max_tokens: int,
        offset: int,
    ) -> dict[str, object]:
        budget = ResponseBudget(max_tokens=max_tokens)
        truncated = bool(payload.get("truncated"))
        rendered, measurement = with_budget_metadata(
            payload,
            budget=budget,
            truncated=truncated,
            stop_reason="limit" if truncated else "end_of_results",
        )
        recent = rendered.get("recent")
        while not measurement.fits and isinstance(recent, list) and recent:
            recent.pop()
            rendered["next_offset"] = offset + len(recent)
            rendered, measurement = with_budget_metadata(
                rendered,
                budget=budget,
                truncated=True,
                stop_reason="token_budget",
            )
            recent = rendered.get("recent")
        return rendered

    def _watch_payload(
        self,
        started_at: float,
        watch_seconds: float,
        poll_seconds: float,
        *,
        status_changed: bool = False,
        timed_out: bool = False,
        error_returned: bool = False,
        already_terminal: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "enabled": True,
            "status_changed": status_changed,
            "timed_out": timed_out,
            "watch_seconds": watch_seconds,
            "poll_seconds": poll_seconds,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
        if error_returned:
            payload["error_returned"] = True
        if already_terminal:
            payload["already_terminal"] = True
        return payload
