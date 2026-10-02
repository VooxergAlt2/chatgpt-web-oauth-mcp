from __future__ import annotations

import io
import json
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
    monkeypatch.setattr(local_agent.tempfile, "gettempdir", lambda: str(tmp_path))

    with local_agent.acquire_local_slot(
        endpoint="http://local.invalid:8081",
        model="qwen-coder-next",
        timeout_seconds=1,
    ) as first:
        assert first == local_agent.LocalSlotLease(slot_id=0, slot_count=2)
        with local_agent.acquire_local_slot(
            endpoint="http://local.invalid:8081",
            model="qwen-coder-next",
            timeout_seconds=1,
        ) as second:
            assert second == local_agent.LocalSlotLease(slot_id=1, slot_count=2)

    with local_agent.acquire_local_slot(
        endpoint="http://local.invalid:8081",
        model="qwen-coder-next",
        timeout_seconds=1,
    ) as reused:
        assert reused == local_agent.LocalSlotLease(slot_id=0, slot_count=2)


def test_non_coder_next_slot_lease_does_not_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        local_agent,
        "_discover_parallel_slots",
        lambda *_args, **_kwargs: pytest.fail("unexpected slot probe"),
    )

    with local_agent.acquire_local_slot(
        endpoint="http://local.invalid:8081",
        model="qwen-local",
        timeout_seconds=1,
    ) as lease:
        assert lease == local_agent.LocalSlotLease(slot_id=None, slot_count=None)


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
        },
    )()
    invocation = harness.build_invocation(task)
    assert invocation.read_only_enforced is True
    assert invocation.args[1:3] == ["-m", "chatgpt_web_oauth_mcp.local_openai_delegate"]
    assert "--endpoint" in invocation.args
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
                "usage": {"total_tokens": 50},
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
                "usage": {"total_tokens": 70},
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
    assert metadata["usage"]["total_tokens"] == 120
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
    assert route["reason"] == "primary_unavailable_balanced_fallback"
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


def test_local_agent_rejects_excessive_tool_calls_per_turn(
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

    with pytest.raises(RuntimeError, match="per-turn tool-call limit"):
        local_agent.run_agent(
            endpoint="http://local.invalid:8081",
            model="qwen-local",
            cwd=tmp_path,
            prompt="inspect",
            max_turns=1,
        )


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


def test_local_agent_rejects_excessive_total_tool_calls(
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

    with pytest.raises(RuntimeError, match="total tool-call limit"):
        local_agent.run_agent(
            endpoint="http://local.invalid:8081",
            model="qwen-local",
            cwd=tmp_path,
            prompt="inspect",
            max_turns=4,
            max_tool_calls_per_turn=2,
            max_tool_calls_total=2,
        )


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
        max_turns=2,
    )

    assert result["manifest"]["summary"] == "verified"
    assert payloads[0]["tool_choice"] == "auto"
    assert "tools" in payloads[0]
    assert payloads[1]["tool_choice"] == "none"
    assert "tools" not in payloads[1]
    assert "Final turn" in payloads[1]["messages"][-1]["content"]


def test_coder_next_bounded_runtime_profile_is_short_and_wide() -> None:
    profile = local_agent.resolve_runtime_profile(
        model="qwen-coder-next",
        prompt="Find the exact default value in config.py.",
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


def test_local_manifest_normalizes_scalar_string_lists() -> None:
    manifest = _manifest()
    manifest["commands_run"] = "read_text"
    manifest["findings"] = "Found exact evidence."
    manifest["blockers"] = ""

    normalized = local_agent._normalize_manifest(manifest)

    assert normalized["commands_run"] == ["read_text"]
    assert normalized["findings"] == ["Found exact evidence."]
    assert normalized["blockers"] == []
