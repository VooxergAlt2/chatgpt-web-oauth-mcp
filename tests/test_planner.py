from __future__ import annotations

from chatgpt_web_oauth_mcp.routing.planner import (
    CandidateRoute,
    SplitPlan,
    SplitShard,
    build_explicit_file_split_plan,
    normalize_planner_mode,
    plan_route,
)
from chatgpt_web_oauth_mcp.routing.task_shape import TaskShape


def _shape(**overrides: object) -> TaskShape:
    values = {
        "purpose": "discovery",
        "breadth": "small",
        "depth": "low",
        "impact": "low",
        "verification_level": "basic",
        "parallelism": "medium",
        "risk_class": "normal",
        "context_pressure": "low",
        "estimated_scope": "small",
        "confidence": 0.8,
    }
    values.update(overrides)
    return TaskShape(**values)  # type: ignore[arg-type]


def _candidate(
    harness: str,
    role: str,
    *,
    eligible: bool = True,
    attempts: int = 0,
    accepted_rate: float | None = None,
    duration_p50: float | None = None,
    tokens_p50: float | None = None,
) -> CandidateRoute:
    return CandidateRoute(
        harness=harness,
        route=f"{harness}:explore:model:low",
        role=role,
        model="model",
        reasoning_effort="low",
        eligible=eligible,
        availability_reason="available" if eligible else "unavailable",
        feedback_attempts=attempts,
        feedback_accepted_rate=accepted_rate,
        feedback_duration_p50=duration_p50,
        feedback_tokens_p50=tokens_p50,
    )


def test_shadow_planner_scores_without_changing_legacy_route() -> None:
    decision = plan_route(
        shape=_shape(),
        candidates=[
            _candidate("local", "scout"),
            _candidate("codex", "authoritative"),
        ],
        legacy_route="codex:explore:model:low",
        mode="shadow",
    )

    assert decision.authority == "legacy"
    assert decision.selected_route == "codex:explore:model:low"
    assert decision.shadow_route == "local:explore:model:low"
    assert decision.reason == "shadow_observation"


def test_active_planner_prefers_scout_for_small_discovery() -> None:
    decision = plan_route(
        shape=_shape(),
        candidates=[
            _candidate("local", "scout"),
            _candidate("codex", "authoritative"),
        ],
        legacy_route="codex:explore:model:low",
        mode="active",
    )

    assert decision.authority == "planner"
    assert decision.selected_route == "local:explore:model:low"


def test_sparse_bad_feedback_does_not_overrule_capability_fit() -> None:
    decision = plan_route(
        shape=_shape(),
        candidates=[
            _candidate("local", "scout", attempts=1, accepted_rate=0.0),
            _candidate("codex", "authoritative"),
        ],
        legacy_route="codex:explore:model:low",
        mode="active",
        min_feedback_samples=8,
    )

    assert decision.selected_route == "local:explore:model:low"
    local = next(
        item for item in decision.candidates if item.candidate.harness == "local"
    )
    assert 0.4 < local.components["feedback"] < 0.5


def test_scoring_uses_observed_latency_and_token_cost() -> None:
    fast = _candidate(
        "fast",
        "general",
        attempts=8,
        accepted_rate=0.8,
        duration_p50=10.0,
        tokens_p50=2_000,
    )
    slow = _candidate(
        "slow",
        "general",
        attempts=8,
        accepted_rate=0.8,
        duration_p50=120.0,
        tokens_p50=30_000,
    )

    decision = plan_route(
        shape=_shape(purpose="investigation", depth="medium"),
        candidates=[slow, fast],
        legacy_route=slow.route,
        mode="active",
    )

    assert decision.selected_route == fast.route
    scores = {item.candidate.harness: item for item in decision.candidates}
    assert scores["fast"].components["latency"] > scores["slow"].components["latency"]
    assert (
        scores["fast"].components["token_efficiency"]
        > scores["slow"].components["token_efficiency"]
    )


def test_high_impact_deep_review_does_not_prefer_scout() -> None:
    decision = plan_route(
        shape=_shape(
            purpose="review",
            breadth="large",
            depth="high",
            impact="high",
            verification_level="concrete",
            risk_class="runtime",
            context_pressure="high",
        ),
        candidates=[
            _candidate("local", "scout"),
            _candidate("local2", "reviewer"),
            _candidate("codex", "authoritative"),
        ],
        legacy_route="codex:explore:model:low",
        mode="active",
    )

    assert decision.selected_route == "codex:explore:model:low"


def test_split_plan_marks_plan_kind_without_hidden_dag_fields() -> None:
    split = SplitPlan(
        executable=True,
        reason="bounded_explicit_file_scope",
        shards=(
            SplitShard("shard-01", ("a.py", "b.py"), 1000),
            SplitShard("shard-02", ("c.py",), 500),
        ),
    )
    decision = plan_route(
        shape=_shape(breadth="medium"),
        candidates=[_candidate("local", "scout")],
        legacy_route="codex:explore:model:low",
        mode="active",
        split_plan=split,
    )

    payload = decision.to_dict()
    assert decision.plan_kind == "split"
    assert payload["split_plan"]["shards"][0]["files"] == ["a.py", "b.py"]
    assert payload["plan_id"] is None
    assert payload["depends_on_node_ids"] == []



def test_explicit_file_split_plan_is_complete_and_bounded(tmp_path) -> None:
    files = []
    for index in range(5):
        path = tmp_path / f"f{index}.py"
        path.write_text("x = 1\n", encoding="utf-8")
        files.append(path.name)

    split = build_explicit_file_split_plan(
        root=tmp_path,
        files_in_scope=files,
        max_shards=4,
        max_files_per_shard=2,
        max_bytes_per_shard=1024,
        max_concurrency=4,
    )

    assert split.executable is True
    assert [list(shard.files) for shard in split.shards] == [
        ["f0.py", "f1.py"],
        ["f2.py", "f3.py"],
        ["f4.py"],
    ]
    assert split.max_concurrency == 3


def test_split_plan_fails_closed_for_directory_scope(tmp_path) -> None:
    scope = tmp_path / "src"
    scope.mkdir()
    (scope / "a.py").write_text("x = 1\n", encoding="utf-8")

    split = build_explicit_file_split_plan(
        root=tmp_path,
        files_in_scope=["src"],
        max_shards=4,
        max_files_per_shard=2,
        max_bytes_per_shard=1024,
        max_concurrency=4,
    )

    assert split.executable is False
    assert split.reason == "non_file_scope_requires_future_discovery_node"


def test_planner_mode_rejects_unknown_value() -> None:
    try:
        normalize_planner_mode("magic")
    except ValueError as exc:
        assert "planner mode" in str(exc)
    else:
        raise AssertionError("unknown planner mode must be rejected")
