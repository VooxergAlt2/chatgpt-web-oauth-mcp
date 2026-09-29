from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Protocol

from .delegate_models import DelegateTask, TaskKind
from .delegate_process import Invocation, ParsedHarnessOutput
from .process_env import sanitized_child_env


DEFAULT_VALUE = "default"
IS_WINDOWS = os.name == "nt"

DEFAULT_AGENT_MANIFEST_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["succeeded", "partial", "blocked"]},
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "commands_run": {"type": "array", "items": {"type": "string"}},
        "verification": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "recommended_next_action": {"type": ["string", "null"]},
    },
    "required": [
        "status",
        "summary",
        "files_changed",
        "commands_run",
        "verification",
        "findings",
        "blockers",
        "recommended_next_action",
    ],
    "additionalProperties": False,
}


def _schema_for(task: DelegateTask) -> dict[str, object]:
    return task.output_schema or DEFAULT_AGENT_MANIFEST_SCHEMA


def _normalized_effort(value: str, *, allowed: tuple[str, ...]) -> str | None:
    normalized = optional_value(value)
    if normalized is None:
        return None
    if normalized in allowed:
        return normalized
    if normalized in {"none", "minimal"} and "low" in allowed:
        return "low"
    if normalized in {"xhigh", "max"} and "high" in allowed:
        return "high"
    return None


def _json_objects(stdout: str) -> list[dict[str, object]]:
    stripped = (stdout or "").strip()
    if not stripped:
        raise ValueError("harness returned empty stdout")
    rows: list[dict[str, object]] = []
    for line in stripped.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        value = json.loads(candidate)
        if not isinstance(value, dict):
            raise ValueError("harness JSON event is not an object")
        rows.append(value)
    if not rows:
        raise ValueError("harness returned no JSON events")
    return rows


def _maybe_json(value: object) -> object | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _claude_output(stdout: str, _stderr: str) -> ParsedHarnessOutput:
    events = _json_objects(stdout)
    payload = next(
        (item for item in reversed(events) if item.get("type") == "result"),
        events[-1],
    )
    metadata = {
        key: payload[key]
        for key in (
            "session_id",
            "duration_ms",
            "duration_api_ms",
            "num_turns",
            "total_cost_usd",
            "usage",
            "modelUsage",
            "terminal_reason",
        )
        if key in payload
    }
    metadata["event_count"] = len(events)
    if bool(payload.get("is_error")) or payload.get("terminal_reason") == "api_error":
        message = str(payload.get("result") or "Claude Code returned an error result.")
        return ParsedHarnessOutput(
            metadata=metadata,
            error={"code": "claude_result_error", "message": message},
        )
    structured = payload.get("structured_output")
    if structured is None:
        structured = _maybe_json(payload.get("result"))
    if structured is None and not str(payload.get("result") or "").strip():
        return ParsedHarnessOutput(
            metadata=metadata,
            error={
                "code": "empty_harness_result",
                "message": "Claude Code exited without a result or structured output.",
            },
        )
    return ParsedHarnessOutput(structured_output=structured, metadata=metadata)


def _antigravity_output(stdout: str, stderr: str) -> ParsedHarnessOutput:
    events = _json_objects(stdout)
    result_event = next(
        (
            item.get("result")
            for item in reversed(events)
            if item.get("event") == "result" and isinstance(item.get("result"), dict)
        ),
        None,
    )
    payload = result_event if isinstance(result_event, dict) else events[-1]
    metadata = {
        key: payload[key]
        for key in ("conversation_id", "duration_seconds", "num_turns", "usage", "status")
        if key in payload
    }
    metadata["event_count"] = len(events)
    metadata["progress_event_count"] = sum(
        1 for item in events if item.get("event") == "step_update"
    )
    denied_actions = payload.get("denied_actions")
    if isinstance(denied_actions, list) and denied_actions:
        metadata["denied_actions"] = denied_actions
    status = str(payload.get("status") or "").upper()
    if status and status != "SUCCESS":
        error_text = str(payload.get("error") or payload.get("response") or "").strip()
        combined_error = "\n".join(
            part for part in (error_text, stderr.strip()) if part
        )
        if (
            "RESOURCE_EXHAUSTED" in combined_error
            or "Individual quota reached" in combined_error
            or '"error_code":429' in combined_error.replace(" ", "")
        ):
            reset_match = re.search(
                r"Resets? in\s+([^\n.]+)",
                combined_error,
                re.IGNORECASE,
            )
            retry_after = reset_match.group(1).strip() if reset_match else None
            metadata["quota_exhausted"] = True
            if retry_after:
                metadata["retry_after"] = retry_after
            error: dict[str, object] = {
                "code": "antigravity_quota_exhausted",
                "message": error_text or "Antigravity subscription quota is exhausted.",
                "retryable": True,
            }
            if retry_after:
                error["retry_after"] = retry_after
            return ParsedHarnessOutput(metadata=metadata, error=error)
        return ParsedHarnessOutput(
            metadata=metadata,
            error={
                "code": "antigravity_result_error",
                "message": error_text or f"Antigravity returned status {status}.",
            },
        )
    structured = payload.get("structured_output")
    if structured is None:
        structured = _maybe_json(payload.get("response"))
    if (
        structured is None
        and not str(payload.get("response") or "").strip()
        and isinstance(denied_actions, list)
        and denied_actions
    ):
        return ParsedHarnessOutput(
            metadata=metadata,
            error={
                "code": "antigravity_permission_denied",
                "message": "Antigravity could not complete because a tool action was denied.",
                "retryable": False,
            },
        )
    if structured is None and not str(payload.get("response") or "").strip():
        return ParsedHarnessOutput(
            metadata=metadata,
            error={
                "code": "empty_harness_result",
                "message": "Antigravity exited successfully but returned no response or structured output.",
            },
        )
    return ParsedHarnessOutput(structured_output=structured, metadata=metadata)


def split_command(command: str) -> list[str]:
    return shlex.split(command)


def binary_name(binary: str) -> str:
    if IS_WINDOWS:
        return PureWindowsPath(binary).stem.lower()
    return Path(binary).stem.lower()


def resolve_command_parts(command: str, *, expected_binary: str | None = None) -> list[str]:
    parts = split_command(command)
    if not IS_WINDOWS or not parts:
        return parts
    if expected_binary is not None and binary_name(parts[0]) != expected_binary:
        return parts
    resolved = shutil.which(parts[0])
    if resolved:
        parts[0] = resolved
    return parts


def command_available(command: str | None) -> bool:
    if not command:
        return False
    parts = split_command(command)
    if not parts:
        return False
    binary = parts[0]
    if Path(binary).exists():
        return True
    return shutil.which(binary) is not None


@lru_cache(maxsize=8)
def codex_read_only_sandbox_available(command: str | None) -> bool:
    """Probe whether the local Codex read-only sandbox can actually start."""

    parts = resolve_command_parts(command or "", expected_binary="codex")
    if not parts or binary_name(parts[0]) != "codex":
        return False
    if not command_available(command):
        return False
    if not sys.platform.startswith("linux"):
        return True
    try:
        completed = subprocess.run(
            [*parts, "sandbox", "/bin/true"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            env=sanitized_child_env(),
            close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def optional_value(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized or normalized.lower() == DEFAULT_VALUE:
        return None
    return normalized


@dataclass(frozen=True)
class HarnessTaskDefaults:
    model: str
    reasoning_effort: str
    sandbox_mode: str


class DelegateHarness(Protocol):
    """Adapter boundary between generic delegate scheduling and a CLI agent."""

    name: str
    display_name: str
    command: str | None

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults: ...

    def command_for(self, kind: TaskKind) -> str | None: ...

    def supports_read_only(self) -> bool: ...

    def build_invocation(self, task: DelegateTask) -> Invocation: ...

    def info(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class CodexHarness:
    command: str | None
    allow_unsafe_explore_command: bool = False
    name: str = "codex"
    display_name: str = "Codex"

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults:
        if kind == "explore":
            return HarnessTaskDefaults(
                model="gpt-5.6-luna",
                reasoning_effort="low",
                sandbox_mode="read-only",
            )
        return HarnessTaskDefaults(
            model="gpt-5.6-sol",
            reasoning_effort="xhigh",
            sandbox_mode="danger-full-access",
        )

    def command_for(self, kind: TaskKind) -> str | None:
        return self.command

    def supports_read_only(self) -> bool:
        if self.allow_unsafe_explore_command:
            return True
        parts = resolve_command_parts(self.command or "", expected_binary="codex")
        return bool(parts and binary_name(parts[0]) == "codex")

    def build_invocation(self, task: DelegateTask) -> Invocation:
        parts = resolve_command_parts(self.command or "", expected_binary="codex")
        if parts and binary_name(parts[0]) == "codex":
            args = [*parts, "exec"]
            model = optional_value(task.model)
            reasoning_effort = optional_value(task.reasoning_effort)
            if model:
                args.extend(["--model", model])
            if reasoning_effort:
                args.extend(
                    ["-c", f"model_reasoning_effort={json.dumps(reasoning_effort)}"]
                )
            if task.kind == "explore":
                args.extend(["--sandbox", "read-only", "--ephemeral"])
            else:
                args.append("--dangerously-bypass-approvals-and-sandbox")
            args.extend(["-C", str(task.cwd)])
            if task.project.git_common_dir is None:
                args.append("--skip-git-repo-check")
            args.append("-")
            return Invocation(
                args=args,
                use_shell=False,
                stdin=task.prompt.encode("utf-8"),
                read_only_enforced=task.kind == "explore",
            )

        # Backward compatibility for deployments that used
        # CHATGPT_MCP_CODEX_COMMAND as a complete shell command.
        return Invocation(args=self.command or "", use_shell=True)

    def info(self) -> dict[str, object]:
        payload = _harness_info(self)
        runtime_available = codex_read_only_sandbox_available(self.command)
        payload["read_only_runtime_available"] = runtime_available
        if (
            bool(payload.get("explore_available"))
            and bool(payload.get("read_only_supported"))
            and not runtime_available
        ):
            payload["explore_available"] = False
            payload["read_only_runtime_reason"] = "codex_sandbox_unavailable"
        return payload


@dataclass(frozen=True)
class PiHarness:
    command: str | None
    name: str = "pi"
    display_name: str = "Pi"

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults:
        if kind == "explore":
            return HarnessTaskDefaults(
                model=DEFAULT_VALUE,
                reasoning_effort=DEFAULT_VALUE,
                sandbox_mode="tool-allowlist-read-only",
            )
        return HarnessTaskDefaults(
            model=DEFAULT_VALUE,
            reasoning_effort=DEFAULT_VALUE,
            sandbox_mode="full-tool-access",
        )

    def command_for(self, kind: TaskKind) -> str | None:
        return self.command

    def supports_read_only(self) -> bool:
        parts = resolve_command_parts(self.command or "", expected_binary="pi")
        return bool(parts and binary_name(parts[0]) == "pi")

    def build_invocation(self, task: DelegateTask) -> Invocation:
        parts = resolve_command_parts(self.command or "", expected_binary="pi")
        args = [
            *parts,
            "--no-session",
            "--mode",
            "text",
        ]
        model = optional_value(task.model)
        reasoning_effort = optional_value(task.reasoning_effort)
        if model:
            args.extend(["--model", model])
        if reasoning_effort:
            args.extend(["--thinking", reasoning_effort])
        if task.kind == "explore":
            args.extend(
                [
                    "--no-approve",
                    "--no-extensions",
                    "--no-skills",
                    "--no-context-files",
                    "--tools",
                    "read,grep,find,ls",
                ]
            )
        else:
            args.append("--approve")
        args.append("--print")
        return Invocation(
            args=args,
            use_shell=False,
            stdin=task.prompt.encode("utf-8"),
            read_only_enforced=task.kind == "explore",
        )

    def info(self) -> dict[str, object]:
        return _harness_info(self)


@dataclass(frozen=True)
class ClaudeHarness:
    command: str | None
    bypass_permissions: bool = False
    name: str = "claude"
    display_name: str = "Claude Code"

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults:
        return HarnessTaskDefaults(
            model=DEFAULT_VALUE,
            reasoning_effort=DEFAULT_VALUE,
            sandbox_mode="permission-mode-plan" if kind == "explore" else (
                "bypassPermissions" if self.bypass_permissions else "acceptEdits"
            ),
        )

    def command_for(self, kind: TaskKind) -> str | None:
        return self.command

    def supports_read_only(self) -> bool:
        parts = resolve_command_parts(self.command or "", expected_binary="claude")
        return bool(parts and binary_name(parts[0]) == "claude")

    def build_invocation(self, task: DelegateTask) -> Invocation:
        parts = resolve_command_parts(self.command or "", expected_binary="claude")
        args = [
            *parts,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-prompts",
            "none",
        ]
        model = optional_value(task.model)
        effort = _normalized_effort(
            task.reasoning_effort,
            allowed=("low", "medium", "high", "xhigh", "max"),
        )
        if model:
            args.extend(["--model", model])
        if effort:
            args.extend(["--effort", effort])
        if task.kind == "explore":
            args.extend(["--permission-mode", "plan"])
        elif self.bypass_permissions:
            args.extend(["--permission-mode", "bypassPermissions"])
        else:
            args.extend(["--permission-mode", "acceptEdits"])
        args.extend(["--json-schema", json.dumps(_schema_for(task), separators=(",", ":"))])
        return Invocation(
            args=args,
            use_shell=False,
            stdin=task.prompt.encode("utf-8"),
            output_parser=_claude_output,
            read_only_enforced=task.kind == "explore",
        )

    def info(self) -> dict[str, object]:
        payload = _harness_info(self)
        payload["code_permission_mode"] = (
            "bypassPermissions" if self.bypass_permissions else "acceptEdits"
        )
        return payload


@dataclass(frozen=True)
class AntigravityHarness:
    command: str | None
    skip_permissions: bool = False
    default_model: str = "gemini-3.8-flash"
    default_reasoning_effort: str = "high"
    name: str = "antigravity"
    display_name: str = "Antigravity"
    home_dir: Path | None = None

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults:
        return HarnessTaskDefaults(
            model=self.default_model,
            reasoning_effort=self.default_reasoning_effort,
            sandbox_mode="plan+sandbox" if kind == "explore" else (
                "dangerously-skip-permissions" if self.skip_permissions else "accept-edits"
            ),
        )

    def command_for(self, kind: TaskKind) -> str | None:
        return self.command

    def supports_read_only(self) -> bool:
        parts = resolve_command_parts(self.command or "", expected_binary="agy")
        return bool(parts and binary_name(parts[0]) == "agy")

    def build_invocation(self, task: DelegateTask) -> Invocation:
        parts = resolve_command_parts(self.command or "", expected_binary="agy")
        args = [*parts, "--output-format", "stream-json"]
        if task.execution_timeout_seconds > 0:
            args.extend(["--print-timeout", f"{task.execution_timeout_seconds}s"])
        model = optional_value(task.model)
        effort = _normalized_effort(task.reasoning_effort, allowed=("low", "medium", "high"))
        if model:
            args.extend(["--model", model])
        if effort:
            args.extend(["--effort", effort])
        resume_conversation_id = getattr(task, "resume_conversation_id", None)
        if resume_conversation_id:
            args.extend(["--conversation", str(resume_conversation_id)])
        if task.kind == "explore":
            args.extend(["--mode", "plan", "--sandbox"])
        else:
            args.extend(["--mode", "accept-edits"])
        if self.skip_permissions:
            args.append("--dangerously-skip-permissions")
        args.extend(["--json-schema", json.dumps(_schema_for(task), separators=(",", ":"))])
        return Invocation(
            args=args,
            use_shell=False,
            stdin=task.prompt.encode("utf-8"),
            output_parser=_antigravity_output,
            # `agy --mode plan --sandbox` limits agent behavior but does not
            # provide a filesystem-level read-only guarantee. Keep explore
            # available, but make the generic runner enforce the contract via
            # repository-drift auditing instead of trusting the harness.
            read_only_enforced=False,
            env_overrides=self._env_overrides(),
        )

    def _env_overrides(self) -> dict[str, str] | None:
        if self.home_dir is None:
            return None
        home = Path(self.home_dir).expanduser()
        return {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
        }

    def _credential_path(self) -> Path | None:
        if self.home_dir is None:
            return None
        return (
            Path(self.home_dir).expanduser()
            / ".gemini"
            / "antigravity-cli"
            / "antigravity-oauth-token"
        )

    def info(self) -> dict[str, object]:
        payload = _harness_info(self)
        credential_path = self._credential_path()
        if credential_path is not None:
            try:
                authenticated = (
                    credential_path.is_file()
                    and credential_path.stat().st_size > 0
                )
            except OSError:
                authenticated = False
            payload["account_authenticated"] = authenticated
            if not authenticated:
                payload["available"] = False
                payload["explore_available"] = False
                payload["availability_reason"] = "account_authentication_required"
        payload["skip_permissions"] = self.skip_permissions
        return payload


@dataclass(frozen=True)
class GenericCliHarness:
    """Programmatic adapter for stdin-driven CLI agents.

    Integrations can register this adapter without changing the scheduler. A separate
    ``explore_command`` is required before read-only tasks are accepted, so a generic
    command never gains read-only status from prompt wording alone.
    """

    name: str
    command: str | None
    explore_command: str | None = None
    display_name: str = "CLI agent"
    model_args: tuple[str, ...] = ()
    reasoning_args: tuple[str, ...] = ()
    explore_defaults: HarnessTaskDefaults = HarnessTaskDefaults(
        model=DEFAULT_VALUE,
        reasoning_effort=DEFAULT_VALUE,
        sandbox_mode="adapter-enforced-read-only",
    )
    code_defaults: HarnessTaskDefaults = HarnessTaskDefaults(
        model=DEFAULT_VALUE,
        reasoning_effort=DEFAULT_VALUE,
        sandbox_mode="adapter-defined",
    )

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults:
        return self.explore_defaults if kind == "explore" else self.code_defaults

    def command_for(self, kind: TaskKind) -> str | None:
        return self.explore_command if kind == "explore" else self.command

    def supports_read_only(self) -> bool:
        return bool(self.explore_command)

    def build_invocation(self, task: DelegateTask) -> Invocation:
        command = self.explore_command if task.kind == "explore" else self.command
        parts = resolve_command_parts(command or "")
        args = [*parts]
        model = optional_value(task.model)
        reasoning_effort = optional_value(task.reasoning_effort)
        if model:
            args.extend(part.replace("{value}", model) for part in self.model_args)
        if reasoning_effort:
            args.extend(
                part.replace("{value}", reasoning_effort) for part in self.reasoning_args
            )
        return Invocation(
            args=args,
            use_shell=False,
            stdin=task.prompt.encode("utf-8"),
        )

    def info(self) -> dict[str, object]:
        return _harness_info(self)


def _harness_info(harness: DelegateHarness) -> dict[str, object]:
    explore = harness.task_defaults("explore")
    code = harness.task_defaults("code")
    return {
        "name": harness.name,
        "display_name": harness.display_name,
        "command": harness.command,
        "available": command_available(harness.command_for("code")),
        "explore_available": command_available(harness.command_for("explore")),
        "read_only_supported": harness.supports_read_only(),
        "explore": {
            "model": explore.model,
            "reasoning_effort": explore.reasoning_effort,
            "sandbox_mode": explore.sandbox_mode,
        },
        "code": {
            "model": code.model,
            "reasoning_effort": code.reasoning_effort,
            "sandbox_mode": code.sandbox_mode,
        },
    }
