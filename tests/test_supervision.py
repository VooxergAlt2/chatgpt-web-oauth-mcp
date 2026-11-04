from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from chatgpt_web_oauth_mcp.acp_gateway import AcpReadonlyGateway
from chatgpt_web_oauth_mcp.supervision import (
    AgentEndpoint, SupervisorBinding, SupervisorChannel, SupervisorStore, SupervisionError,
)


def setup(tmp_path):
    binding = SupervisorBinding("a" * 64, "delegate-one", "session-one",
                                AgentEndpoint("google", "acp", "gemini-account-4", "flash-high", "high"),
                                str(tmp_path), "b" * 64)
    store = SupervisorStore(tmp_path / "supervision")
    store.open(binding, expires_at=time.time() + 60)
    return store, binding


def publish(store, binding):
    return store.publish(binding, report={"phase": "plan", "summary": "Preserve API contract", "evidence": ["a.py:1"]},
                         observations={"baseline_commit": "c" * 40, "workspace_digest": "d" * 64})


def decide(store, binding, checkpoint, *, action="steer", command_id="decision-1", instruction="Use the canonical branch"):
    return store.command(binding, action=action, command_id=command_id,
                         expected_revision=checkpoint["revision"], checkpoint_id=checkpoint["checkpoint"]["id"],
                         instruction=instruction)


def test_checkpoint_survives_controller_recreation_and_same_turn_steering(tmp_path):
    store, binding = setup(tmp_path)
    async def scenario():
        cancel = asyncio.Event()
        channel = SupervisorChannel(store, binding, deadline=time.monotonic() + 5, cancel=cancel,
                                    observe=lambda: {"baseline": "one"})
        task = asyncio.create_task(channel.checkpoint(phase="plan", summary="Need architecture decision"))
        await asyncio.sleep(0)
        reopened = SupervisorStore(store.root)
        checkpoint = reopened.snapshot(binding)
        assert checkpoint["state"] == "paused_for_supervisor" and not task.done()
        assert checkpoint["checkpoint"]["report_trust"] == "unverified_agent_claims"
        decide(reopened, binding, checkpoint, instruction="Сохрани контракт; исправь основной модуль.")
        result = await task
        assert result["action"] == "steer" and "контракт" in result["instruction"]
        assert result["scope_changed"] is False and result["accepted"] is False
        assert reopened.snapshot(binding)["decision"]["delivery"] == "issued_to_worker"
        assert not cancel.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["account", "provider", "transport", "model", "scope"])
def test_execution_binding_rejects_silent_endpoint_or_scope_switch(tmp_path, change):
    store, binding = setup(tmp_path)
    if change == "scope":
        changed = replace(binding, scope_digest="f" * 64)
    else:
        key = "account_id" if change == "account" else change
        changed = replace(binding, endpoint=replace(binding.endpoint, **{key: "different"}))
    assert changed.key == binding.key
    with pytest.raises(SupervisionError, match="supervisor_binding_mismatch"):
        store.snapshot(changed)


def test_future_api_account_uses_same_contract_without_provider_sdk(tmp_path):
    store, original = setup(tmp_path)
    binding = replace(original, execution_id="api-task", session_ref="api-conversation",
                      endpoint=AgentEndpoint("provider-x", "api", "api-account-7", "model/version-2", "adaptive"))
    store.open(binding, expires_at=time.time() + 60)
    checkpoint = publish(store, binding)
    decided = decide(store, binding, checkpoint)
    assert decided["binding"]["endpoint"]["transport"] == "api"
    assert store.snapshot(original)["state"] == "running"


def test_review_holds_checkpoint_stale_commands_and_idempotency(tmp_path):
    store, binding = setup(tmp_path)
    checkpoint = publish(store, binding)
    reviewed = decide(store, binding, checkpoint, action="review", command_id="review-1")
    assert reviewed["state"] == "review_requested" and reviewed["decision_required"]
    with pytest.raises(SupervisionError, match="supervisor_stale_revision"):
        decide(store, binding, checkpoint)
    decided = decide(store, binding, reviewed)
    assert decide(store, binding, reviewed)["already_recorded"] is True
    with pytest.raises(SupervisionError, match="supervisor_command_conflict"):
        decide(store, binding, reviewed, instruction="Different payload")
    result = store.claim_decision(binding, checkpoint_id=checkpoint["checkpoint"]["id"],
                                  observations=checkpoint["checkpoint"]["observations"])
    assert result["delivery"] == "issued_to_worker"
    assert store.claim_decision(binding, checkpoint_id=checkpoint["checkpoint"]["id"],
                                observations=checkpoint["checkpoint"]["observations"]) is None
    assert decided["revision"] > reviewed["revision"]


def test_parallel_decisions_have_one_winner(tmp_path):
    store, binding = setup(tmp_path)
    checkpoint = publish(store, binding)
    def attempt(number):
        try:
            return decide(store, binding, checkpoint, command_id=f"decision-{number}")["state"]
        except SupervisionError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    assert sorted(results) == ["decision_ready", "supervisor_stale_revision"]


def test_changed_snapshot_cannot_receive_stale_correction(tmp_path):
    store, binding = setup(tmp_path)
    checkpoint = publish(store, binding)
    decide(store, binding, checkpoint)
    with pytest.raises(SupervisionError, match="supervisor_snapshot_changed"):
        store.claim_decision(binding, checkpoint_id=checkpoint["checkpoint"]["id"], observations={"changed": True})


@pytest.mark.parametrize("action", ["cancel", "stop"])
def test_checkpoint_cancellation_and_late_decisions(tmp_path, action):
    store, binding = setup(tmp_path)
    async def scenario():
        cancel = asyncio.Event()
        channel = SupervisorChannel(store, binding, deadline=time.monotonic() + 5,
                                    cancel=cancel, observe=lambda: {})
        task = asyncio.create_task(channel.checkpoint(phase="research", summary="Checkpoint"))
        await asyncio.sleep(0)
        checkpoint = store.snapshot(binding)
        if action == "stop":
            decide(store, binding, checkpoint, action="stop")
        else:
            cancel.set()
        with pytest.raises(SupervisionError, match="supervisor_(cancelled|stopped)"):
            await task
        assert store.snapshot(binding)["state"] == "closed"
        with pytest.raises(SupervisionError, match="supervisor_execution_closed"):
            decide(store, binding, store.snapshot(binding), command_id="too-late")
    asyncio.run(scenario())


def test_gateway_request_is_cooperative_and_checkpoint_releases_reads(tmp_path):
    store, binding = setup(tmp_path)
    (tmp_path / "a.py").write_text("value = 1\n")
    async def scenario():
        channel = SupervisorChannel(store, binding, deadline=time.monotonic() + 5,
                                    cancel=asyncio.Event(), observe=lambda: {})
        gateway = AcpReadonlyGateway(workspace=tmp_path, owner=binding.owner, task_id=binding.execution_id,
                                     files_in_scope=["a.py"], supervisor=channel)
        await gateway.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        store.command(binding, action="request_checkpoint", command_id="request-1", expected_revision=0)
        response = await gateway.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                           "params": {"name": "read_text", "arguments": {"path": "a.py"}}})
        assert "supervisor_checkpoint_requested" in json.dumps(response)
        checkpoint_call = asyncio.create_task(gateway.dispatch({"jsonrpc": "2.0", "id": 3,
            "method": "tools/call", "params": {"name": "supervisor_checkpoint",
            "arguments": {"phase": "plan", "summary": "Ready for correction"}}}))
        await asyncio.sleep(0)
        checkpoint = store.snapshot(binding)
        decide(store, binding, checkpoint, action="continue")
        response = await checkpoint_call
        assert response["result"]["isError"] is False
        assert gateway.summary()["tool_calls"]["supervisor_checkpoint"] == 1
        store.assert_released(binding)
    asyncio.run(scenario())


def test_invalid_utf8_size_and_unresolved_checkpoint_prevent_acceptance(tmp_path):
    store, binding = setup(tmp_path)
    with pytest.raises(SupervisionError, match="supervisor_invalid_text"):
        store.publish(binding, report={"phase": "plan", "summary": "я" * 5000, "evidence": []}, observations={})
    checkpoint = publish(store, binding)
    with pytest.raises(SupervisionError, match="supervisor_checkpoint_unresolved"):
        store.assert_released(binding)
    decide(store, binding, checkpoint, action="escalate")
    assert store.snapshot(binding)["state"] == "escalated"


def test_authenticated_checkpoint_http_can_outlive_normal_request_timeout(tmp_path, monkeypatch):
    import chatgpt_web_oauth_mcp.acp_gateway as module
    monkeypatch.setattr(module, "HTTP_TIMEOUT", 0.05)
    store, binding = setup(tmp_path)
    (tmp_path / "a.py").write_text("value = 1\n")
    async def scenario():
        channel = SupervisorChannel(store, binding, deadline=time.monotonic() + 5,
                                    cancel=asyncio.Event(), observe=lambda: {})
        gateway = AcpReadonlyGateway(workspace=tmp_path, owner=binding.owner, task_id=binding.execution_id,
                                     files_in_scope=["a.py"], supervisor=channel)
        await gateway.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        async with await asyncio.start_server(gateway.serve_http, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "supervisor_checkpoint", "arguments": {"phase": "plan", "summary": "Review"}}}).encode()
            token = gateway.mcp_server()["headers"][0]["value"]
            writer.write((f"POST /mcp HTTP/1.1\r\nContent-Type: application/json\r\n"
                          f"X-Opss-Task-Token: {token}\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
            await writer.drain()
            await asyncio.sleep(0.15)
            checkpoint = store.snapshot(binding)
            assert checkpoint["state"] == "paused_for_supervisor"
            decide(store, binding, checkpoint, instruction="HTTP correction")
            response = await asyncio.wait_for(reader.read(), 2)
            assert response.startswith(b"HTTP/1.1 200") and b"HTTP correction" in response
            writer.close()
            await writer.wait_closed()
    asyncio.run(scenario())


def test_pinned_failure_recommendation_never_promises_provider_switch():
    from chatgpt_web_oauth_mcp.routing.failure_taxonomy import classify_failure
    from chatgpt_web_oauth_mcp.routing.replan import replan_for_failure
    analysis = classify_failure({"status": "timed_out", "error": {"code": "acp_timeout"}})
    known = replan_for_failure(analysis, allow_provider_switch=False).to_dict()
    assert known["automatic"] is False and known["action"] == "reassess_task"
    uncertain = replan_for_failure(analysis, allow_provider_switch=False, outcome_uncertain=True).to_dict()
    assert uncertain["automatic"] is False and uncertain["action"] == "reconcile_outcome"
