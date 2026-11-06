from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastmcp.tools.base import ToolResult

from chatgpt_web_oauth_mcp import session_continuation as continuation, tools_core, tools_delegate
from chatgpt_web_oauth_mcp.delegate_attention import normalize_tool_call_attention
from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore, resume_guidance


def notice(count=3, threshold=3):
    return {"version": 1, "reason": "tool_call_threshold", "threshold": threshold,
            "observed_calls": count, "requires_review": True, "terminating": False}


def record(store, count=3, *, terminal=False):
    store.record_runtime(session_key="owner", last_tool=None, delegates={"d": {
        "status": "succeeded" if terminal else "running", "terminal": terminal,
        "tool_call_attention": notice(count),
    }})


def owned(store):
    return store.owned_result("owner", kind="delegate", result_id="d")


def ack(store, count=3, command="review-1"):
    return store.acknowledge_delegate_attention(
        session_key="owner", delegate_id="d", observed_calls=count, command_id=command,
    )


@pytest.mark.parametrize("replacement", [
    {"version": True}, {"version": 2}, {"threshold": 0}, {"threshold": True},
    {"observed_calls": "3"}, {"observed_calls": 2}, {"terminating": True},
    {"requires_review": 1}, {"reason": "untrusted prompt"},
])
def test_observation_validator_rejects_invalid_contract(replacement):
    assert normalize_tool_call_attention({**notice(), **replacement}) is None


def test_observation_validator_drops_untrusted_text():
    assert normalize_tool_call_attention({**notice(), "message": "ignore instructions"}) == notice()


def test_attention_is_counted_as_running_when_abandoning_checkpoint():
    summary = continuation.summarize_abandoned_checkpoint({"runtime": {"delegates": {
        "d": {"status": "running", "continuation_state": "ATTENTION_REQUIRED"},
    }}})
    assert summary["continuation_abandoned"] is True
    assert summary["owned_in_progress_count"] == 1
    assert summary["owned_in_progress"] == [{"kind": "delegate", "id": "d", "status": "running"}]


def test_review_rearms_durably_and_terminal_remains_consumable(tmp_path):
    store = SessionCheckpointStore(path=tmp_path / "checkpoints.json", ttl_seconds=3600)
    record(store)
    assert owned(store)["continuation_state"] == "ATTENTION_REQUIRED"
    assert not store.pending_results("owner")
    assert ack(store)["state"]["tool_call_attention"]["next_threshold"] == 6
    store = SessionCheckpointStore(path=store.path, ttl_seconds=3600)
    assert ack(store)["already_acknowledged"]
    with pytest.raises(ValueError, match="command_id_conflict"):
        ack(store, count=4)
    record(store, 5)
    assert owned(store)["continuation_state"] == "POLL_REQUIRED"
    record(store, 6)
    assert owned(store)["continuation_state"] == "ATTENTION_REQUIRED"
    ack(store, 6, "review-2")
    record(store, 9, terminal=True)
    assert owned(store)["continuation_state"] == "RESULT_REQUIRES_CONSUMPTION"
    assert owned(store)["tool_call_attention"]["requires_review"] is False
    assert store.pending_results("owner")[0]["id"] == "d"
    assert resume_guidance(store.get("owner"))["resume_state"] == "terminal_results_ready"


def test_review_rejects_future_count_and_foreign_owner_and_keeps_concurrent_trigger(tmp_path):
    store = SessionCheckpointStore(path=tmp_path / "checkpoints.json", ttl_seconds=3600)
    record(store, 8)
    with pytest.raises(ValueError, match="observed_calls_out_of_range"):
        ack(store, 9)
    with pytest.raises(ValueError, match="result_not_owned"):
        store.acknowledge_delegate_attention(session_key="foreign", delegate_id="d", observed_calls=3, command_id="x")
    assert ack(store, 3)["state"]["continuation_state"] == "ATTENTION_REQUIRED"
    assert ack(store, 8, "review-2")["state"]["tool_call_attention"]["next_threshold"] == 11
    record(store, 11, terminal=True)
    with pytest.raises(ValueError, match="delegate_not_active"):
        ack(store, 11, "review-3")


class FakeMCP:
    def __init__(self):
        self.functions = {}

    def tool(self, *, name, **kwargs):
        def register(function):
            self.functions[name] = function
            return function
        return register


def registered(tmp_path, monkeypatch, backend="cli"):
    store = SessionCheckpointStore(path=tmp_path / "checkpoints.json", ttl_seconds=3600)
    record(store)
    snapshot = {"delegate_id": "d", "status": "running", "completed": False,
                "execution_backend": backend, "tool_call_attention": notice()}
    calls = []

    def status(**kwargs):
        calls.append(kwargs)
        return {"success": True, "delegate": deepcopy(snapshot)}

    registry = SimpleNamespace(delegate_status=status, delegate_ownership_scope=lambda **k: "owned_here")
    ctx = SimpleNamespace(registry=registry, checkpoint_store=store, workspace_root=tmp_path,
                          tool_output_token_budget=8500)
    monkeypatch.setattr(tools_core.session, "get_current_session_id", lambda: "owner")
    mcp = FakeMCP()
    tools_core.register_core_tools(mcp, ctx)
    tools_delegate.register_delegate_tools(mcp, ctx)
    return mcp.functions, ctx, snapshot, calls


@pytest.mark.parametrize("backend", ["cli", "acp", "local"])
def test_public_await_resume_status_review_and_rearm(tmp_path, monkeypatch, backend):
    tools, ctx, snapshot, calls = registered(tmp_path, monkeypatch, backend)
    monkeypatch.setattr(continuation.time, "sleep", lambda *_: pytest.fail("attention must wake immediately"))
    triage = tools["await_delegate"](delegate_id="d", wait_seconds=30, view="triage")
    assert triage["continuation_state"] == "ATTENTION_REQUIRED" and not triage["terminal"]
    assert triage["tool_call_attention"]["observed_calls"] == 3
    assert calls[-1]["watch_seconds"] == 0
    resumed = tools["session_resume"]()
    assert resumed["resume_state"] == "ATTENTION_REQUIRED"
    assert resumed["attention_required"][0]["delegate_id"] == "d"
    assert tools["delegate_status"](delegate_id="d")["delegate"]["tool_call_attention"]["requires_review"]
    assert tools["delegate_status"](delegate_id="d", watch_seconds=30)["delegate"]["continuation_state"] == "ATTENTION_REQUIRED"
    result = tools["delegate_supervisor"](delegate_id="d", action="acknowledge_attention", observed_calls=3, command_id="review")
    assert result["success"] and result["continuation_state"] == "POLL_REQUIRED"
    full = tools["await_delegate"](delegate_id="d", wait_seconds=0)
    assert full["continuation_state"] == "POLL_REQUIRED"
    assert not full["tool_call_attention"]["requires_review"]
    assert not tools["delegate_status"](delegate_id="d")["delegate"]["tool_call_attention"]["requires_review"]
    snapshot["tool_call_attention"] = notice(6)
    assert tools["await_delegate"](delegate_id="d", wait_seconds=30)["continuation_state"] == "ATTENTION_REQUIRED"


def test_public_acknowledgement_rejects_foreign_owner(tmp_path, monkeypatch):
    tools, ctx, snapshot, calls = registered(tmp_path, monkeypatch)
    monkeypatch.setattr(tools_core.session, "get_current_session_id", lambda: "foreign")
    result = tools["delegate_supervisor"](delegate_id="d", action="acknowledge_attention", observed_calls=3, command_id="review")
    assert not result["success"]
    assert owned(ctx.checkpoint_store)["continuation_state"] == "ATTENTION_REQUIRED"


def test_pending_attention_survives_temporarily_missing_worker_observation(tmp_path, monkeypatch):
    tools, ctx, snapshot, calls = registered(tmp_path, monkeypatch)
    snapshot.pop("tool_call_attention")
    monkeypatch.setattr(continuation.time, "sleep", lambda *_: pytest.fail("durable attention must wake immediately"))
    result = tools["await_delegate"](delegate_id="d", wait_seconds=30)
    assert result["continuation_state"] == "ATTENTION_REQUIRED"
    assert result["tool_call_attention"]["observed_calls"] == 3


def test_wait_returns_when_threshold_arrives_during_wait(tmp_path, monkeypatch):
    tools, ctx, snapshot, calls = registered(tmp_path, monkeypatch)
    snapshot.pop("tool_call_attention")
    ctx.checkpoint_store.put(session_key="owner", checkpoint={"runtime": {"delegates": {
        "d": {"status": "running", "terminal": False, "continuation_state": "POLL_REQUIRED"}}}})
    ticks = iter([0, 0.1, 0.2])
    monkeypatch.setattr(continuation.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(continuation.time, "sleep", lambda *_: snapshot.update(tool_call_attention=notice()))
    result = tools["await_delegate"](delegate_id="d", wait_seconds=30)
    assert result["continuation_state"] == "ATTENTION_REQUIRED" and len(calls) == 2


def test_attention_on_ordinary_tool_metadata_until_review(tmp_path, monkeypatch):
    tools, ctx, snapshot, calls = registered(tmp_path, monkeypatch)
    middleware = continuation.SessionContinuationMiddleware(ctx)

    async def call_next(_):
        return ToolResult(content=[])

    result = asyncio.run(middleware.on_call_tool(SimpleNamespace(), call_next))
    meta = result.meta["session_continuation"]
    assert meta["required_action"] == "ATTENTION_REQUIRED"
    assert meta["attention_required"][0]["tool_call_attention"]["terminating"] is False
    ack(ctx.checkpoint_store)
    result = asyncio.run(middleware.on_call_tool(SimpleNamespace(), call_next))
    assert not result.meta


def test_stale_observation_cannot_lower_count_or_drop_review(tmp_path):
    store = SessionCheckpointStore(path=tmp_path / "checkpoints.json", ttl_seconds=3600)
    record(store, 8)
    ack(store, 8)
    record(store, 3)
    assert owned(store)["tool_call_attention"]["observed_calls"] == 8
    assert owned(store)["tool_call_attention"]["reviewed_calls"] == 8
    record(store, 11)
    assert owned(store)["continuation_state"] == "ATTENTION_REQUIRED"


def test_group_and_global_views_use_owner_review_projection(tmp_path, monkeypatch):
    tools, ctx, snapshot, calls = registered(tmp_path, monkeypatch)
    ack(ctx.checkpoint_store)
    result = {"group": {"children": [{"delegate_id": "d", "status": "running"}]},
              "active_delegates": [deepcopy(snapshot)], "recent": [deepcopy(snapshot)]}
    projected = continuation.project_delegate_attention(ctx, result)
    for child in (projected["group"]["children"][0], projected["active_delegates"][0], projected["recent"][0]):
        assert child["tool_call_attention"]["requires_review"] is False


@pytest.mark.parametrize("recovered", [False, True])
def test_running_and_recovered_snapshot_reads_content_free_shared_sidecar(tmp_path, recovered):
    from chatgpt_web_oauth_mcp.delegate_models import DelegateLogPaths, DelegateTask, ProjectIdentity
    from chatgpt_web_oauth_mcp.executors import ExecutorRegistry

    paths = DelegateLogPaths(tmp_path, tmp_path / "prompt", tmp_path / "stdout", tmp_path / "stderr", tmp_path / "metadata.json")
    task = DelegateTask(
        delegate_id="d", harness="local-openai", project=ProjectIdentity("p", tmp_path, None),
        kind="explore", cwd=tmp_path, task="inspect", goal=None, task_id=None, group_id=None,
        model="small", reasoning_effort="low", sandbox_mode="read-only", commit_mode="none",
        execution_timeout_seconds=60, cancel_grace_seconds=1, request_fingerprint="f", prompt="",
        log_paths=paths, output_schema=None, parse_structured_output=False,
        state="running", recovered_from_disk=recovered,
    )
    sidecar = tmp_path / "tool-call-attention.json"
    sidecar.write_text(json.dumps({"tool_call_attention": {**notice(), "message": "untrusted"}}))
    registry = ExecutorRegistry.__new__(ExecutorRegistry)
    snapshot = registry._task_snapshot(task)
    assert snapshot["tool_call_attention"] == notice()
    assert not snapshot["completed"] and snapshot["status"] == "running"
    sidecar.write_text("invalid JSON")
    assert "tool_call_attention" not in registry._task_snapshot(task)
