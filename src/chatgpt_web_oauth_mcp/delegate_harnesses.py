from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Protocol

from .delegate_models import DelegateTask, TaskKind
from .delegate_process import Invocation, ParsedHarnessOutput, extract_structured_output
from .local_openai_delegate import probe_endpoint
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


def _agy_json_events(stdout: str) -> tuple[list[dict[str, object]], bool]:
    """Recover valid NDJSON events without accepting a truncated final event."""

    try:
        return _json_objects(stdout), False
    except json.JSONDecodeError:
        if not stdout or stdout.endswith(("\n", "\r")):
            raise
        prefix, sep, _unfinished = stdout.rpartition("\n")
        if not sep or not prefix.strip():
            raise
        return _json_objects(prefix), True


def _maybe_json(value: object) -> object | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


_CODEX_TOKENS_USED_RE = re.compile(
    r"(?:^|\n)tokens used\s*\n\s*([0-9][0-9,]*)\s*(?:\n|$)",
    re.IGNORECASE,
)


def parse_codex_output(stdout: str, stderr: str) -> ParsedHarnessOutput:
    structured = extract_structured_output(stdout) or extract_structured_output(stderr)
    metadata: dict[str, object] = {}
    match = _CODEX_TOKENS_USED_RE.search(stderr or "")
    if match is not None:
        try:
            total_tokens = int(match.group(1).replace(",", ""))
        except ValueError:
            total_tokens = -1
        if total_tokens >= 0:
            metadata["usage"] = {"total_tokens": total_tokens}
    return ParsedHarnessOutput(
        structured_output=structured,
        metadata=metadata or None,
    )


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
    events, truncated_tail = _agy_json_events(stdout)
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
    if not metadata.get("conversation_id"):
        # A bounded AGY exploration can be terminated before the final
        # result event. Its durable init/step events still identify the
        # conversation and can support an explicit, account-pinned resume.
        for event in reversed(events):
            progress = event.get("step_update")
            candidate = (
                event.get("conversation_id")
                or (progress.get("conversation_id") if isinstance(progress, dict) else None)
            )
            if isinstance(candidate, str) and candidate.strip():
                metadata["conversation_id"] = candidate.strip()
                break
    metadata["event_count"] = len(events)
    if truncated_tail:
        metadata["stream_truncated"] = True
    metadata["progress_event_count"] = sum(
        1 for item in events if item.get("event") == "step_update"
    )
    sandbox_exec_eperm_count = 0
    command_stream_eagain_count = 0
    tool_call_counts: dict[str, int] = {}
    seen_tool_steps: set[tuple[str, int, str]] = set()
    for item in events:
        step_update = item.get("step_update")
        if not isinstance(step_update, dict):
            continue
        tool_name = step_update.get("tool_name")
        step_index = step_update.get("step_index")
        if (
            isinstance(tool_name, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool_name)
        ):
            if tool_name not in tool_call_counts and len(tool_call_counts) >= 32:
                tool_name = "other"
            step_key = (
                str(step_update.get("conversation_id") or item.get("conversation_id") or ""),
                step_index,
                tool_name,
            )
            if isinstance(step_index, int) and not isinstance(step_index, bool):
                if step_key not in seen_tool_steps:
                    seen_tool_steps.add(step_key)
                    tool_call_counts[tool_name] = tool_call_counts.get(tool_name, 0) + 1
            elif step_update.get("state") in {"ACTIVE", "ERROR"}:
                tool_call_counts[tool_name] = tool_call_counts.get(tool_name, 0) + 1
        if step_update.get("state") != "ERROR":
            continue
        if step_update.get("tool_name") != "run_command":
            continue
        tool_info = step_update.get("tool_info")
        if not isinstance(tool_info, dict):
            continue
        tool_error = tool_info.get("error")
        if not isinstance(tool_error, dict):
            continue
        error_message = str(tool_error.get("message") or "")
        if (
            "fork/exec " in error_message
            and "agy" in error_message
            and "operation not permitted" in error_message.lower()
        ):
            sandbox_exec_eperm_count += 1
        if "read s1: resource temporarily unavailable" in error_message.lower():
            command_stream_eagain_count += 1
    if sandbox_exec_eperm_count:
        metadata["sandbox_exec_eperm_count"] = sandbox_exec_eperm_count
    if command_stream_eagain_count:
        metadata["command_stream_eagain_count"] = command_stream_eagain_count
    if tool_call_counts:
        # Only tool names and counts; no arguments, file paths or agent text.
        metadata["tool_call_counts"] = dict(sorted(tool_call_counts.items()))
        metadata["tool_calls"] = sum(tool_call_counts.values())
    denied_actions = payload.get("denied_actions")
    if isinstance(denied_actions, list) and denied_actions:
        metadata["denied_actions"] = denied_actions
    # Agy may report ERROR after a stream interruption even when the model
    # delivered a structured final answer. Keep that answer as *advisory*
    # evidence while preserving the process error and failed task status.
    structured = payload.get("structured_output")
    if structured is None:
        structured = _maybe_json(payload.get("response"))
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
            return ParsedHarnessOutput(
                structured_output=structured, metadata=metadata, error=error,
            )
        return ParsedHarnessOutput(
            structured_output=structured,
            metadata=metadata,
            error={
                "code": "antigravity_result_error",
                "message": error_text or f"Antigravity returned status {status}.",
            },
        )
    if truncated_tail:
        return ParsedHarnessOutput(
            structured_output=structured,
            metadata=metadata,
            error={
                "code": "antigravity_stream_truncated",
                "message": "Antigravity output ended with an incomplete JSON event.",
                "retryable": True,
            },
        )
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
    if (
        structured is None
        and not str(payload.get("response") or "").strip()
        and sandbox_exec_eperm_count
    ):
        return ParsedHarnessOutput(
            metadata=metadata,
            error={
                "code": "antigravity_sandbox_exec_denied",
                "message": "Antigravity sandbox could not execute its command runner.",
                "retryable": False,
            },
        )
    if (
        structured is None
        and not str(payload.get("response") or "").strip()
        and command_stream_eagain_count
    ):
        return ParsedHarnessOutput(
            metadata=metadata,
            error={
                "code": "antigravity_command_stream_unavailable",
                "message": (
                    "Antigravity command output stream became temporarily unavailable."
                ),
                "retryable": True,
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


def _local_openai_output(stdout: str, _stderr: str) -> ParsedHarnessOutput:
    stripped = (stdout or "").strip()
    if not stripped:
        return ParsedHarnessOutput(
            error={
                "code": "local_openai_error",
                "message": "Local OpenAI delegate returned empty stdout.",
            }
        )
    payload = json.loads(stripped)
    if not isinstance(payload, dict):
        raise ValueError("local OpenAI delegate output is not an object")
    error = payload.get("error")
    if isinstance(error, dict):
        return ParsedHarnessOutput(error=dict(error))
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("local OpenAI delegate output has no manifest object")
    metadata = payload.get("metadata")
    return ParsedHarnessOutput(
        structured_output=dict(manifest),
        metadata=dict(metadata) if isinstance(metadata, dict) else None,
    )


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


@lru_cache(maxsize=32)
def _claude_auth_probe(command: str, interval: int) -> tuple[bool, str]:
    """Bounded read-only CLI probe, cached by command and a 60-second epoch."""
    del interval
    parts = resolve_command_parts(command, expected_binary="claude")
    if not parts or binary_name(parts[0]) != "claude":
        return False, "not_a_claude_cli"
    try:
        completed = subprocess.run(
            [*parts, "auth", "status"],
            capture_output=True, text=True, check=False,
            timeout=4, env=sanitized_child_env(), close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, "auth_probe_unavailable"
    try:
        payload = json.loads(completed.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return False, (
            "auth_probe_failed" if completed.returncode != 0
            else "auth_probe_invalid"
        )
    if isinstance(payload, dict) and payload.get("loggedIn") is False:
        return False, "account_authentication_required"
    if completed.returncode != 0:
        return False, "auth_probe_failed"
    if not isinstance(payload, dict) or payload.get("loggedIn") is not True:
        return False, "auth_probe_invalid"
    return True, "authenticated"


@lru_cache(maxsize=64)
def _agy_models_probe(
    command: str, account_home: str | None, interval: int,
) -> frozenset[str] | None:
    """Model list per *account*, never AGY1's ambient credentials on AGY2."""
    del interval
    parts = resolve_command_parts(command, expected_binary="agy")
    if not parts or binary_name(parts[0]) != "agy":
        return None
    overrides = None
    if account_home is not None:
        home = Path(account_home)
        overrides = {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
        }
    try:
        completed = subprocess.run(
            [*parts, "models"],
            capture_output=True, text=True, check=False,
            timeout=7, env=sanitized_child_env(overrides), close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    models = {
        match.group(1).lower()
        for line in completed.stdout.splitlines()
        if (match := re.match(r"^([a-zA-Z0-9][a-zA-Z0-9._-]+)\s+\S", line))
        and match.group(1).lower().startswith(("gemini-", "claude-", "gpt-oss-"))
    }
    return frozenset(models) if models else None


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
                model="gpt-5.6-terra",
                reasoning_effort="low",
                sandbox_mode="read-only",
            )
        return HarnessTaskDefaults(
            model="gpt-6.1-sol",
            reasoning_effort="low",
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
                output_parser=parse_codex_output,
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
        if payload["available"]:
            authenticated, reason = _claude_auth_probe(
                self.command or "", int(time.monotonic() // 60),
            )
            payload["account_authenticated"] = (
                authenticated if reason in {"authenticated", "account_authentication_required"}
                else None
            )
            payload["auth_probe_status"] = reason
            if not authenticated:
                payload["available"] = False
                payload["explore_available"] = False
                payload["availability_reason"] = reason
        return payload


@dataclass(frozen=True)
class AntigravityHarness:
    command: str | None
    skip_permissions: bool = False
    default_model: str = "gemini-3.8-flash-high"
    default_reasoning_effort: str = "high"
    configured_models: tuple[str, ...] = ()
    name: str = "antigravity"
    display_name: str = "Antigravity"
    home_dir: Path | None = None

    @staticmethod
    def reasoning_effort_for_model(
        model: str | None,
        reasoning_effort: str | None,
    ) -> str:
        normalized_model = str(model or "").strip().lower()
        if normalized_model.startswith("claude-"):
            # Claude thinking is encoded in the selected AGY model route;
            # separate --effort is not supported by the CLI for these routes.
            return "none"
        # AGY embeds Gemini Flash effort into suffixed model identifiers.
        # --model ...-medium --effort high is rejected before any useful work.
        for level in ("low", "medium", "high"):
            if normalized_model.startswith("gemini-") and normalized_model.endswith(
                f"-{level}"
            ):
                return level
        return _normalized_effort(
            reasoning_effort,
            allowed=("low", "medium", "high"),
        ) or DEFAULT_VALUE

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
        effort = self.reasoning_effort_for_model(model, task.reasoning_effort)
        if model:
            args.extend(["--model", model])
        if effort not in {DEFAULT_VALUE, "none"}:
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
        filesystem_readonly = False
        worktree = getattr(task, "cwd", None)
        if task.kind == "explore" and os.name == "posix" and isinstance(worktree, Path):
            bwrap = shutil.which("bwrap")
            if bwrap and worktree.is_dir():
                # AGY plan+sandbox does not prevent filesystem writes.
                # Bind the working tree and separate Git directory read-only.
                protected = [worktree.resolve()]
                project = getattr(task, "project", None)
                git_common = getattr(project, "git_common_dir", None)
                if git_common is not None:
                    common_path = Path(git_common).resolve()
                    if not any(common_path == p or p in common_path.parents for p in protected):
                        protected.append(common_path)
                args = [
                    bwrap, "--die-with-parent", "--bind", "/", "/",
                    *(part for path in protected for part in ("--ro-bind", str(path), str(path))),
                    "--", *args,
                ]
                filesystem_readonly = True
        return Invocation(
            args=args,
            use_shell=False,
            stdin=task.prompt.encode("utf-8"),
            output_parser=_antigravity_output,
            # Without bwrap, the runner still fails closed on Git drift.
            read_only_enforced=filesystem_readonly,
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

    def model_admission(self, model: str | None) -> tuple[bool, str]:
        desired = optional_value(model)
        if desired is None:
            return True, "default_model"
        supported = _agy_models_probe(
            self.command or "",
            str(Path(self.home_dir).expanduser()) if self.home_dir is not None else None,
            int(time.monotonic() // 120),
        )
        if supported is None:
            # Failed model lookup is not proof of provider unavailability.
            return True, "model_catalog_unverified"
        if desired.lower() not in supported:
            return False, "model_not_available_in_account"
        return True, "model_confirmed"

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
        payload["configured_models"] = list(
            dict.fromkeys(
                (
                    self.default_model,
                    *(
                        str(item).strip()
                        for item in self.configured_models
                        if str(item).strip()
                    ),
                )
            )
        )
        return payload


@dataclass(frozen=True)
class LocalOpenAIHarness:
    endpoint: str
    default_model: str
    role: str = "scout"
    enabled: bool = True
    health_timeout_seconds: float = 0.35
    request_timeout_seconds: float = 120.0
    max_turns: int = 12
    max_tokens: int = 1200
    enable_thinking: bool = False
    name: str = "local"
    display_name: str = "Scout"
    command: str | None = sys.executable

    def task_defaults(self, kind: TaskKind) -> HarnessTaskDefaults:
        if kind == "explore":
            return HarnessTaskDefaults(
                model=self.default_model,
                reasoning_effort="none",
                sandbox_mode="server-enforced-read-only-tools",
            )
        return HarnessTaskDefaults(
            model=self.default_model,
            reasoning_effort="none",
            sandbox_mode="unavailable",
        )

    def command_for(self, kind: TaskKind) -> str | None:
        if kind != "explore" or not self.enabled:
            return None
        return self.command

    def supports_read_only(self) -> bool:
        return self.enabled

    def build_invocation(self, task: DelegateTask) -> Invocation:
        if task.kind != "explore":
            raise OSError("Local OpenAI harness currently supports explore tasks only.")
        model = optional_value(task.model) or self.default_model
        args = [
            sys.executable,
            "-m",
            "chatgpt_web_oauth_mcp.local_openai_delegate",
            "--endpoint",
            self.endpoint,
            "--model",
            model,
            "--cwd",
            str(task.cwd),
            "--timeout-seconds",
            str(self.request_timeout_seconds),
            "--max-turns",
            str(self.max_turns),
            "--max-tokens",
            str(self.max_tokens),
        ]
        max_tool_calls = getattr(task, "max_tool_calls", None)
        if max_tool_calls is not None:
            args.extend(["--max-tool-calls-total", str(max_tool_calls)])
        if self.enable_thinking:
            args.append("--enable-thinking")
        return Invocation(
            args=args,
            use_shell=False,
            stdin=task.prompt.encode("utf-8"),
            output_parser=_local_openai_output,
            read_only_enforced=True,
        )

    def info(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "display_name": self.display_name,
            "role": self.role,
            "command": self.command,
            "available": False,
            "explore_available": False,
            "code_available": False,
            "read_only_supported": self.supports_read_only(),
            "endpoint": self.endpoint,
            "explore": {
                "model": self.default_model,
                "reasoning_effort": "none",
                "sandbox_mode": "server-enforced-read-only-tools",
            },
            "code": {
                "model": self.default_model,
                "reasoning_effort": "none",
                "sandbox_mode": "unavailable",
            },
        }
        if not self.enabled:
            payload["availability_reason"] = "local_delegate_disabled"
            return payload
        if not command_available(self.command):
            payload["availability_reason"] = "python_runtime_unavailable"
            return payload
        healthy, reason, _health = probe_endpoint(
            self.endpoint,
            model=self.default_model,
            timeout_seconds=self.health_timeout_seconds,
        )
        if not healthy:
            payload["availability_reason"] = "local_endpoint_unavailable"
            payload["availability_detail"] = reason
            return payload
        payload["available"] = True
        payload["explore_available"] = True
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
