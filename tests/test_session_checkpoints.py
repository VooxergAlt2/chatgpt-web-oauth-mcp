from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from chatgpt_web_oauth_mcp.session_checkpoints import (
    CLAIM_RESERVATION_TTL_SECONDS,
    MAX_CONSUMED_RUNTIME_REFERENCES,
    MAX_OWNED_RUNTIME_REFERENCES,
    MAX_UNCONSUMED_RUNTIME_REFERENCES,
    SessionCheckpointStore,
)


def _put_checkpoint_worker(path: str, index: int, start_at: float) -> str:
    delay = start_at - time.time()
    if delay > 0:
        time.sleep(delay)
    session_key = f"openai:parallel-{index}"
    SessionCheckpointStore(path=Path(path), ttl_seconds=86400).put(
        session_key=session_key,
        checkpoint={"next_action": f"continue-{index}"},
    )
    return session_key


def _record_owned_result_worker(
    path: str,
    index: int,
    start_at: float,
) -> str:
    delay = start_at - time.time()
    if delay > 0:
        time.sleep(delay)
    result_id = f"job_parallel_{index}"
    SessionCheckpointStore(path=Path(path), ttl_seconds=86400).record_runtime(
        session_key="openai:parallel-owned",
        last_tool=f"worker-{index}",
        jobs={
            result_id: {
                "status": "succeeded",
                "terminal": True,
                "exit_code": 0,
            }
        },
    )
    return result_id


def _consume_owned_result_worker(path: str, start_at: float) -> bool:
    delay = start_at - time.time()
    if delay > 0:
        time.sleep(delay)
    result = SessionCheckpointStore(
        path=Path(path),
        ttl_seconds=86400,
    ).mark_result_consumed(
        "openai:parallel-consume",
        kind="job",
        result_id="job_shared",
    )
    return bool(result["already_consumed"])


def _reserve_claim_worker(path: str, start_at: float) -> tuple[str, str]:
    delay = start_at - time.time()
    if delay > 0:
        time.sleep(delay)
    store = SessionCheckpointStore(path=Path(path), ttl_seconds=86400)
    try:
        reservation_id = store.reserve_claim_capacity("openai:claim-race", slots=1)
    except ValueError as exc:
        return "error", str(exc)
    return "ok", reservation_id


def test_checkpoint_store_persists_expires_and_closes(tmp_path: Path) -> None:
    path = tmp_path / "session-checkpoints.json"
    store = SessionCheckpointStore(path=path, ttl_seconds=86400)

    saved = store.put(
        session_key="openai:abc",
        checkpoint={
            "goal": "finish slice",
            "current_slice": "tests",
            "next_action": "inspect results",
            "done_means": ["tests pass"],
            "job_ids": ["job_1"],
            "delegate_ids": [],
            "cwd": str(tmp_path),
        },
        now=100.0,
    )

    assert saved["expires_at"] == 86500.0
    assert store.count(now=200.0) == 1
    restored = store.get("openai:abc", now=200.0)
    assert restored is not None
    assert restored["next_action"] == "inspect results"
    assert path.stat().st_mode & 0o777 == 0o600

    reloaded = SessionCheckpointStore(path=path, ttl_seconds=86400)
    assert reloaded.get("openai:abc", now=300.0)["job_ids"] == ["job_1"]

    assert reloaded.get("openai:abc", now=86500.1) is None
    assert reloaded.count(now=86500.1) == 0

    reloaded.put(
        session_key="openai:def",
        checkpoint={
            "goal": "another",
            "current_slice": "review",
            "next_action": "continue",
        },
        now=90000.0,
    )
    assert reloaded.close("openai:def") is True
    assert reloaded.get("openai:def", now=90001.0) is None
    assert reloaded.close("openai:def") is False


def test_runtime_checkpoint_survives_semantic_checkpoint_update(tmp_path: Path) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    runtime = store.record_runtime(
        session_key="openai:runtime",
        last_tool="job_start",
        cwd="/srv/project",
        jobs={
            "job_1": {
                "status": "running",
                "terminal": False,
            }
        },
        next_action="poll job",
        now=100.0,
    )
    assert runtime["runtime"]["jobs"]["job_1"]["status"] == "running"

    saved = store.put(
        session_key="openai:runtime",
        checkpoint={
            "goal": "finish slice",
            "current_slice": "tests",
            "next_action": "inspect terminal result",
            "cwd": "/srv/project",
        },
        now=110.0,
    )
    assert saved["runtime"]["jobs"]["job_1"]["status"] == "running"
    assert saved["runtime"]["last_tool"] == "job_start"
    assert saved["next_action"] == "inspect terminal result"

    updated = store.record_runtime(
        session_key="openai:runtime",
        last_tool="job_status",
        jobs={
            "job_1": {
                "status": "succeeded",
                "terminal": True,
                "exit_code": 0,
            }
        },
        next_action="consume result",
        now=120.0,
    )
    assert updated["goal"] == "finish slice"
    assert updated["runtime"]["jobs"]["job_1"]["status"] == "succeeded"
    assert updated["runtime"]["next_action"] == "consume result"
    assert (
        updated["runtime"]["jobs"]["job_1"]["continuation_state"]
        == "RESULT_REQUIRES_CONSUMPTION"
    )


def test_runtime_result_inbox_requires_explicit_consumption(tmp_path: Path) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    running = store.record_runtime(
        session_key="openai:inbox",
        last_tool="job_start",
        jobs={
            "job_1": {
                "status": "running",
                "terminal": False,
            }
        },
        now=100.0,
    )
    job = running["runtime"]["jobs"]["job_1"]
    assert job["continuation_state"] == "POLL_REQUIRED"
    assert job["owned_at"] == 100.0
    assert store.pending_results("openai:inbox", now=101.0) == []

    terminal = store.record_runtime(
        session_key="openai:inbox",
        last_tool="job_status",
        jobs={
            "job_1": {
                "status": "succeeded",
                "terminal": True,
                "exit_code": 0,
            }
        },
        now=120.0,
    )
    job = terminal["runtime"]["jobs"]["job_1"]
    assert job["continuation_state"] == "RESULT_REQUIRES_CONSUMPTION"
    assert job["owned_at"] == 100.0
    assert job["result_ready_at"] == 120.0

    pending = store.pending_results("openai:inbox", now=121.0)
    assert len(pending) == 1
    assert pending[0]["kind"] == "job"
    assert pending[0]["id"] == "job_1"
    assert pending[0]["exit_code"] == 0

    consumed = store.mark_result_consumed(
        "openai:inbox",
        kind="job",
        result_id="job_1",
        now=130.0,
    )
    assert consumed["found"] is True
    assert consumed["consumed"] is True
    assert consumed["already_consumed"] is False
    assert consumed["state"]["continuation_state"] == "RESULT_CONSUMED"
    assert consumed["state"]["result_consumed_at"] == 130.0
    assert store.pending_results("openai:inbox", now=131.0) == []

    refreshed = store.record_runtime(
        session_key="openai:inbox",
        last_tool="job_status",
        jobs={
            "job_1": {
                "status": "succeeded",
                "terminal": True,
                "exit_code": 0,
            }
        },
        now=140.0,
    )
    job = refreshed["runtime"]["jobs"]["job_1"]
    assert job["continuation_state"] == "RESULT_CONSUMED"
    assert job["result_consumed_at"] == 130.0
    assert job["result_ready_at"] == 120.0


def test_result_consumption_rejects_nonterminal_and_unknown_refs(tmp_path: Path) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:inbox",
        last_tool="delegate_task",
        delegates={
            "delegate_1": {
                "status": "running",
                "terminal": False,
            }
        },
        now=100.0,
    )

    nonterminal = store.mark_result_consumed(
        "openai:inbox",
        kind="delegate",
        result_id="delegate_1",
        now=110.0,
    )
    assert nonterminal["found"] is True
    assert nonterminal["consumed"] is False
    assert nonterminal["reason"] == "result_not_terminal"

    missing = store.mark_result_consumed(
        "openai:inbox",
        kind="job",
        result_id="missing",
        now=110.0,
    )
    assert missing == {"found": False, "consumed": False}


def test_runtime_checkpoint_preserves_active_refs_and_caps_consumed_history(
    tmp_path: Path,
) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    for index in range(12):
        store.record_runtime(
            session_key="openai:capped",
            last_tool="job_status",
            jobs={
                f"job_{index}": {
                    "status": "running",
                    "terminal": False,
                }
            },
            delegates={
                f"delegate_{index}": {
                    "status": "running",
                    "terminal": False,
                }
            },
            now=float(index),
        )

    checkpoint = store.get("openai:capped", now=20.0)
    assert checkpoint is not None
    all_jobs = [f"job_{index}" for index in range(12)]
    all_delegates = [f"delegate_{index}" for index in range(12)]
    assert checkpoint["runtime"]["job_order"] == all_jobs
    assert checkpoint["runtime"]["delegate_order"] == all_delegates

    for index in range(12):
        store.record_runtime(
            session_key="openai:capped",
            last_tool="result",
            jobs={
                f"job_{index}": {
                    "status": "succeeded",
                    "terminal": True,
                }
            },
            delegates={
                f"delegate_{index}": {
                    "status": "succeeded",
                    "terminal": True,
                }
            },
            now=100.0 + index,
        )

    pending = store.pending_results("openai:capped", now=120.0)
    assert len([item for item in pending if item["kind"] == "job"]) == 12
    assert len([item for item in pending if item["kind"] == "delegate"]) == 12

    for index in range(12):
        assert store.mark_result_consumed(
            "openai:capped",
            kind="job",
            result_id=f"job_{index}",
            now=200.0 + index,
        )["consumed"] is True
        assert store.mark_result_consumed(
            "openai:capped",
            kind="delegate",
            result_id=f"delegate_{index}",
            now=300.0 + index,
        )["consumed"] is True

    checkpoint = store.get("openai:capped", now=400.0)
    assert checkpoint is not None
    retained_jobs = all_jobs[-MAX_CONSUMED_RUNTIME_REFERENCES:]
    retained_delegates = all_delegates[-MAX_CONSUMED_RUNTIME_REFERENCES:]
    assert checkpoint["runtime"]["job_order"] == retained_jobs
    assert checkpoint["runtime"]["delegate_order"] == retained_delegates
    assert set(checkpoint["runtime"]["jobs"]) == set(retained_jobs)
    assert set(checkpoint["runtime"]["delegates"]) == set(retained_delegates)
    assert store.result_ownership_scope(
        "openai:capped",
        kind="job",
        result_id="job_0",
        now=400.0,
    ) == "unowned"
    assert store.result_ownership_scope(
        "openai:capped",
        kind="delegate",
        result_id="delegate_0",
        now=400.0,
    ) == "unowned"

    reservation_id = store.reserve_claim_capacity(
        "openai:capped",
        slots=MAX_UNCONSUMED_RUNTIME_REFERENCES,
        now=401.0,
    )
    assert store.release_claim_reservation(
        "openai:capped",
        reservation_id,
        now=402.0,
    ) is True


def test_runtime_checkpoint_rejects_overflow_without_dropping_unread_results(
    tmp_path: Path,
) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:capacity",
        last_tool="job_status",
        jobs={
            f"job_{index}": {
                "status": "succeeded",
                "terminal": True,
            }
            for index in range(MAX_UNCONSUMED_RUNTIME_REFERENCES)
        },
        now=100.0,
    )

    pending = store.pending_results("openai:capacity", now=101.0)
    assert len(pending) == MAX_UNCONSUMED_RUNTIME_REFERENCES

    with pytest.raises(ValueError, match="session ownership capacity exceeded"):
        store.record_runtime(
            session_key="openai:capacity",
            last_tool="delegate_task",
            delegates={
                "delegate_overflow": {
                    "status": "running",
                    "terminal": False,
                }
            },
            now=102.0,
        )

    pending_after_rejection = store.pending_results("openai:capacity", now=103.0)
    assert len(pending_after_rejection) == MAX_UNCONSUMED_RUNTIME_REFERENCES
    checkpoint = store.get("openai:capacity", now=103.0)
    assert checkpoint is not None
    assert "delegate_overflow" not in checkpoint["runtime"].get("delegates", {})

    consumed = store.mark_result_consumed(
        "openai:capacity",
        kind="job",
        result_id="job_0",
        now=104.0,
    )
    assert consumed["consumed"] is True
    store.record_runtime(
        session_key="openai:capacity",
        last_tool="delegate_task",
        delegates={
            "delegate_after_consume": {
                "status": "running",
                "terminal": False,
            }
        },
        now=105.0,
    )
    checkpoint = store.get("openai:capacity", now=106.0)
    assert checkpoint is not None
    assert "delegate_after_consume" in checkpoint["runtime"]["delegates"]


def test_claim_reservation_rejects_before_launch_without_mutating_runtime(
    tmp_path: Path,
) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:admission",
        last_tool="job_status",
        jobs={
            f"job_{index}": {
                "status": "succeeded",
                "terminal": True,
            }
            for index in range(MAX_UNCONSUMED_RUNTIME_REFERENCES)
        },
        now=100.0,
    )

    before = store.get("openai:admission", now=101.0)
    with pytest.raises(ValueError, match="session ownership capacity exceeded"):
        store.reserve_claim_capacity(
            "openai:admission",
            slots=1,
            now=102.0,
        )
    after = store.get("openai:admission", now=103.0)

    assert after == before


def test_claim_reservation_accounts_for_all_batch_children(tmp_path: Path) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:batch-admission",
        last_tool="job_status",
        jobs={
            f"job_{index}": {
                "status": "running",
                "terminal": False,
            }
            for index in range(MAX_UNCONSUMED_RUNTIME_REFERENCES - 1)
        },
        now=100.0,
    )

    store.reserve_claim_capacity(
        "openai:batch-admission",
        slots=1,
        now=101.0,
    )
    with pytest.raises(ValueError, match="session ownership capacity exceeded"):
        store.reserve_claim_capacity(
            "openai:batch-admission",
            slots=2,
            now=102.0,
        )


@pytest.mark.skipif(os.name != "posix", reason="Production checkpoint locking is exercised on Linux.")
def test_claim_reservation_serializes_last_available_slot_across_processes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-checkpoints.json"
    store = SessionCheckpointStore(path=path, ttl_seconds=86400)
    store.record_runtime(
        session_key="openai:claim-race",
        last_tool="job_status",
        jobs={
            f"job_{index}": {
                "status": "running",
                "terminal": False,
            }
            for index in range(MAX_UNCONSUMED_RUNTIME_REFERENCES - 1)
        },
    )

    start_at = time.time() + 0.25
    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        results = [
            future.result(timeout=10)
            for future in [
                pool.submit(_reserve_claim_worker, str(path), start_at),
                pool.submit(_reserve_claim_worker, str(path), start_at),
            ]
        ]

    successful = [detail for status, detail in results if status == "ok"]
    rejected = [detail for status, detail in results if status == "error"]
    assert len(successful) == 1
    assert len(rejected) == 1
    assert "session ownership capacity exceeded" in rejected[0]

    reservation_id = successful[0]
    with pytest.raises(ValueError, match="session ownership capacity exceeded"):
        store.record_runtime(
            session_key="openai:claim-race",
            last_tool="delegate_task",
            delegates={
                "delegate_unreserved": {
                    "status": "running",
                    "terminal": False,
                }
            },
        )

    store.record_runtime(
        session_key="openai:claim-race",
        last_tool="delegate_task",
        delegates={
            "delegate_reserved": {
                "status": "running",
                "terminal": False,
            }
        },
        claim_reservation_id=reservation_id,
    )
    checkpoint = store.get("openai:claim-race")
    assert checkpoint is not None
    assert "delegate_reserved" in checkpoint["runtime"]["delegates"]
    assert checkpoint["runtime"]["claim_reservations"] == {}


def test_claim_reservation_release_and_expiry_restore_capacity(tmp_path: Path) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:claim-lease",
        last_tool="job_status",
        jobs={
            f"job_{index}": {
                "status": "running",
                "terminal": False,
            }
            for index in range(MAX_UNCONSUMED_RUNTIME_REFERENCES - 1)
        },
        now=100.0,
    )

    first = store.reserve_claim_capacity(
        "openai:claim-lease",
        slots=1,
        now=101.0,
    )
    assert store.release_claim_reservation(
        "openai:claim-lease",
        first,
        now=102.0,
    ) is True
    second = store.reserve_claim_capacity(
        "openai:claim-lease",
        slots=1,
        now=103.0,
    )
    third = store.reserve_claim_capacity(
        "openai:claim-lease",
        slots=1,
        now=103.0 + CLAIM_RESERVATION_TTL_SECONDS + 1.0,
    )

    assert second != third
    checkpoint = store.get(
        "openai:claim-lease",
        now=103.0 + CLAIM_RESERVATION_TTL_SECONDS + 2.0,
    )
    assert checkpoint is not None
    reservations = checkpoint["runtime"]["claim_reservations"]
    assert second not in reservations
    assert third in reservations


def test_batch_claim_requires_exact_reserved_slot_count(tmp_path: Path) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    reservation_id = store.reserve_claim_capacity(
        "openai:batch-claim",
        slots=2,
        now=100.0,
    )

    with pytest.raises(ValueError, match="slot count does not match"):
        store.record_runtime(
            session_key="openai:batch-claim",
            last_tool="delegate_batch",
            delegates={
                "delegate_only_one": {
                    "status": "running",
                    "terminal": False,
                }
            },
            claim_reservation_id=reservation_id,
            now=101.0,
        )

    checkpoint = store.get("openai:batch-claim", now=102.0)
    assert checkpoint is not None
    assert reservation_id in checkpoint["runtime"]["claim_reservations"]
    assert checkpoint["runtime"].get("delegates", {}) == {}


def test_checkpoint_store_prunes_only_expired_entries(tmp_path: Path) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=100,
    )
    store.put(
        session_key="old",
        checkpoint={"next_action": "old"},
        now=0.0,
    )
    store.put(
        session_key="new",
        checkpoint={"next_action": "new"},
        now=80.0,
    )

    assert store.count(now=120.0) == 1
    assert store.get("old", now=120.0) is None
    assert store.get("new", now=120.0)["next_action"] == "new"


@pytest.mark.skipif(os.name != "posix", reason="Production checkpoint locking is exercised on Linux.")
def test_parallel_process_puts_preserve_every_session(tmp_path: Path) -> None:
    path = tmp_path / "session-checkpoints.json"
    start_at = time.time() + 0.5
    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=8, mp_context=context) as pool:
        futures = [
            pool.submit(_put_checkpoint_worker, str(path), index, start_at)
            for index in range(8)
        ]
        session_keys = [future.result(timeout=10) for future in futures]

    reloaded = SessionCheckpointStore(path=path, ttl_seconds=86400)
    assert reloaded.count() == 8
    assert {key for key in session_keys if reloaded.get(key) is not None} == set(session_keys)
    assert not list(tmp_path.glob(".session-checkpoints.json.*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="Production checkpoint locking is exercised on Linux.")
def test_parallel_reconcile_preserves_every_result_for_same_session(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session-checkpoints.json"
    start_at = time.time() + 0.5
    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=8, mp_context=context) as pool:
        futures = [
            pool.submit(_record_owned_result_worker, str(path), index, start_at)
            for index in range(8)
        ]
        result_ids = [future.result(timeout=10) for future in futures]

    reloaded = SessionCheckpointStore(path=path, ttl_seconds=86400)
    pending = reloaded.pending_results("openai:parallel-owned")
    assert {item["id"] for item in pending} == set(result_ids)
    assert len(pending) == 8


@pytest.mark.skipif(os.name != "posix", reason="Production checkpoint locking is exercised on Linux.")
def test_parallel_consume_is_idempotent_for_same_result(tmp_path: Path) -> None:
    path = tmp_path / "session-checkpoints.json"
    store = SessionCheckpointStore(path=path, ttl_seconds=86400)
    store.record_runtime(
        session_key="openai:parallel-consume",
        last_tool="job_status",
        jobs={
            "job_shared": {
                "status": "succeeded",
                "terminal": True,
                "exit_code": 0,
            }
        },
    )

    start_at = time.time() + 0.5
    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        futures = [
            pool.submit(_consume_owned_result_worker, str(path), start_at)
            for _ in range(4)
        ]
        already_consumed = [future.result(timeout=10) for future in futures]

    assert already_consumed.count(False) == 1
    assert already_consumed.count(True) == 3
    assert store.pending_results("openai:parallel-consume") == []
