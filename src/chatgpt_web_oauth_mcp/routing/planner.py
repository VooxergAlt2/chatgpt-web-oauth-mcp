from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from pathlib import Path
from typing import Any, Literal

from .task_shape import TaskShape


PlannerMode = Literal["off", "shadow", "active"]
PlanKind = Literal["direct", "split", "review", "escalate"]

PLANNER_MODES = frozenset({"off", "shadow", "active"})
PLANNER_PLAN_KINDS = frozenset({"direct", "split", "review", "escalate"})


@dataclass(frozen=True)
class CandidateRoute:
    harness: str
    route: str
    role: str
    model: str
    reasoning_effort: str
    eligible: bool
    availability_reason: str
    quota_headroom_percent: float = 50.0
    active_load: int = 0
    feedback_attempts: int = 0
    feedback_accepted_rate: float | None = None
    feedback_duration_p50: float | None = None
    feedback_tokens_p50: float | None = None
    # Optional opportunity-cost preference; zero preserves legacy scoring.
    account_preference_bonus: float = 0.0


@dataclass(frozen=True)
class SplitShard:
    shard_id: str
    files: tuple[str, ...]
    estimated_bytes: int
    estimated_context_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "shard_id": self.shard_id,
            "files": list(self.files),
            "estimated_bytes": self.estimated_bytes,
            "estimated_context_tokens": self.estimated_context_tokens,
        }


@dataclass(frozen=True)
class SplitPlan:
    executable: bool
    reason: str
    target_harness: str = "local"
    max_concurrency: int = 4
    shards: tuple[SplitShard, ...] = ()
    context_pool_tokens: int | None = None
    context_limited: bool = False
    synthesis_contract: str = "deterministic_findings_union_v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "reason": self.reason,
            "target_harness": self.target_harness,
            "max_concurrency": self.max_concurrency,
            "shards": [shard.to_dict() for shard in self.shards],
            "context_pool_tokens": self.context_pool_tokens,
            "context_limited": self.context_limited,
            "synthesis_contract": self.synthesis_contract,
        }


@dataclass(frozen=True)
class ScoredRoute:
    candidate: CandidateRoute
    score: float
    components: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self.candidate),
            "score": self.score,
            "components": dict(self.components),
        }


@dataclass(frozen=True)
class PlannerDecision:
    mode: PlannerMode
    plan_kind: PlanKind
    authority: str
    legacy_route: str | None
    selected_route: str | None
    shadow_route: str | None
    reason: str
    confidence: float
    candidates: tuple[ScoredRoute, ...] = ()
    split_plan: SplitPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "plan_kind": self.plan_kind,
            "authority": self.authority,
            "legacy_route": self.legacy_route,
            "selected_route": self.selected_route,
            "shadow_route": self.shadow_route,
            "reason": self.reason,
            "confidence": self.confidence,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "split_plan": self.split_plan.to_dict() if self.split_plan else None,
            # Durable schema hooks for later graph planning. They are intentionally
            # empty in Planner Lite so no hidden DAG semantics are introduced.
            "plan_id": None,
            "parent_plan_id": None,
            "node_id": None,
            "depends_on_node_ids": [],
        }


def normalize_planner_mode(value: str | None) -> PlannerMode:
    normalized = str(value or "off").strip().lower()
    if normalized not in PLANNER_MODES:
        raise ValueError(
            f"planner mode must be one of {sorted(PLANNER_MODES)}; got {value!r}"
        )
    return normalized  # type: ignore[return-value]


def build_explicit_file_split_plan(
    *,
    root: Path,
    files_in_scope: list[str] | None,
    max_shards: int,
    max_files_per_shard: int,
    max_bytes_per_shard: int,
    max_concurrency: int,
    target_harness: str = "local",
    context_pool_tokens: int | None = None,
    context_overhead_tokens: int = 0,
    context_bytes_per_token: int = 4,
) -> SplitPlan:
    """Split a complete explicit file scope into bounded shards or fail closed."""

    raw_files = [
        str(item).strip()
        for item in (files_in_scope or [])
        if str(item).strip()
    ]
    if not raw_files:
        return SplitPlan(executable=False, reason="no_explicit_files")
    canonical_root = Path(root).expanduser().resolve()
    rows: list[tuple[str, int]] = []
    seen: set[str] = set()
    for raw in raw_files:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = canonical_root / candidate
        try:
            resolved = candidate.resolve()
            relative = resolved.relative_to(canonical_root)
        except (OSError, ValueError):
            return SplitPlan(executable=False, reason="scope_outside_project")
        if not resolved.is_file():
            return SplitPlan(
                executable=False,
                reason="non_file_scope_requires_future_discovery_node",
            )
        normalized = relative.as_posix()
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            size = resolved.stat().st_size
        except OSError:
            return SplitPlan(executable=False, reason="scope_stat_failed")
        if size > max_bytes_per_shard:
            return SplitPlan(executable=False, reason="file_exceeds_shard_budget")
        rows.append((normalized, size))
    if not rows:
        return SplitPlan(executable=False, reason="empty_explicit_file_scope")

    total_bytes = sum(size for _, size in rows)
    if (
        len(rows) <= max_files_per_shard
        and total_bytes <= max_bytes_per_shard
    ):
        return SplitPlan(executable=False, reason="scope_fits_single_shard")

    minimum_shards = max(
        2,
        math.ceil(len(rows) / max_files_per_shard),
        math.ceil(total_bytes / max_bytes_per_shard),
    )
    if minimum_shards > max_shards:
        return SplitPlan(executable=False, reason="split_exceeds_shard_limit")

    # Longest-processing-time bin packing keeps parallel shards balanced while
    # respecting both hard file-count and byte ceilings. Stable input indexes
    # make the result deterministic for identical file sizes.
    indexed_rows = [
        (index, normalized, size)
        for index, (normalized, size) in enumerate(rows)
    ]
    packed: list[list[tuple[int, str, int]]] | None = None
    for shard_count in range(minimum_shards, max_shards + 1):
        bins: list[list[tuple[int, str, int]]] = [
            [] for _ in range(shard_count)
        ]
        bin_bytes = [0 for _ in range(shard_count)]
        failed = False
        for item in sorted(indexed_rows, key=lambda row: (-row[2], row[0])):
            _, _, size = item
            eligible = [
                index
                for index in range(shard_count)
                if len(bins[index]) < max_files_per_shard
                and bin_bytes[index] + size <= max_bytes_per_shard
            ]
            if not eligible:
                failed = True
                break
            target = min(
                eligible,
                key=lambda index: (
                    bin_bytes[index],
                    len(bins[index]),
                    index,
                ),
            )
            bins[target].append(item)
            bin_bytes[target] += size
        if not failed and all(bins):
            packed = bins
            break
    if packed is None:
        # LPT is a balancing heuristic, not a complete bin-packing solver. Do
        # not regress scopes that the previous stable sequential packer can
        # represent within the configured hard shard limits.
        sequential: list[list[tuple[int, str, int]]] = []
        current: list[tuple[int, str, int]] = []
        current_bytes = 0
        for item in indexed_rows:
            _, _, size = item
            if current and (
                len(current) >= max_files_per_shard
                or current_bytes + size > max_bytes_per_shard
            ):
                sequential.append(current)
                current = []
                current_bytes = 0
            current.append(item)
            current_bytes += size
        if current:
            sequential.append(current)
        if len(sequential) > max_shards:
            return SplitPlan(executable=False, reason="split_exceeds_shard_limit")
        packed = sequential

    shards = [
        SplitShard(
            shard_id=f"shard-{index + 1:02d}",
            files=tuple(
                item[1]
                for item in sorted(bucket, key=lambda row: row[0])
            ),
            estimated_bytes=sum(item[2] for item in bucket),
        )
        for index, bucket in enumerate(packed)
    ]

    requested_concurrency = min(max_concurrency, len(shards))
    effective_concurrency = requested_concurrency
    context_limited = False
    if context_pool_tokens is not None and context_pool_tokens > 0:
        bytes_per_token = max(1, int(context_bytes_per_token))
        overhead_tokens = max(0, int(context_overhead_tokens))
        shards = [
            SplitShard(
                shard_id=shard.shard_id,
                files=shard.files,
                estimated_bytes=shard.estimated_bytes,
                estimated_context_tokens=(
                    overhead_tokens
                    + math.ceil(shard.estimated_bytes / bytes_per_token)
                ),
            )
            for shard in shards
        ]
        largest_contexts = sorted(
            (int(shard.estimated_context_tokens or 0) for shard in shards),
            reverse=True,
        )
        while (
            effective_concurrency > 1
            and sum(largest_contexts[:effective_concurrency])
            > int(context_pool_tokens)
        ):
            effective_concurrency -= 1
        context_limited = effective_concurrency < requested_concurrency

    return SplitPlan(
        executable=True,
        reason="bounded_explicit_file_scope",
        target_harness=target_harness,
        max_concurrency=effective_concurrency,
        shards=tuple(shards),
        context_pool_tokens=(
            int(context_pool_tokens)
            if context_pool_tokens is not None and context_pool_tokens > 0
            else None
        ),
        context_limited=context_limited,
    )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _capability_fit(shape: TaskShape, role: str) -> float:
    role = role.strip().lower() or "general"
    by_purpose = {
        "discovery": {
            "scout": 1.00,
            "reviewer": 0.76,
            "general": 0.72,
            "authoritative": 0.70,
        },
        "review": {
            "scout": 0.66,
            "reviewer": 1.00,
            "general": 0.90,
            "authoritative": 0.96,
        },
        "investigation": {
            "scout": 0.78,
            "reviewer": 0.90,
            "general": 0.95,
            "authoritative": 0.96,
        },
        "implementation": {
            "scout": 0.20,
            "reviewer": 0.28,
            "general": 0.90,
            "authoritative": 1.00,
        },
    }
    fit = by_purpose.get(shape.purpose, by_purpose["discovery"]).get(role, 0.70)
    if shape.breadth == "large":
        fit -= {"scout": 0.30, "reviewer": 0.16}.get(role, 0.0)
    elif shape.breadth == "medium" and role == "scout":
        fit -= 0.06
    if shape.depth == "high":
        fit -= {"scout": 0.48, "reviewer": 0.15}.get(role, 0.0)
    if shape.impact == "high":
        fit -= {"scout": 0.30, "reviewer": 0.12}.get(role, 0.0)
    if shape.context_pressure == "high":
        fit -= {"scout": 0.30, "reviewer": 0.16}.get(role, 0.0)
    if shape.verification_level == "concrete" and shape.purpose == "review":
        fit += {"reviewer": 0.08, "general": 0.04, "authoritative": 0.06}.get(
            role, 0.0
        )
    return _clamp(fit)


def _feedback_score(candidate: CandidateRoute, *, min_samples: int) -> float:
    accepted_rate = candidate.feedback_accepted_rate
    if accepted_rate is None or candidate.feedback_attempts <= 0:
        return 0.50
    confidence = min(
        1.0,
        float(candidate.feedback_attempts) / float(max(1, min_samples)),
    )
    return _clamp(0.50 + (float(accepted_rate) - 0.50) * confidence)


def _cost_score(role: str) -> float:
    return {
        "scout": 1.00,
        "reviewer": 0.88,
        "general": 0.62,
        "authoritative": 0.52,
    }.get(role.strip().lower(), 0.60)


def _latency_score(candidate: CandidateRoute) -> float:
    duration = candidate.feedback_duration_p50
    if duration is None:
        return 0.50
    return _clamp(1.0 / (1.0 + max(0.0, float(duration)) / 60.0))


def _token_efficiency_score(candidate: CandidateRoute) -> float:
    total_tokens = candidate.feedback_tokens_p50
    if total_tokens is None:
        return 0.50
    return _clamp(
        1.0 / (1.0 + max(0.0, float(total_tokens)) / 12_000.0)
    )


def score_candidate(
    shape: TaskShape,
    candidate: CandidateRoute,
    *,
    min_feedback_samples: int = 6,
) -> ScoredRoute:
    if not candidate.eligible:
        return ScoredRoute(
            candidate=candidate,
            score=0.0,
            components={
                "capability": 0.0,
                "feedback": 0.0,
                "quota": 0.0,
                "load": 0.0,
                "cost": 0.0,
                "latency": 0.0,
                "token_efficiency": 0.0,
            },
        )
    capability = _capability_fit(shape, candidate.role)
    feedback = _feedback_score(candidate, min_samples=min_feedback_samples)
    quota = _clamp(float(candidate.quota_headroom_percent) / 50.0)
    load = 1.0 / (1.0 + max(0, int(candidate.active_load)))
    cost = _cost_score(candidate.role)
    latency = _latency_score(candidate)
    token_efficiency = _token_efficiency_score(candidate)
    components = {
        "capability": round(capability, 4),
        "feedback": round(feedback, 4),
        "quota": round(quota, 4),
        "load": round(load, 4),
        "cost": round(cost, 4),
        "latency": round(latency, 4),
        "token_efficiency": round(token_efficiency, 4),
    }
    score = (
        0.42 * capability
        + 0.20 * feedback
        + 0.10 * quota
        + 0.08 * load
        + 0.08 * cost
        + 0.06 * latency
        + 0.06 * token_efficiency
        + max(0.0, float(candidate.account_preference_bonus))
    )
    if candidate.account_preference_bonus:
        components["account_preference_bonus"] = round(candidate.account_preference_bonus, 4)
    return ScoredRoute(
        candidate=candidate,
        score=round(_clamp(score), 4),
        components=components,
    )


def plan_route(
    *,
    shape: TaskShape,
    candidates: list[CandidateRoute],
    legacy_route: str | None,
    mode: str,
    min_feedback_samples: int = 6,
    split_plan: SplitPlan | None = None,
    active_allowed: bool = True,
    active_block_reason: str | None = None,
) -> PlannerDecision:
    planner_mode = normalize_planner_mode(mode)
    scored = tuple(
        sorted(
            (
                score_candidate(
                    shape,
                    candidate,
                    min_feedback_samples=min_feedback_samples,
                )
                for candidate in candidates
            ),
            key=lambda item: (-item.score, item.candidate.route),
        )
    )
    best = next((item for item in scored if item.candidate.eligible), None)
    split_best = (
        next(
            (
                item
                for item in scored
                if split_plan is not None
                and split_plan.executable
                and item.candidate.eligible
                and item.candidate.harness == split_plan.target_harness
            ),
            None,
        )
        if split_plan is not None
        else None
    )
    if split_best is not None:
        best = split_best
    shadow_route = best.candidate.route if best is not None else None
    plan_kind: PlanKind = (
        "split"
        if split_plan is not None
        and split_plan.executable
        and split_best is not None
        else "direct"
    )
    planner_confidence = shape.confidence
    if best is not None:
        planner_confidence = min(
            0.99,
            max(
                shape.confidence,
                0.55 + (0.35 * best.score),
            ),
        )
    active = (
        planner_mode == "active"
        and active_allowed
        and best is not None
        and shape.confidence >= 0.65
    )
    selected_route = shadow_route if active else legacy_route
    authority = "planner" if active else "legacy"
    if planner_mode == "off":
        reason = "planner_disabled"
    elif planner_mode == "active" and not active_allowed:
        reason = active_block_reason or "active_planner_not_allowed"
    elif best is None:
        reason = "no_eligible_planner_candidate"
    elif planner_mode == "shadow":
        reason = "shadow_observation"
    elif shape.confidence < 0.65:
        reason = "classifier_confidence_below_active_threshold"
    elif plan_kind == "split":
        reason = "active_split_plan"
    else:
        reason = "active_direct_route"
    return PlannerDecision(
        mode=planner_mode,
        plan_kind=plan_kind,
        authority=authority,
        legacy_route=legacy_route,
        selected_route=selected_route,
        shadow_route=shadow_route,
        reason=reason,
        confidence=round(planner_confidence, 4),
        candidates=scored,
        split_plan=split_plan,
    )
