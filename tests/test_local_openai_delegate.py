from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

import chatgpt_web_oauth_mcp.delegate_harnesses as delegate_harnesses
import chatgpt_web_oauth_mcp.executors as executors
import chatgpt_web_oauth_mcp.local_openai_delegate as local_agent
from chatgpt_web_oauth_mcp.delegate_harnesses import (
    GenericCliHarness,
    LocalOpenAIHarness,
    _local_openai_output,
)
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.shell import JobRegistry


def _manifest(summary: str = "ok") -> dict[str, object]:
    return {
        "status": "succeeded",
        "summary": summary,
        "files_changed": [],
        "commands_run": ["read_text"],
        "verification": "read-only evidence collected",
        "findings": ["evidence"],
        "blockers": [],
        "recommended_next_action": None,
    }


def test_json_request_maps_transient_http_errors_to_unavailable(monkeypatch) -> None:
    error = urllib.error.HTTPError(
        "http://local.invalid/v1/chat/completions",
        503,
        "Service Unavailable",
        hdrs=None,
        fp=io.BytesIO(b'{"error":{"message":"Loading model"}}'),
    )

    def raise_error(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(local_agent.urllib.request, "urlopen", raise_error)

    with pytest.raises(ConnectionError, match="temporarily unavailable"):
        local_agent._json_request(
            "http://local.invalid/v1/chat/completions",
            {"model": "qwen-coder-next"},
            timeout_seconds=1,
        )


def test_coder_next_slot_leases_use_lowest_free_slot(monkeypatch, tmp_path: Path) -> None:
    if local_agent.fcntl is None:
        pytest.skip("POSIX flock is unavailable")
    monkeypatch.setattr(local_agent, "_discover_parallel_slots", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(local_agent, "_discover_context_size", lambda *_args, **_kwargs: 8192)
    monkeypatch.setattr(local_agent.tempfile, "gettempdir", lambda: str(tmp_path))

    with local_agent.acquire_local_slot(
        endpoint="http://local.invalid:8081",
        model="qwen-coder-next",
        timeout_seconds=1,
    ) as first:
        assert first == local_agent.LocalSlotLease(slot_id=0, slot_count=2, context_size=8192)
        with local_agent.acquire_local_slot(
            endpoint="http://local.invalid:8081",
            model="qwen-coder-next",
            timeout_seconds=1,
        ) as second:
            assert second == local_agent.LocalSlotLease(slot_id=1, slot_count=2, context_size=8192)

    with local_agent.acquire_local_slot(
        endpoint="http://local.invalid:8081",
        model="qwen-coder-next",
        timeout_seconds=1,
    ) as reused:
        assert reused == local_agent.LocalSlotLease(slot_id=0, slot_count=2, context_size=8192)


def test_non_coder_next_slot_lease_skips_slot_probe_but_discovers_context(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        local_agent,
        "_discover_parallel_slots",
        lambda *_args, **_kwargs: pytest.fail("unexpected slot probe"),
    )
    monkeypatch.setattr(
        local_agent,
        "_discover_context_size",
        lambda *_args, **_kwargs: 32768,
    )

    with local_agent.acquire_local_slot(
        endpoint="http://local.invalid:8081",
        model="qwen-local",
        timeout_seconds=1,
    ) as lease:
        assert lease == local_agent.LocalSlotLease(
            slot_id=None,
            slot_count=None,
            context_size=32768,
        )


def test_qwen38_slot_lease_uses_parallel_slots(monkeypatch, tmp_path: Path) -> None:
    if local_agent.fcntl is None:
        pytest.skip("POSIX flock is unavailable")
    monkeypatch.setattr(local_agent, "_discover_parallel_slots", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(local_agent, "_discover_context_size", lambda *_args, **_kwargs: 49152)
    monkeypatch.setattr(local_agent.tempfile, "gettempdir", lambda: str(tmp_path))

    with local_agent.acquire_local_slot(
        endpoint="http://local.invalid:8084",
        model="qwen38-27b",
        timeout_seconds=1,
    ) as lease:
        assert lease == local_agent.LocalSlotLease(
            slot_id=0,
            slot_count=2,
            context_size=49152,
        )


def test_context_budget_compacts_old_tool_output(monkeypatch) -> None:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read_text", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "read_text",
            "content": "x" * 6000,
        },
    ]

    def fake_count(**kwargs) -> int:
        tool_content = next(
            str(item["content"])
            for item in kwargs["messages"]
            if item.get("role") == "tool"
        )
        return 7000 if len(tool_content) > 1000 else 6000

    monkeypatch.setattr(local_agent, "_chat_prompt_token_count", fake_count)

    fitted, prompt_tokens, compactions = local_agent._fit_messages_to_context(
        endpoint="http://local.invalid:8081",
        messages=messages,
        tools=local_agent.TOOLS,
        enable_thinking=False,
        context_size=8192,
        max_tokens=1400,
        timeout_seconds=1,
    )

    fitted_tool = next(item for item in fitted if item.get("role") == "tool")
    assert prompt_tokens == 6000
    assert compactions > 0
    assert len(str(fitted_tool["content"])) <= 1000
    assert len(str(messages[-1]["content"])) == 6000


def test_local_output_parser_extracts_manifest_and_usage() -> None:
    parsed = _local_openai_output(
        json.dumps(
            {
                "manifest": _manifest(),
                "metadata": {"usage": {"total_tokens": 123}, "turns": 2},
            }
        ),
        "",
    )

    assert parsed.error is None
    assert parsed.structured_output == _manifest()
    assert parsed.metadata == {"usage": {"total_tokens": 123}, "turns": 2}


def test_local_output_parser_preserves_unavailable_error() -> None:
    parsed = _local_openai_output(
        json.dumps(
            {
                "error": {
                    "code": "local_openai_unavailable",
                    "message": "connection refused",
                }
            }
        ),
        "",
    )

    assert parsed.structured_output is None
    assert parsed.error == {
        "code": "local_openai_unavailable",
        "message": "connection refused",
    }


def test_local_harness_is_explore_only_and_health_gated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(
        delegate_harnesses,
        "probe_endpoint",
        lambda *_args, **_kwargs: (True, None, {"status": "ok"}),
    )
    harness = LocalOpenAIHarness(
        endpoint="http://192.0.2.10:8081",
        default_model="qwen-local",
    )

    info = harness.info()

    assert info["available"] is True
    assert info["explore_available"] is True
    assert info["code_available"] is False
    assert harness.command_for("code") is None
    task = type(
        "Task",
        (),
        {
            "kind": "explore",
            "model": "qwen-local",
            "cwd": tmp_path,
            "prompt": "inspect",
            "max_tool_calls": 8,
        },
    )()
    invocation = harness.build_invocation(task)
    assert invocation.read_only_enforced is True
    assert invocation.args[1:3] == ["-m", "chatgpt_web_oauth_mcp.local_openai_delegate"]
    assert "--endpoint" in invocation.args
    budget_index = invocation.args.index("--max-tool-calls-total")
    assert invocation.args[budget_index + 1] == "8"
    assert invocation.stdin == b"inspect"


def test_local_harness_reports_offline_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(
        delegate_harnesses,
        "probe_endpoint",
        lambda *_args, **_kwargs: (False, "connection refused", None),
    )
    harness = LocalOpenAIHarness(
        endpoint="http://192.0.2.10:8081",
        default_model="qwen-local",
    )

    info = harness.info()

    assert info["available"] is False
    assert info["explore_available"] is False
    assert info["availability_reason"] == "local_endpoint_unavailable"
    assert info["availability_detail"] == "connection refused"


def test_local_agent_tool_loop_uses_readonly_tool_and_returns_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    calls: list[dict[str, object]] = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_text",
                                        "arguments": json.dumps({"path": "target.py"}),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_manifest("found VALUE")),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 55, "completion_tokens": 15, "total_tokens": 70},
            },
        ]
    )

    def fake_request(_url: str, payload: dict[str, object] | None, *, timeout_seconds: float):
        assert payload is not None
        calls.append(payload)
        return next(responses)

    monkeypatch.setattr(local_agent, "_json_request", fake_request)

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8081",
        model="qwen-local",
        cwd=tmp_path,
        prompt="inspect target.py",
        max_turns=3,
        slot_id=2,
        slot_count=4,
    )

    assert result["manifest"] == _manifest("found VALUE")
    metadata = result["metadata"]
    assert metadata["usage"] == {
        "input_tokens": 95,
        "output_tokens": 25,
        "total_tokens": 120,
    }
    assert metadata["tool_calls"] == 1
    assert metadata["slot_id"] == 2
    assert metadata["slot_count"] == 4
    assert len(calls) == 2
    assert all(call["id_slot"] == 2 for call in calls)
    second_messages = calls[1]["messages"]
    tool_message = next(item for item in second_messages if item.get("role") == "tool")
    assert "VALUE = 42" in tool_message["content"]


def test_local_agent_requires_tool_evidence_before_accepting_manifest(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_manifest("guessed")),
                        }
                    }
                ],
                "usage": {"total_tokens": 20},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_text",
                                        "arguments": json.dumps({"path": "target.py"}),
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"total_tokens": 30},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_manifest("verified")),
                        }
                    }
                ],
                "usage": {"total_tokens": 40},
            },
        ]
    )

    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: next(responses),
    )

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8081",
        model="qwen-local",
        cwd=tmp_path,
        prompt="inspect target.py",
        max_turns=4,
    )

    assert result["manifest"]["summary"] == "verified"
    assert result["manifest"]["commands_run"] == ["read_text"]
    assert result["metadata"]["tool_calls"] == 1
    assert result["metadata"]["turns"] == 3


def test_local_manifest_rejects_non_string_findings_items() -> None:
    invalid = _manifest()
    invalid["findings"] = [{"message": "not-a-string"}]

    with pytest.raises(ValueError, match="findings"):
        local_agent._normalize_manifest(invalid)


def test_local_agent_rejects_path_escape(tmp_path: Path) -> None:
    result = local_agent._execute_tool(
        tmp_path,
        "read_text",
        {"path": "../outside.txt"},
    )

    assert result["ok"] is False
    assert "escapes repository root" in result["error"]


def test_local_git_tools_support_committed_ranges(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    target = tmp_path / "target.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "target.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    target.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "target.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "head"], check=True)
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    diff = local_agent._execute_tool(
        tmp_path, "git_diff", {"base": base, "head": head, "path": "target.py"}
    )
    assert diff["ok"] is True
    assert "-VALUE = 1" in diff["stdout"]
    assert "+VALUE = 2" in diff["stdout"]

    log = local_agent._execute_tool(tmp_path, "git_log", {"ref": head, "max_count": 2})
    assert log["ok"] is True
    assert "head" in log["stdout"]
    assert "base" in log["stdout"]

    shown = local_agent._execute_tool(tmp_path, "git_show", {"ref": head, "path": "target.py"})
    assert shown["ok"] is True
    assert "+VALUE = 2" in shown["stdout"]

    unsafe = local_agent._execute_tool(tmp_path, "git_show", {"ref": "-p"})
    assert unsafe["ok"] is False
    assert "invalid ref" in unsafe["error"]


def test_local_git_tools_disable_repository_textconv(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    marker = tmp_path / "textconv-ran"
    helper = tmp_path / "textconv.py"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n"
        "print(Path(sys.argv[1]).read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    (tmp_path / ".gitattributes").write_text("target.txt diff=evil\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "diff.evil.textconv", str(helper)],
        check=True,
    )
    target = tmp_path / "target.txt"
    target.write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitattributes", "target.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    target.write_text("two\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "target.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "head"], check=True)
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    subprocess.run(
        ["git", "-C", str(tmp_path), "diff", base, head, "--", "target.txt"],
        check=True,
        capture_output=True,
    )
    assert marker.exists()
    marker.unlink()

    diff = local_agent._execute_tool(
        tmp_path,
        "git_diff",
        {"base": base, "head": head, "path": "target.txt"},
    )
    assert diff["ok"] is True
    assert not marker.exists()

    shown = local_agent._execute_tool(
        tmp_path,
        "git_show",
        {"ref": head, "path": "target.txt"},
    )
    assert shown["ok"] is True
    assert not marker.exists()


def test_local_search_can_return_bounded_context(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_text("before\nneedle\nafter\n", encoding="utf-8")

    result = local_agent._tool_search(
        tmp_path,
        {"query": "needle", "path": "target.py", "before": 1, "after": 1},
    )

    assert result["ok"] is True
    joined = "\n".join(result["matches"])
    assert "before" in joined
    assert "needle" in joined
    assert "after" in joined


def test_automatic_routing_falls_back_when_local_is_offline(monkeypatch) -> None:
    local = LocalOpenAIHarness(
        endpoint="http://192.0.2.10:8081",
        default_model="qwen-local",
    )
    cloud = GenericCliHarness(
        name="antigravity",
        command="cloud-agent",
        explore_command="cloud-agent-ro",
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[local, cloud],
        automatic_routing=True,
        primary_harness="local",
        fallback_harnesses=("antigravity",),
    )
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda command: bool(command))
    monkeypatch.setattr(
        delegate_harnesses,
        "probe_endpoint",
        lambda *_args, **_kwargs: (False, "offline", None),
    )

    name, _adapter, _quota, route = registry._select_harness(
        harness=None,
        kind="explore",
        model=None,
        fresh=False,
        record_selection=False,
    )

    assert name == "antigravity"
    assert route["reason"] == "primary_unavailable_balanced_fallback"
    assert route["candidates"][0]["reason"] == "local_endpoint_unavailable"


def test_automatic_code_routing_never_selects_local_scout(monkeypatch) -> None:
    local = LocalOpenAIHarness(
        endpoint="http://192.0.2.10:8081",
        default_model="qwen-local",
    )
    cloud = GenericCliHarness(
        name="antigravity",
        command="cloud-agent",
        explore_command="cloud-agent-ro",
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[local, cloud],
        automatic_routing=True,
        primary_harness="local",
        fallback_harnesses=("antigravity",),
    )
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda command: bool(command))
    monkeypatch.setattr(
        delegate_harnesses,
        "probe_endpoint",
        lambda *_args, **_kwargs: (True, None, {"status": "ok"}),
    )

    name, _adapter, _quota, route = registry._select_harness(
        harness=None,
        kind="code",
        model=None,
        fresh=False,
        record_selection=False,
    )

    assert name == "antigravity"
    assert route["reason"] == "primary_unavailable_ordered_fallback"
    assert route["candidates"][0]["reason"] == "command_unavailable"


def test_local_unavailable_terminal_uses_short_cooldown(monkeypatch) -> None:
    registry = ExecutorRegistry(
        codex_command=None,
        automatic_routing=True,
        primary_harness="local",
        routing_unavailable_cooldown_seconds=900,
        routing_unavailable_cooldowns={"local": 60},
    )
    monkeypatch.setattr("chatgpt_web_oauth_mcp.executors.time.time", lambda: 1000.0)

    registry._note_routing_terminal(
        {
            "harness": "local",
            "error": {
                "code": "local_openai_unavailable",
                "message": "connection refused",
            },
        }
    )

    block = registry._routing_unavailable_until["local"]
    assert block["until_epoch"] == 1060.0
    assert block["reason"] == "runtime_provider_unavailable"


def test_explicit_local_code_task_is_rejected_before_queue(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-local",
            )
        ],
    )

    result = registry.run_delegate(
        harness="local",
        kind="code",
        task="change a file",
        cwd=tmp_path,
        wait_seconds=0,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "delegate_harness_unavailable"


def test_failed_tool_call_does_not_count_as_repository_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_text",
                                        "arguments": json.dumps({"path": "../outside.txt"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_manifest("guessed")),
                        }
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(RuntimeError, match="without successful repository evidence"):
        local_agent.run_agent(
            endpoint="http://local.invalid:8081",
            model="qwen-local",
            cwd=tmp_path,
            prompt="inspect",
            max_turns=2,
        )


def test_explore_complexity_medium_falls_back_to_local_when_local2_is_absent(monkeypatch) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    monkeypatch.setattr(
        delegate_harnesses,
        "probe_endpoint",
        lambda *_args, **_kwargs: (True, None, {"status": "ok"}),
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            ),
            GenericCliHarness(
                name="antigravity2",
                command="pro-agent",
                explore_command="pro-agent-ro",
            ),
            GenericCliHarness(
                name="codex",
                command="sol-agent",
                explore_command="sol-agent-ro",
            ),
        ],
        automatic_routing=True,
        primary_harness="local",
        fallback_harnesses=("antigravity2", "codex"),
    )

    medium, _adapter, _quota, medium_route = registry._select_harness(
        harness=None,
        kind="explore",
        model=None,
        complexity="medium",
        fresh=False,
        record_selection=False,
    )
    high, _adapter, _quota, high_route = registry._select_harness(
        harness=None,
        kind="explore",
        model=None,
        complexity="high",
        fresh=False,
        record_selection=False,
    )

    assert medium == "local"
    assert medium_route["reason"] == "complexity_medium_selected"
    assert [row["harness"] for row in medium_route["candidates"]] == [
        "local2",
        "local",
        "antigravity2",
        "codex",
    ]
    assert medium_route["candidates"][0]["reason"] == "unsupported_harness"
    assert high == "antigravity2"
    assert high_route["reason"] == "complexity_high_selected"
    assert all(
        row["harness"] not in {"local", "local2"}
        for row in high_route["candidates"]
    )


def test_explore_complexity_medium_prefers_local2_when_available(monkeypatch) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    monkeypatch.setattr(
        delegate_harnesses,
        "probe_endpoint",
        lambda *_args, **_kwargs: (True, None, {"status": "ok"}),
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            ),
            LocalOpenAIHarness(
                name="local2",
                display_name="Local Devstral reviewer",
                endpoint="http://127.0.0.1:8083",
                default_model="devstral-gip",
            ),
            GenericCliHarness(
                name="antigravity2",
                command="pro-agent",
                explore_command="pro-agent-ro",
            ),
        ],
        automatic_routing=True,
        primary_harness="local",
        fallback_harnesses=("antigravity2",),
    )

    selected, adapter, _quota, route = registry._select_harness(
        harness=None,
        kind="explore",
        model=None,
        complexity="medium",
        fresh=False,
        record_selection=False,
    )

    assert selected == "local2"
    assert adapter is not None
    assert adapter.task_defaults("explore").model == "devstral-gip"
    assert route["reason"] == "complexity_medium_selected"
    assert route["candidates"][0]["harness"] == "local2"
    assert route["candidates"][0]["eligible"] is True


def test_local_automatic_runtime_failure_retries_same_task_on_cloud(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    cloud = GenericCliHarness(
        name="antigravity",
        command="cloud-agent",
        explore_command="cloud-agent-ro",
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            ),
            cloud,
        ],
        automatic_routing=True,
        primary_harness="local",
        fallback_harnesses=("antigravity",),
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="inspect",
        goal=None,
        task_id=None,
        group_id=None,
        model="qwen-coder-next",
        reasoning_effort="none",
        sandbox_mode="server-enforced-read-only-tools",
        commit_mode="forbidden",
        execution_timeout_seconds=120,
        depends_on_group_ids=(),
        files_in_scope=[],
        out_of_scope=[],
        context_files=[],
        acceptance_criteria=[],
        done_means=[],
        verification_commands=[],
        output_schema=None,
        parse_structured_output=True,
        request_fingerprint="runtime-fallback",
        complexity="low",
        routing_mode="automatic",
        routing_reason="primary_available",
    )
    calls: list[str] = []

    def fake_start(*, delegate_task):
        calls.append(delegate_task.harness)
        if len(calls) == 1:
            return {
                "success": False,
                "status": "failed",
                "harness": "local",
                "model": "qwen-coder-next",
                "error": {
                    "code": "local_openai_unavailable",
                    "message": "timed out",
                },
            }
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)

    result = registry._run_scheduled_task(task)

    assert calls == ["local", "antigravity"]
    assert result["success"] is True
    assert result["harness"] == "antigravity"
    assert result["routing_fallback"]["from_harness"] == "local"
    assert result["routing_fallback"]["to_harness"] == "antigravity"
    assert task.routing_mode == "automatic_fallback"
    assert task.routing_reason == "local_runtime_failure_to_antigravity"


def test_local2_medium_runtime_failure_retries_same_task_on_cloud(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    cloud = GenericCliHarness(
        name="antigravity2",
        command="pro-agent",
        explore_command="pro-agent-ro",
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[
            LocalOpenAIHarness(
                name="local2",
                display_name="Local Devstral reviewer",
                endpoint="http://127.0.0.1:8083",
                default_model="devstral-gip",
            ),
            cloud,
        ],
        automatic_routing=True,
        primary_harness="local",
        fallback_harnesses=("antigravity2",),
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local2",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="inspect medium scope",
        goal=None,
        task_id=None,
        group_id=None,
        model="devstral-gip",
        reasoning_effort="none",
        sandbox_mode="server-enforced-read-only-tools",
        commit_mode="forbidden",
        execution_timeout_seconds=300,
        depends_on_group_ids=(),
        files_in_scope=[],
        out_of_scope=[],
        context_files=[],
        acceptance_criteria=[],
        done_means=[],
        verification_commands=[],
        output_schema=None,
        parse_structured_output=True,
        request_fingerprint="local2-runtime-fallback",
        complexity="medium",
        routing_mode="complexity",
        routing_reason="complexity_medium_selected",
    )
    calls: list[str] = []

    def fake_start(*, delegate_task):
        calls.append(delegate_task.harness)
        if len(calls) == 1:
            return {
                "success": False,
                "status": "failed",
                "harness": "local2",
                "model": "devstral-gip",
                "error": {
                    "code": "local_openai_unavailable",
                    "message": "endpoint offline",
                },
            }
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)

    result = registry._run_scheduled_task(task)

    assert calls == ["local2", "antigravity2"]
    assert result["success"] is True
    assert result["routing_fallback"]["from_harness"] == "local2"
    assert result["routing_fallback"]["to_harness"] == "antigravity2"
    assert task.routing_mode == "automatic_fallback"
    assert task.routing_reason == "local_runtime_failure_to_antigravity2"


def test_local_runtime_fallback_moves_forward_without_retrying_same_tier(monkeypatch) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    monkeypatch.setattr(
        delegate_harnesses,
        "probe_endpoint",
        lambda *_args, **_kwargs: (True, None, {"status": "ok"}),
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            ),
            LocalOpenAIHarness(
                name="local2",
                display_name="Local Devstral reviewer",
                endpoint="http://127.0.0.1:8083",
                default_model="devstral-gip",
            ),
            GenericCliHarness(
                name="antigravity2",
                command="pro-agent",
                explore_command="pro-agent-ro",
            ),
        ],
        automatic_routing=True,
        primary_harness="local",
        fallback_harnesses=("local2", "antigravity2"),
    )

    medium = type("Task", (), {"complexity": "medium", "harness": "local2", "kind": "explore"})()
    medium_name, _medium_adapter = registry._select_local_runtime_fallback(medium)
    assert medium_name == "local"

    low = type("Task", (), {"complexity": "low", "harness": "local2", "kind": "explore"})()
    low_name, _low_adapter = registry._select_local_runtime_fallback(low)
    assert low_name == "antigravity2"


def test_local_runtime_fallback_to_codex_uses_sol_model(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    monkeypatch.setattr(
        delegate_harnesses,
        "codex_read_only_sandbox_available",
        lambda _command: True,
    )
    registry = ExecutorRegistry(
        codex_command="python3 -c \"print('done')\"",
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            ),
        ],
        allow_unsafe_explore_command=True,
        automatic_routing=True,
        primary_harness="local",
        fallback_harnesses=("codex",),
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="inspect",
        goal=None,
        task_id=None,
        group_id=None,
        model="qwen-coder-next",
        reasoning_effort="none",
        sandbox_mode="server-enforced-read-only-tools",
        commit_mode="forbidden",
        execution_timeout_seconds=120,
        depends_on_group_ids=(),
        files_in_scope=[],
        out_of_scope=[],
        context_files=[],
        acceptance_criteria=[],
        done_means=[],
        verification_commands=[],
        output_schema=None,
        parse_structured_output=True,
        request_fingerprint="runtime-fallback-codex",
        complexity="low",
        routing_mode="automatic",
        routing_reason="primary_available",
    )
    observed: list[tuple[str, str]] = []

    def fake_start(*, delegate_task):
        observed.append((delegate_task.harness, delegate_task.model))
        if len(observed) == 1:
            return {
                "success": False,
                "status": "failed",
                "harness": "local",
                "model": "qwen-coder-next",
                "error": {
                    "code": "local_openai_unavailable",
                    "message": "offline",
                },
            }
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)

    result = registry._run_scheduled_task(task)

    assert observed == [
        ("local", "qwen-coder-next"),
        ("codex", "gpt-6.1-sol"),
    ]
    assert result["routing_fallback"]["to_harness"] == "codex"
    assert result["routing_fallback"]["to_model"] == "gpt-6.1-sol"


def test_explicit_local_runtime_failure_is_not_silently_rerouted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            )
        ],
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="diagnose local",
        goal=None,
        task_id=None,
        group_id=None,
        model="qwen-coder-next",
        reasoning_effort="none",
        sandbox_mode="server-enforced-read-only-tools",
        commit_mode="forbidden",
        execution_timeout_seconds=120,
        depends_on_group_ids=(),
        files_in_scope=[],
        out_of_scope=[],
        context_files=[],
        acceptance_criteria=[],
        done_means=[],
        verification_commands=[],
        output_schema=None,
        parse_structured_output=True,
        request_fingerprint="explicit-local",
        complexity="low",
        routing_mode="explicit",
        routing_reason="explicit_harness",
    )
    calls = 0

    def fake_start(*, delegate_task):
        nonlocal calls
        calls += 1
        return {
            "success": False,
            "status": "failed",
            "harness": delegate_task.harness,
            "error": {
                "code": "local_openai_unavailable",
                "message": "offline",
            },
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)

    result = registry._run_scheduled_task(task)

    assert calls == 1
    assert result["success"] is False
    assert "routing_fallback" not in result


def test_local_harness_is_durable_by_default_when_job_registry_is_available(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(
        delegate_harnesses,
        "probe_endpoint",
        lambda *_args, **_kwargs: (True, None, {"status": "ok"}),
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            )
        ],
        durable_job_registry=JobRegistry(),
        durable_state_dir=tmp_path / "state",
        delegate_state_root=tmp_path / "delegate-state",
    )

    assert "local" in registry.durable_harnesses
    info = registry.harness_info()["local"]
    assert info["durable_execution"] is True
    assert info["durability_backend"] == "job_registry"


def test_probe_endpoint_fails_closed_on_empty_model_inventory(monkeypatch) -> None:
    responses = iter([
        {"status": "ok"},
        {"data": [], "models": []},
    ])
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: next(responses),
    )

    healthy, reason, _payload = local_agent.probe_endpoint(
        "http://local.invalid:8081",
        model="qwen-local",
    )

    assert healthy is False
    assert reason == "Model inventory returned no recognized model IDs."


def test_local_agent_finalizes_when_per_turn_tool_budget_is_exceeded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    tool_calls = [
        {
            "id": f"call-{index}",
            "type": "function",
            "function": {
                "name": "search",
                "arguments": json.dumps({"query": f"needle-{index}"}),
            },
        }
        for index in range(local_agent.DEFAULT_MAX_TOOL_CALLS_PER_TURN + 1)
    ]
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": tool_calls}}]
        },
    )

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8081",
        model="qwen-local",
        cwd=tmp_path,
        prompt="inspect",
        max_turns=1,
    )

    assert result["manifest"]["status"] == "partial"
    assert result["metadata"]["tool_calls"] == local_agent.DEFAULT_MAX_TOOL_CALLS_PER_TURN
    assert result["metadata"]["forced_finalization_reason"] == "per_turn_tool_call_budget_exhausted"


def test_unknown_tool_does_not_count_as_repository_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "unknown_tool",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_manifest("guessed")),
                        }
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(RuntimeError, match="without successful repository evidence"):
        local_agent.run_agent(
            endpoint="http://local.invalid:8081",
            model="qwen-local",
            cwd=tmp_path,
            prompt="inspect",
            max_turns=2,
        )


def test_local_agent_finalizes_when_total_tool_budget_is_exceeded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    one_call = [
        {
            "id": "call",
            "type": "function",
            "function": {
                "name": "search",
                "arguments": json.dumps({"query": "needle"}),
            },
        }
    ]
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "choices": [
                {"message": {"role": "assistant", "content": "", "tool_calls": one_call}}
            ]
        },
    )

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8081",
        model="qwen-local",
        cwd=tmp_path,
        prompt="inspect",
        max_turns=4,
        max_tool_calls_per_turn=2,
        max_tool_calls_total=2,
    )

    assert result["manifest"]["status"] == "partial"
    assert result["metadata"]["tool_calls"] == 2
    assert result["metadata"]["forced_finalization_reason"] == "tool_call_budget_exhausted"


def test_qwen_runtime_profile_respects_explicit_total_tool_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    one_call = [
        {
            "id": "call",
            "type": "function",
            "function": {
                "name": "search",
                "arguments": json.dumps({"query": "needle"}),
            },
        }
    ]
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "choices": [
                {"message": {"role": "assistant", "content": "", "tool_calls": one_call}}
            ]
        },
    )

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8081",
        model="qwen-coder-next",
        cwd=tmp_path,
        prompt="inspect",
        max_turns=6,
        max_tool_calls_total_override=2,
    )

    assert result["manifest"]["status"] == "partial"
    assert result["metadata"]["runtime_profile"] == "coder-next-bounded"
    assert result["metadata"]["max_tool_calls_total"] == 2
    assert result["metadata"]["tool_calls"] == 2
    assert result["metadata"]["forced_finalization_reason"] == "tool_call_budget_exhausted"


def test_local_agent_forces_final_manifest_on_last_turn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    payloads: list[dict[str, object]] = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_text",
                                        "arguments": json.dumps({"path": "target.py"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_manifest("verified")),
                        }
                    }
                ]
            },
        ]
    )

    def fake_request(_url: str, payload: dict[str, object] | None, *, timeout_seconds: float):
        assert payload is not None
        payloads.append(payload)
        return next(responses)

    monkeypatch.setattr(local_agent, "_json_request", fake_request)

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8081",
        model="qwen-local",
        cwd=tmp_path,
        prompt="inspect",
        max_turns=1,
    )

    assert result["manifest"]["summary"] == "verified"
    assert payloads[0]["tool_choice"] == "auto"
    assert "tools" in payloads[0]
    assert payloads[1]["tool_choice"] == "none"
    assert "tools" not in payloads[1]
    assert payloads[1]["response_format"]["type"] == "json_schema"
    assert payloads[1]["response_format"]["schema"] == local_agent.FINAL_MANIFEST_SCHEMA
    assert "Finalization turn" in payloads[1]["messages"][-1]["content"]
    assert result["metadata"]["tool_turns"] == 1
    assert result["metadata"]["finalization_turns"] == 1
    assert result["metadata"]["max_turns"] == 3
    assert result["metadata"]["max_finalization_turns"] == 2


def test_qwen38_prefetched_review_uses_scout_then_medium_schema(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    payloads: list[dict[str, object]] = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "hypotheses": [
                                        {
                                            "title": "candidate race",
                                            "claim": "stale read can remove newer state",
                                            "failure_scenario": "A reads old; B writes new; A removes new",
                                            "wrong_outcome": "new state is lost",
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "reviews": [
                                        {
                                            "candidate_index": 0,
                                            "verdict": "confirmed",
                                            "reason": "reachable wrong state",
                                            "finding": "confirmed race",
                                            "contradiction_quote": "",
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140},
            },
        ]
    )

    def fake_request(_url: str, payload: dict[str, object] | None, *, timeout_seconds: float):
        assert payload is not None
        payloads.append(payload)
        return next(responses)

    monkeypatch.setattr(local_agent, "_json_request", fake_request)

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8084",
        model="qwen38-27b",
        cwd=tmp_path,
        prompt="Files in scope:\n- target.py\n\nTask:\nreview",
        max_turns=1,
    )

    assert result["manifest"]["summary"] == (
        "Verified 1 concrete finding(s) from 1 scout hypothesis/hypotheses."
    )
    assert result["manifest"]["findings"] == ["confirmed race"]
    assert result["manifest"]["commands_run"] == ["read_text"]
    assert result["metadata"]["execution_mode"] == "prefetched_review"
    assert result["metadata"]["turns"] == 2
    assert result["metadata"]["tool_calls"] == 1
    assert result["metadata"]["usage"] == {
        "input_tokens": 220,
        "output_tokens": 30,
        "total_tokens": 250,
    }
    assert payloads[0]["chat_template_kwargs"]["enable_thinking"] is False
    assert payloads[0]["cache_prompt"] is True
    assert "reasoning_budget_tokens" not in payloads[0]
    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert payloads[0]["json_schema"] == local_agent.QWEN38_SCOUT_SCHEMA
    assert payloads[1]["chat_template_kwargs"]["enable_thinking"] is True
    assert payloads[1]["cache_prompt"] is True
    assert payloads[1]["chat_template_kwargs"]["reasoning_effort"] == "medium"
    assert (
        payloads[1]["reasoning_budget_tokens"]
        == local_agent.QWEN38_PREFETCH_REASONING_BUDGET_TOKENS
    )
    assert payloads[1]["response_format"] == {"type": "json_object"}
    assert payloads[1]["json_schema"] == local_agent.QWEN38_VERIFIER_SCHEMA
    assert payloads[1]["temperature"] == 0
    assert payloads[0]["messages"][0]["content"] == payloads[1]["messages"][0]["content"]
    common_system = str(payloads[0]["messages"][0]["content"])
    assert "VALUE = 42" in common_system
    assert "<original_task>" in common_system
    assert "<scoped_source_evidence>" in common_system
    scout_user = str(payloads[0]["messages"][1]["content"])
    verifier_user = str(payloads[1]["messages"][1]["content"])
    assert scout_user.startswith("<stage_instructions>")
    assert verifier_user.startswith("<stage_instructions>")
    assert scout_user != verifier_user
    assert result["metadata"]["scout_max_tokens"] == local_agent.QWEN38_PREFETCH_SCOUT_MAX_TOKENS
    assert result["metadata"]["verifier_max_tokens"] == payloads[1]["max_tokens"]
    assert result["metadata"]["rejection_audit_max_tokens"] == 0
    assert result["metadata"]["verified_hypotheses"] == 1
    assert result["metadata"]["rejected_hypotheses"] == 0
    assert result["metadata"]["uncertain_hypotheses"] == 0


@pytest.mark.parametrize(
    ("bad_response", "message"),
    [
        (
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "{}"},
                    }
                ]
            },
            "scout exhausted its generation budget",
        ),
        (
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "{"},
                    }
                ]
            },
            "scout returned invalid JSON",
        ),
    ],
)
def test_qwen38_prefetched_scout_generation_errors_fail_closed(
    monkeypatch,
    tmp_path: Path,
    bad_response: dict[str, object],
    message: str,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: bad_response,
    )

    with pytest.raises(RuntimeError, match=message):
        local_agent.run_agent(
            endpoint="http://local.invalid:8084",
            model="qwen38-27b",
            cwd=tmp_path,
            prompt="Files in scope:\n- target.py\n\nTask:\nreview",
            max_turns=1,
        )


@pytest.mark.parametrize(
    ("bad_response", "message"),
    [
        (
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "{}"},
                    }
                ]
            },
            "verifier exhausted its generation budget",
        ),
        (
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "{"},
                    }
                ]
            },
            "verifier returned invalid JSON",
        ),
    ],
)
def test_qwen38_prefetched_verifier_generation_errors_fail_closed(
    monkeypatch,
    tmp_path: Path,
    bad_response: dict[str, object],
    message: str,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    responses = iter(
        [
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "hypotheses": [
                                        {
                                            "title": "candidate",
                                            "claim": "claim",
                                            "failure_scenario": "scenario",
                                            "wrong_outcome": "wrong",
                                        }
                                    ]
                                }
                            ),
                        },
                    }
                ]
            },
            bad_response,
        ]
    )
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(RuntimeError, match=message):
        local_agent.run_agent(
            endpoint="http://local.invalid:8084",
            model="qwen38-27b",
            cwd=tmp_path,
            prompt="Files in scope:\n- target.py\n\nTask:\nreview",
            max_turns=1,
        )


def test_qwen38_prefetched_review_rejects_silent_verifier_drop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "hypotheses": [
                                        {
                                            "title": "candidate one",
                                            "claim": "claim one",
                                            "failure_scenario": "scenario one",
                                            "wrong_outcome": "wrong one",
                                        },
                                        {
                                            "title": "candidate two",
                                            "claim": "claim two",
                                            "failure_scenario": "scenario two",
                                            "wrong_outcome": "wrong two",
                                        },
                                    ]
                                }
                            ),
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "reviews": [
                                        {
                                            "candidate_index": 0,
                                            "verdict": "rejected",
                                            "reason": "contradicted",
                                            "finding": "",
                                            "contradiction_quote": "VALUE = 42",
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ]
            },
        ]
    )

    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: next(responses),
    )

    with pytest.raises(
        RuntimeError,
        match="exactly one review per scout hypothesis",
    ):
        local_agent.run_agent(
            endpoint="http://local.invalid:8084",
            model="qwen38-27b",
            cwd=tmp_path,
            prompt="Files in scope:\n- target.py\n\nTask:\nreview",
            max_turns=1,
        )


def test_qwen38_prefetched_review_downgrades_unproven_rejection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "hypotheses": [
                                        {
                                            "title": "candidate race",
                                            "claim": "stale state can remove newer state",
                                            "failure_scenario": "A reads old; B writes new; A removes new",
                                            "wrong_outcome": "new state is lost",
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "reviews": [
                                        {
                                            "candidate_index": 0,
                                            "verdict": "rejected",
                                            "reason": "sounds benign",
                                            "finding": "",
                                            "contradiction_quote": "",
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ]
            },
        ]
    )

    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: next(responses),
    )

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8084",
        model="qwen38-27b",
        cwd=tmp_path,
        prompt="Files in scope:\n- target.py\n\nTask:\nreview",
        max_turns=1,
    )

    assert result["manifest"]["status"] == "partial"
    assert result["manifest"]["findings"] == []
    assert result["manifest"]["blockers"][0].startswith(
        "Needs verification: candidate race:"
    )
    assert result["metadata"]["rejected_hypotheses"] == 0
    assert result["metadata"]["uncertain_hypotheses"] == 1
    assert result["metadata"]["verifier_reviews"][0]["verdict"] == "uncertain"


def test_qwen38_prefetched_review_reverse_audits_supported_rejection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    payloads: list[dict[str, object]] = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "hypotheses": [
                                        {
                                            "title": "candidate race",
                                            "claim": "stale state can overwrite newer state",
                                            "failure_scenario": "A reads old; B writes new; A overwrites new",
                                            "wrong_outcome": "new state is lost",
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "reviews": [
                                        {
                                            "candidate_index": 0,
                                            "verdict": "rejected",
                                            "reason": "VALUE assignment prevents the race",
                                            "finding": "",
                                            "contradiction_quote": "VALUE   =\n 42",
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140},
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "reviews": [
                                        {
                                            "candidate_index": 0,
                                            "verdict": "rejection_unsound",
                                            "reason": "the quoted line does not prevent the proposed ordering",
                                            "corrected_finding": "confirmed race after reverse audit",
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ],
                "usage": {"prompt_tokens": 140, "completion_tokens": 30, "total_tokens": 170},
            },
        ]
    )

    def fake_request(_url: str, payload: dict[str, object] | None, *, timeout_seconds: float):
        assert payload is not None
        payloads.append(payload)
        return next(responses)

    monkeypatch.setattr(local_agent, "_json_request", fake_request)

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8084",
        model="qwen38-27b",
        cwd=tmp_path,
        prompt="Files in scope:\n- target.py\n\nTask:\nreview",
        max_turns=1,
    )

    assert result["manifest"]["findings"] == ["confirmed race after reverse audit"]
    assert result["metadata"]["turns"] == 3
    assert result["metadata"]["verified_hypotheses"] == 1
    assert result["metadata"]["rejected_hypotheses"] == 0
    assert result["metadata"]["uncertain_hypotheses"] == 0
    assert result["metadata"]["usage"] == {
        "input_tokens": 360,
        "output_tokens": 60,
        "total_tokens": 420,
    }
    assert payloads[2]["json_schema"] == local_agent.QWEN38_REJECTION_AUDIT_SCHEMA
    assert payloads[2]["cache_prompt"] is True
    assert (
        payloads[2]["reasoning_budget_tokens"]
        == local_agent.QWEN38_PREFETCH_REASONING_BUDGET_TOKENS
    )
    assert payloads[0]["messages"][0]["content"] == payloads[2]["messages"][0]["content"]
    scout_user = str(payloads[0]["messages"][1]["content"])
    audit_user = str(payloads[2]["messages"][1]["content"])
    assert scout_user.startswith("<stage_instructions>")
    assert audit_user.startswith("<stage_instructions>")
    assert scout_user != audit_user
    assert result["metadata"]["rejection_audit_reviews"][0]["verdict"] == "rejection_unsound"


def test_qwen38_prefetched_verifier_budget_scales_with_candidate_count(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    payloads: list[dict[str, object]] = []
    hypotheses = [
        {
            "title": f"candidate {index}",
            "claim": f"claim {index}",
            "failure_scenario": f"scenario {index}",
            "wrong_outcome": f"wrong {index}",
        }
        for index in range(4)
    ]
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"hypotheses": hypotheses}),
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "reviews": [
                                        {
                                            "candidate_index": index,
                                            "verdict": "confirmed",
                                            "reason": "proven",
                                            "finding": f"finding {index}",
                                            "contradiction_quote": "",
                                        }
                                        for index in range(4)
                                    ]
                                }
                            ),
                        }
                    }
                ]
            },
        ]
    )

    def fake_request(_url: str, payload: dict[str, object] | None, *, timeout_seconds: float):
        assert payload is not None
        payloads.append(payload)
        return next(responses)

    monkeypatch.setattr(local_agent, "_json_request", fake_request)

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8084",
        model="qwen38-27b",
        cwd=tmp_path,
        prompt="Files in scope:\n- target.py\n\nTask:\nreview",
        max_turns=1,
    )

    assert result["manifest"]["findings"] == [
        "finding 0",
        "finding 1",
        "finding 2",
        "finding 3",
    ]
    assert payloads[1]["max_tokens"] == 3800


def test_qwen38_tool_loop_fallback_keeps_tools_schema_for_prompt_reuse(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text(
        "".join(f"VALUE_{index} = {index}\n" for index in range(601)),
        encoding="utf-8",
    )
    payloads: list[dict[str, object]] = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_text",
                                        "arguments": json.dumps({"path": "target.py"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_manifest("verified")),
                        }
                    }
                ]
            },
        ]
    )

    def fake_request(_url: str, payload: dict[str, object] | None, *, timeout_seconds: float):
        assert payload is not None
        payloads.append(payload)
        return next(responses)

    monkeypatch.setattr(local_agent, "_json_request", fake_request)

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8084",
        model="qwen38-27b",
        cwd=tmp_path,
        prompt="Files in scope:\n- target.py\n\nTask:\nreview",
        max_turns=1,
    )

    assert result["manifest"]["summary"] == "verified"
    assert payloads[0]["tool_choice"] == "auto"
    assert payloads[1]["tool_choice"] == "none"
    assert payloads[1]["tools"] == payloads[0]["tools"]
    assert payloads[1]["response_format"]["type"] == "json_schema"


def test_local_agent_finalization_payload_maintains_semantic_alternation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    payloads: list[dict[str, object]] = []
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_text",
                                        "arguments": json.dumps({"path": "target.py"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_manifest("verified")),
                        }
                    }
                ]
            },
        ]
    )

    def fake_request(_url: str, payload: dict[str, object] | None, *, timeout_seconds: float):
        assert payload is not None
        payloads.append(payload)
        return next(responses)

    monkeypatch.setattr(local_agent, "_json_request", fake_request)

    local_agent.run_agent(
        endpoint="http://local.invalid:8081",
        model="qwen-local",
        cwd=tmp_path,
        prompt="inspect",
        max_turns=1,
    )

    finalization_messages = payloads[1]["messages"]

    semantic_roles = [
        msg["role"]
        for msg in finalization_messages
        if msg["role"] in ("user", "assistant")
        and not (msg["role"] == "assistant" and msg.get("tool_calls"))
    ]

    assert semantic_roles[-3:] == ["user", "assistant", "user"]
    assert finalization_messages[-2]["content"] == "Acknowledged."
    assert "Finalization turn" in finalization_messages[-1]["content"]

def test_coder_next_bounded_runtime_profile_is_short_and_wide() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt="Inspect routing configuration and summarize the relevant settings.",
        timeout_seconds=30,
        max_turns=8,
        max_tokens=900,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=24,
    )

    assert profile.name == "coder-next-bounded"
    assert profile.timeout_seconds == 60
    assert profile.max_turns == 4
    assert profile.max_tokens == 1000
    assert profile.max_tool_calls_per_turn == 8
    assert profile.max_tool_calls_total == 16
    assert profile.context_window_tokens == 16384


def test_coder_next_bounded_profile_ignores_wrapper_architecture_boilerplate() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt=(
            "Architecture contract:\n"
            "- ChatGPT Web is the architect/manager/reviewer.\n"
            "Task: Find the exact default value in config.py."
        ),
        timeout_seconds=30,
        max_turns=8,
        max_tokens=900,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=24,
    )

    assert profile.name == "coder-next-quick"
    assert profile.max_turns == 3
    assert profile.max_tokens == 800
    assert profile.context_window_tokens == 12288


def test_coder_next_quick_profile_exposes_only_search_and_read_text() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt="Find the exact default value with line numbers.",
        timeout_seconds=30,
        max_turns=8,
        max_tokens=1000,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=24,
    )

    names = {
        str(tool["function"]["name"])
        for tool in local_agent._tools_for_profile(profile)
    }

    assert names == {"read_text", "search"}
    first_turn_names = {
        str(tool["function"]["name"])
        for tool in local_agent._tools_for_turn(profile, 1)
    }
    assert first_turn_names == {"search"}
    assert local_agent._tool_choice_for_turn(profile, 1) == "required"
    assert local_agent._tool_choice_for_turn(profile, 2) == "auto"


def test_local_system_prompt_requires_search_before_declaring_config_absent() -> None:
    prompt = local_agent._system_prompt()

    assert "configuration or default-value questions" in prompt
    assert "first repository evidence call" in prompt
    assert "Never claim a setting is absent" in prompt
    assert "follow next_start_line" in prompt


def test_coder_next_deep_runtime_profile_allows_broader_exploration() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt="Architecture review: explain exactly why routing crosses modules.",
        timeout_seconds=30,
        max_turns=8,
        max_tokens=900,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=24,
    )

    assert profile.name == "coder-next-deep"
    assert profile.timeout_seconds == 90
    assert profile.max_turns == 6
    assert profile.max_tokens == 1400
    assert profile.max_tool_calls_per_turn == 8
    assert profile.max_tool_calls_total == 32
    assert profile.context_window_tokens == 28672


def test_explicit_medium_complexity_forces_coder_next_deep_profile() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt="Task complexity:\nmedium\nTask:\nInspect this bounded integration slice.",
        timeout_seconds=30,
        max_turns=8,
        max_tokens=900,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=24,
    )

    assert profile.name == "coder-next-deep"
    assert profile.max_turns == 6
    assert profile.context_window_tokens == 28672


def test_coder_next_pagination_review_is_deep() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt="Check pagination correctness, starvation freedom, and mutation while iterating.",
        timeout_seconds=30,
        max_turns=8,
        max_tokens=1000,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=24,
    )

    assert profile.name == "coder-next-deep"


def test_read_text_exposes_continuation_start(tmp_path: Path) -> None:
    target = tmp_path / "long.txt"
    target.write_text("\n".join(f"line-{index}" for index in range(10)), encoding="utf-8")

    page = local_agent._tool_read_text(
        tmp_path,
        {"path": "long.txt", "start_line": 1, "line_limit": 4},
    )

    assert page["has_more"] is True
    assert page["next_start_line"] == 5


def test_forced_final_invalid_manifest_retries_once(monkeypatch, tmp_path: Path) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_text",
                                        "arguments": json.dumps({"path": "target.py"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "not-json"}}]},
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(_manifest("recovered")),
                        }
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: next(responses),
    )

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8081",
        model="qwen-local",
        cwd=tmp_path,
        prompt="inspect",
        max_turns=1,
    )

    assert result["manifest"]["status"] == "succeeded"
    assert result["manifest"]["summary"] == "recovered"
    assert result["metadata"]["finalization_turns"] == 2


def test_forced_final_two_invalid_manifests_degrade_to_partial(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    responses = iter(
        [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_text",
                                        "arguments": json.dumps({"path": "target.py"}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "not-json"}}]},
            {"choices": [{"message": {"role": "assistant", "content": "still-not-json"}}]},
        ]
    )
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: next(responses),
    )

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8081",
        model="qwen-local",
        cwd=tmp_path,
        prompt="inspect",
        max_turns=1,
    )

    assert result["manifest"]["status"] == "partial"
    assert result["manifest"]["findings"] == []
    assert result["metadata"]["finalization_turns"] == 2
    assert "Final manifest validation failed" in result["manifest"]["blockers"][0]


def test_non_coder_next_model_keeps_configured_runtime_profile() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen38-35b",
        prompt="Architecture review",
        timeout_seconds=45,
        max_turns=7,
        max_tokens=850,
        max_tool_calls_per_turn=3,
        max_tool_calls_total=15,
    )

    assert profile.name == "default"
    assert profile.timeout_seconds == 45
    assert profile.max_turns == 7
    assert profile.max_tokens == 850
    assert profile.max_tool_calls_per_turn == 3
    assert profile.max_tool_calls_total == 15


def test_qwen38_review_profile_is_bounded_but_keeps_discovered_context() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen38-27b",
        prompt="Architecture review: inspect rollback safety.",
        timeout_seconds=45,
        max_turns=8,
        max_tokens=1400,
        max_tool_calls_per_turn=8,
        max_tool_calls_total=24,
    )

    assert profile.name == "qwen38-review"
    assert profile.timeout_seconds == 120
    assert profile.max_turns == 4
    assert profile.max_tokens == 1200
    assert profile.max_tool_calls_per_turn == 4
    assert profile.max_tool_calls_total == 12
    assert profile.context_window_tokens is None


def test_qwen38_system_prompt_enforces_scope_and_concise_findings() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen38-27b",
        prompt="review",
        timeout_seconds=45,
        max_turns=8,
        max_tokens=1400,
        max_tool_calls_per_turn=8,
        max_tool_calls_total=24,
    )
    prompt = local_agent._system_prompt(profile)

    assert "hard boundaries" in prompt
    assert "Do not inspect tests, callers, installers" in prompt
    assert "distinguish detection from prevention" in prompt
    assert "try to falsify it" in prompt
    assert "local variables frozen" in prompt
    assert "does not retroactively change" in prompt
    assert "Eventual self-healing does not make" in prompt
    assert "identify the exact impossible step" in prompt
    assert "failed HTTP request or readiness timeout" in prompt
    assert "enumerate every explicit return/failure branch" in prompt
    assert "line_limit=600" in prompt
    assert "at most four high-value findings" in prompt


def test_qwen38_extracts_and_enforces_hard_file_scope(tmp_path: Path) -> None:
    scoped = tmp_path / "scripts" / "dem_gpu_mode_control.py"
    scoped.parent.mkdir(parents=True)
    scoped.write_text("VALUE = 1\n", encoding="utf-8")
    other = tmp_path / "scripts" / "install_local_reviewer_gpu_mode.py"
    other.write_text("VALUE = 2\n", encoding="utf-8")
    prompt = (
        "Files in scope:\n"
        "- scripts/dem_gpu_mode_control.py\n\n"
        "Out of scope:\n"
        "- Do not inspect unrelated files\n"
    )

    allowed = local_agent._extract_files_in_scope(prompt)

    assert allowed == ("scripts/dem_gpu_mode_control.py",)
    assert local_agent._tool_path_within_scope(
        tmp_path,
        name="read_text",
        args={"path": "scripts/dem_gpu_mode_control.py"},
        allowed_paths=allowed,
    )
    assert not local_agent._tool_path_within_scope(
        tmp_path,
        name="read_text",
        args={"path": "scripts/install_local_reviewer_gpu_mode.py"},
        allowed_paths=allowed,
    )
    assert not local_agent._tool_path_within_scope(
        tmp_path,
        name="search",
        args={"query": "gpu-mode"},
        allowed_paths=allowed,
    )


def test_qwen38_profile_exposes_and_executes_600_line_reads(tmp_path: Path) -> None:
    target = tmp_path / "long.py"
    target.write_text("\n".join(f"line_{index}" for index in range(500)), encoding="utf-8")
    profile = local_agent.resolve_runtime_profile(
        model="qwen38-27b",
        prompt="review",
        timeout_seconds=45,
        max_turns=8,
        max_tokens=1400,
        max_tool_calls_per_turn=8,
        max_tool_calls_total=24,
    )
    read_tool = next(
        tool
        for tool in local_agent._tools_for_profile(profile)
        if tool["function"]["name"] == "read_text"
    )

    assert read_tool["function"]["parameters"]["properties"]["line_limit"]["maximum"] == 600
    assert local_agent.TOOLS[0]["function"]["parameters"]["properties"]["line_limit"]["maximum"] == 400
    result = local_agent._execute_tool(
        tmp_path,
        "read_text",
        {"path": "long.py", "line_limit": 600},
        read_text_max_lines=600,
    )

    assert result["ok"] is True
    assert result["end_line"] == 500
    assert result["has_more"] is False


def test_local_manifest_normalizes_scalar_string_lists() -> None:
    manifest = _manifest()
    manifest["commands_run"] = "read_text"
    manifest["findings"] = "Found exact evidence."
    manifest["blockers"] = ""

    normalized = local_agent._normalize_manifest(manifest)

    assert normalized["commands_run"] == ["read_text"]
    assert normalized["findings"] == ["Found exact evidence."]
    assert normalized["blockers"] == []
