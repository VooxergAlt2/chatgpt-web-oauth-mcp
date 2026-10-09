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


def test_local_git_diff_base_only_includes_current_working_tree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    staged = tmp_path / "staged.txt"
    unstaged = tmp_path / "unstaged.txt"
    staged.write_text("before staged\n", encoding="utf-8")
    unstaged.write_text("before unstaged\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    staged.write_text("after staged\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "staged.txt"], check=True)
    unstaged.write_text("after unstaged\n", encoding="utf-8")

    diff = local_agent._execute_tool(tmp_path, "git_diff", {"base": base})

    assert diff["ok"] is True
    assert "after staged" in diff["stdout"]
    assert "after unstaged" in diff["stdout"]


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
        "local",
        "local2",
        "codex",
        "antigravity2",
    ]
    assert medium_route["candidates"][0]["eligible"] is True
    assert medium_route["candidates"][1]["reason"] == "unsupported_harness"
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
                role="reviewer",
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


def test_medium_routing_and_authority_follow_harness_roles_not_ids(
    monkeypatch,
) -> None:
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
                name="local",
                role="reviewer",
                endpoint="http://192.0.2.10:8081",
                default_model="qwen38-27b",
            ),
            LocalOpenAIHarness(
                name="local2",
                role="scout",
                endpoint="http://192.0.2.11:8081",
                default_model="qwen-coder-next",
            ),
        ],
        automatic_routing=True,
    )

    assert registry._medium_explore_route()[:2] == ("local", "local2")
    selected, _adapter, _quota, route = registry._select_harness(
        harness=None,
        kind="explore",
        model=None,
        complexity="medium",
        fresh=False,
        record_selection=False,
    )
    assert selected == "local"
    assert route["candidates"][0]["harness"] == "local"

    reviewer_prompt = registry._build_prompt(
        harness="local",
        task="Review bounded scope",
        goal=None,
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="forbidden",
        kind="explore",
        complexity="low",
    )
    scout_prompt = registry._build_prompt(
        harness="local2",
        task="Inspect bounded scope",
        goal=None,
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="forbidden",
        kind="explore",
        complexity="low",
    )
    scout_as_reviewer_prompt = registry._build_prompt(
        harness="local2",
        task="Review bounded scope",
        goal=None,
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="forbidden",
        kind="explore",
        complexity="medium",
    )
    assert "Local agent role:\nreviewer" in reviewer_prompt
    assert "Local agent role:\nscout" in scout_prompt
    assert "Local agent role:\nreviewer" in scout_as_reviewer_prompt

    authoritative_task = type(
        "Task",
        (),
        {
            "harness": "local",
            "kind": "explore",
            "complexity": "medium",
            "routing_mode": "complexity",
            "prompt": reviewer_prompt,
        },
    )()
    advisory_task = type(
        "Task",
        (),
        {
            "harness": "local2",
            "kind": "explore",
            "complexity": "medium",
            "routing_mode": "complexity",
            "prompt": scout_as_reviewer_prompt,
        },
    )()
    succeeded = {"success": True, "status": "succeeded"}
    assert registry._runtime_retry_reason(task=authoritative_task, result=succeeded) is None
    assert (
        registry._runtime_retry_reason(task=advisory_task, result=succeeded)
        == "reviewer_non_authoritative"
    )


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
                role="reviewer",
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


def test_medium_reviewer_fallback_on_scout_escalates_to_codex(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    monkeypatch.setattr(
        delegate_harnesses,
        "probe_endpoint",
        lambda *_args, **_kwargs: (True, None, {"status": "ok"}),
    )
    monkeypatch.setattr(
        delegate_harnesses,
        "codex_read_only_sandbox_available",
        lambda _command: True,
    )
    registry = ExecutorRegistry(
        codex_command="codex",
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            ),
            LocalOpenAIHarness(
                name="local2",
                role="reviewer",
                display_name="Reviewer",
                endpoint="http://127.0.0.1:8083",
                default_model="qwen38-27b",
            ),
        ],
        automatic_routing=True,
        primary_harness="local",
        fallback_harnesses=("local2", "codex"),
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local2",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="review bounded concurrency scope",
        goal=None,
        task_id=None,
        group_id=None,
        model="qwen38-27b",
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
        request_fingerprint="reviewer-two-hop-fallback",
        complexity="medium",
        routing_mode="complexity",
        routing_reason="complexity_medium_selected",
    )
    calls: list[str] = []

    def fake_start(*, delegate_task):
        calls.append(delegate_task.harness)
        if delegate_task.harness == "local2":
            return {
                "success": False,
                "status": "failed",
                "harness": "local2",
                "model": "qwen38-27b",
                "error": {
                    "code": "local_openai_unavailable",
                    "message": "reviewer endpoint offline",
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

    assert calls == ["local2", "local", "codex"]
    assert result["success"] is True
    assert result["harness"] == "codex"
    assert [attempt["trigger"] for attempt in result["routing_fallback"]["attempts"]] == [
        "local_runtime_failure",
        "reviewer_non_authoritative",
    ]
    assert result["routing_fallback"]["from_harness"] == "local"
    assert result["routing_fallback"]["to_harness"] == "codex"
    assert task.routing_reason == "reviewer_non_authoritative_to_codex"
    assert "Reviewer fallback contract:" in task.prompt
    assert "Local agent role:\nreviewer" in task.prompt


def test_medium_scout_reviewer_success_escalates_to_authoritative_reviewer(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = ExecutorRegistry(
        codex_command="codex",
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            )
        ],
        automatic_routing=True,
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="review bounded concurrency",
        goal=None,
        task_id=None,
        group_id=None,
        model="qwen-coder-next",
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
        request_fingerprint="reviewer-authority-gate",
        complexity="medium",
        routing_mode="complexity",
        routing_reason="complexity_medium_selected",
    )
    assert "Local agent role:\nreviewer" in task.prompt

    codex = registry.harnesses["codex"]
    monkeypatch.setattr(
        registry,
        "_select_runtime_fallback",
        lambda _task: ("codex", codex),
    )
    calls: list[tuple[str, str]] = []

    def fake_start(*, delegate_task):
        calls.append((delegate_task.harness, delegate_task.prompt))
        if len(calls) == 1:
            return {
                "success": True,
                "status": "succeeded",
                "harness": "local",
                "model": "qwen-coder-next",
                "harness_metadata": {"usage": {"total_tokens": 7000}},
                "structured_output": {
                    "status": "succeeded",
                    "findings": ["advisory candidate"],
                },
            }
        return {
            "success": True,
            "status": "succeeded",
            "harness": "codex",
            "model": delegate_task.model,
            "structured_output": {
                "status": "succeeded",
                "findings": ["authoritative finding"],
            },
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)

    result = registry._run_scheduled_task(task)

    assert [name for name, _prompt in calls] == ["local", "codex"]
    assert result["success"] is True
    assert result["structured_output"]["findings"] == ["authoritative finding"]
    attempt = result["routing_fallback"]["attempts"][0]
    assert attempt["trigger"] == "reviewer_non_authoritative"
    assert attempt["advisory_output"]["findings"] == ["advisory candidate"]
    assert "Reviewer fallback contract:" in calls[1][1]
    assert "advisory candidate" not in calls[1][1]
    assert task.routing_reason == "reviewer_non_authoritative_to_codex"
    route_attempts = result["route_attempts"]
    assert len(route_attempts) == 2
    assert route_attempts[0]["route"].startswith("local:explore:qwen-coder-next:")
    assert route_attempts[0]["accepted"] is False
    assert route_attempts[0]["failure_type"] == "low_confidence"
    assert route_attempts[0]["replan_action"] == "add_reviewer"
    assert route_attempts[0]["total_tokens"] == 7000
    assert route_attempts[1]["route"].startswith("codex:explore:")
    assert route_attempts[1]["accepted"] is True
    assert result["route_chain"]["attempt_count"] == 2
    assert result["route_chain"]["fallback_count"] == 1


def test_medium_scout_reviewer_fails_closed_without_authoritative_reviewer(
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
        automatic_routing=True,
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="review bounded concurrency",
        goal=None,
        task_id=None,
        group_id=None,
        model="qwen-coder-next",
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
        request_fingerprint="reviewer-authority-blocked",
        complexity="medium",
        routing_mode="complexity",
        routing_reason="complexity_medium_selected",
    )
    monkeypatch.setattr(registry, "_select_runtime_fallback", lambda _task: None)
    monkeypatch.setattr(
        registry,
        "_start_codex_delegate_impl",
        lambda **_kwargs: {
            "success": True,
            "status": "succeeded",
            "harness": "local",
            "model": "qwen-coder-next",
            "structured_output": {
                "status": "succeeded",
                "findings": ["advisory candidate"],
            },
        },
    )

    result = registry._run_scheduled_task(task)

    assert result["success"] is False
    assert result["status"] == "blocked"
    assert result["error"]["code"] == "authoritative_reviewer_unavailable"
    assert result["reviewer_advisory"]["structured_output"]["findings"] == [
        "advisory candidate"
    ]
    assert result["routing_fallback"]["selected"] is None
    assert result["routing_fallback"]["trigger"] == "reviewer_non_authoritative"


def test_local2_failure_then_scout_reviewer_escalates_to_authoritative_reviewer(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = ExecutorRegistry(
        codex_command="codex",
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            ),
            LocalOpenAIHarness(
                name="local2",
                role="reviewer",
                endpoint="http://127.0.0.1:8083",
                default_model="qwen38-27b",
            ),
        ],
        automatic_routing=True,
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local2",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="review bounded concurrency",
        goal=None,
        task_id=None,
        group_id=None,
        model="qwen38-27b",
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
        request_fingerprint="reviewer-multihop",
        complexity="medium",
        routing_mode="complexity",
        routing_reason="complexity_medium_selected",
    )
    local = registry.harnesses["local"]
    codex = registry.harnesses["codex"]

    def fake_fallback(current_task):
        if current_task.harness == "local2":
            return "local", local
        if current_task.harness == "local":
            return "codex", codex
        return None

    monkeypatch.setattr(registry, "_select_runtime_fallback", fake_fallback)
    calls: list[str] = []

    def fake_start(*, delegate_task):
        calls.append(delegate_task.harness)
        if delegate_task.harness == "local2":
            return {
                "success": False,
                "status": "failed",
                "harness": "local2",
                "model": "qwen38-27b",
                "error": {
                    "code": "local_openai_unavailable",
                    "message": "reviewer endpoint offline",
                },
            }
        if delegate_task.harness == "local":
            return {
                "success": True,
                "status": "succeeded",
                "harness": "local",
                "model": "qwen-coder-next",
                "structured_output": {
                    "status": "succeeded",
                    "findings": ["advisory candidate"],
                },
            }
        return {
            "success": True,
            "status": "succeeded",
            "harness": "codex",
            "model": delegate_task.model,
            "structured_output": {
                "status": "succeeded",
                "findings": ["authoritative finding"],
            },
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)

    result = registry._run_scheduled_task(task)

    assert calls == ["local2", "local", "codex"]
    attempts = result["routing_fallback"]["attempts"]
    assert [attempt["trigger"] for attempt in attempts] == [
        "local_runtime_failure",
        "reviewer_non_authoritative",
    ]
    assert attempts[1]["advisory_output"]["findings"] == ["advisory candidate"]
    assert result["structured_output"]["findings"] == ["authoritative finding"]


def test_reviewer_scope_preflight_checks_bounded_sources(tmp_path: Path) -> None:
    def prompt(*paths: str) -> str:
        return (
            "Local agent role:\nreviewer\n\nFiles in scope:\n"
            + "".join(f"- {name}\n" for name in paths)
            + "\nTask:\nReview"
        )

    (tmp_path / "small.py").write_text("x = 1\n", encoding="utf-8")
    assert local_agent.reviewer_scope_preflight(
        tmp_path, prompt("small.py")
    ) is None
    assert local_agent.reviewer_scope_preflight(
        tmp_path, prompt()
    )["reason"] == "missing_explicit_scope"
    assert local_agent.reviewer_scope_preflight(
        tmp_path, prompt(*[f"file-{n}.py" for n in range(13)])
    )["reason"] == "too_many_files"
    assert local_agent.reviewer_scope_preflight(
        tmp_path, prompt("missing.py")
    )["reason"] == "missing_unreadable_or_oversized_source"
    assert local_agent.reviewer_scope_preflight(
        tmp_path, prompt("../outside.py")
    )["reason"] == "invalid_scope_path"
    (tmp_path / "giant.py").write_text(
        "x" * (local_agent.REVIEWER_PREFETCH_MAX_CHARS + 1), encoding="utf-8"
    )
    assert local_agent.reviewer_scope_preflight(
        tmp_path, prompt("giant.py")
    )["reason"] == "missing_unreadable_or_oversized_source"


def test_local_reviewer_scope_rejected_before_process_start(
    monkeypatch, tmp_path: Path,
) -> None:
    registry = ExecutorRegistry(
        harnesses=[
            LocalOpenAIHarness(
                name="local2",
                role="reviewer",
                endpoint="http://127.0.0.1:8083",
                default_model="qwen38-27b",
            ),
        ],
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    def forbidden_run(*_args, **_kwargs):
        raise AssertionError("No model process should be launched for invalid scope")
    monkeypatch.setattr(registry._process_runner, "run", forbidden_run)
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local2", project=project, cwd=tmp_path,
        kind="explore", task="Review nonexistent file",
        goal=None, task_id=None, group_id=None, model="qwen38-27b",
        reasoning_effort="none", sandbox_mode="server-enforced-read-only-tools",
        commit_mode="forbidden", execution_timeout_seconds=300,
        depends_on_group_ids=(), files_in_scope=["missing.py"],
        out_of_scope=[], context_files=[], acceptance_criteria=[],
        done_means=[], verification_commands=[], output_schema=None,
        parse_structured_output=True,
        request_fingerprint="reviewer-preflight-regression",
        complexity="medium", routing_mode="complexity",
        routing_reason="complexity_medium_selected",
    )
    result = registry._start_delegate_impl(delegate_task=task)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "reviewer_scope_preflight_failed"
    assert result["scope_preflight"]["reason"] == "missing_unreadable_or_oversized_source"
    assert result["duration_seconds"] == 0
    assert (
        registry._runtime_retry_reason(task=task, result=result)
        == "reviewer_prefetch_scope_failure"
    )


def test_reviewer_preflight_auto_fallback_skips_both_local_models(
    monkeypatch, tmp_path: Path,
) -> None:
    registry = ExecutorRegistry(
        codex_command="codex",
        harnesses=[
            LocalOpenAIHarness(
                name="local",
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            ),
            LocalOpenAIHarness(
                name="local2", role="reviewer",
                endpoint="http://127.0.0.1:8083",
                default_model="qwen38-27b",
            ),
        ],
        automatic_routing=True,
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local2", project=project, cwd=tmp_path,
        kind="explore", task="Review missing scoped file for correctness defects",
        goal=None, task_id=None, group_id=None, model="qwen38-27b",
        reasoning_effort="none", sandbox_mode="server-enforced-read-only-tools",
        commit_mode="forbidden", execution_timeout_seconds=300,
        depends_on_group_ids=(), files_in_scope=["does-not-exist.py"],
        out_of_scope=[], context_files=[], acceptance_criteria=[],
        done_means=[], verification_commands=[], output_schema=None,
        parse_structured_output=True,
        request_fingerprint="reviewer-preflight-automatic",
        complexity="medium", routing_mode="complexity",
        routing_reason="complexity_medium_selected",
    )
    cloud = registry.harnesses["codex"]
    exclusions = []
    def select_cloud(_task, *, excluded_harnesses=None):
        exclusions.append(set(excluded_harnesses or ()))
        return "codex", cloud
    monkeypatch.setattr(registry, "_select_runtime_fallback", select_cloud)
    original_start = registry._start_delegate_impl
    invocations = []
    def start(*, delegate_task):
        invocations.append(delegate_task.harness)
        if delegate_task.harness == "local2":
            return original_start(delegate_task=delegate_task)
        assert delegate_task.harness == "codex"
        return {
            "success": True,
            "status": "succeeded",
            "harness": "codex",
            "model": delegate_task.model,
            "structured_output": {"status": "succeeded", "findings": []},
        }
    monkeypatch.setattr(registry, "_start_codex_delegate_impl", start)
    result = registry._run_scheduled_task(task)
    assert result["status"] == "succeeded"
    assert invocations == ["local2", "codex"]
    assert exclusions == [{"local", "local2"}]
    attempt = result["route_attempts"][0]
    assert attempt["failure_type"] == "scope_too_large"
    assert result["routing_fallback"]["attempts"][0]["trigger"] == (
        "reviewer_prefetch_scope_failure"
    )


def test_reviewer_executor_uses_research_mode_for_factual_task(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        harnesses=[
            LocalOpenAIHarness(
                name="local2", role="reviewer",
                endpoint="http://127.0.0.1:8083",
                default_model="qwen38-27b",
            ),
        ],
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)

    def make_task(text: str):
        return registry._make_task(
            harness="local2", project=project, cwd=tmp_path, kind="explore",
            task=text, goal=None, task_id=None, group_id=None,
            model="qwen38-27b", reasoning_effort="none",
            sandbox_mode="server-enforced-read-only-tools",
            commit_mode="forbidden", execution_timeout_seconds=120,
            depends_on_group_ids=(), files_in_scope=["README.md"],
            out_of_scope=[], context_files=[], acceptance_criteria=[],
            done_means=[], verification_commands=[], output_schema=None,
            parse_structured_output=True, request_fingerprint="test-mode",
        )

    factual = make_task("Read README.md and report the project title with line evidence.")
    assert local_agent._explicit_local_task_mode(factual.prompt) == "research"
    profile = local_agent.resolve_runtime_profile(
        model=factual.model, prompt=factual.prompt, timeout_seconds=45,
        max_turns=8, max_tokens=1400, max_tool_calls_per_turn=8,
        max_tool_calls_total=24,
    )
    assert profile.name == "reviewer-general"
    assert "Answer the specific user question" in local_agent._system_prompt(profile)
    assert "Reviewer strict mode" not in local_agent._system_prompt(profile)
    assert local_agent.local_task_mode_for_request(
        "Verify the project title in README.md.", None
    ) == "research"
    assert local_agent.local_task_mode_for_request(
        "Read README.md; give its title with README.md:1 evidence. "
        "This is a factual lookup, not a defect review.", None
    ) == "research"
    assert local_agent.local_task_mode_for_request(
        "Найди название проекта, без ревью.", None
    ) == "research"

    review = make_task("Review README.md for correctness defects and regressions.")
    assert local_agent._explicit_local_task_mode(review.prompt) == "review"
    strict = local_agent.resolve_runtime_profile(
        model=review.model, prompt=review.prompt, timeout_seconds=45,
        max_turns=8, max_tokens=1400, max_tool_calls_per_turn=8,
        max_tool_calls_total=24,
    )
    assert strict.name == "reviewer-strict"


def test_research_evidence_changes_feedback_not_process_success(
    monkeypatch, tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Project Falcon\n", encoding="utf-8")
    registry = ExecutorRegistry(
        harnesses=[LocalOpenAIHarness(
            name="local2", role="reviewer", endpoint="http://127.0.0.1:8083",
            default_model="qwen38-27b",
        )],
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local2", project=project, cwd=tmp_path, kind="explore",
        task="Report the project title with exact file:line evidence",
        goal=None, task_id=None, group_id=None, model="qwen38-27b",
        reasoning_effort="none", sandbox_mode="server-enforced-read-only-tools",
        commit_mode="forbidden", execution_timeout_seconds=120,
        depends_on_group_ids=(), files_in_scope=["README.md"],
        out_of_scope=[], context_files=[], acceptance_criteria=[],
        done_means=[], verification_commands=[], output_schema=None,
        parse_structured_output=True, request_fingerprint="evidence-feedback",
    )
    assert local_agent._explicit_local_task_mode(task.prompt) == "research"
    def fake_start(*, delegate_task):
        assert delegate_task is task
        return {
            "status": "succeeded", "success": True, "harness": "local2",
            "model": task.model,
            "structured_output": {
                "status": "succeeded",
                "summary": "Scout found no concrete review candidate requiring verification.",
                "findings": [],
            },
        }
    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry._run_scheduled_task(task)
    assert result["status"] == "succeeded"
    assert result["content_acceptance"]["status"] == "wrong_workflow"
    assert result["route_attempts"][0]["success"] is True
    assert result["route_attempts"][0]["accepted"] is False


def test_research_criteria_handoff_preserves_technical_success(
    monkeypatch, tmp_path: Path,
) -> None:
    (tmp_path / "README.md").write_text("# Falcon\nRelease: 2.1\n", encoding="utf-8")
    registry = ExecutorRegistry(
        harnesses=[LocalOpenAIHarness(
            name="local2", role="reviewer", endpoint="http://127.0.0.1:8083",
            default_model="qwen38-27b",
        )],
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    task = registry._make_task(
        harness="local2", project=registry.project_resolver.resolve(tmp_path),
        cwd=tmp_path, kind="explore", task="Report title and release from README.md",
        goal=None, task_id=None, group_id=None, model="qwen38-27b",
        reasoning_effort="none", sandbox_mode="server-enforced-read-only-tools",
        commit_mode="forbidden", execution_timeout_seconds=120,
        depends_on_group_ids=(), files_in_scope=["README.md"],
        out_of_scope=[], context_files=[],
        acceptance_criteria=["Identify title", "Identify release"],
        done_means=[], verification_commands=[], output_schema=None,
        parse_structured_output=True, request_fingerprint="criteria-handoff",
    )
    assert task.acceptance_criteria == ("Identify title", "Identify release")
    assert "Criterion evidence contract:" in task.prompt

    def fake_start(*, delegate_task):
        assert delegate_task is task
        return {
            "status": "succeeded", "success": True, "harness": "local2",
            "model": task.model,
            "structured_output": {
                "status": "succeeded", "summary": "Falcon (README.md:1)",
                "findings": ["Criterion 1: Title Falcon (README.md:1)"],
            },
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry._run_scheduled_task(task)
    assert result["status"] == "succeeded"
    assert result["route_attempts"][0]["success"] is True
    assert result["route_attempts"][0]["accepted"] is False
    assert result["content_acceptance"]["status"] == "criteria_incomplete"
    handoff = result["content_acceptance"]["review_handoff"]
    assert handoff["questions"] == [
        {
            "index": 2, "criterion": "Identify release",
            "issue": "unreported", "candidate": None,
        },
    ]


def test_reviewer_prefetch_scope_failure_skips_redundant_local_reviewer_hop(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = ExecutorRegistry(
        codex_command="codex",
        harnesses=[
            LocalOpenAIHarness(
                endpoint="http://192.0.2.10:8081",
                default_model="qwen-coder-next",
            ),
            LocalOpenAIHarness(
                name="local2",
                role="reviewer",
                endpoint="http://127.0.0.1:8083",
                default_model="qwen38-27b",
            ),
        ],
        automatic_routing=True,
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="local2",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="review an explicit scope that exceeds strict prefetch bounds",
        goal=None,
        task_id=None,
        group_id=None,
        model="qwen38-27b",
        reasoning_effort="none",
        sandbox_mode="server-enforced-read-only-tools",
        commit_mode="forbidden",
        execution_timeout_seconds=300,
        depends_on_group_ids=(),
        files_in_scope=[f"file-{index}.txt" for index in range(6)],
        out_of_scope=[],
        context_files=[],
        acceptance_criteria=[],
        done_means=[],
        verification_commands=[],
        output_schema=None,
        parse_structured_output=True,
        request_fingerprint="reviewer-prefetch-skip-local",
        complexity="medium",
        routing_mode="complexity",
        routing_reason="complexity_medium_selected",
    )
    wrapped_prefetch_error = {
        "success": False,
        "status": "failed",
        "error": {
            "code": "local_openai_error",
            "message": (
                "transport wrapper: Reviewer strict mode could not prefetch the complete "
                "explicit Files in scope evidence. retryable"
            ),
        },
    }
    assert (
        registry._runtime_retry_reason(task=task, result=wrapped_prefetch_error)
        == "local_runtime_failure"
    )
    codex = registry.harnesses["codex"]
    fallback_calls: list[set[str]] = []

    def fake_fallback(current_task, *, excluded_harnesses=None):
        assert current_task is task
        excluded = set(excluded_harnesses or ())
        fallback_calls.append(excluded)
        return "codex", codex

    monkeypatch.setattr(registry, "_select_runtime_fallback", fake_fallback)
    calls: list[str] = []

    def fake_start(*, delegate_task):
        calls.append(delegate_task.harness)
        if delegate_task.harness == "local2":
            return {
                "success": False,
                "status": "failed",
                "harness": "local2",
                "model": "qwen38-27b",
                "error": {
                    "code": "local_openai_error",
                    "message": (
                        "Reviewer strict mode could not prefetch the complete explicit "
                        "Files in scope evidence."
                    ),
                },
            }
        return {
            "success": True,
            "status": "succeeded",
            "harness": "codex",
            "model": delegate_task.model,
            "structured_output": {
                "status": "succeeded",
                "findings": ["authoritative finding"],
            },
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)

    result = registry._run_scheduled_task(task)

    assert calls == ["local2", "codex"]
    assert fallback_calls == [{"local", "local2"}]
    assert result["success"] is True
    assert result["harness"] == "codex"
    attempt = result["routing_fallback"]["attempts"][0]
    assert attempt["trigger"] == "reviewer_prefetch_scope_failure"
    assert attempt["from_harness"] == "local2"
    assert attempt["to_harness"] == "codex"
    assert task.routing_reason == "reviewer_prefetch_scope_failure_to_codex"


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
                role="reviewer",
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
    medium_name, _medium_adapter = registry._select_runtime_fallback(medium)
    assert medium_name == "local"

    low = type("Task", (), {"complexity": "low", "harness": "local2", "kind": "explore"})()
    low_name, _low_adapter = registry._select_runtime_fallback(low)
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
    assert info["display_name"] == "Scout"
    assert info["role"] == "scout"
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
    assert result["metadata"]["runtime_profile"] == "scout-bounded"
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


def test_reviewer_prefetch_pages_complete_multiline_file(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_text(
        "\n".join(f"VALUE_{index} = {index}" for index in range(730)) + "\n",
        encoding="utf-8",
    )

    prefetched = local_agent._prefetch_review_scope(tmp_path, ("target.py",))

    assert prefetched is not None
    evidence, trace = prefetched
    assert "1: VALUE_0 = 0" in evidence
    assert "730: VALUE_729 = 729" in evidence
    assert trace == ["read_text", "read_text"]


def test_reviewer_prefetch_preserves_splitlines_line_numbers(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_text("ALPHA\u2028BETA\u2029GAMMA\n", encoding="utf-8")

    prefetched = local_agent._prefetch_review_scope(tmp_path, ("target.py",))

    assert prefetched is not None
    evidence, trace = prefetched
    assert "1: ALPHA\n2: BETA\n3: GAMMA" in evidence
    assert trace == ["read_text"]


def test_reviewer_prefetch_rejects_oversized_single_line(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    target.write_text(
        "X" * (local_agent.REVIEWER_PREFETCH_MAX_CHARS + 1000),
        encoding="utf-8",
    )

    assert local_agent._prefetch_review_scope(tmp_path, ("target.py",)) is None


def test_reviewer_prefetch_accepts_wide_scope_within_memory_budget(tmp_path: Path) -> None:
    paths: list[str] = []
    for index in range(6):
        path = tmp_path / f"scope-{index}.txt"
        path.write_text((f"VALUE_{index} = 1\n" * 1500), encoding="utf-8")
        paths.append(path.name)

    prefetched = local_agent._prefetch_review_scope(tmp_path, tuple(paths))

    assert prefetched is not None
    evidence, trace = prefetched
    assert all(f'<repository_evidence path="scope-{index}.txt">' in evidence for index in range(6))
    assert len(evidence) > 80_000
    assert len(evidence) < local_agent.REVIEWER_PREFETCH_MAX_CHARS
    assert len(trace) >= 12


def test_reviewer_prefetch_still_rejects_too_many_files(tmp_path: Path) -> None:
    paths: list[str] = []
    for index in range(local_agent.REVIEWER_PREFETCH_MAX_FILES + 1):
        path = tmp_path / f"scope-{index}.txt"
        path.write_text("VALUE = 1\n", encoding="utf-8")
        paths.append(path.name)

    assert local_agent._prefetch_review_scope(tmp_path, tuple(paths)) is None


def test_prefetched_review_keeps_legacy_cap_when_context_is_unknown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.py"
    target.write_text("VALUE = 1\n" * 10000, encoding="utf-8")
    prompt = "Local agent role:\nreviewer\n\nFiles in scope:\n- target.py\n\nTask:\nreview"
    profile = local_agent.resolve_runtime_profile(
        model="qwen38-27b",
        prompt=prompt,
        timeout_seconds=120,
        max_turns=4,
        max_tokens=1200,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=12,
    )
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: pytest.fail(
            "unknown-context wide prefetch must fail closed before inference"
        ),
    )

    result = local_agent._run_prefetched_review(
        root=tmp_path,
        endpoint="http://local.invalid:8084",
        model="qwen38-27b",
        prompt=prompt,
        profile=profile,
        allowed_scope_paths=("target.py",),
        timeout_seconds=120,
        context_size=None,
        slot_id=None,
        slot_count=None,
        reasoning_effort=None,
    )

    assert result is None


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
                                    "transition_inventory": [
                                        {
                                            "location": "target.py:1",
                                            "operation": "example mutation",
                                            "failure_disposition": "returns without repair",
                                            "state_confirmation": "",
                                        }
                                    ],
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
                                            "verdict": "rejected",
                                            "rollback_safety": "violated",
                                            "mutual_exclusion": "preserved",
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
        prompt="Files in scope:\n- target.py\n\nTask:\nreview rollback state transition safety",
        max_turns=1,
    )

    assert result["manifest"]["summary"] == (
        "Verified 1 concrete finding(s) from 1 review candidate(s)."
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
    assert payloads[0]["json_schema"] == local_agent.REVIEW_SCOUT_SCHEMA
    assert set(local_agent.REVIEW_SCOUT_SCHEMA["required"]) == {
        "transition_inventory",
        "hypotheses",
    }
    assert payloads[1]["chat_template_kwargs"]["enable_thinking"] is True
    assert payloads[1]["cache_prompt"] is True
    assert payloads[1]["chat_template_kwargs"]["reasoning_effort"] == "medium"
    assert (
        payloads[1]["reasoning_budget_tokens"]
        == local_agent.REVIEWER_REASONING_BUDGET_TOKENS
    )
    assert payloads[1]["response_format"] == {"type": "json_object"}
    assert payloads[1]["json_schema"] == local_agent.REVIEW_VERIFIER_SCHEMA
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
    assert "terminal owner state is left unverified" in scout_user
    assert "without either restoring the previous owner or re-verifying the owner state" in scout_user
    assert "different callers do not consume multiple slots for the same defect" in scout_user
    assert "Prefer a direct transition-request failure over derivative" in scout_user
    assert "transition_inventory is mandatory coverage" in scout_user
    assert "CONFIRM the unverified terminal owner state as the defect" in verifier_user
    assert "refusing to guess which remote state actually resulted" in verifier_user
    assert "transition_inventory" not in verifier_user
    assert result["metadata"]["transition_inventory"] == [
        {
            "location": "target.py:1",
            "operation": "example mutation",
            "failure_disposition": "returns without repair",
            "state_confirmation": "",
        }
    ]
    assert result["metadata"]["verifier_candidate_source"] == "transition_inventory"
    assert len(result["metadata"]["verifier_candidates"]) == 1
    assert result["metadata"]["verifier_reviews"][0]["model_verdict"] == "rejected"
    assert result["metadata"]["verifier_reviews"][0]["verdict"] == "confirmed"
    assert result["metadata"]["scout_max_tokens"] == local_agent.REVIEWER_SCOUT_MAX_TOKENS
    assert result["metadata"]["verifier_max_tokens"] == payloads[1]["max_tokens"]
    assert result["metadata"]["rejection_audit_max_tokens"] == 0
    assert result["metadata"]["verified_candidates"] == 1
    assert result["metadata"]["rejected_candidates"] == 0
    assert result["metadata"]["uncertain_candidates"] == 0


def test_qwen38_prefetched_review_skips_verifier_when_scout_finds_no_candidates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    payloads: list[dict[str, object]] = []

    def fake_request(_url: str, payload: dict[str, object] | None, *, timeout_seconds: float):
        assert payload is not None
        payloads.append(payload)
        if len(payloads) > 1:
            pytest.fail("zero-candidate non-state review must not call the verifier")
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "transition_inventory": [],
                                "hypotheses": [],
                            }
                        ),
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
            },
        }

    monkeypatch.setattr(local_agent, "_json_request", fake_request)

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8084",
        model="qwen38-27b",
        cwd=tmp_path,
        prompt="Files in scope:\n- target.py\n\nTask:\nreview correctness",
        max_turns=1,
    )

    assert len(payloads) == 1
    assert result["manifest"]["status"] == "succeeded"
    assert result["manifest"]["findings"] == []
    assert "verifier skipped" in result["manifest"]["verification"].lower()
    assert result["metadata"]["turns"] == 1
    assert result["metadata"]["finalization_turns"] == 0
    assert result["metadata"]["verifier_skipped_reason"] == "no_candidates"
    assert result["metadata"]["usage"] == {
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 110,
    }


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
            "Reviewer scout stage exhausted its generation budget",
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
            "Reviewer scout stage returned invalid JSON",
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


def test_qwen38_state_review_requires_nonempty_transition_inventory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "transition_inventory": [],
                                "hypotheses": [],
                            }
                        ),
                    },
                }
            ]
        },
    )

    with pytest.raises(RuntimeError, match="no mandatory transition inventory"):
        local_agent.run_agent(
            endpoint="http://local.invalid:8084",
            model="qwen38-27b",
            cwd=tmp_path,
            prompt=(
                "Files in scope:\n- target.py\n\n"
                "Task:\nReview rollback safety and state transitions."
            ),
            max_turns=1,
        )


@pytest.mark.parametrize(
    "bad_item",
    [
        {
            "location": {},
            "operation": [],
            "failure_disposition": 1,
            "state_confirmation": "",
        },
        {
            "location": "target.py:1",
            "operation": "suspend",
            "failure_disposition": "returns",
            "state_confirmation": "",
            "unexpected": "field",
        },
        {
            "location": "x" * 161,
            "operation": "suspend",
            "failure_disposition": "returns",
            "state_confirmation": "",
        },
    ],
)
def test_qwen38_state_review_rejects_malformed_transition_inventory(
    monkeypatch,
    tmp_path: Path,
    bad_item: dict[str, object],
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    monkeypatch.setattr(
        local_agent,
        "_json_request",
        lambda *_args, **_kwargs: {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "transition_inventory": [
                                    bad_item
                                ],
                                "hypotheses": [],
                            }
                        ),
                    },
                }
            ]
        },
    )

    with pytest.raises(RuntimeError, match="malformed transition inventory"):
        local_agent.run_agent(
            endpoint="http://local.invalid:8084",
            model="qwen38-27b",
            cwd=tmp_path,
            prompt=(
                "Files in scope:\n- target.py\n\n"
                "Task:\nReview rollback safety and state transitions."
            ),
            max_turns=1,
        )


def test_non_state_review_schema_allows_only_not_applicable_dimensions() -> None:
    schema = local_agent._review_schema_for_task(
        local_agent.REVIEW_VERIFIER_SCHEMA,
        state_transition_review=False,
    )
    properties = schema["properties"]["reviews"]["items"]["properties"]

    assert properties["rollback_safety"]["enum"] == ["not_applicable"]
    assert properties["mutual_exclusion"]["enum"] == ["not_applicable"]
    assert local_agent.REVIEW_VERIFIER_SCHEMA["properties"]["reviews"]["items"]["properties"][
        "rollback_safety"
    ]["enum"] != ["not_applicable"]


def test_qwen38_non_state_review_rejects_state_specific_dimensions(
    monkeypatch,
    tmp_path: Path,
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
                                    "transition_inventory": [],
                                    "hypotheses": [
                                        {
                                            "title": "candidate race",
                                            "claim": "stale state can overwrite newer state",
                                            "failure_scenario": "A reads old; B writes new; A overwrites new",
                                            "wrong_outcome": "new state is lost",
                                        }
                                    ],
                                }
                            ),
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "reviews": [
                                        {
                                            "candidate_index": 0,
                                            "verdict": "confirmed",
                                            "rollback_safety": "violated",
                                            "mutual_exclusion": "preserved",
                                            "reason": "proven",
                                            "finding": "race",
                                            "contradiction_quote": "",
                                        }
                                    ]
                                }
                            ),
                        },
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

    with pytest.raises(RuntimeError, match="non-state verifier returned state-specific dimensions"):
        local_agent.run_agent(
            endpoint="http://local.invalid:8084",
            model="qwen38-27b",
            cwd=tmp_path,
            prompt="Files in scope:\n- target.py\n\nTask:\nReview concurrency correctness.",
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
            "Reviewer verifier stage exhausted its generation budget",
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
            "Reviewer verifier stage returned invalid JSON",
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
                                    "transition_inventory": [],
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
                                    "transition_inventory": [],
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
        match="exactly one review per review candidate",
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
                                    "transition_inventory": [],
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
                                            "rollback_safety": "not_applicable",
                                            "mutual_exclusion": "not_applicable",
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
    assert result["metadata"]["rejected_candidates"] == 0
    assert result["metadata"]["uncertain_candidates"] == 1
    assert result["metadata"]["verifier_reviews"][0]["verdict"] == "uncertain"
    assert result["metadata"]["verifier_reviews"][0]["model_verdict"] == "rejected"


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
                                    "transition_inventory": [],
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
                                            "rollback_safety": "not_applicable",
                                            "mutual_exclusion": "not_applicable",
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
                                            "rollback_safety": "not_applicable",
                                            "mutual_exclusion": "not_applicable",
                                            "reason": "the quoted line does not prevent the proposed ordering",
                                            "defect_present": True,
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
    assert result["metadata"]["verified_candidates"] == 1
    assert result["metadata"]["rejected_candidates"] == 0
    assert result["metadata"]["uncertain_candidates"] == 0
    assert result["metadata"]["verifier_reviews"][0]["model_verdict"] == "rejected"
    assert result["metadata"]["verifier_reviews"][0]["rollback_safety"] == "not_applicable"
    assert result["metadata"]["verifier_reviews"][0]["mutual_exclusion"] == "not_applicable"
    assert result["metadata"]["usage"] == {
        "input_tokens": 360,
        "output_tokens": 60,
        "total_tokens": 420,
    }
    assert payloads[2]["json_schema"] == local_agent._review_schema_for_task(
        local_agent.REVIEW_REJECTION_AUDIT_SCHEMA,
        state_transition_review=False,
    )
    assert payloads[2]["cache_prompt"] is True
    assert (
        payloads[2]["reasoning_budget_tokens"]
        == local_agent.REVIEWER_REASONING_BUDGET_TOKENS
    )
    assert payloads[0]["messages"][0]["content"] == payloads[2]["messages"][0]["content"]
    scout_user = str(payloads[0]["messages"][1]["content"])
    audit_user = str(payloads[2]["messages"][1]["content"])
    assert scout_user.startswith("<stage_instructions>")
    assert audit_user.startswith("<stage_instructions>")
    assert scout_user != audit_user
    assert result["metadata"]["rejection_audit_reviews"][0]["verdict"] == "rejection_unsound"
    assert result["metadata"]["rejection_audit_reviews"][0]["defect_present"] is True


def test_rejection_audit_cannot_overturn_rejection_while_denying_defect(
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
                                    "transition_inventory": [],
                                    "hypotheses": [
                                        {
                                            "title": "candidate race",
                                            "claim": "stale state can overwrite newer state",
                                            "failure_scenario": "A reads old; B clears; A writes old",
                                            "wrong_outcome": "cleared state is reinserted",
                                        }
                                    ],
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
                                            "rollback_safety": "not_applicable",
                                            "mutual_exclusion": "not_applicable",
                                            "reason": "the guarded lookup sees no current entry after clear",
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
                                            "rollback_safety": "not_applicable",
                                            "mutual_exclusion": "not_applicable",
                                            "reason": "no incorrect state or wrong outcome occurs",
                                            "defect_present": False,
                                            "corrected_finding": "No TOCTOU defect occurs.",
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

    assert result["manifest"]["findings"] == []
    assert result["metadata"]["verified_candidates"] == 0
    assert result["metadata"]["rejected_candidates"] == 1
    audit = result["metadata"]["rejection_audit_reviews"][0]
    assert audit["model_verdict"] == "rejection_unsound"
    assert audit["verdict"] == "rejection_sound"
    assert audit["defect_present"] is False
    assert "did not consistently assert a concrete defect" in audit["reason"]


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
                            "content": json.dumps(
                                {"transition_inventory": [], "hypotheses": hypotheses}
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
                                            "candidate_index": index,
                                            "verdict": "confirmed",
                                            "rollback_safety": "not_applicable",
                                            "mutual_exclusion": "not_applicable",
                                            "reason": "proven",
                                            "finding": f"finding {index // 2}",
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
    ]
    assert result["metadata"]["verified_candidates"] == 4
    assert "4 confirmed" in result["manifest"]["verification"]
    assert payloads[1]["max_tokens"] == 3800


def test_qwen38_tool_loop_fallback_keeps_tools_schema_for_prompt_reuse(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text(
        "".join(f"VALUE_{index} = {index}\n" for index in range(15000)),
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
    assert payloads[1]["max_tokens"] == local_agent.REVIEWER_FINALIZATION_MAX_TOKENS
    assert result["metadata"]["max_tokens"] == 1200
    assert (
        result["metadata"]["finalization_max_tokens"]
        == local_agent.REVIEWER_FINALIZATION_MAX_TOKENS
    )


def test_reviewer_finalization_budget_respects_small_context_ceiling(
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
                            "content": json.dumps(_manifest("small-context-ok")),
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
    monkeypatch.setattr(local_agent, "_chat_prompt_token_count", lambda **_kwargs: 100)

    result = local_agent.run_agent(
        endpoint="http://local.invalid:8084",
        model="qwen38-27b",
        cwd=tmp_path,
        prompt="Inspect target.py and summarize the evidence.",
        max_turns=1,
        context_size=2048,
    )

    assert result["manifest"]["summary"] == "small-context-ok"
    assert payloads[0]["max_tokens"] == 1200
    assert payloads[1]["max_tokens"] == 1280
    assert result["metadata"]["finalization_max_tokens"] == 1280


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

def test_scout_bounded_runtime_profile_is_short_and_wide() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt="Inspect routing configuration and summarize the relevant settings.",
        timeout_seconds=30,
        max_turns=8,
        max_tokens=900,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=24,
    )

    assert profile.name == "scout-bounded"
    assert profile.role == "scout"
    assert profile.timeout_seconds == 60
    assert profile.max_turns == 4
    assert profile.max_tokens == 1000
    assert profile.max_tool_calls_per_turn == 8
    assert profile.max_tool_calls_total == 16
    assert profile.context_window_tokens == 16384


def test_scout_quick_profile_ignores_wrapper_architecture_boilerplate() -> None:
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

    assert profile.name == "scout-quick"
    assert profile.role == "scout"
    assert profile.max_turns == 3
    assert profile.max_tokens == 800
    assert profile.context_window_tokens == 12288


def test_scout_quick_profile_exposes_only_search_and_read_text() -> None:
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


def test_scout_deep_runtime_profile_allows_broader_exploration() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt=(
            "Local agent role:\nscout\n\n"
            "Architecture review: explain exactly why routing crosses modules."
        ),
        timeout_seconds=30,
        max_turns=8,
        max_tokens=900,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=24,
    )

    assert profile.name == "scout-deep"
    assert profile.timeout_seconds == 90
    assert profile.max_turns == 6
    assert profile.max_tokens == 1400
    assert profile.max_tool_calls_per_turn == 8
    assert profile.max_tool_calls_total == 32
    assert profile.context_window_tokens == 28672


def test_medium_complexity_without_explicit_role_keeps_scout_deep_profile() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt="Task complexity:\nmedium\nTask:\nInspect this bounded integration slice.",
        timeout_seconds=30,
        max_turns=8,
        max_tokens=900,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=24,
    )

    assert profile.name == "scout-deep"
    assert profile.max_turns == 6
    assert profile.context_window_tokens == 28672


def test_explicit_scout_role_overrides_review_wording() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt=(
            "Local agent role:\nscout\n\n"
            "Check pagination correctness, starvation freedom, and mutation while iterating."
        ),
        timeout_seconds=30,
        max_turns=8,
        max_tokens=1000,
        max_tool_calls_per_turn=4,
        max_tool_calls_total=24,
    )

    assert profile.name == "scout-deep"
    assert profile.role == "scout"


def test_coder_next_reviewer_role_uses_strict_review_profile() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt=(
            "Local agent role:\nreviewer\n\n"
            "Files in scope:\n- quota_admission.py\n\n"
            "Task:\nReview concurrency correctness."
        ),
        timeout_seconds=30,
        max_turns=8,
        max_tokens=900,
        max_tool_calls_per_turn=8,
        max_tool_calls_total=24,
    )

    assert profile.name == "reviewer-strict"
    assert profile.role == "reviewer"
    assert profile.timeout_seconds == 120
    assert profile.max_turns == 4
    assert profile.max_tokens == 1000
    assert profile.context_window_tokens is None


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


def test_explicit_reviewer_role_is_model_agnostic() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="future-local-model",
        prompt="Local agent role:\nreviewer\n\nTask:\nReview bounded correctness.",
        timeout_seconds=30,
        max_turns=8,
        max_tokens=900,
        max_tool_calls_per_turn=8,
        max_tool_calls_total=24,
    )

    assert profile.name == "reviewer-strict"
    assert profile.role == "reviewer"


def test_explicit_reviewer_without_file_scope_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(
        RuntimeError,
        match="Reviewer strict mode requires at least one explicit file in Files in scope",
    ):
        local_agent.run_agent(
            endpoint="http://local.invalid:8081",
            model="future-local-model",
            cwd=tmp_path,
            prompt=(
                "Local agent role:\nreviewer\n\n"
                "Task:\nReview bounded correctness."
            ),
        )


def test_explicit_reviewer_prefetch_refusal_fails_closed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "target.py").write_text("VALUE = 42\n", encoding="utf-8")
    monkeypatch.setattr(local_agent, "_run_prefetched_review", lambda **_kwargs: None)

    with pytest.raises(
        RuntimeError,
        match="Reviewer strict mode could not prefetch the complete explicit Files in scope evidence",
    ):
        local_agent.run_agent(
            endpoint="http://local.invalid:8081",
            model="future-local-model",
            cwd=tmp_path,
            prompt=(
                "Local agent role:\nreviewer\n\n"
                "Files in scope:\n- target.py\n\n"
                "Task:\nReview bounded correctness."
            ),
        )


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


def test_reviewer_strict_profile_is_bounded_but_keeps_discovered_context() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen38-27b",
        prompt="Architecture review: inspect rollback safety.",
        timeout_seconds=45,
        max_turns=8,
        max_tokens=1400,
        max_tool_calls_per_turn=8,
        max_tool_calls_total=24,
    )

    assert profile.name == "reviewer-strict"
    assert profile.role == "reviewer"
    assert profile.timeout_seconds == 120
    assert profile.max_turns == 4
    assert profile.max_tokens == 1200
    assert profile.max_tool_calls_per_turn == 4
    assert profile.max_tool_calls_total == 12
    assert profile.context_window_tokens is None


def test_reviewer_model_fact_task_uses_general_profile() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen38-27b",
        prompt=(
            "Files in scope:\n- scripts/install_local_reviewer_gpu_mode.py\n\n"
            "Task:\nRead the file and report the model basename, context, slots, and port."
        ),
        timeout_seconds=45,
        max_turns=8,
        max_tokens=1400,
        max_tool_calls_per_turn=8,
        max_tool_calls_total=24,
    )

    assert profile.name == "reviewer-general"
    assert profile.role == "reviewer"
    assert "careful local read-only repository reviewer" in local_agent._system_prompt(profile)
    assert "Reviewer strict mode" not in local_agent._system_prompt(profile)


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
