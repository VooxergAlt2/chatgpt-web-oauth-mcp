"""Fixed native profiles, checkpoint migration, and legacy owner controls."""
from __future__ import annotations

import hashlib
import json
import re
import time
from copy import deepcopy
from dataclasses import asdict

import pytest

from chatgpt_web_oauth_mcp.acp_control import AcpControlStore
from chatgpt_web_oauth_mcp.acp_session_store import AcpSessionIdentity, AcpSessionStore, AcpStateError
from chatgpt_web_oauth_mcp.acp_tool_profiles import (
    bind_native_tool_profile, native_tool_profile, profile_sdk_kwargs,
)


PROFILES = [
    (False, False, "opss-explore-v1", ["finish", "view_file"]),
    (False, True, "opss-explore-questions-v1", ["ask_question", "finish", "view_file"]),
    (True, False, "opss-code-v1", ["create_file", "edit_file", "finish", "view_file"]),
    (True, True, "opss-code-questions-v1", ["ask_question", "create_file", "edit_file", "finish", "view_file"]),
]


@pytest.mark.parametrize("writer,questions,identifier,tools", PROFILES)
def test_profiles_match_fixed_role_contract(writer, questions, identifier, tools):
    profile = native_tool_profile(writer=writer, questions=questions)
    payload = {"version": 1, "id": identifier, "enabled_tools": tools}
    expected_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert profile == {**payload, "sha256": expected_hash}
    assert re.fullmatch("[a-f0-9]{64}", profile["sha256"])
    assert profile_sdk_kwargs(profile) == {"antigravity": {"enabledTools": tools}}
    assert {"execute", "web_search", "web_fetch", "browser"}.isdisjoint(profile["enabled_tools"])


@pytest.mark.parametrize("writer,questions", [(1, False), (False, 0), ("code", True)])
def test_profile_selector_requires_actual_trusted_booleans(writer, questions):
    with pytest.raises(AcpStateError, match="acp_tool_profile_invalid"):
        native_tool_profile(writer=writer, questions=questions)


def test_binding_is_durable_schema_transition_and_owns_its_data():
    profile = native_tool_profile(writer=False, questions=True)
    state = {"version": 1, "phase": "new", "identity": {"owner": "original-owner"}}
    assert bind_native_tool_profile(state, profile) is True
    assert state["version"] == 2  # version-1 workers reject bound sessions on downgrade
    assert state["identity"] == {"owner": "original-owner"}
    assert state["phase"] == "new"
    saved = deepcopy(state)
    assert bind_native_tool_profile(state, profile) is False
    assert state == saved
    kwargs = profile_sdk_kwargs(profile)
    kwargs["antigravity"]["enabledTools"].append("execute")
    profile["enabled_tools"].append("execute")
    assert state == saved
    assert native_tool_profile(writer=False, questions=True) == saved["native_tool_profile"]


@pytest.mark.parametrize("writer,questions", [(True, False), (False, True)])
def test_binding_cannot_drift_even_before_a_provider_session_exists(writer, questions):
    profile = native_tool_profile(writer=False, questions=False)
    state = {"version": 1}
    bind_native_tool_profile(state, profile)
    saved = deepcopy(state)
    assert "session_id" not in state
    replacement = native_tool_profile(writer=writer, questions=questions)
    with pytest.raises(AcpStateError, match="acp_tool_profile_mismatch"):
        bind_native_tool_profile(state, replacement)
    assert state == saved


@pytest.mark.parametrize("replacement", [
    {"version": True},
    {"id": "unregistered-profile"},
    {"sha256": "0" * 64},
    {"enabled_tools": ["view_file", "finish"]},
    {"enabled_tools": ["finish", "view_file", "execute"]},
    {"enabled_tools": ["finish", "view_file", "view_file"]},
    {"enabled_tools": ("finish", "view_file")},
    {"extra": "UNTRUSTED_PROFILE_VALUE"},
])
def test_malformed_profile_cannot_be_bound_or_forwarded(replacement):
    profile = {**native_tool_profile(writer=False, questions=False), **replacement}
    state = {"version": 1}
    with pytest.raises(AcpStateError, match="acp_tool_profile_invalid"):
        bind_native_tool_profile(state, profile)
    assert state == {"version": 1}
    with pytest.raises(AcpStateError, match="acp_tool_profile_invalid"):
        profile_sdk_kwargs(profile)


def test_self_consistent_hash_does_not_authorize_a_custom_tool_set():
    payload = {"version": 1, "id": "opss-explore-v1", "enabled_tools": ["execute", "finish", "view_file"]}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    forged = {**payload, "sha256": digest}
    with pytest.raises(AcpStateError, match="acp_tool_profile_invalid"):
        profile_sdk_kwargs(forged)
    state = {"version": 2, "native_tool_profile": forged, "session_id": "provider-session"}
    before = deepcopy(state)
    with pytest.raises(AcpStateError, match="acp_tool_profile_invalid"):
        bind_native_tool_profile(state, native_tool_profile(writer=False, questions=False))
    assert state == before


@pytest.mark.parametrize("version,has_profile", [(2, False), (1, True), (True, False), (3, True)])
def test_checkpoint_schema_cannot_be_silently_repaired(version, has_profile):
    profile = native_tool_profile(writer=False, questions=False)
    state = {"version": version}
    if has_profile:
        state["native_tool_profile"] = profile
    before = deepcopy(state)
    with pytest.raises(AcpStateError, match="acp_tool_profile_invalid"):
        bind_native_tool_profile(state, profile)
    assert state == before


def test_legacy_provider_history_requires_an_explicit_new_session():
    profile = native_tool_profile(writer=False, questions=False)
    state = {"version": 1, "session_id": "legacy-provider-session", "phase": "completed"}
    before = deepcopy(state)
    with pytest.raises(AcpStateError, match="acp_tool_profile_legacy_requires_new_session"):
        bind_native_tool_profile(state, profile)
    assert state == before


@pytest.mark.parametrize("bound", [False, True], ids=["legacy-v1", "profile-bound-v2"])
def test_owner_controls_and_abandon_keep_original_identity_for_both_schemas(tmp_path, bound):
    reference, turn = "a" * 32, "original-turn"
    identity = AcpSessionIdentity(owner="owner", account="account",
                                  workspace=str(tmp_path.resolve()), scope_digest="b" * 64)
    original_identity = asdict(identity)
    assert set(original_identity) == {"owner", "account", "workspace", "scope_digest", "model", "thinking"}
    store = AcpSessionStore(tmp_path / "sessions")
    state = store.load(reference, identity)
    if bound:
        bind_native_tool_profile(state, native_tool_profile(writer=True, questions=True))
    state.update(session_id="provider-session", phase="running", turn=turn, result=None)
    store.save(reference, state)
    control = AcpControlStore(tmp_path / "control")
    with store.lease(reference):
        control.begin_turn(reference, identity, turn_id=turn)
        control.publish_request(
            reference, identity, turn_id=turn, request_id="original-question",
            title="Proceed?", options=[{"id": "yes", "name": "Yes", "kind": "allow_once"}],
            expires_at=time.time() + 60,
        )
    # A controller restart reconstructs the same legacy identity without a
    # profile field; both old and newly bound sessions remain operable.
    restored_identity = AcpSessionIdentity(**original_identity)
    restored_control = AcpControlStore(control.root)
    assert restored_control.snapshot(reference, restored_identity)["pending_request"]["status"] == "pending"
    restored_control.reply(reference, restored_identity, turn_id=turn,
                           request_id="original-question", option_id="yes")
    assert restored_control.consume_reply(reference, restored_identity, turn_id=turn,
                                          request_id="original-question") == "yes"
    recovered = store.load(reference, restored_identity)
    assert recovered["identity"] == original_identity
    recovered["result"] = {"success": False, "outcome_uncertain": True}
    store.save(reference, recovered)
    receipt = store.abandon(reference, restored_identity, turn_id=turn, reason="Reviewed interrupted owner")
    assert receipt["requires_new_session"] is True and receipt["resume_allowed"] is False
    restored_control.finish_turn(reference, restored_identity, turn_id=turn, status="abandoned")
    final = store.load(reference, restored_identity)
    assert final["phase"] == "abandoned"
    assert final["version"] == (2 if bound else 1)
    mailbox = json.loads((control.root / (reference + ".json")).read_text())
    assert mailbox["version"] == 1 and mailbox["identity"] == original_identity


@pytest.mark.parametrize("version", [True, False, 0, 3, "2"])
def test_session_store_rejects_unrecognized_schema_versions(tmp_path, version):
    identity = AcpSessionIdentity(owner="owner", account="account",
                                  workspace=str(tmp_path.resolve()), scope_digest="b" * 64)
    store = AcpSessionStore(tmp_path / "sessions")
    state = store.load("a" * 32, identity)
    state["version"] = version
    store.save("a" * 32, state)
    with pytest.raises(AcpStateError, match="acp_checkpoint_version"):
        store.load("a" * 32, identity)
