from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp.delegate_ownership import persisted_owner_scope
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore
from chatgpt_web_oauth_mcp import session_continuation, server
from chatgpt_web_oauth_mcp.acp_session_store import AcpSessionIdentity, AcpSessionStore


OWNER = "openai:original-owner"
FOREIGN = "openai:foreign-owner"
FIRST = "a" * 12


def metadata(root, delegate_id=FIRST, owner=OWNER):
    directory = root / delegate_id
    directory.mkdir(parents=True)
    path = directory / "metadata.json"
    payload = {"delegate_id": delegate_id, "logical_session_id": owner,
               "harness": "antigravity", "execution_backend": "acp", "kind": "explore",
               "status": "succeeded", "completed": True, "success": True,
               "harness_metadata": {"owner_digest": hashlib.sha256(owner.encode()).hexdigest()},
               "structured_output": {"summary": "retained evidence", "findings": []}}
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return path, payload


def recovered(root):
    registry = ExecutorRegistry(codex_command="true", delegate_state_root=root)
    registry.recover_persisted_delegates(roots=[root])
    return registry


def store_with_pruned_history(path, first=FIRST):
    store = SessionCheckpointStore(path=path, ttl_seconds=86400)
    ids = [first, *(f"{i:012x}" for i in range(12))]
    for delegate_id in ids:
        store.record_runtime(session_key=OWNER, last_tool="delegate_task",
                             delegates={delegate_id: {"status": "succeeded", "terminal": True}})
        store.mark_result_consumed(OWNER, kind="delegate", result_id=delegate_id)
    assert store.owned_result(OWNER, kind="delegate", result_id=first) is None
    return SessionCheckpointStore(path=path, ttl_seconds=86400)


def test_recovered_terminal_packet_survives_consumed_inbox_pruning(tmp_path, monkeypatch):
    root = tmp_path / "delegates"
    metadata(root)
    store = store_with_pruned_history(tmp_path / "checkpoints.json")
    registry = recovered(root)
    assert registry.scheduler.get_task(FIRST) is None
    monkeypatch.setattr(session_continuation.session, "get_current_session_id", lambda: OWNER)
    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", registry)
    fn = getattr(server.delegate_packet, "fn", server.delegate_packet)
    result = fn(delegate_id=FIRST)
    assert result["success"] is True
    assert result["packet"]["agent_claims"]["summary"] == "retained evidence"
    assert not result["packet"]["reuse_eligible"]
    assert len(store.get(OWNER)["runtime"]["delegate_order"]) == 8


def test_recovered_foreign_owner_is_denied_and_hidden_from_discovery(tmp_path, monkeypatch):
    root = tmp_path / "delegates"
    metadata(root)
    registry = recovered(root)
    store = store_with_pruned_history(tmp_path / "checkpoints.json")
    monkeypatch.setattr(session_continuation.session, "get_current_session_id", lambda: FOREIGN)
    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", registry)
    monkeypatch.setattr(registry, "delegate_evidence_snapshot", lambda _: pytest.fail("must deny before evidence read"))
    fn = getattr(server.delegate_packet, "fn", server.delegate_packet)
    assert fn(delegate_id=FIRST)["error"]["code"] == "delegate_packet_owner_required"
    assert FIRST in registry.foreign_delegate_ids(logical_session_id=FOREIGN)
    assert FIRST not in registry.foreign_delegate_ids(logical_session_id=OWNER)


def test_checkpoint_and_durable_owner_conflict_remains_denied(tmp_path, monkeypatch):
    root = tmp_path / "delegates"
    metadata(root, owner=FOREIGN)
    registry = recovered(root)
    store = SessionCheckpointStore(path=tmp_path / "checkpoints.json", ttl_seconds=86400)
    store.record_runtime(session_key=OWNER, last_tool="old", delegates={FIRST: {"terminal": True}})
    monkeypatch.setattr(session_continuation.session, "get_current_session_id", lambda: OWNER)
    assert session_continuation.result_access_scope(
        SimpleNamespace(checkpoint_store=store, registry=registry), kind="delegate", result_id=FIRST,
    ) == "conflict"


@pytest.mark.parametrize("fault", ["missing", "json", "id", "owner", "digest", "digest_null"])
def test_unusable_persisted_metadata_does_not_authorize(tmp_path, fault):
    root = tmp_path / "delegates"
    path, payload = metadata(root)
    registry = recovered(root)
    if fault == "missing":
        path.unlink()
    elif fault == "json":
        path.write_text("invalid JSON")
    else:
        if fault == "id":
            payload["delegate_id"] = "b" * 12
        elif fault == "owner":
            payload["logical_session_id"] = 123
        else:
            payload["harness_metadata"]["owner_digest"] = None if fault == "digest_null" else "0" * 64
        path.write_text(json.dumps(payload))
    assert registry.delegate_ownership_scope(logical_session_id=OWNER, delegate_id=FIRST) != "owned_here"


def test_no_owner_is_inferred_from_agent_claims_or_digest_only(tmp_path):
    root = tmp_path / "delegates"
    path, payload = metadata(root)
    del payload["logical_session_id"]
    payload["structured_output"]["logical_session_id"] = OWNER
    path.write_text(json.dumps(payload))
    registry = recovered(root)
    assert registry.delegate_ownership_scope(logical_session_id=OWNER, delegate_id=FIRST) != "owned_here"


@pytest.mark.parametrize("owner", [True, 7, "", [], {}])
def test_owner_field_is_not_coerced(owner):
    assert persisted_owner_scope({"delegate_id": FIRST, "logical_session_id": owner},
                                 delegate_id=FIRST, session_id=OWNER) == "conflict"


def test_legacy_cli_owner_and_exact_session_comparison():
    payload = {"delegate_id": FIRST, "logical_session_id": OWNER, "execution_backend": "cli"}
    assert persisted_owner_scope(payload, delegate_id=FIRST, session_id=OWNER) == "owned_here"
    assert persisted_owner_scope(payload, delegate_id=FIRST, session_id=OWNER + " ") == "owned_elsewhere"


def test_acp_private_resume_owner_conflict_is_denied():
    payload = {"delegate_id": FIRST, "logical_session_id": OWNER, "execution_backend": "acp",
               "acp_resume_context": {"owner_digest": hashlib.sha256(FOREIGN.encode()).hexdigest()}}
    assert persisted_owner_scope(payload, delegate_id=FIRST, session_id=OWNER) == "conflict"


def test_real_process_preserves_private_owner_through_restart_and_pruning(tmp_path, monkeypatch):
    root = tmp_path / "delegates"
    registry = ExecutorRegistry(codex_command="python3 -c \"print('done')\"", delegate_state_root=root)
    started = []
    original = registry._process_runner._write_started_metadata

    def capture_started(task, *args, **kwargs):
        original(task, *args, **kwargs)
        started.append(json.loads(task.log_paths.metadata.read_text()))

    monkeypatch.setattr(registry._process_runner, "_write_started_metadata", capture_started)
    result = registry.run_codex(task="finish", cwd=tmp_path, timeout=5, logical_session_id=OWNER)
    assert result["success"] is True
    assert started and started[0]["logical_session_id"] == OWNER
    assert "logical_session_id" not in result
    persisted = json.loads(Path(result["logs"]["metadata"]).read_text())
    assert persisted["logical_session_id"] == OWNER
    delegate_id = result["delegate_id"]
    store = store_with_pruned_history(tmp_path / "checkpoints.json", delegate_id)
    restarted = recovered(root / "codex-delegates")
    assert restarted.scheduler.get_task(delegate_id) is None
    monkeypatch.setattr(session_continuation.session, "get_current_session_id", lambda: OWNER)
    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", restarted)
    fn = getattr(server.delegate_packet, "fn", server.delegate_packet)
    assert fn(delegate_id=delegate_id)["success"] is True

    # Queued cancellation must preserve the private binding as well.
    task = registry.scheduler.get_task(delegate_id)
    task.acp_resume_context = {"session_ref": "b" * 32}
    cancelled = registry._cancelled_result(task)
    private = json.loads(task.log_paths.metadata.read_text())
    assert "logical_session_id" not in cancelled
    assert private["logical_session_id"] == OWNER
    assert private["acp_resume_context"] == task.acp_resume_context


def bound_acp_result(tmp_path):
    root = tmp_path / "delegates"
    path, payload = metadata(root)
    del payload["logical_session_id"]
    payload.update(cwd=str(tmp_path.resolve()), model="gemini-3.8-flash-medium", reasoning_effort="medium")
    identity = AcpSessionIdentity(owner=hashlib.sha256(OWNER.encode()).hexdigest(),
        account="antigravity", workspace=str(tmp_path.resolve()), scope_digest="c" * 64)
    reference = "d" * 32
    payload["harness_metadata"].update(session_ref=reference, scope_digest=identity.scope_digest, turn_id=FIRST)
    path.write_text(json.dumps(payload))
    sessions = AcpSessionStore(tmp_path / "acp" / "sessions")
    state = sessions.load(reference, identity)
    state.update(phase="completed", turn=FIRST, turns_seen=[FIRST])
    sessions.save(reference, state)
    registry = recovered(root)
    registry.acp_backend = SimpleNamespace(state_root=tmp_path / "acp", accounts={"antigravity": {}, "antigravity2": {}})
    return registry, path, payload, sessions, reference


def test_historical_acp_packet_requires_bound_private_session(tmp_path, monkeypatch):
    registry, _, _, _, _ = bound_acp_result(tmp_path)
    assert registry.delegate_ownership_scope(logical_session_id=FOREIGN, delegate_id=FIRST) == "owned_elsewhere"
    assert FIRST in registry.foreign_delegate_ids(logical_session_id=FOREIGN)
    store = store_with_pruned_history(tmp_path / "checkpoints.json")
    monkeypatch.setattr(session_continuation.session, "get_current_session_id", lambda: OWNER)
    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", registry)
    fn = getattr(server.delegate_packet, "fn", server.delegate_packet)
    assert fn(delegate_id=FIRST)["success"] is True


@pytest.mark.parametrize("fault", ["missing", "corrupt", "permissions", "session_turn", "metadata_turn",
                                  "account", "workspace", "scope", "reference", "owner"])
def test_acp_identity_or_execution_mismatch_never_grants_access(tmp_path, fault):
    registry, path, payload, sessions, reference = bound_acp_result(tmp_path)
    checkpoint = sessions.root / (reference + ".json")
    if fault == "missing":
        checkpoint.unlink()
    elif fault == "corrupt":
        checkpoint.write_text("invalid JSON")
    elif fault == "permissions":
        checkpoint.chmod(0o644)
    elif fault == "session_turn":
        state = json.loads(checkpoint.read_text())
        state["turn"] = "b" * 12
        checkpoint.write_text(json.dumps(state))
    else:
        if fault == "metadata_turn":
            payload["harness_metadata"]["turn_id"] = "b" * 12
        elif fault == "account":
            payload["harness"] = "antigravity2"
        elif fault == "workspace":
            payload["cwd"] = str(tmp_path / "other")
        elif fault == "scope":
            payload["harness_metadata"]["scope_digest"] = "e" * 64
        elif fault == "reference":
            payload["harness_metadata"]["session_ref"] = "e" * 32
        else:
            payload["harness_metadata"]["owner_digest"] = hashlib.sha256(FOREIGN.encode()).hexdigest()
        path.write_text(json.dumps(payload))
    assert registry.delegate_ownership_scope(logical_session_id=OWNER, delegate_id=FIRST) == "conflict"
