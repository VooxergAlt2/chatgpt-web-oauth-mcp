from __future__ import annotations

import hashlib
import json
import multiprocessing
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace

import pytest

from chatgpt_web_oauth_mcp.acp_control import AcpControlStore
from chatgpt_web_oauth_mcp.acp_session_store import AcpSessionIdentity, AcpSessionStore, AcpStateError

REF = "c" * 32
STEERING_HEADING = "\n\nAdditional instructions queued by the session owner for this turn:\n"
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


@pytest.mark.parametrize("overflow", [False, True], ids=["exact-limit", "one-byte-over"])
@pytest.mark.parametrize("old_status", ["pending", "replied"])
def test_prompt_composition_counts_utf8_header_and_separators_and_replaces_old_request(tmp_path, overflow, old_status):
    store, who = pending(tmp_path)
    if old_status == "replied":
        store.reply(REF, who, turn_id="turn-1", request_id="interaction_abc", option_id="yes")
    entries = [("first", "Проверь café."), ("second", "Then check 🙂.")]
    for message_id, text in entries:
        store.enqueue(REF, who, source_turn_id="turn-1", message_id=message_id, text=text)
    path = store.root / (REF + ".json")
    before = json.loads(path.read_text())
    prompt = "Исходный запрос"
    expected = prompt + STEERING_HEADING + "\n\n".join(text for _, text in entries)
    limit = len(expected.encode("utf-8")) - int(overflow)
    assert len(expected) < limit  # a character-count implementation would wrongly admit overflow
    composed, claimed = store.begin_prompt_turn(REF, who, turn_id="turn-2", prompt=prompt, max_prompt_bytes=limit)
    after = json.loads(path.read_text())
    assert after["active_turn"] == "turn-2" and after["turn_state"] == "active"
    assert after["request"] is None
    assert after["requests_seen"] == {key: "cancelled" for key in before["requests_seen"]}
    if overflow:
        assert composed is None and claimed == []
        assert after["messages"] == before["messages"]
    else:
        assert composed == expected and len(composed.encode("utf-8")) == limit
        assert claimed == [{"message_id": key, "text": text} for key, text in entries]
        assert all(row["status"] == "claimed" and row["target_turn_id"] == "turn-2"
                   for row in after["messages"])
    with pytest.raises(AcpStateError, match="acp_control_turn_mismatch"):
        store.reply(REF, who, turn_id="turn-1", request_id="interaction_abc", option_id="no")
    store.publish_request(REF, who, turn_id="turn-2", request_id="current-question",
                          title="Current turn?", options=OPTIONS, expires_at=time.time() + 60)
    store.reply(REF, who, turn_id="turn-2", request_id="current-question", option_id="no")
    assert store.consume_reply(REF, who, turn_id="turn-2", request_id="current-question") == "no"


def test_partial_batch_fit_claims_none_and_preserves_records_until_explicit_shorter_turn(tmp_path):
    store, who = pending(tmp_path)
    entries = [("m-first", "First🙂"), ("m-second", "Вторая инструкция")]
    for message_id, text in entries:
        store.enqueue(REF, who, source_turn_id="turn-1", message_id=message_id, text=text)
    path = store.root / (REF + ".json")
    before = json.loads(path.read_text())["messages"]
    prompt = "Original"
    first_only_limit = len((prompt + STEERING_HEADING + entries[0][1]).encode("utf-8"))
    assert store.begin_prompt_turn(REF, who, turn_id="too-large", prompt=prompt,
                                   max_prompt_bytes=first_only_limit) == (None, [])
    after = json.loads(path.read_text())["messages"]
    assert after == before
    assert [row["message_id"] for row in after] == ["m-first", "m-second"]
    assert [row["text"].encode("utf-8") for row in after] == [text.encode("utf-8") for _, text in entries]
    assert [row["text_sha256"] for row in after] == [hashlib.sha256(text.encode()).hexdigest() for _, text in entries]
    assert all(row["status"] == "queued" and row["delivery"] == "queued_next_turn" for row in after)
    store.finish_turn(REF, who, turn_id="too-large", status="failed")
    with pytest.raises(AcpStateError, match="acp_control_turn_already_started"):
        store.begin_prompt_turn(REF, who, turn_id="too-large", prompt="Short")
    restarted = AcpControlStore(store.root)
    composed, claimed = restarted.begin_prompt_turn(REF, who, turn_id="shorter", prompt="Short")
    assert composed == "Short" + STEERING_HEADING + "\n\n".join(text for _, text in entries)
    assert claimed == [{"message_id": key, "text": text} for key, text in entries]
    rows = json.loads(path.read_text())["messages"]
    assert [row["text_sha256"] for row in rows] == [row["text_sha256"] for row in before]
    assert all(row["delivery"] == "claimed_for_turn" and row["target_turn_id"] == "shorter" for row in rows)
    restarted.finish_turn(REF, who, turn_id="shorter", status="completed")
    assert restarted.begin_prompt_turn(REF, who, turn_id="after", prompt="After") == ("After", [])


@pytest.mark.parametrize("extra", ["", "x"], ids=["max-bytes", "max-plus-one"])
def test_empty_queue_preserves_full_unicode_prompt_or_records_overflow(tmp_path, extra):
    store = AcpControlStore(tmp_path / "control")
    prompt = "я" * 65536 + extra
    composed, claimed = store.begin_prompt_turn(REF, identity(tmp_path), turn_id="empty", prompt=prompt)
    assert composed == (None if extra else prompt) and claimed == []
    state = store.snapshot(REF, identity(tmp_path))
    assert state["turn_id"] == "empty" and state["turn_state"] == "active"
    assert state["steering"] == []


@pytest.mark.parametrize("changes", [
    {"prompt": None}, {"prompt": "  "}, {"prompt": "\ud800"},
    {"max_prompt_bytes": True}, {"max_prompt_bytes": 0}, {"max_prompt_bytes": 131073},
])
def test_invalid_composition_input_does_not_mutate_mailbox(tmp_path, changes):
    store, who = pending(tmp_path)
    store.enqueue(REF, who, source_turn_id="turn-1", message_id="queued", text="Keep this.")
    path = store.root / (REF + ".json")
    before = path.read_bytes()
    arguments = {"turn_id": "invalid", "prompt": "Valid", "max_prompt_bytes": 131072, **changes}
    with pytest.raises(ValueError, match="Invalid ACP prompt"):
        store.begin_prompt_turn(REF, who, **arguments)
    assert path.read_bytes() == before


@pytest.mark.parametrize("first_operation", ["enqueue", "compose"])
def test_concurrent_enqueue_has_deterministic_order_around_atomic_composition(tmp_path, monkeypatch, first_operation):
    store, who = pending(tmp_path)
    store.enqueue(REF, who, source_turn_id="turn-1", message_id="before", text="Before")
    producer, composer = AcpControlStore(store.root), AcpControlStore(store.root)
    first_store, second_store = (producer, composer) if first_operation == "enqueue" else (composer, producer)
    loaded, second_attempted, release = threading.Event(), threading.Event(), threading.Event()
    original_load, original_lease = first_store._load, second_store._lease

    def held_load(*args, **kwargs):
        state = original_load(*args, **kwargs)
        loaded.set()
        assert release.wait(5), "test coordinator did not release the first mailbox lease"
        return state

    @contextmanager
    def attempted_lease(reference):
        second_attempted.set()
        with original_lease(reference):
            yield

    monkeypatch.setattr(first_store, "_load", held_load)
    monkeypatch.setattr(second_store, "_lease", attempted_lease)

    def enqueue():
        return producer.enqueue(REF, who, source_turn_id="turn-1" if first_operation == "enqueue" else "turn-2",
                                message_id="racing", text="Racing")

    def compose():
        return composer.begin_prompt_turn(REF, who, turn_id="turn-2", prompt="Prompt")

    first_call, second_call = (enqueue, compose) if first_operation == "enqueue" else (compose, enqueue)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_call)
        try:
            assert loaded.wait(5)
            second = pool.submit(second_call)
            assert second_attempted.wait(5)
        finally:
            release.set()
        first_result, second_result = first.result(timeout=5), second.result(timeout=5)
    composed, claimed = second_result if first_operation == "enqueue" else first_result
    expected_ids = ["before", "racing"] if first_operation == "enqueue" else ["before"]
    assert [row["message_id"] for row in claimed] == expected_ids
    assert composed == "Prompt" + STEERING_HEADING + ("Before\n\nRacing" if first_operation == "enqueue" else "Before")
    fresh = AcpControlStore(store.root)
    rows = fresh.snapshot(REF, who)["steering"]
    assert [row["message_id"] for row in rows] == ["before", "racing"]
    assert rows[0]["target_turn_id"] == "turn-2"
    assert rows[1]["status"] == ("claimed" if first_operation == "enqueue" else "queued")
    fresh.finish_turn(REF, who, turn_id="turn-2", status="completed")
    next_prompt, next_claimed = fresh.begin_prompt_turn(REF, who, turn_id="turn-3", prompt="Next")
    assert next_prompt == ("Next" if first_operation == "enqueue" else "Next" + STEERING_HEADING + "Racing")
    assert [row["message_id"] for row in next_claimed] == ([] if first_operation == "enqueue" else ["racing"])


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


def test_steering_summary_tracks_queued_records_and_utf8_bytes_without_exposing_text(tmp_path):
    who = identity(tmp_path)
    store = AcpControlStore(tmp_path / "control")
    assert store.snapshot(REF, who)["steering_summary"] == {"queued_count": 0, "queued_text_utf8_bytes": 0}
    assert store.begin_turn(REF, who, turn_id="turn-1") == []
    store.enqueue(REF, who, source_turn_id="turn-1", message_id="m1", text="a")
    store.enqueue(REF, who, source_turn_id="turn-1", message_id="m2", text="я🙂")
    queued = store.snapshot(REF, who)
    assert queued["steering_summary"] == {"queued_count": 2, "queued_text_utf8_bytes": 7}
    assert all("text" not in item for item in queued["steering"])
    assert "\u044f\U0001f642" not in json.dumps(queued, ensure_ascii=False)
    store.finish_turn(REF, who, turn_id="turn-1", status="completed")
    prompt, claimed = store.begin_prompt_turn(REF, who, turn_id="turn-2", prompt="continue")
    assert len(claimed) == 2
    claimed_view = store.snapshot(REF, who)
    assert claimed_view["steering_summary"] == {"queued_count": 0, "queued_text_utf8_bytes": 0}
    assert all("text" not in item for item in claimed_view["steering"])


def test_begin_prompt_turn_overflow_preserves_queued_summary_until_shorter_continuation(tmp_path):
    who = identity(tmp_path)
    store = AcpControlStore(tmp_path / "control")
    assert store.begin_turn(REF, who, turn_id="turn-1") == []
    messages = [{"message_id": "m1", "text": "a"}, {"message_id": "m2", "text": "я🙂"}]
    for item in messages:
        store.enqueue(REF, who, source_turn_id="turn-1", **item)
    before_overflow = store.snapshot(REF, who)
    assert before_overflow["steering_summary"] == {"queued_count": 2, "queued_text_utf8_bytes": 7}
    store.finish_turn(REF, who, turn_id="turn-1", status="completed")
    prompt, claimed = store.begin_prompt_turn(REF, who, turn_id="overflow", prompt="x" * 131072)
    assert prompt is None and claimed == []
    overflow_view = store.snapshot(REF, who)
    assert overflow_view["turn_id"] == "overflow"
    assert overflow_view["steering"] == before_overflow["steering"]
    assert overflow_view["steering_summary"] == {"queued_count": 2, "queued_text_utf8_bytes": 7}
    store.finish_turn(REF, who, turn_id="overflow", status="failed")
    failed_view = store.snapshot(REF, who)
    assert failed_view["turn_state"] == "failed"
    assert failed_view["steering_summary"] == {"queued_count": 2, "queued_text_utf8_bytes": 7}
    prompt, claimed = store.begin_prompt_turn(REF, who, turn_id="short", prompt="short")
    assert claimed == messages
    final_view = store.snapshot(REF, who)
    assert final_view["steering_summary"] == {"queued_count": 0, "queued_text_utf8_bytes": 0}
    assert all(item["status"] == "claimed" and item["target_turn_id"] == "short" for item in final_view["steering"])
    store.finish_turn(REF, who, turn_id="short", status="completed")
