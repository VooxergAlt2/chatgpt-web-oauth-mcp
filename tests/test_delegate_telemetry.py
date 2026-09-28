from __future__ import annotations

import json
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
