from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import os
from pathlib import Path
import time

import pytest

from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore


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


def test_runtime_checkpoint_caps_automatic_references(tmp_path: Path) -> None:
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
    expected_jobs = [f"job_{index}" for index in range(4, 12)]
    expected_delegates = [f"delegate_{index}" for index in range(4, 12)]
    assert set(checkpoint["runtime"]["jobs"]) == set(expected_jobs)
    assert checkpoint["runtime"]["job_order"] == expected_jobs
    assert set(checkpoint["runtime"]["delegates"]) == set(expected_delegates)
    assert checkpoint["runtime"]["delegate_order"] == expected_delegates


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
