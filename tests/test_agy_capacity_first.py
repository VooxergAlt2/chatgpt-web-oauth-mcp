"""Capacity-first AGY account routing, independent of heavy-model escalation."""
from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

import chatgpt_web_oauth_mcp.executors as module
import chatgpt_web_oauth_mcp.delegate_harnesses as harness_module
from chatgpt_web_oauth_mcp.delegate_harnesses import GenericCliHarness
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


class ModelQuota:
    def __init__(self, *, deny_claude: bool = False, deny_agy1: bool = False):
        self.deny_claude = deny_claude
        self.deny_agy1 = deny_agy1

    def decision(self, *, harness: str, model: str | None, fresh: bool = False) -> dict:
        denied = (
            self.deny_claude and str(model or "").startswith("claude-")
            or self.deny_agy1 and harness == "antigravity"
        )
        return {
            "allowed": not denied, "harness": harness, "model": model,
            "provider_status": "ok",
            "reason": "quota_threshold_reached" if denied else None,
            "buckets": [],
        }

    def note_terminal(self, _result: dict) -> None:
        pass


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ExecutorRegistry:
    monkeypatch.setattr(module, "_command_available", lambda _command: True)
    monkeypatch.setattr(harness_module, "command_available", lambda _command: True)
    monkeypatch.setattr(
        harness_module, "codex_read_only_sandbox_available",
        lambda _command: True,
    )
    harnesses = [
        GenericCliHarness(
            name=name, command="true", explore_command="true",
        )
        for name in ("antigravity", "antigravity2")
    ]
    return ExecutorRegistry(
        codex_command="codex",
        harnesses=harnesses,
        default_harness="antigravity",
        primary_harness="local",
        fallback_harnesses=("local2", "antigravity", "antigravity2", "codex"),
        automatic_routing=True,
        agy_capacity_first=True,
        planner_mode="active",
        max_explore_per_project=10,
        max_explore_global=12,
        max_active_per_harness={"antigravity": 4, "antigravity2": 4},
        max_explore_per_harness={"antigravity": 4, "antigravity2": 4},
        max_code_per_project=3,
        max_code_global=5,
        durable_harnesses=(),
        allow_unsafe_explore_command=True,
        quota_admission_gate=ModelQuota(),
        delegate_state_root=tmp_path / "state",
    )


def test_low_and_medium_code_require_only_agy_accounts(registry: ExecutorRegistry) -> None:
    for kind in ("explore", "code"):
        for complexity, model in (
            ("low", "gemini-3.8-flash-medium"),
            ("medium", "gemini-3.8-flash-high"),
        ):
            harness, _, _, route = registry._select_harness(
                harness=None, kind=kind, model=None, complexity=complexity,
            )
            assert harness == "antigravity"
            assert route["preferred_model"] == model
            assert route["reason"] == "agy_capacity_first"
            assert {row["harness"] for row in route["candidates"]} == {
                "antigravity", "antigravity2",
            }
    guidance = registry.routing_guidance()
    assert guidance["planner"]["active_route_selection"] is False
    assert guidance["policy"]["agy_capacity_first"] is True


def test_fill_agy1_then_agy2_then_queue_without_codex(
    registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    release = Event()

    def run(*, delegate_task):
        assert release.wait(5)
        return {"success": True, "status": "succeeded",
                "delegate_id": delegate_task.delegate_id}

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", run)
    try:
        for number in range(4):
            cwd = tmp_path / f"first_{number}"
            cwd.mkdir()
            result = registry.run_delegate(
                task=f"read-only scout {number}", kind="explore", cwd=cwd,
                harness="antigravity", wait_seconds=0,
            )
            assert result["status"] == "running"
        assert registry.scheduler.harness_capacity("antigravity") == {
            "active": 4, "limit": 4,
        }
        next_harness, _, _, route = registry._select_harness(
            harness=None, kind="explore", model=None, complexity="low",
        )
        assert next_harness == "antigravity2"
        assert route["reason"] == "agy_capacity_first"

        for number in range(4):
            cwd = tmp_path / f"second_{number}"
            cwd.mkdir()
            result = registry.run_delegate(
                task=f"second-account scout {number}", kind="explore", cwd=cwd,
                harness="antigravity2", wait_seconds=0,
            )
            assert result["status"] == "running"
        assert registry.scheduler.harness_capacity("antigravity2")["active"] == 4
        next_harness, _, _, route = registry._select_harness(
            harness=None, kind="code", model=None, complexity="medium",
        )
        assert next_harness == "antigravity"
        assert route["reason"] == "agy_capacity_wait"
        assert all(row["harness"] != "codex" for row in route["candidates"])
        assert all(
            row["reason"] == "account_capacity_full"
            for row in route["candidates"]
        )
        heavy, _, _, heavy_route = registry._select_harness(
            harness=None, kind="explore", model=None, complexity="high",
        )
        assert heavy == "antigravity"
        assert heavy_route["reason"] == "agy_capacity_wait"
        assert all(row["harness"] != "codex" for row in heavy_route["candidates"])
    finally:
        release.set()


def test_code_and_explore_share_one_account_slot_budget(
    registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    release = Event()
    monkeypatch.setattr(
        registry, "_start_codex_delegate_impl",
        lambda *, delegate_task: (
            release.wait(5) and {
                "success": True, "status": "succeeded",
                "delegate_id": delegate_task.delegate_id,
            }
        ),
    )
    try:
        for number, kind in enumerate(("code", "explore", "explore", "explore")):
            cwd = tmp_path / f"mixed_{number}"
            cwd.mkdir()
            result = registry.run_delegate(
                task=f"mixed task {number}", kind=kind, cwd=cwd,
                harness="antigravity", wait_seconds=0,
            )
            assert result["status"] == "running"
        assert registry.scheduler.harness_capacity("antigravity") == {
            "active": 4, "limit": 4,
        }
        assert registry._select_harness(
            harness=None, kind="code", model=None, complexity="low"
        )[0] == "antigravity2"
    finally:
        release.set()


def test_code_fallback_must_atomically_respect_agy2_capacity(
    registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    code_release, explore_release = Event(), Event()

    def run(*, delegate_task):
        gate = code_release if delegate_task.kind == "code" else explore_release
        assert gate.wait(5)
        return {
            "success": True, "status": "succeeded",
            "delegate_id": delegate_task.delegate_id,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", run)
    try:
        code_path = tmp_path / "writer"
        code_path.mkdir()
        response = registry.run_delegate(
            task="isolated code writer", kind="code", cwd=code_path,
            harness="antigravity", wait_seconds=0,
        )
        assert response["status"] == "running"
        writer = registry.scheduler.get_task(response["delegate_id"])
        assert writer is not None
        readers = []
        for i in range(4):
            path = tmp_path / f"agy2_reader_{i}"
            path.mkdir()
            result = registry.run_delegate(
                task=f"AGY2 reader {i}", kind="explore", cwd=path,
                harness="antigravity2", wait_seconds=0,
            )
            assert result["status"] == "running"
            readers.append(result["delegate_id"])
        assert registry.scheduler.harness_capacity("antigravity2")["active"] == 4
        assert not registry.scheduler.transition_running_code_harness(
            writer, "antigravity2"
        )
        assert writer.harness == "antigravity"

        explore_release.set()
        for delegate_id in readers:
            reader = registry.scheduler.get_task(delegate_id)
            assert reader is not None and reader.completed_event.wait(timeout=5)
        assert registry.scheduler.transition_running_code_harness(
            writer, "antigravity2"
        )
        assert writer.harness == "antigravity2"
        assert registry.scheduler.harness_capacity("antigravity")["active"] == 0
        assert registry.scheduler.harness_capacity("antigravity2")["active"] == 1
    finally:
        explore_release.set()
        code_release.set()


def test_heavy_task_uses_claude_not_a_capacity_overflow(
    registry: ExecutorRegistry,
) -> None:
    harness, _, _, route = registry._select_harness(
        harness=None, kind="code", model=None, complexity="high",
    )
    assert harness == "antigravity"
    assert route["task_tier"] == "claude"
    assert route["preferred_model"] == "claude-opus-4-6-thinking"


def test_claude_quota_exhaustion_uses_sol_medium(
    registry: ExecutorRegistry,
) -> None:
    registry.quota_admission_gate = ModelQuota(deny_claude=True)
    harness, _, _, route = registry._select_harness(
        harness=None, kind="explore", model=None, complexity="high",
    )
    assert harness == "codex"
    assert route["reason"] == "claude_unavailable_sol_medium"
    assert route["preferred_model"] == "gpt-6.1-sol"
    assert route["preferred_reasoning_effort"] == "medium"


def test_high_complexity_sol_fallback_retains_medium_effort(
    registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    registry.quota_admission_gate = ModelQuota(deny_claude=True)
    captured = []

    def run(*, delegate_task):
        captured.append(delegate_task)
        return {"success": True, "status": "succeeded",
                "delegate_id": delegate_task.delegate_id}

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", run)
    result = registry.run_delegate(
        task="Perform a high-risk code architecture audit.",
        kind="code", complexity="high", cwd=tmp_path, wait_seconds=3,
    )
    assert result["status"] == "succeeded"
    assert captured[0].harness == "codex"
    assert captured[0].model == "gpt-6.1-sol"
    assert captured[0].reasoning_effort == "medium"


def test_codex_manual_medium_and_xhigh_remain_distinct_cli_routes(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from chatgpt_web_oauth_mcp.delegate_harnesses import CodexHarness

    harness = CodexHarness(command="codex")
    for effort in ("medium", "xhigh"):
        task = SimpleNamespace(
            kind="code", model="gpt-6.1-sol", reasoning_effort=effort,
            cwd=tmp_path, prompt="Audit one bounded change",
            project=SimpleNamespace(git_common_dir=None),
        )
        invocation = harness.build_invocation(task)
        assert "--model" in invocation.args
        assert invocation.args[invocation.args.index("--model") + 1] == "gpt-6.1-sol"
        assert f'model_reasoning_effort="{effort}"' in invocation.args


def test_agy1_unavailable_routes_to_agy2_without_expensive_upgrade(
    registry: ExecutorRegistry,
) -> None:
    registry.quota_admission_gate = ModelQuota(deny_agy1=True)
    harness, _, _, route = registry._select_harness(
        harness=None, kind="code", model=None, complexity="low",
    )
    assert harness == "antigravity2"
    assert route["task_tier"] == "gemini"


def test_explicit_sol_and_resume_keep_original_account_semantics(
    registry: ExecutorRegistry,
) -> None:
    harness, _, _, route = registry._select_harness(
        harness="codex", kind="code", model="gpt-6.1-sol", complexity="high",
    )
    assert harness == "codex"
    assert route is None
    harness, _, _, route = registry._select_harness(
        harness="antigravity2", kind="explore",
        model="gemini-3.8-flash-high", complexity="low",
    )
    assert harness == "antigravity2"
    assert route is None
