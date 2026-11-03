from __future__ import annotations

import hashlib
import json
import multiprocessing
import time
from dataclasses import replace

import pytest

from chatgpt_web_oauth_mcp.acp_control import AcpControlStore
from chatgpt_web_oauth_mcp.acp_session_store import AcpSessionIdentity, AcpSessionStore, AcpStateError

REF = "c" * 32
OPTIONS = [{"id": "yes", "name": "Use proposed scope", "kind": "allow_once"},
           {"id": "no", "name": "Keep current scope", "kind": "reject_once"}]


def identity(tmp_path):
    return AcpSessionIdentity(owner="owner-one", account="account-three", workspace=str(tmp_path.resolve()),
                              scope_digest=hashlib.sha256(b"readonly-scope").hexdigest())


def pending(tmp_path, expiry=None):
    who = identity(tmp_path)
    store = AcpControlStore(tmp_path / "control")
    assert store.begin_turn(REF, who, turn_id="turn-1") == []
    store.publish_request(REF, who, turn_id="turn-1", request_id="interaction_abc",
                          title="Which scope?", options=OPTIONS,
                          expires_at=expiry or time.time() + 60)
    return store, who


def test_live_reply_is_durable_private_and_consumed_once_after_controller_restart(tmp_path):
    store, who = pending(tmp_path)
    before = store.snapshot(REF, who)["pending_request"]
    assert before["status"] == "pending" and before["expires_at"] > time.time()
    restarted = AcpControlStore(store.root)
    receipt = restarted.reply(REF, who, turn_id="turn-1", request_id="interaction_abc", option_id="no")
    assert receipt["delivery"] == "recorded"
    assert AcpControlStore(store.root).consume_reply(
        REF, who, turn_id="turn-1", request_id="interaction_abc") == "no"
    after = store.snapshot(REF, who)["pending_request"]
    assert after["status"] == "consumed" and after["disposition"] == "response_committed"
    with pytest.raises(AcpStateError, match="acp_control_reply_duplicate"):
        restarted.reply(REF, who, turn_id="turn-1", request_id="interaction_abc", option_id="no")
    with pytest.raises(AcpStateError, match="acp_control_request_closed"):
        store.consume_reply(REF, who, turn_id="turn-1", request_id="interaction_abc")
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert (store.root / (REF + ".json")).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mutation", [
    {"owner": "other-owner"}, {"account": "other-account"}, {"scope_digest": "f" * 64}, {"thinking": "high"},
])
def test_reply_binding_rejects_other_owner_account_scope_and_model_effort(tmp_path, mutation):
    store, who = pending(tmp_path)
    with pytest.raises(AcpStateError, match="acp_session_binding_mismatch"):
        store.reply(REF, replace(who, **mutation), turn_id="turn-1", request_id="interaction_abc", option_id="yes")
    assert store.snapshot(REF, who)["pending_request"]["status"] == "pending"


@pytest.mark.parametrize("changes,code", [
    ({"turn_id": "turn-old"}, "acp_control_turn_mismatch"),
    ({"request_id": "interaction_other"}, "acp_control_request_mismatch"),
    ({"option_id": "allow_always"}, "acp_control_option_mismatch"),
])
def test_reply_rejects_stale_turn_wrong_request_and_unoffered_option(tmp_path, changes, code):
    store, who = pending(tmp_path)
    args = {"turn_id": "turn-1", "request_id": "interaction_abc", "option_id": "yes"}
    with pytest.raises(AcpStateError, match=code):
        store.reply(REF, who, **(args | changes))


def test_expiry_rejects_even_a_previously_recorded_but_unconsumed_reply(tmp_path, monkeypatch):
    store, who = pending(tmp_path)
    store.reply(REF, who, turn_id="turn-1", request_id="interaction_abc", option_id="yes")
    expiry = store.snapshot(REF, who)["pending_request"]["expires_at"]
    monkeypatch.setattr("chatgpt_web_oauth_mcp.acp_control.time.time", lambda: expiry + 1)
    with pytest.raises(AcpStateError, match="acp_input_expired"):
        store.consume_reply(REF, who, turn_id="turn-1", request_id="interaction_abc")
    with pytest.raises(AcpStateError, match="acp_control_request_closed"):
        store.reply(REF, who, turn_id="turn-1", request_id="interaction_abc", option_id="yes")
    assert store.snapshot(REF, who)["pending_request"]["status"] == "expired"


def _racing_reply(root, who, barrier, results):
    store = AcpControlStore(root)
    barrier.wait(5)
    try:
        store.reply(REF, who, turn_id="turn-1", request_id="interaction_abc", option_id="yes")
        results.put("accepted")
    except AcpStateError as exc:
        results.put(exc.code)


def test_concurrent_controller_processes_cannot_accept_duplicate_replies(tmp_path):
    store, who = pending(tmp_path)
    context = multiprocessing.get_context("fork")
    barrier, results = context.Barrier(2), context.Queue()
    children = [context.Process(target=_racing_reply, args=(store.root, who, barrier, results)) for _ in range(2)]
    try:
        for child in children:
            child.start()
        replies = sorted(results.get(timeout=5) for _ in children)
        assert replies == ["accepted", "acp_control_reply_duplicate"]
        for child in children:
            child.join(5)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
                child.join(5)


def test_steering_is_queued_for_next_turn_and_ids_never_reused(tmp_path):
    store, who = pending(tmp_path)
    receipt = store.enqueue(REF, who, source_turn_id="turn-1", message_id="steer-1", text="Also inspect error handling.")
    assert receipt["delivery"] == "queued_next_turn"
    assert store.snapshot(REF, who)["steering"][0]["status"] == "queued"
    with pytest.raises(AcpStateError, match="acp_control_message_duplicate"):
        store.enqueue(REF, who, source_turn_id="turn-1", message_id="steer-1", text="Different body.")
    store.finish_turn(REF, who, turn_id="turn-1", status="completed")
    claimed = AcpControlStore(store.root).begin_turn(REF, who, turn_id="turn-2")
    assert claimed == [{"message_id": "steer-1", "text": "Also inspect error handling."}]
    assert store.snapshot(REF, who)["steering"][0]["delivery"] == "claimed_for_turn"
    store.finish_turn(REF, who, turn_id="turn-2", status="completed")
    assert store.begin_turn(REF, who, turn_id="turn-3") == []
    with pytest.raises(AcpStateError, match="acp_control_turn_mismatch"):
        store.enqueue(REF, who, source_turn_id="turn-1", message_id="steer-2", text="Stale steering")


def test_uncertain_session_controls_freeze_and_abandonment_drops_queued_delivery(tmp_path):
    store, who = pending(tmp_path)
    store.enqueue(REF, who, source_turn_id="turn-1", message_id="steer-1", text="Review later.")
    store.finish_turn(REF, who, turn_id="turn-1", status="uncertain")
    with pytest.raises(AcpStateError, match="acp_control_session_frozen"):
        store.begin_turn(REF, who, turn_id="turn-2")
    with pytest.raises(AcpStateError, match="acp_control_turn_closed"):
        store.reply(REF, who, turn_id="turn-1", request_id="interaction_abc", option_id="yes")
    store.finish_turn(REF, who, turn_id="turn-1", status="abandoned")
    state = store.snapshot(REF, who)
    assert state["steering"][0]["delivery"] == "session_abandoned"
    assert state["pending_request"]["status"] == "cancelled"


def test_control_queue_is_bounded_and_broad_permissions_cannot_be_published(tmp_path):
    store, who = pending(tmp_path)
    for i in range(8):
        store.enqueue(REF, who, source_turn_id="turn-1", message_id=f"m{i}", text="Bounded instruction")
    with pytest.raises(AcpStateError, match="acp_control_message_limit"):
        store.enqueue(REF, who, source_turn_id="turn-1", message_id="overflow", text="No room")
    with pytest.raises(AcpStateError, match="acp_control_invalid_options"):
        store.publish_request(REF, who, turn_id="turn-1", request_id="interaction_new", title="Approve all?",
                              options=[{"id": "always", "name": "Everything", "kind": "allow_always"}],
                              expires_at=time.time() + 60)


@pytest.mark.parametrize("result", [None, {"success": False, "status": "failed", "outcome_uncertain": True,
                                        "error": {"code": "acp_cancel_unconfirmed"}}])
def test_explicit_abandon_freezes_uncertain_checkpoint_without_fabricating_outcome(tmp_path, result):
    who = identity(tmp_path)
    sessions = AcpSessionStore(tmp_path / "sessions")
    state = sessions.load(REF, who)
    state.update(phase="running", turn="turn-1", result=result, turns_seen=["turn-1"], session_id="provider-session")
    sessions.save(REF, state)
    with sessions.lease(REF):
        with pytest.raises(AcpStateError, match="acp_session_busy"):
            sessions.abandon(REF, who, turn_id="turn-1", reason="Reviewed worker failure.")
    with pytest.raises(AcpStateError, match="acp_control_turn_mismatch"):
        sessions.abandon(REF, who, turn_id="turn-old", reason="Wrong attempt")
    receipt = sessions.abandon(REF, who, turn_id="turn-1", reason="Reviewed artifacts; begin fresh explicitly.")
    after = sessions.load(REF, who)
    assert receipt["requires_new_session"] and not receipt["resume_allowed"]
    assert receipt["outcome_uncertain"] and after["phase"] == "abandoned"
    assert after["result"] == result and after["session_id"] == "provider-session"
    assert after["turns_seen"] == ["turn-1"]
    assert after["identity"] == state["identity"]  # old identity schema remains compatible


def test_terminal_success_cannot_be_abandoned_as_unknown(tmp_path):
    who = identity(tmp_path)
    sessions = AcpSessionStore(tmp_path / "sessions")
    state = sessions.load(REF, who)
    state.update(phase="completed", turn="turn-1", result={"success": True})
    sessions.save(REF, state)
    with pytest.raises(AcpStateError, match="acp_abandon_requires_uncertain_turn"):
        sessions.abandon(REF, who, turn_id="turn-1", reason="Do not overwrite evidence")


def test_mailbox_symlink_and_nonprivate_checkpoint_fail_closed(tmp_path):
    store, who = pending(tmp_path)
    path = store.root / (REF + ".json")
    path.chmod(0o644)
    with pytest.raises(AcpStateError, match="acp_control_invalid"):
        store.snapshot(REF, who)
    path.unlink()
    target = tmp_path / "outside.json"
    target.write_text("{}")
    path.symlink_to(target)
    with pytest.raises(OSError):
        store.snapshot(REF, who)
    assert json.loads(target.read_text()) == {}
