from __future__ import annotations

from pathlib import Path

from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore


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
