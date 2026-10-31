from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_telemetry import DelegateTelemetryStore
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def _snapshot(
    delegate_id: str,
    *,
    harness: str = "antigravity",
    kind: str = "explore",
    model: str = "gemini-test",
    effort: str = "high",
    status: str = "succeeded",
    duration: float = 10.0,
    total_tokens: int | None = None,
    error_code: str | None = None,
    error_message: str = "",
    routing_mode: str = "unknown",
    routing_reason: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "delegate_id": delegate_id,
        "harness": harness,
        "kind": kind,
        "model": model,
        "reasoning_effort": effort,
        "status": status,
        "success": status == "succeeded",
        "duration_seconds": duration,
        "group_id": None,
        "resume_from_delegate_id": None,
        "routing_mode": routing_mode,
        "routing_reason": routing_reason,
    }
    if error_code is not None:
        result["error"] = {
            "code": error_code,
            "message": error_message or error_code,
        }
    if total_tokens is not None:
        result["harness_metadata"] = {
            "usage": {
                "input_tokens": total_tokens - 30,
                "output_tokens": 10,
                "thinking_tokens": 20,
                "cache_read_tokens": total_tokens * 2,
                "total_tokens": total_tokens,
            }
        }
    return result


def test_telemetry_separates_process_success_from_advisory_evidence_and_failures(
    tmp_path: Path,
) -> None:
    store = DelegateTelemetryStore(tmp_path / "quality-metrics.json")
    now = time.time()
    success = _snapshot("evidence00001")
    success["research_evidence"] = {
        "status": "partial_coverage",
        "reason": "source_line_existence_only",
        "finding_count": 3,
        "checked_finding_count": 3,
        "anchored_finding_count": 1,
        "semantics_verified": False,
        "source_commit": "a" * 40,
        "findings": [{"index": 1, "secret_finding": "never copy free text"}],
    }
    store.record_terminal(success, completed_at_epoch=now)
    interrupted = _snapshot(
        "interrupted001", status="failed",
        error_code="durable_job_interrupted",
    )
    interrupted["error"]["interruption_reason"] = (
        "supervisor_and_command_gone_without_terminal_record"
    )
    interrupted["interruption_diagnostics"] = {
        "stdout_bytes": 16384,
        "stderr_bytes": 0,
        "shared_cgroup_oom_kill_delta": 1,
        "secret": "never copy raw diagnostic extras",
    }
    interrupted["failure_analysis"] = {
        "failure_type": "infrastructure_error", "retryable": True,
    }
    store.record_terminal(interrupted, completed_at_epoch=now)
    stale = _snapshot("stale00000001", harness="codex")
    store.record_terminal(stale, completed_at_epoch=now - 8 * 86400)

    snapshot = store.snapshot()
    overall = snapshot["overall"]
    assert overall["terminal"] == 3
    assert overall["succeeded"] == 2
    assert overall["error_codes"]["durable_job_interrupted"] == 1
    assert overall["failure_types"]["infrastructure_error"] == 1
    assert overall["interruption_reasons"] == {
        "supervisor_and_command_gone_without_terminal_record": 1,
    }
    raw_interrupted = json.loads(
        (tmp_path / "quality-metrics.json").read_text(encoding="utf-8")
    )["records"]["interrupted001"]
    assert raw_interrupted["interruption_diagnostics"] == {
        "stdout_bytes": 16384,
        "stderr_bytes": 0,
        "shared_cgroup_oom_kill_delta": 1,
    }
    evidence = overall["research_evidence"]
    assert evidence["records"] == 1
    assert evidence["statuses"] == {"partial_coverage": 1}
    assert evidence["checked_findings"] == 3
    assert evidence["anchored_findings"] == 1
    assert evidence["line_anchor_coverage"] == 0.3333
    assert evidence["semantics_verified"] is False
    assert snapshot["recent_windows"]["24h"]["terminal"] == 2
    assert snapshot["recent_windows"]["7d"]["terminal"] == 2
    assert snapshot["recent_windows"]["24h"]["sample_sufficient_for_monitoring"] is False
    assert snapshot["recent_windows"]["7d"]["by_harness_counts"] == {"antigravity": 2}
    raw = json.loads((tmp_path / "quality-metrics.json").read_text(encoding="utf-8"))
    recorded = raw["records"]["evidence00001"]["research_evidence"]
    assert "findings" not in recorded
    assert recorded["source_commit"] == "a" * 40
    assert DelegateTelemetryStore._feedback_outcome(raw["records"]["evidence00001"])[:2] == (
        True, True,
    )


def test_telemetry_archives_pruned_rows_once_and_exposes_daily_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.json"
    store = DelegateTelemetryStore(
        path, max_records=2, retention_seconds=10_000,
    )
    now = time.time()
    first = _snapshot("archive000001", total_tokens=100)
    first["task"] = "secret prompt must never enter a daily aggregate"
    first["research_evidence"] = {
        "status": "tracked_unscoped",
        "checked_finding_count": 4,
        "anchored_finding_count": 2,
        "scope_basis": "git_tracked_unscoped",
    }
    second = _snapshot(
        "archive000002", harness="antigravity2", kind="code",
        status="failed", error_code="code_verification_failed",
    )
    second["code_verification"] = {"enabled": True, "verified": False}
    third = _snapshot("archive000003", harness="codex", status="succeeded")
    for i, row in enumerate((first, second, third)):
        store.record_terminal(row, completed_at_epoch=now - 3 + i)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert len(raw["records"]) == 2
    assert raw["archived_ids"] == {"archive000001": raw["archived_ids"]["archive000001"]}
    assert sum(x["terminal"] for x in raw["archived_daily"].values()) == 1
    assert "secret prompt" not in json.dumps(raw["archived_daily"])
    snapshot = store.snapshot()
    daily = snapshot["daily_history"]
    assert daily["coverage"] == "observed_records_only"
    assert daily["archived_terminal_in_window"] == 1
    assert sum(x["terminal"] for x in daily["days"]) == 3
    assert sum(x["research_evidence"].get("checked", 0) for x in daily["days"]
               if isinstance(x.get("research_evidence"), dict)) == 4
    assert sum(x["research_evidence"].get("anchored", 0) for x in daily["days"]
               if isinstance(x.get("research_evidence"), dict)) == 2
    assert sum(x["code_verification"].get("rejected", 0) for x in daily["days"]
               if isinstance(x.get("code_verification"), dict)) == 1
    assert sum(x.get("total_tokens", 0) for x in daily["days"]) == 100
    assert sum(x["error_codes"].get("code_verification_failed", 0) for x in daily["days"]
               if isinstance(x.get("error_codes"), dict)) == 1
    restarted = DelegateTelemetryStore(
        path, max_records=2, retention_seconds=10_000,
    )
    assert restarted.snapshot()["daily_history"]["days"] == daily["days"]
    restarted.record_terminal(first, completed_at_epoch=now + 2)
    assert restarted.record_terminals_batch([(first, now + 2)]) == 0
    assert restarted.reconcile_terminals_batch([(first, now + 2, "runtime_terminal")]) == {
        "added": 0, "updated": 0,
    }
    assert restarted.snapshot()["daily_history"]["days"] == daily["days"]

    fourth = _snapshot("archive000004", harness="antigravity3")
    restarted.record_terminal(fourth, completed_at_epoch=now + 3)
    after = restarted.snapshot()["daily_history"]
    assert after["archived_terminal_in_window"] == 2
    assert sum(x["terminal"] for x in after["days"]) == 4
    assert len(json.loads(path.read_text())["records"]) == 2


def test_telemetry_archives_expired_30_day_record_not_older_than_archive_window(
    tmp_path: Path,
) -> None:
    now = time.time()
    store = DelegateTelemetryStore(
        tmp_path / "aged.json",
        max_records=2, retention_seconds=60,
    )
    store.record_terminal(
        _snapshot("oldbutkept001", harness="antigravity3"),
        completed_at_epoch=now - 24 * 86400,
    )
    result = store.snapshot()
    assert result["records"] == 0
    assert result["daily_history"]["archived_terminal_in_window"] == 1
    assert result["daily_history"]["days"][0]["by_harness"]["antigravity3"] == {
        "terminal": 1, "succeeded": 1,
    }
    store.record_terminal(
        _snapshot("reallyold0001"),
        completed_at_epoch=now - 200 * 86400,
    )
    assert store.snapshot()["daily_history"]["archived_terminal_in_window"] == 1


def test_delegate_telemetry_snapshot_reports_planner_shadow_disagreement(
    tmp_path: Path,
) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    row = _snapshot(
        "planner000001",
        harness="local",
        model="qwen-coder-next",
        effort="none",
        routing_mode="automatic",
        routing_reason="primary_available",
    )
    row["route_decision"] = {
        "legacy_complexity": "low",
        "task_shape_hash": "shape1234567890",
        "task_shape": {"purpose": "discovery", "breadth": "small"},
        "initial_route": "local:explore:qwen-coder-next:none",
        "shadow_route": "codex:explore:gpt-6.1-sol:low",
        "planner": {
            "mode": "shadow",
            "authority": "legacy",
            "plan_kind": "direct",
            "legacy_route": "local:explore:qwen-coder-next:none",
            "selected_route": "local:explore:qwen-coder-next:none",
            "shadow_route": "codex:explore:gpt-6.1-sol:low",
        },
    }
    store.record_terminal(row)

    planner = store.snapshot()["planner"]

    assert planner["records"] == 1
    assert planner["shadow_disagreements"] == 1
    assert planner["shadow_disagreement_rate"] == 1.0
    assert planner["modes"] == {"shadow": 1}
    assert planner["authorities"] == {"legacy": 1}
    assert planner["plan_kinds"] == {"direct": 1}


def test_delegate_telemetry_records_terminal_and_consumption(tmp_path: Path) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    now = time.time()
    store.record_terminal(
        _snapshot("aaaaaaaaaaaa", duration=12.0, total_tokens=100),
        completed_at_epoch=now,
    )
    store.record_terminal(
        _snapshot(
            "bbbbbbbbbbbb",
            harness="codex",
            kind="code",
            model="gpt-test",
            effort="xhigh",
            status="failed",
            duration=20.0,
        ),
        completed_at_epoch=now + 1,
    )

    assert store.mark_consumed("aaaaaaaaaaaa", consumed_at_epoch=now + 10) is True
    assert store.mark_consumed("missing", consumed_at_epoch=now + 10) is False

    snapshot = store.snapshot()
    assert snapshot["records"] == 2
    assert snapshot["overall"]["terminal"] == 2
    assert snapshot["overall"]["succeeded"] == 1
    assert snapshot["overall"]["failed"] == 1
    assert snapshot["overall"]["consumed"] == 1
    assert snapshot["overall"]["successful_consume_rate"] == 1.0
    assert snapshot["overall"]["duration_seconds"]["p50"] == 12.0
    assert snapshot["overall"]["duration_seconds"]["p90"] == 20.0
    assert snapshot["by_harness"]["antigravity"]["usage_totals"]["total_tokens"] == 100
    assert snapshot["by_harness"]["antigravity"]["usage_totals"]["cache_read_tokens"] == 200

    raw = json.loads((tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8"))
    stored = raw["records"]["aaaaaaaaaaaa"]
    assert "task" not in stored
    assert "goal" not in stored
    assert "prompt" not in stored
    assert stored["consumed_at_epoch"] == now + 10


def test_delegate_telemetry_records_sanitized_route_decision(tmp_path: Path) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    item = _snapshot("route00000001", routing_mode="complexity")
    item["route_decision"] = {
        "legacy_complexity": "medium",
        "initial_route": "local2:reviewer",
        "shadow_route": None,
        "task_shape": {
            "purpose": "investigation",
            "breadth": "small",
            "depth": "high",
            "impact": "medium",
            "verification_level": "concrete",
            "parallelism": "low",
            "risk_class": "normal",
            "context_pressure": "low",
            "estimated_scope": "small",
            "confidence": 0.65,
        },
    }
    item["failure_analysis"] = {
        "failure_type": "context_overflow",
        "retryable": True,
        "evidence": "maximum context window exceeded",
    }
    item["replan"] = {
        "action": "escalate_context",
        "reason": "context_overflow",
        "automatic": False,
        "preserve_evidence": True,
    }
    item["task"] = "secret task text must not be persisted"

    store.record_terminal(item)

    raw = json.loads((tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8"))
    stored = raw["records"]["route00000001"]
    assert stored["route_decision"]["legacy_complexity"] == "medium"
    assert stored["route_decision"]["task_shape"]["purpose"] == "investigation"
    assert stored["failure_analysis"]["failure_type"] == "context_overflow"
    assert stored["replan"]["action"] == "escalate_context"
    assert "task" not in stored


def test_delegate_telemetry_builds_feedback_by_shape_and_actual_route(
    tmp_path: Path,
) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    shape_hash = "0123456789abcdef"

    accepted = _snapshot(
        "feedback00001",
        harness="local2",
        model="reviewer",
        duration=8.0,
        total_tokens=100,
    )
    accepted["route_decision"] = {
        "legacy_complexity": "medium",
        "task_shape_hash": shape_hash,
        "task_shape": {"purpose": "investigation", "breadth": "small"},
        "initial_route": "local:explore:scout:none",
        "shadow_route": None,
    }
    accepted["route_attempts"] = [
        {
            "route": "local:explore:scout:none",
            "harness": "local",
            "kind": "explore",
            "model": "scout",
            "reasoning_effort": "none",
            "status": "partial",
            "success": False,
            "accepted": False,
            "duration_seconds": 4.0,
            "total_tokens": 40,
            "failure_type": "low_confidence",
            "replan_action": "add_reviewer",
        },
        {
            "route": "local2:explore:reviewer:high",
            "harness": "local2",
            "kind": "explore",
            "model": "reviewer",
            "reasoning_effort": "high",
            "status": "succeeded",
            "success": True,
            "accepted": True,
            "duration_seconds": 8.0,
            "total_tokens": 100,
            "failure_type": "none",
            "replan_action": "none",
        },
    ]
    accepted["route_chain"] = {
        "initial_route": "local:explore:scout:none",
        "final_route": "local2:explore:reviewer:high",
        "attempt_count": 2,
        "fallback_count": 1,
        "total_duration_seconds": 12.0,
        "total_tokens": 140,
    }
    accepted["failure_analysis"] = {
        "failure_type": "none",
        "retryable": False,
        "evidence": None,
    }
    accepted["replan"] = {
        "action": "none",
        "reason": "none",
        "automatic": False,
        "preserve_evidence": True,
    }
    accepted["routing_fallback"] = {
        "attempted": True,
        "attempts": [{"from_harness": "local", "to_harness": "local2"}],
    }

    rejected = _snapshot(
        "feedback00002",
        harness="codex",
        kind="code",
        model="gpt-code",
        effort="high",
        duration=20.0,
        total_tokens=500,
    )
    rejected["route_decision"] = {
        "legacy_complexity": "medium",
        "task_shape_hash": shape_hash,
        "task_shape": {"purpose": "investigation", "breadth": "small"},
        "initial_route": "codex:code:gpt-code:high",
        "shadow_route": None,
    }
    rejected["route_attempts"] = [
        {
            "route": "codex:code:gpt-code:high",
            "harness": "codex",
            "kind": "code",
            "model": "gpt-code",
            "reasoning_effort": "high",
            "status": "succeeded",
            "success": True,
            "accepted": True,
            "duration_seconds": 20.0,
            "total_tokens": 500,
            "failure_type": "none",
            "replan_action": "none",
        }
    ]
    rejected["route_chain"] = {
        "initial_route": "codex:code:gpt-code:high",
        "final_route": "codex:code:gpt-code:high",
        "attempt_count": 1,
        "fallback_count": 0,
        "total_duration_seconds": 20.0,
        "total_tokens": 500,
    }
    rejected["code_verification"] = {
        "enabled": True,
        "verified": False,
        "verification_commands": [{"command": "pytest -q", "passed": False}],
    }
    rejected["failure_analysis"] = {
        "failure_type": "verification_failed",
        "retryable": True,
        "evidence": "code_verification_failed",
    }
    rejected["replan"] = {
        "action": "implementation_loop",
        "reason": "verification_failed",
        "automatic": False,
        "preserve_evidence": True,
    }

    store.record_terminal(accepted)
    store.record_terminal(rejected)

    feedback = store.snapshot()["route_feedback"][shape_hash]
    overall = feedback["overall"]
    assert overall["chains"] == 2
    assert overall["attempts"] == 3
    assert overall["succeeded"] == 2
    assert overall["success_rate"] == 1.0
    assert overall["accepted"] == 1
    assert overall["accepted_rate"] == 0.5
    assert overall["verification"]["attempts"] == 1
    assert overall["verification"]["pass_rate"] == 0.0
    assert overall["total_tokens"]["p50"] == 140.0
    assert overall["total_tokens"]["p90"] == 500.0
    assert overall["duration_seconds"]["p50"] == 12.0
    assert overall["fallback_count"]["average"] == 0.5
    assert overall["failure_types"] == {"verification_failed": 1}
    assert overall["replan_actions"] == {"implementation_loop": 1}

    routes = feedback["routes"]
    assert routes["local:explore:scout:none"]["attempts"] == 1
    assert routes["local:explore:scout:none"]["accepted_rate"] == 0.0
    assert routes["local:explore:scout:none"]["failure_types"] == {
        "low_confidence": 1
    }
    assert routes["local2:explore:reviewer:high"]["accepted_rate"] == 1.0
    assert routes["codex:code:gpt-code:high"]["accepted_rate"] == 0.0

    raw = json.loads((tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8"))
    stored_feedback = raw["records"]["feedback00001"]["route_feedback"]
    assert stored_feedback["initial_route"] == "local:explore:scout:none"
    assert stored_feedback["final_route"] == "local2:explore:reviewer:high"
    assert stored_feedback["attempt_count"] == 2
    assert stored_feedback["fallback_count"] == 1
    assert stored_feedback["total_tokens"] == 140
    assert stored_feedback["duration_seconds"] == 12.0
    assert len(stored_feedback["attempts"]) == 2
    assert "task" not in raw["records"]["feedback00001"]


def test_delegate_telemetry_aggregates_cancel_reasons(tmp_path: Path) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    store.record_terminal(
        _snapshot(
            "cancelled001",
            status="cancelled",
            error_code="explicit_cancel",
        )
    )
    store.record_terminal(
        _snapshot(
            "cancelled002",
            status="cancelled",
            error_code="server_shutdown",
        )
    )
    store.record_terminal(
        _snapshot(
            "cancelled003",
            status="cancelled",
            error_code="cancelled",
        )
    )

    aggregate = store.snapshot()["overall"]
    assert aggregate["cancelled"] == 3
    assert aggregate["cancel_reasons"] == {
        "cancelled": 1,
        "explicit_cancel": 1,
        "server_shutdown": 1,
    }


def test_delegate_telemetry_records_code_verification_summary(tmp_path: Path) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    item = _snapshot(
        "verified000001",
        harness="codex",
        kind="code",
        model="gpt-code",
        status="succeeded",
    )
    item["code_verification"] = {
        "enabled": True,
        "verified": True,
        "contract_hash": "abc123",
        "changed_file_count": 2,
        "added_lines": 15,
        "deleted_lines": 4,
        "scope_violations": [],
        "verification_commands": [
            {"command": "pytest -q", "passed": True, "exit_code": 0},
            {"command": "python -m compileall src", "passed": True, "exit_code": 0},
        ],
    }

    store.record_terminal(item)

    raw = json.loads(
        (tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8")
    )
    verification = raw["records"]["verified000001"]["code_verification"]
    assert verification["verified"] is True
    assert verification["contract_hash"] == "abc123"
    assert verification["changed_file_count"] == 2
    assert verification["scope_violation_count"] == 0
    assert verification["verification_command_count"] == 2
    assert verification["verification_failed_count"] == 0
    aggregate = store.snapshot()["overall"]["code_verification"]
    assert aggregate == {
        "terminal": 1,
        "passed": 1,
        "rejected": 0,
        "pass_rate": 1.0,
        "scope_violation_records": 0,
        "scope_violation_count": 0,
        "verification_failed_records": 0,
        "verification_failed_count": 0,
    }


def test_delegate_telemetry_aggregates_verified_code_rejections(tmp_path: Path) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    accepted = _snapshot(
        "verified000002",
        harness="codex",
        kind="code",
        status="succeeded",
    )
    accepted["code_verification"] = {
        "enabled": True,
        "verified": True,
        "scope_violations": [],
        "verification_commands": [{"command": "pytest -q", "passed": True}],
    }
    rejected = _snapshot(
        "verified000003",
        harness="antigravity",
        kind="code",
        status="failed",
        error_code="code_scope_violation",
    )
    rejected["code_verification"] = {
        "enabled": True,
        "verified": False,
        "scope_violations": ["outside.py", "other.txt"],
        "verification_commands": [
            {"command": "pytest -q", "passed": False},
            {"command": "python -m compileall src", "passed": True},
        ],
    }

    store.record_terminal(accepted)
    store.record_terminal(rejected)

    aggregate = store.snapshot()["overall"]["code_verification"]
    assert aggregate["terminal"] == 2
    assert aggregate["passed"] == 1
    assert aggregate["rejected"] == 1
    assert aggregate["pass_rate"] == 0.5
    assert aggregate["scope_violation_records"] == 1
    assert aggregate["scope_violation_count"] == 2
    assert aggregate["verification_failed_records"] == 1
    assert aggregate["verification_failed_count"] == 1


def test_delegate_telemetry_rejects_bool_as_completion_timestamp(
    tmp_path: Path,
) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    before = time.time()

    store.record_terminal(
        _snapshot("ffffffffffff"),
        completed_at_epoch=True,
    )

    raw = json.loads(
        (tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8")
    )
    timestamp = raw["records"]["ffffffffffff"]["completed_at_epoch"]
    assert timestamp >= before
    assert timestamp != 1.0


def test_delegate_telemetry_is_bounded_and_preserves_consumed_on_refresh(
    tmp_path: Path,
) -> None:
    store = DelegateTelemetryStore(
        tmp_path / "delegate-telemetry.json",
        max_records=2,
        retention_seconds=10_000,
    )
    now = time.time()
    store.record_terminal(
        _snapshot("111111111111"),
        completed_at_epoch=now - 4,
    )
    assert store.mark_consumed(
        "111111111111",
        consumed_at_epoch=now - 3,
    ) is True
    store.record_terminal(
        _snapshot("111111111111", duration=11.0),
        completed_at_epoch=now - 2,
    )
    store.record_terminal(
        _snapshot("222222222222"),
        completed_at_epoch=now - 1,
    )
    store.record_terminal(
        _snapshot("333333333333"),
        completed_at_epoch=now,
    )

    raw = json.loads((tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8"))
    assert raw["order"] == ["222222222222", "333333333333"]
    assert "111111111111" not in raw["records"]


def test_delegate_telemetry_batch_backfill_is_atomic_and_skips_existing(
    tmp_path: Path,
) -> None:
    store = DelegateTelemetryStore(
        tmp_path / "delegate-telemetry.json",
        max_records=10,
        retention_seconds=10_000,
    )
    now = time.time()
    store.record_terminal(
        _snapshot("aaaaaaaaaaaa", duration=1.0),
        completed_at_epoch=now - 3,
    )

    added = store.record_terminals_batch(
        [
            (_snapshot("aaaaaaaaaaaa", duration=99.0), now - 2),
            (_snapshot("bbbbbbbbbbbb", duration=2.0), now - 1),
            (_snapshot("cccccccccccc", duration=3.0), now),
        ],
        skip_existing=True,
    )

    assert added == 2
    raw = json.loads(
        (tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8")
    )
    assert raw["order"] == [
        "aaaaaaaaaaaa",
        "bbbbbbbbbbbb",
        "cccccccccccc",
    ]
    assert raw["records"]["aaaaaaaaaaaa"]["duration_seconds"] == 1.0
    assert raw["records"]["bbbbbbbbbbbb"]["duration_seconds"] == 2.0
    assert raw["records"]["cccccccccccc"]["duration_seconds"] == 3.0


def test_delegate_telemetry_classifies_outcomes_and_route_provenance(
    tmp_path: Path,
) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    now = time.time()

    blocked = _snapshot("101010101010", harness="codex", status="failed")
    blocked["success"] = True  # Terminal status is canonical.
    blocked["error"] = {
        "code": "delegate_reported_blocked",
        "message": "bounded review found a real blocker",
    }
    blocked["routing_mode"] = "automatic"
    blocked["routing_reason"] = "primary_unavailable_balanced_fallback"
    infrastructure = _snapshot("202020202020", status="failed")
    infrastructure["error"] = {
        "code": "antigravity_result_error",
        "message": "Eligibility check failed: account is not eligible.",
    }
    cancelled = _snapshot("303030303030", status="cancelled")
    cancelled["error"] = {
        "code": "ownership_cleanup",
        "message": "cancelled during ownership cleanup",
    }
    cancelled["cancel_source"] = "explicit_group_cancel"
    cancelled["cancel_rationale"] = "group result no longer needed"

    store.record_terminal(blocked, completed_at_epoch=now)
    store.record_terminal(infrastructure, completed_at_epoch=now + 1)
    store.record_terminal(cancelled, completed_at_epoch=now + 2)

    snapshot = store.snapshot()
    assert snapshot["overall"]["outcomes"] == {
        "blocked": 1,
        "cancelled": 1,
        "infrastructure_error": 1,
    }
    assert snapshot["overall"]["succeeded"] == 0
    assert (
        snapshot["by_route_provenance"][
            "automatic:primary_unavailable_balanced_fallback"
        ]["terminal"]
        == 1
    )
    raw = json.loads(
        (tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8")
    )
    assert raw["records"]["101010101010"]["success"] is False
    assert raw["records"]["303030303030"]["cancel_reason"] == "ownership_cleanup"
    assert raw["records"]["303030303030"]["cancel_source"] == "explicit_group_cancel"
    assert raw["records"]["303030303030"]["cancel_rationale"] == "group result no longer needed"
    assert snapshot["overall"]["cancel_sources"] == {"explicit_group_cancel": 1}


def test_delegate_telemetry_reconcile_enriches_existing_record_and_preserves_consumption(
    tmp_path: Path,
) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    now = time.time()
    wrong_mtime = now - 10
    corrected_completion = now - 295
    store.record_terminal(
        _snapshot("404040404040", duration=5.0),
        completed_at_epoch=wrong_mtime,
        completion_timestamp_source="source_metadata_mtime",
    )
    assert store.mark_consumed(
        "404040404040",
        consumed_at_epoch=now - 5,
    )

    enriched = _snapshot("404040404040", duration=5.0)
    enriched["routing_mode"] = "automatic"
    enriched["routing_reason"] = "primary_available"
    reconciled = store.reconcile_terminals_batch(
        [(enriched, corrected_completion, "started_plus_duration")]
    )

    assert reconciled == {"added": 0, "updated": 1}
    raw = json.loads(
        (tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8")
    )
    record = raw["records"]["404040404040"]
    assert record["completed_at_epoch"] == corrected_completion
    assert record["completion_timestamp_source"] == "started_plus_duration"
    assert record["consumed_at_epoch"] == now - 5
    assert record["routing_mode"] == "automatic"
    assert record["routing_reason"] == "primary_available"


def test_delegate_telemetry_reconcile_does_not_upgrade_low_confidence_acceptance(
    tmp_path: Path,
) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    delegate_id = "414141414141"
    shape_hash = "fedcba9876543210"
    original = _snapshot(delegate_id, status="succeeded")
    original["route_decision"] = {
        "legacy_complexity": "medium",
        "task_shape_hash": shape_hash,
        "task_shape": {"purpose": "investigation", "breadth": "small"},
        "initial_route": "local2:reviewer",
        "shadow_route": None,
    }
    original["failure_analysis"] = {
        "failure_type": "low_confidence",
        "retryable": True,
        "evidence": "confidence",
    }
    original["replan"] = {
        "action": "add_reviewer",
        "reason": "low_confidence",
        "automatic": False,
        "preserve_evidence": True,
    }
    store.record_terminal(original)

    stale_recovery = _snapshot(delegate_id, status="succeeded")
    stale_recovery["route_decision"] = dict(original["route_decision"])
    reconciled = store.reconcile_terminals_batch(
        [(stale_recovery, time.time(), "runtime_terminal")]
    )

    assert reconciled == {"added": 0, "updated": 1}
    raw = json.loads(
        (tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8")
    )
    record = raw["records"][delegate_id]
    assert record["failure_analysis"]["failure_type"] == "low_confidence"
    assert record["replan"]["action"] == "add_reviewer"
    assert record["route_feedback"]["accepted"] is False
    assert record["route_feedback"]["failure_type"] == "low_confidence"
    assert record["route_feedback"]["replan_action"] == "add_reviewer"


def test_delegate_telemetry_reconcile_preserves_multi_attempt_chain(
    tmp_path: Path,
) -> None:
    store = DelegateTelemetryStore(tmp_path / "delegate-telemetry.json")
    delegate_id = "424242424242"
    shape_hash = "abcdef0123456789"
    original = _snapshot(
        delegate_id,
        harness="codex",
        model="gpt-reviewer",
        effort="low",
        status="succeeded",
        total_tokens=200,
    )
    original["route_decision"] = {
        "legacy_complexity": "medium",
        "task_shape_hash": shape_hash,
        "task_shape": {"purpose": "review", "breadth": "small"},
        "initial_route": "local:explore:qwen:none",
        "shadow_route": None,
    }
    original["route_attempts"] = [
        {
            "route": "local:explore:qwen:none",
            "harness": "local",
            "kind": "explore",
            "model": "qwen",
            "reasoning_effort": "none",
            "status": "partial",
            "success": False,
            "accepted": False,
            "duration_seconds": 3.0,
            "total_tokens": 100,
            "failure_type": "low_confidence",
            "replan_action": "add_reviewer",
        },
        {
            "route": "codex:explore:gpt-reviewer:low",
            "harness": "codex",
            "kind": "explore",
            "model": "gpt-reviewer",
            "reasoning_effort": "low",
            "status": "succeeded",
            "success": True,
            "accepted": True,
            "duration_seconds": 7.0,
            "total_tokens": 200,
            "failure_type": "none",
            "replan_action": "none",
        },
    ]
    original["route_chain"] = {
        "initial_route": "local:explore:qwen:none",
        "final_route": "codex:explore:gpt-reviewer:low",
        "attempt_count": 2,
        "fallback_count": 1,
        "total_duration_seconds": 10.0,
        "total_tokens": 300,
    }
    original["failure_analysis"] = {
        "failure_type": "none",
        "retryable": False,
        "evidence": None,
    }
    original["replan"] = {
        "action": "none",
        "reason": "none",
        "automatic": False,
        "preserve_evidence": True,
    }
    store.record_terminal(original)

    stale_recovery = _snapshot(
        delegate_id,
        harness="codex",
        model="gpt-reviewer",
        effort="low",
        status="succeeded",
        total_tokens=200,
    )
    stale_recovery["route_decision"] = dict(original["route_decision"])
    reconciled = store.reconcile_terminals_batch(
        [(stale_recovery, time.time(), "runtime_terminal")]
    )

    assert reconciled == {"added": 0, "updated": 1}
    raw = json.loads(
        (tmp_path / "delegate-telemetry.json").read_text(encoding="utf-8")
    )
    feedback = raw["records"][delegate_id]["route_feedback"]
    assert feedback["initial_route"] == "local:explore:qwen:none"
    assert feedback["final_route"] == "codex:explore:gpt-reviewer:low"
    assert feedback["attempt_count"] == 2
    assert feedback["fallback_count"] == 1
    assert feedback["total_tokens"] == 300
    assert feedback["duration_seconds"] == 10.0
    assert len(feedback["attempts"]) == 2
    assert feedback["attempts"][0]["failure_type"] == "low_confidence"
    assert feedback["attempts"][1]["accepted"] is True


def test_telemetry_completion_timestamp_prefers_execution_time_over_metadata_mtime(
    tmp_path: Path,
) -> None:
    metadata = tmp_path / "metadata.json"
    metadata.write_text("{}", encoding="utf-8")
    snapshot = {
        "completed_at_epoch": 1000.0,
        "started_at_epoch": 900.0,
        "duration_seconds": 20.0,
        "state_source_metadata_mtime_epoch": 5000.0,
    }

    timestamp, source = ExecutorRegistry._telemetry_completion_timestamp(
        snapshot,
        metadata_path=metadata,
    )
    assert (timestamp, source) == (1000.0, "completed_at_epoch")

    snapshot.pop("completed_at_epoch")
    timestamp, source = ExecutorRegistry._telemetry_completion_timestamp(
        snapshot,
        metadata_path=metadata,
    )
    assert (timestamp, source) == (920.0, "started_plus_duration")


def test_registry_runtime_info_exposes_telemetry(tmp_path: Path) -> None:
    telemetry_path = tmp_path / "telemetry.json"
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=tmp_path / "delegates",
        telemetry_state_path=telemetry_path,
    )
    assert registry.telemetry is not None
    registry.telemetry.record_terminal(
        _snapshot(
            "cccccccccccc",
            harness="codex",
            kind="explore",
            model="gpt-test",
            effort="low",
        )
    )

    runtime = registry.runtime_info()

    assert runtime["telemetry"]["enabled"] is True
    assert runtime["telemetry"]["records"] == 1
    assert runtime["telemetry"]["by_route"]["codex:explore:gpt-test:low"]["terminal"] == 1
    assert registry.note_delegate_consumed("cccccccccccc") is True
    assert registry.runtime_info()["telemetry"]["overall"]["consumed"] == 1


def test_runtime_info_reads_telemetry_outside_scheduler_lock(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=tmp_path / "delegates",
        telemetry_state_path=tmp_path / "telemetry.json",
    )
    assert registry.telemetry is not None
    observed: list[bool] = []

    def snapshot() -> dict[str, object]:
        def acquire_scheduler_lock() -> None:
            acquired = registry._lock.acquire(timeout=0.5)
            observed.append(acquired)
            if acquired:
                registry._lock.release()

        thread = threading.Thread(target=acquire_scheduler_lock)
        thread.start()
        thread.join(timeout=1)
        return {"enabled": True}

    registry.telemetry.snapshot = snapshot  # type: ignore[method-assign]

    runtime = registry.runtime_info()

    assert observed == [True]
    assert runtime["telemetry"]["enabled"] is True


def test_recovery_backfills_terminal_delegate_telemetry(tmp_path: Path) -> None:
    state_root = tmp_path / "delegate-state"
    root = state_root / "codex-delegates"
    log_dir = root / "20260928T000000Z-dddddddddddd"
    log_dir.mkdir(parents=True)
    metadata = log_dir / "metadata.json"
    source_mtime = time.time() - 120
    metadata.write_text(
        json.dumps(
            {
                "delegate_id": "dddddddddddd",
                "harness": "codex",
                "executor": "codex",
                "kind": "explore",
                "model": "gpt-test",
                "reasoning_effort": "low",
                "status": "succeeded",
                "success": True,
                "completed": True,
                "in_progress": False,
                "duration_seconds": 4.5,
                "state_source_metadata_mtime_epoch": source_mtime,
            }
        ),
        encoding="utf-8",
    )
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=state_root,
        telemetry_state_path=tmp_path / "telemetry.json",
    )

    recovered = registry.recover_persisted_delegates(roots=[root])
    telemetry = registry.runtime_info()["telemetry"]

    assert recovered["terminal_loaded"] == 1
    assert recovered["telemetry_backfilled"] == 1
    assert telemetry["records"] == 1
    assert telemetry["by_harness"]["codex"]["succeeded"] == 1
    assert telemetry["by_route"]["codex:explore:gpt-test:low"]["duration_seconds"]["p50"] == 4.5
    raw = json.loads((tmp_path / "telemetry.json").read_text(encoding="utf-8"))
    assert abs(
        raw["records"]["dddddddddddd"]["completed_at_epoch"] - source_mtime
    ) < 0.01


def test_recovery_enriches_existing_telemetry_with_execution_timestamp(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "delegate-state"
    root = state_root / "codex-delegates"
    log_dir = root / "20260928T000000Z-abababababab"
    log_dir.mkdir(parents=True)
    metadata = log_dir / "metadata.json"
    started_at = time.time() - 300
    duration = 4.5
    wrong_mtime = time.time() - 10
    payload = {
        "delegate_id": "abababababab",
        "harness": "codex",
        "executor": "codex",
        "kind": "explore",
        "model": "gpt-test",
        "reasoning_effort": "low",
        "status": "succeeded",
        "success": True,
        "completed": True,
        "in_progress": False,
        "started_at_epoch": started_at,
        "duration_seconds": duration,
        "state_source_metadata_mtime_epoch": wrong_mtime,
        "routing_mode": "automatic",
        "routing_reason": "primary_available",
    }
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    telemetry_path = tmp_path / "telemetry.json"
    store = DelegateTelemetryStore(telemetry_path)
    store.record_terminal(
        payload,
        completed_at_epoch=wrong_mtime,
        completion_timestamp_source="source_metadata_mtime",
    )
    assert store.mark_consumed(
        "abababababab",
        consumed_at_epoch=wrong_mtime + 1,
    )
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=state_root,
        telemetry_state_path=telemetry_path,
    )

    recovered = registry.recover_persisted_delegates(roots=[root])

    assert recovered["terminal_loaded"] == 1
    assert recovered["telemetry_backfilled"] == 0
    assert recovered["telemetry_enriched"] == 1
    raw = json.loads(telemetry_path.read_text(encoding="utf-8"))
    record = raw["records"]["abababababab"]
    assert abs(record["completed_at_epoch"] - (started_at + duration)) < 0.01
    assert record["completion_timestamp_source"] == "started_plus_duration"
    assert record["routing_mode"] == "automatic"
    assert record["consumed_at_epoch"] == wrong_mtime + 1


def test_recovery_excludes_migrated_legacy_history_from_telemetry_baseline(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "delegate-state"
    root = state_root / "codex-delegates"
    log_dir = root / "20260928T000000Z-eeeeeeeeeeee"
    log_dir.mkdir(parents=True)
    metadata = log_dir / "metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "delegate_id": "eeeeeeeeeeee",
                "harness": "codex",
                "executor": "codex",
                "kind": "explore",
                "model": "gpt-test",
                "reasoning_effort": "low",
                "status": "succeeded",
                "success": True,
                "completed": True,
                "in_progress": False,
                "duration_seconds": 0.01,
                "state_migrated_from": "/tmp/legacy-test-artifact",
                "state_source_metadata_mtime_epoch": time.time() - 120,
            }
        ),
        encoding="utf-8",
    )
    registry = ExecutorRegistry(
        codex_command="true",
        delegate_state_root=state_root,
        telemetry_state_path=tmp_path / "telemetry.json",
    )

    recovered = registry.recover_persisted_delegates(roots=[root])
    telemetry = registry.runtime_info()["telemetry"]

    assert recovered["terminal_loaded"] == 1
    assert recovered["telemetry_backfilled"] == 0
    assert recovered["telemetry_legacy_skipped"] == 1
    assert telemetry["records"] == 0
