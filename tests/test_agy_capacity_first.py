"""Capacity-first AGY account routing, independent of heavy-model escalation."""
from __future__ import annotations

from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

import chatgpt_web_oauth_mcp.executors as module
import chatgpt_web_oauth_mcp.delegate_harnesses as harness_module
from chatgpt_web_oauth_mcp.delegate_harnesses import GenericCliHarness
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.quota_admission import DelegateQuotaAdmissionGate


class ModelQuota:
    def __init__(
        self, *, deny_claude: bool = False, deny_agy1: bool = False,
        deny_gemini: bool = False,
    ):
        self.deny_claude = deny_claude
        self.deny_agy1 = deny_agy1
        self.deny_gemini = deny_gemini

    def decision(self, *, harness: str, model: str | None, fresh: bool = False) -> dict:
        denied = (
            self.deny_claude and str(model or "").startswith("claude-")
            or self.deny_agy1 and harness == "antigravity"
            or self.deny_gemini and str(model or "").startswith("gemini-")
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


def test_unverified_third_account_is_not_selected_when_first_two_are_full(
    registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    registry.harnesses["antigravity3"] = GenericCliHarness(
        name="antigravity3", command="true", explore_command="true",
    )
    registry.scheduler.max_active_per_harness["antigravity3"] = 4
    usage = {
        "status": "partial",
        "providers": {
            name: {
                "status": "error", "error": "verification_required",
            } if name == "antigravity3" else {
                "status": "ok",
                "windows": [
                    {"group": "Gemini Models", "window": "5h", "remaining_percent": 95},
                    {"group": "Gemini Models", "window": "weekly", "remaining_percent": 95},
                ],
            }
            for name in ("antigravity", "antigravity2", "antigravity3")
        },
    }
    registry.quota_admission_gate = DelegateQuotaAdmissionGate(
        usage_provider=lambda: usage,
        fresh_usage_provider=lambda _name: usage,
        policy_path=tmp_path / "quota.json",
    )
    monkeypatch.setattr(
        registry.scheduler, "harness_capacity",
        lambda name: {
            "active": 4 if name != "antigravity3" else 0, "limit": 4,
        },
    )
    selected, _, _, route = registry._select_harness(
        harness=None, kind="explore", model=None, complexity="low",
    )
    assert selected == "antigravity"
    assert route["reason"] == "agy_capacity_wait"
    assert [row["harness"] for row in route["candidates"]] == [
        "antigravity", "antigravity3", "antigravity2",
    ]
    assert route["candidates"][1]["reason"] == "account_verification_required"
    assert route["candidates"][1]["eligible"] is False

    # An explicit request is rejected before starting a delegate process.
    denied = registry.run_delegate(
        harness="antigravity3", kind="explore",
        task="Do not execute while account is unverified.",
        cwd=tmp_path, wait_seconds=0,
    )
    assert denied["success"] is False
    assert denied["error"]["code"] == "delegate_quota_blocked"
    assert len(registry.scheduler.tasks) == 0


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


def test_capacity_first_places_agy3_ahead_of_agy2_for_all_tiers(
    registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.harnesses["antigravity3"] = GenericCliHarness(
        name="antigravity3", command="true", explore_command="true",
    )
    registry.scheduler.max_active_per_harness["antigravity3"] = 4
    limits = {
        "antigravity": {"active": 4, "limit": 4},
        "antigravity2": {"active": 0, "limit": 4},
        "antigravity3": {"active": 0, "limit": 4},
    }
    monkeypatch.setattr(
        registry.scheduler, "harness_capacity",
        lambda name: limits[name],
    )
    for kind in ("explore", "code"):
        for complexity, expected_model in (
            ("low", "gemini-3.8-flash-medium"),
            ("medium", "gemini-3.8-flash-high"),
            ("high", "gemini-3.8-flash-high"),
        ):
            selected, _, _, route = registry._select_harness(
                harness=None, kind=kind, model=None, complexity=complexity,
            )
            assert selected == "antigravity3"
            assert route["preferred_model"] == expected_model
            assert route["reason"] == "agy_capacity_first"
            assert [row["harness"] for row in route["candidates"]] == [
                "antigravity", "antigravity3", "antigravity2",
            ]
            assert [row["reason"] for row in route["candidates"]] == [
                "account_capacity_full", "available", "available",
            ]
    limits["antigravity3"]["active"] = 4
    selected, _, _, route = registry._select_harness(
        harness=None, kind="code", model=None, complexity="medium",
    )
    assert selected == "antigravity2"
    assert [row["reason"] for row in route["candidates"]] == [
        "account_capacity_full", "account_capacity_full", "available",
    ]
    limits["antigravity2"]["active"] = 4
    selected, _, _, route = registry._select_harness(
        harness=None, kind="explore", model=None, complexity="low",
    )
    assert selected == "antigravity"
    assert route["reason"] == "agy_capacity_wait"
    assert len(route["candidates"]) == 3
    assert all(row["harness"] != "codex" for row in route["candidates"])
    policy = registry.routing_guidance()["policy"]
    assert policy["capacity_first_order"] == [
        "antigravity", "antigravity3", "antigravity2",
    ]
    assert policy["agy_accounts"] == policy["capacity_first_order"]
    assert "AGY3" in policy["complexity_levels"]["explore"]["low"]
    assert "AGY3" in policy["complexity_levels"]["code"]["medium"]


def test_runtime_fallback_prefers_agy3_before_agy2(
    registry: ExecutorRegistry,
) -> None:
    registry.harnesses["antigravity3"] = GenericCliHarness(
        name="antigravity3", command="true", explore_command="true",
    )
    for kind in ("explore", "code"):
        for complexity in ("low", "medium", "high"):
            model = "gemini-3.8-flash-high"
            first = SimpleNamespace(
                harness="antigravity", kind=kind, complexity=complexity, model=model,
            )
            assert registry._select_runtime_fallback(first)[0] == "antigravity3"
            assert registry._select_runtime_fallback(
                first, excluded_harnesses={"antigravity3"},
            )[0] == "antigravity2"
            third = SimpleNamespace(
                harness="antigravity3", kind=kind, complexity=complexity, model=model,
            )
            assert registry._select_runtime_fallback(third)[0] == "antigravity2"
            second = SimpleNamespace(
                harness="antigravity2", kind=kind, complexity=complexity, model=model,
            )
            assert registry._select_runtime_fallback(second) is None


def test_explicit_and_resumed_agy_accounts_keep_original_pin(
    registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry.harnesses["antigravity3"] = GenericCliHarness(
        name="antigravity3", command="true", explore_command="true",
    )
    for name in ("antigravity2", "antigravity3"):
        selected, _, _, route = registry._select_harness(
            harness=name, kind="explore", model=None, complexity="low",
        )
        assert selected == name
        assert route is None
        monkeypatch.setattr(
            registry, "_resume_source_snapshot",
            lambda _id, account=name: {"harness": account},
        )
        selected, _, _, route = registry._select_harness(
            harness=None, kind="explore", model=None,
            complexity="low", resume_from_delegate_id="old-task-id",
        )
        assert selected == name
        assert route["reason"] == "resume_account_pinned"


def test_heavy_task_uses_gemini_high_and_preserves_cheap_account_routing(
    registry: ExecutorRegistry,
) -> None:
    harness, _, _, route = registry._select_harness(
        harness=None, kind="code", model=None, complexity="high",
    )
    assert harness == "antigravity"
    assert route["task_tier"] == "gemini"
    assert route["preferred_model"] == "gemini-3.8-flash-high"
    assert route["preferred_reasoning_effort"] == "high"
    assert all(row["harness"] != "codex" for row in route["candidates"])


def test_claude_quota_exhaustion_does_not_spend_sol(
    registry: ExecutorRegistry,
) -> None:
    registry.quota_admission_gate = ModelQuota(deny_claude=True)
    harness, _, _, route = registry._select_harness(
        harness=None, kind="explore", model=None, complexity="high",
    )
    assert harness == "antigravity"
    assert route["reason"] == "agy_capacity_first"
    assert route["preferred_model"] == "gemini-3.8-flash-high"
    assert route["preferred_reasoning_effort"] == "high"


def test_high_complexity_stays_on_gemini_high_even_when_claude_unavailable(
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
    assert captured[0].harness == "antigravity"
    assert captured[0].model == "gemini-3.8-flash-high"
    assert captured[0].reasoning_effort == "high"
    assert captured[0].route_decision["planner"]["selected_route"] == (
        captured[0].route_decision["initial_route"]
    )


def test_all_gemini_quotas_blocked_never_silently_uses_codex(
    registry: ExecutorRegistry,
) -> None:
    registry.quota_admission_gate = ModelQuota(deny_gemini=True)
    harness, _, _, route = registry._select_harness(
        harness=None, kind="code", model=None, complexity="high",
    )
    assert harness == "antigravity"
    assert route["reason"] == "agy_no_admissible_account"
    assert all(row["harness"] != "codex" for row in route["candidates"])
    assert all(row["eligible"] is False for row in route["candidates"])

def test_unpinned_model_overrides_respect_economic_provider_boundaries(
    registry: ExecutorRegistry,
) -> None:
    for kind in ("code", "explore"):
        selected, _, _, route = registry._select_harness(
            harness=None, kind=kind, model="gemini-3.8-flash-high",
            complexity="low",
        )
        assert selected == "antigravity"
        assert route["preferred_model"] == "gemini-3.8-flash-high"
        assert route["task_tier"] == "gemini"
        assert all(row["harness"] != "codex" for row in route["candidates"])

        selected, _, _, route = registry._select_harness(
            harness=None, kind=kind, model="gpt-6.1-sol",
            complexity="low",
        )
        assert selected == "codex"
        assert route["preferred_model"] == "gpt-6.1-sol"
        assert route["reason"] == "explicit_gpt_model"
        assert registry._routing_provenance(
            requested_harness=None, route=route,
        ) == ("explicit_model", "explicit_gpt_model")


def test_explicit_claude_model_is_never_chosen_without_model_pin(
    registry: ExecutorRegistry,
) -> None:
    selected, _, _, route = registry._select_harness(
        harness=None, kind="explore", model="claude-opus-4-6-thinking",
        complexity="low",
    )
    assert selected == "antigravity"
    assert route["task_tier"] == "claude"
    assert route["preferred_model"] == "claude-opus-4-6-thinking"
    default = registry._select_harness(
        harness=None, kind="explore", model=None, complexity="high",
    )[3]
    assert default["task_tier"] == "gemini"


def test_failed_gemini_admission_never_starts_codex_delegate(
    registry: ExecutorRegistry, tmp_path: Path,
) -> None:
    registry.quota_admission_gate = ModelQuota(deny_gemini=True)
    result = registry.run_delegate(
        task="Blocked Gemini high implementation",
        kind="code", complexity="high", cwd=tmp_path, wait_seconds=0,
    )
    assert result["success"] is False
    assert result["error"]["code"] == "delegate_quota_blocked"
    assert len(registry.scheduler.tasks) == 0


def test_no_configured_gemini_harness_fails_closed_without_codex(
    registry: ExecutorRegistry, tmp_path: Path,
) -> None:
    for name in ("antigravity", "antigravity2", "antigravity3"):
        registry.harnesses.pop(name, None)
    selected, adapter, _, route = registry._select_harness(
        harness=None, kind="code", model=None, complexity="high",
    )
    assert selected == "antigravity"
    assert adapter is None
    assert route["reason"] == "gemini_harness_not_configured"
    assert route["candidates"] == []
    result = registry.run_delegate(
        task="Requires an explicitly configured AGY harness.",
        cwd=tmp_path, kind="code", complexity="high", wait_seconds=0,
    )
    assert result["success"] is False
    assert result["error"]["code"] == "unsupported_delegate_harness"


def test_explicit_high_codex_pin_runs_sol_medium_only_by_request(
    registry: ExecutorRegistry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    captured = []

    def run(*, delegate_task):
        captured.append(delegate_task)
        return {"success": True, "status": "succeeded",
                "delegate_id": delegate_task.delegate_id}

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", run)
    result = registry.run_delegate(
        task="Explicitly escalate a high-risk patch to Codex.",
        kind="code", complexity="high", harness="codex",
        cwd=tmp_path, wait_seconds=3,
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
