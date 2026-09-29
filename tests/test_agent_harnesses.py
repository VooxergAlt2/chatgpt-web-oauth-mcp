from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import chatgpt_web_oauth_mcp.executors as executors
from chatgpt_web_oauth_mcp.delegate_harnesses import (
    AntigravityHarness,
    ClaudeHarness,
    DEFAULT_AGENT_MANIFEST_SCHEMA,
    _antigravity_output,
    _claude_output,
)
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def _task(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "kind": "explore",
        "model": "default",
        "reasoning_effort": "default",
        "prompt": "inspect only",
        "output_schema": None,
        "execution_timeout_seconds": 900,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_claude_explore_invocation_uses_plan_json_and_schema() -> None:
    harness = ClaudeHarness(command="claude")

    invocation = harness.build_invocation(_task())

    assert invocation.args[:4] == ["claude", "-p", "--output-format", "stream-json"]
    assert "--verbose" in invocation.args
    assert invocation.args[invocation.args.index("--permission-mode") + 1] == "plan"
    assert invocation.args[invocation.args.index("--permission-prompts") + 1] == "none"
    schema = json.loads(invocation.args[invocation.args.index("--json-schema") + 1])
    assert schema == DEFAULT_AGENT_MANIFEST_SCHEMA
    assert invocation.stdin == b"inspect only"
    assert invocation.output_parser is not None
    assert invocation.read_only_enforced is True


def test_claude_code_invocation_defaults_to_accept_edits_without_bypass() -> None:
    harness = ClaudeHarness(command="claude", bypass_permissions=False)

    invocation = harness.build_invocation(
        _task(kind="code", model="sonnet", reasoning_effort="xhigh")
    )

    assert invocation.args[invocation.args.index("--permission-mode") + 1] == "acceptEdits"
    assert invocation.args[invocation.args.index("--model") + 1] == "sonnet"
    assert invocation.args[invocation.args.index("--effort") + 1] == "xhigh"
    assert "--dangerously-skip-permissions" not in invocation.args
    assert invocation.read_only_enforced is False


def test_claude_code_invocation_supports_explicit_bypass() -> None:
    harness = ClaudeHarness(command="claude", bypass_permissions=True)

    invocation = harness.build_invocation(_task(kind="code"))

    assert invocation.args[invocation.args.index("--permission-mode") + 1] == "bypassPermissions"


def test_antigravity_explore_invocation_uses_plan_sandbox_without_claiming_hard_readonly() -> None:
    harness = AntigravityHarness(command="agy")

    invocation = harness.build_invocation(_task(reasoning_effort="xhigh"))

    assert invocation.args[0] == "agy"
    assert "--output-format" in invocation.args
    assert invocation.args[invocation.args.index("--output-format") + 1] == "stream-json"
    assert invocation.args[invocation.args.index("--print-timeout") + 1] == "900s"
    assert invocation.args[invocation.args.index("--mode") + 1] == "plan"
    assert "--sandbox" in invocation.args
    assert "-p" not in invocation.args
    assert "--print" not in invocation.args
    assert invocation.args[invocation.args.index("--effort") + 1] == "high"
    assert invocation.stdin == b"inspect only"
    assert invocation.read_only_enforced is False


def test_antigravity_explore_invocation_supports_configured_permission_bypass() -> None:
    harness = AntigravityHarness(command="agy", skip_permissions=True)

    invocation = harness.build_invocation(_task())

    assert invocation.args[invocation.args.index("--mode") + 1] == "plan"
    assert "--sandbox" in invocation.args
    assert "--dangerously-skip-permissions" in invocation.args
    assert invocation.read_only_enforced is False


def test_antigravity_invocation_can_resume_conversation() -> None:
    harness = AntigravityHarness(command="agy")
    conversation_id = "11111111-2222-3333-4444-555555555555"

    invocation = harness.build_invocation(
        _task(resume_conversation_id=conversation_id)
    )

    assert invocation.args[invocation.args.index("--conversation") + 1] == conversation_id


def test_antigravity_defaults_to_flash_high() -> None:
    harness = AntigravityHarness(command="agy")

    explore = harness.task_defaults("explore")
    code = harness.task_defaults("code")

    assert explore.model == "gemini-3.8-flash"
    assert explore.reasoning_effort == "high"
    assert code.model == "gemini-3.8-flash"
    assert code.reasoning_effort == "high"


def test_antigravity_code_invocation_supports_configured_permission_bypass() -> None:
    harness = AntigravityHarness(command="agy", skip_permissions=True)

    invocation = harness.build_invocation(_task(kind="code", reasoning_effort="minimal"))

    assert invocation.args[invocation.args.index("--mode") + 1] == "accept-edits"
    assert "--sandbox" not in invocation.args
    assert "--dangerously-skip-permissions" in invocation.args
    assert invocation.args[invocation.args.index("--effort") + 1] == "low"
    assert invocation.read_only_enforced is False


def test_claude_parser_extracts_stream_result_and_session_metadata() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "session-1"}),
            json.dumps(
                {
                    "type": "result",
                    "session_id": "session-1",
                    "is_error": False,
                    "result": "{\"status\":\"succeeded\"}",
                    "structured_output": {"status": "succeeded", "summary": "ok"},
                    "usage": {"input_tokens": 10},
                    "num_turns": 2,
                }
            ),
        ]
    )
    parsed = _claude_output(stdout, "")

    assert parsed.error is None
    assert parsed.structured_output == {"status": "succeeded", "summary": "ok"}
    assert parsed.metadata["session_id"] == "session-1"
    assert parsed.metadata["num_turns"] == 2
    assert parsed.metadata["event_count"] == 2


def test_claude_parser_turns_api_error_envelope_into_semantic_failure() -> None:
    parsed = _claude_output(
        json.dumps(
            {
                "type": "result",
                "session_id": "session-expired",
                "is_error": True,
                "terminal_reason": "api_error",
                "result": "Failed to authenticate: OAuth session expired",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        ),
        "",
    )

    assert parsed.structured_output is None
    assert parsed.error["code"] == "claude_result_error"
    assert "OAuth session expired" in parsed.error["message"]


def test_antigravity_parser_extracts_stream_result_and_conversation_metadata() -> None:
    stdout = "\n".join(
        [
            json.dumps({"event": "init", "conversation_id": "conv-1"}),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {"state": "ACTIVE", "step_type": "agent_response"},
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "conv-1",
                        "status": "SUCCESS",
                        "response": "{\"status\":\"succeeded\"}",
                        "structured_output": {"status": "succeeded", "summary": "ok"},
                        "usage": {"total_tokens": 100},
                        "num_turns": 1,
                    },
                }
            ),
        ]
    )
    parsed = _antigravity_output(stdout, "")

    assert parsed.error is None
    assert parsed.structured_output == {"status": "succeeded", "summary": "ok"}
    assert parsed.metadata["conversation_id"] == "conv-1"
    assert parsed.metadata["event_count"] == 3
    assert parsed.metadata["progress_event_count"] == 1


def test_antigravity_parser_classifies_empty_success_with_denied_action() -> None:
    denied_actions = [{"action": "command", "display_name": "RunCommand"}]
    stdout = "\n".join(
        [
            json.dumps({"event": "init", "conversation_id": "conv-denied"}),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "conv-denied",
                        "status": "SUCCESS",
                        "response": "",
                        "denied_actions": denied_actions,
                    },
                }
            ),
        ]
    )

    parsed = _antigravity_output(stdout, "")

    assert parsed.structured_output is None
    assert parsed.error == {
        "code": "antigravity_permission_denied",
        "message": "Antigravity could not complete because a tool action was denied.",
        "retryable": False,
    }
    assert parsed.metadata["denied_actions"] == denied_actions


def test_antigravity_parser_records_sandbox_exec_eperm_on_recovered_success() -> None:
    stdout = "\n".join(
        [
            json.dumps({"event": "init", "conversation_id": "conv-sandbox"}),
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "state": "ERROR",
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {
                            "name": "run_command",
                            "error": {
                                "type": "TOOL_ERROR",
                                "message": (
                                    "fork/exec /home/vooxerg/.local/bin/agy: "
                                    "operation not permitted"
                                ),
                            },
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "conv-sandbox",
                        "status": "SUCCESS",
                        "response": "{\"status\":\"succeeded\"}",
                        "structured_output": {"status": "succeeded"},
                    },
                }
            ),
        ]
    )

    parsed = _antigravity_output(stdout, "")

    assert parsed.error is None
    assert parsed.structured_output == {"status": "succeeded"}
    assert parsed.metadata["sandbox_exec_eperm_count"] == 1


def test_antigravity_parser_classifies_empty_success_after_sandbox_exec_eperm() -> None:
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "event": "step_update",
                    "step_update": {
                        "state": "ERROR",
                        "step_type": "tool",
                        "tool_name": "run_command",
                        "tool_info": {
                            "error": {
                                "type": "TOOL_ERROR",
                                "message": (
                                    "fork/exec /home/vooxerg/.local/bin/agy: "
                                    "operation not permitted"
                                ),
                            }
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "conv-sandbox-empty",
                        "status": "SUCCESS",
                        "response": "",
                    },
                }
            ),
        ]
    )

    parsed = _antigravity_output(stdout, "")

    assert parsed.structured_output is None
    assert parsed.error == {
        "code": "antigravity_sandbox_exec_denied",
        "message": "Antigravity sandbox could not execute its command runner.",
        "retryable": False,
    }
    assert parsed.metadata["sandbox_exec_eperm_count"] == 1


def test_antigravity_parser_classifies_subscription_quota_exhaustion() -> None:
    stdout = "\n".join(
        [
            json.dumps({"event": "init", "conversation_id": "conv-quota"}),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "conversation_id": "conv-quota",
                        "status": "ERROR",
                        "response": "",
                        "error": (
                            "Individual quota reached. Please upgrade your subscription "
                            "to increase your limits. Resets in 3h1m0s."
                        ),
                    },
                }
            ),
        ]
    )
    stderr = (
        'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED (code 429): '
        'Individual quota reached.","status":"RESOURCE_EXHAUSTED",'
        '"error_code":429}'
    )

    parsed = _antigravity_output(stdout, stderr)

    assert parsed.structured_output is None
    assert parsed.error is not None
    assert parsed.error["code"] == "antigravity_quota_exhausted"
    assert parsed.error["retryable"] is True
    assert parsed.error["retry_after"] == "3h1m0s"
    assert parsed.metadata["quota_exhausted"] is True
    assert parsed.metadata["retry_after"] == "3h1m0s"


def test_antigravity_empty_success_is_rejected_by_process_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        returncode = 0

        def __init__(self, args, **kwargs) -> None:
            self.args = args

        def communicate(self, timeout=None):
            return (
                json.dumps(
                    {
                        "conversation_id": "conv-empty",
                        "status": "SUCCESS",
                        "response": "",
                        "structured_output": None,
                        "usage": {
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "thinking_tokens": 0,
                            "total_tokens": 0,
                        },
                    }
                ).encode(),
                b"",
            )

    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[AntigravityHarness(command="agy")],
    )
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    monkeypatch.setattr(registry._process_runner, "popen_factory", FakeProcess)

    result = registry.run_delegate(
        harness="antigravity",
        kind="code",
        task="return a manifest",
        cwd=tmp_path,
        wait_seconds=2,
    )

    assert result["status"] == "failed"
    assert result["success"] is False
    assert result["error"]["code"] == "empty_harness_result"
    assert result["harness_metadata"]["conversation_id"] == "conv-empty"


def test_run_delegate_resumes_antigravity_conversation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    seen_args: list[str] = []

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        returncode = 0

        def __init__(self, args, **kwargs) -> None:
            seen_args.extend(args)

        def communicate(self, timeout=None):
            return (
                json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "conversation_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                            "status": "SUCCESS",
                            "response": "{\"status\":\"succeeded\"}",
                            "structured_output": {
                                "status": "succeeded",
                                "summary": "continued",
                            },
                        },
                    }
                ).encode(),
                b"",
            )

    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[AntigravityHarness(command="agy")],
    )
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    monkeypatch.setattr(registry._process_runner, "popen_factory", FakeProcess)
    project = registry.project_resolver.resolve(tmp_path)
    source_conversation = "11111111-2222-3333-4444-555555555555"
    registry._history.append(
        {
            "delegate_id": "abc123abc123",
            "harness": "antigravity",
            "completed": True,
            "project": project.as_payload(),
            "harness_metadata": {"conversation_id": source_conversation},
        }
    )

    result = registry.run_delegate(
        harness="antigravity",
        kind="code",
        task="continue the review",
        cwd=tmp_path,
        resume_from_delegate_id="abc123abc123",
        wait_seconds=2,
    )

    assert result["status"] == "succeeded"
    assert result["resume_from_delegate_id"] == "abc123abc123"
    assert seen_args[seen_args.index("--conversation") + 1] == source_conversation


def test_resume_conversation_resolves_terminal_antigravity_source(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[AntigravityHarness(command="agy")],
    )
    project = registry.project_resolver.resolve(tmp_path)
    conversation_id = "11111111-2222-3333-4444-555555555555"
    registry._history.append(
        {
            "delegate_id": "abc123abc123",
            "harness": "antigravity",
            "completed": True,
            "project": project.as_payload(),
            "harness_metadata": {"conversation_id": conversation_id},
        }
    )

    resolved, error = registry._resolve_resume_conversation(
        resume_from_delegate_id="abc123abc123",
        harness="antigravity",
        project=project,
        cwd=tmp_path,
        timeout=30,
    )

    assert error is None
    assert resolved == conversation_id


def test_resume_conversation_rejects_nonterminal_source(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[AntigravityHarness(command="agy")],
    )
    project = registry.project_resolver.resolve(tmp_path)
    registry._history.append(
        {
            "delegate_id": "abc123abc123",
            "harness": "antigravity",
            "completed": False,
            "project": project.as_payload(),
            "harness_metadata": {
                "conversation_id": "11111111-2222-3333-4444-555555555555"
            },
        }
    )

    resolved, error = registry._resolve_resume_conversation(
        resume_from_delegate_id="abc123abc123",
        harness="antigravity",
        project=project,
        cwd=tmp_path,
        timeout=30,
    )

    assert resolved is None
    assert error is not None
    assert error["error"]["code"] == "delegate_resume_not_terminal"
