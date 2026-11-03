"""Registered public ACP controls use the real controller and durable mailbox."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

import chatgpt_web_oauth_mcp.tools_delegate as module
from test_acp_operations import DID, OWNER, REF, setup, terminal


class FakeMCP:
    def __init__(self):
        self.functions = {}

    def tool(self, *, name, **kwargs):
        def register(function):
            self.functions[name] = function
            return function
        return register


def registered(tmp_path, monkeypatch):
    registry, sessions, control, who = setup(tmp_path)
    ctx = SimpleNamespace(registry=registry, workspace_root=tmp_path)
    monkeypatch.setattr(module.session, "get_current_session_id", lambda: OWNER)
    monkeypatch.setattr(module, "result_access_scope", lambda *args, **kwargs: "owned_here")
    mcp = FakeMCP()
    mapping = module.register_delegate_tools(mcp, ctx)
    assert mapping["delegate_message"] is mcp.functions["delegate_message"]
    return mcp.functions, registry, sessions, control, who


def test_real_fastmcp_schema_exposes_only_bounded_reply_steer_and_abandon(tmp_path):
    registry, _, _, _ = setup(tmp_path)
    mcp = FastMCP("acp-control-schema")
    module.register_delegate_tools(mcp, SimpleNamespace(registry=registry, workspace_root=tmp_path))
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    message = tools["delegate_message"].parameters
    reconcile = tools["delegate_reconcile"].parameters
    assert message["properties"]["action"]["enum"] == ["reply", "steer"]
    assert set(message["required"]) == {"delegate_id", "action"}
    assert reconcile["properties"]["action"]["const"] == "abandon"
    assert {"delegate_id", "reason"} <= set(reconcile["required"])
    assert not {"identity", "owner_digest", "session_ref", "allow_native_tools", "mcp_servers"} & set(message["properties"])
    assert not {"identity", "session_ref", "assume_success", "replay"} & set(reconcile["properties"])


def test_registered_reply_continues_only_owned_live_pending_request(tmp_path, monkeypatch):
    tools, registry, sessions, control, who = registered(tmp_path, monkeypatch)
    with sessions.lease(REF):
        result = tools["delegate_message"](delegate_id=DID, action="reply", turn_id=DID,
                                            request_id="interaction_pending", option_id="a")
        assert result["success"] and result["control"]["delivery"] == "recorded"
        duplicate = tools["delegate_message"](delegate_id=DID, action="reply",
                                               request_id="interaction_pending", option_id="a")
        assert duplicate["error"]["code"] == "acp_control_reply_duplicate"
    assert control.consume_reply(REF, who, turn_id=DID, request_id="interaction_pending") == "a"


def test_registered_steer_is_explicitly_queued_and_deduplicated(tmp_path, monkeypatch):
    tools, registry, sessions, control, who = registered(tmp_path, monkeypatch)
    terminal(registry, sessions, who, status="succeeded", phase="completed", uncertain=False)
    control.finish_turn(REF, who, turn_id=DID, status="completed")
    result = tools["delegate_message"](delegate_id=DID, action="steer", message_id="review-next",
                                       text="Check the second branch on the next turn.")
    assert result["success"] and result["control"]["delivery"] == "queued_next_turn"
    assert control.snapshot(REF, who)["steering"][0]["status"] == "queued"


def test_registered_reconcile_preserves_unknown_evidence_and_freezes_reference(tmp_path, monkeypatch):
    tools, registry, sessions, control, who = registered(tmp_path, monkeypatch)
    terminal(registry, sessions, who)
    before = sessions.load(REF, who)["result"]
    result = tools["delegate_reconcile"](delegate_id=DID, reason="Inspected the failed worker and files.")
    assert result["success"] and result["reconciliation"]["outcome_uncertain"]
    assert result["reconciliation"]["requires_new_session"]
    assert sessions.load(REF, who)["result"] == before
    assert control.snapshot(REF, who)["turn_state"] == "abandoned"


@pytest.mark.parametrize("tool,args", [
    ("delegate_message", {"action": "reply", "request_id": "interaction_pending", "option_id": "a"}),
    ("delegate_reconcile", {"reason": "Wrong owner"}),
])
def test_public_resume_ownership_gate_prevents_controller_dispatch(tmp_path, monkeypatch, tool, args):
    tools, _, _, _, _ = registered(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "result_access_scope", lambda *a, **kw: "owned_elsewhere")
    def unexpected(*args, **kwargs):
        raise AssertionError("controller must not run after outer owner denial")
    monkeypatch.setattr(module, "send_acp_message", unexpected)
    monkeypatch.setattr(module, "reconcile_acp", unexpected)
    result = tools[tool](delegate_id=DID, **args)
    assert result["error"]["code"] == "result_owned_by_another_session"


def test_controller_still_rejects_wrong_owner_when_resume_inbox_has_no_claim(tmp_path, monkeypatch):
    tools, _, _, control, who = registered(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "result_access_scope", lambda *a, **kw: "unowned")
    monkeypatch.setattr(module.session, "get_current_session_id", lambda: "another-logical-session")
    result = tools["delegate_message"](delegate_id=DID, action="reply",
                                       request_id="interaction_pending", option_id="a")
    assert result["error"]["code"] == "delegate_owned_by_another_session"
    assert control.snapshot(REF, who)["pending_request"]["status"] == "pending"
