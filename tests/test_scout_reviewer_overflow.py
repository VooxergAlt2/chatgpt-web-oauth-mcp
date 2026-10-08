from __future__ import annotations

from types import SimpleNamespace

import pytest

import chatgpt_web_oauth_mcp.delegate_harnesses as harnesses
import chatgpt_web_oauth_mcp.executors as executors
from chatgpt_web_oauth_mcp.delegate_harnesses import LocalOpenAIHarness
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.local_openai_delegate import resolve_runtime_profile


def _registry(monkeypatch, tmp_path, *, reviewer_online=True, planner_mode="off"):
    monkeypatch.setattr(harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    monkeypatch.setattr(
        harnesses,
        "probe_endpoint",
        lambda url, **_kwargs: (
            (True, None, {"status": "ok"})
            if reviewer_online or "8083" not in url
            else (False, "connection refused", None)
        ),
    )
    return ExecutorRegistry(
        codex_command=None,
        harnesses=[
            LocalOpenAIHarness(name="local", role="scout", endpoint="http://localhost:8081", default_model="qwen-coder-next"),
            LocalOpenAIHarness(name="local2", role="reviewer", endpoint="http://localhost:8083", default_model="qwen38-27b"),
        ],
        primary_harness="local",
        fallback_harnesses=("local2",),
        automatic_routing=True,
        planner_mode=planner_mode,
        max_explore_per_harness={"local": 2, "local2": 1},
        durable_harnesses=(),
        delegate_state_root=tmp_path / "state",
    )


@pytest.mark.parametrize("busy_scout, busy_reviewer, online, expected", [
    (0, 0, True, False),
    (1, 0, True, False),
    (2, 0, True, True),
    (3, 0, True, True),
    (2, 1, True, False),
    (2, 0, False, False),
])
def test_overflow_requires_saturated_scout_and_free_healthy_reviewer(
    monkeypatch, tmp_path, busy_scout, busy_reviewer, online, expected,
):
    registry = _registry(monkeypatch, tmp_path, reviewer_online=online)
    tasks = [SimpleNamespace(harness="local", kind="explore") for _ in range(busy_scout)]
    tasks += [SimpleNamespace(harness="local2", kind="explore") for _ in range(busy_reviewer)]
    monkeypatch.setattr(registry.scheduler, "nonterminal_tasks", lambda: tasks)
    result = registry._scout_overflow_reviewer()
    assert (result is not None) is expected
    if expected:
        assert result["reviewer"]["harness"] == "local2"
        assert result["scout_limit"] == 2


@pytest.mark.parametrize("planner_mode", ["off", "active"])
def test_automatic_low_discovery_uses_reviewer_for_busy_scout(
    monkeypatch, tmp_path, planner_mode,
):
    registry = _registry(monkeypatch, tmp_path, planner_mode=planner_mode)
    monkeypatch.setattr(
        registry, "_scout_overflow_reviewer",
        lambda: {
            "reviewer": {"harness": "local2", "eligible": True, "quota": None},
            "scout_load": 2, "scout_limit": 2,
        },
    )
    def fake_run(task):
        return {
            "success": True, "status": "succeeded", "harness": task.harness,
            "model": task.model, "structured_output": {"status": "succeeded", "summary": "done"},
        }
    monkeypatch.setattr(registry, "_run_scheduled_task", fake_run)
    result = registry.run_delegate(
        cwd=tmp_path, kind="explore", task="Find the relevant source entrypoints",
        wait_seconds=5,
    )
    assert result["harness"] == "local2"
    assert result["routing_reason"] == "scout_busy_reviewer_overflow"
    prompt = result["logs"]["prompt"]
    text = open(prompt, encoding="utf-8").read()
    assert "Local agent role:\nscout\n" in text
    profile = resolve_runtime_profile(
        model="qwen38-27b", prompt=text, timeout_seconds=120,
        max_turns=4, max_tokens=1200, max_tool_calls_per_turn=4, max_tool_calls_total=12,
    )
    assert profile.name == "reviewer-general"


def test_explicit_scout_remains_pinned_when_busy(monkeypatch, tmp_path):
    registry = _registry(monkeypatch, tmp_path)
    monkeypatch.setattr(registry, "_scout_overflow_reviewer", lambda: pytest.fail("explicit routing must not overflow"))
    monkeypatch.setattr(registry, "_run_scheduled_task", lambda task: {
        "success": True, "status": "succeeded", "harness": task.harness,
    })
    result = registry.run_delegate(
        cwd=tmp_path, kind="explore", harness="local", task="Locate a symbol", wait_seconds=5,
    )
    assert result["harness"] == "local"


def test_automatic_discovery_batch_uses_reviewer_when_scout_busy(monkeypatch, tmp_path):
    registry = _registry(monkeypatch, tmp_path)
    monkeypatch.setattr(
        registry, "_scout_overflow_reviewer",
        lambda: {"reviewer": {"eligible": True}, "scout_load": 2, "scout_limit": 2},
    )
    monkeypatch.setattr(registry, "_run_scheduled_task", lambda task: {
        "success": True, "status": "succeeded", "harness": task.harness,
    })
    result = registry.run_delegate_batch(
        cwd=tmp_path, tasks=[{"task": "Find source entry points"}], wait_seconds=5,
    )
    assert result["success"] is True
    child = result["children"][0]
    assert child["harness"] == "local2"
    child_task = registry.scheduler.get_task(child["delegate_id"])
    assert child_task.routing_reason == "scout_busy_reviewer_overflow"
    assert "Local agent role:\nscout\n" in child_task.prompt
