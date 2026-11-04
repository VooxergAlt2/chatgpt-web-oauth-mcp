from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from chatgpt_web_oauth_mcp.session_checkpoints import (
    CONTINUE_EXECUTION,
    POLL_REQUIRED,
    RESULT_CONSUMED,
    RESULT_REQUIRES_CONSUMPTION,
    SessionCheckpointStore,
    resume_guidance,
)

STALE = "Consume an obsolete result from an earlier tool call."


def _state(continuation, error=None):
    terminal = continuation != POLL_REQUIRED
    state = {
        "status": "succeeded" if terminal else "running",
        "terminal": terminal,
        "completed": terminal,
        "success": terminal,
        "continuation_state": continuation,
    }
    if error:
        state.update(status="cancelled", success=False, error={"code": error})
    return state


def _checkpoint(jobs=None, delegates=None, plan=None):
    checkpoint = {
        "runtime": {
            "jobs": jobs or {},
            "delegates": delegates or {},
            "next_action": STALE,
        }
    }
    if plan is not None:
        checkpoint["next_action"] = plan
    return checkpoint


def _call(tool, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    result = fn(**kwargs)
    return asyncio.run(result) if asyncio.iscoroutine(result) else result


@pytest.fixture
def owned_session(tmp_path, monkeypatch):
    from chatgpt_web_oauth_mcp import server, session

    key = "openai:resume-guidance-" + tmp_path.name
    store = SessionCheckpointStore(path=tmp_path / "checkpoints.json", ttl_seconds=86400)

    class Registry:
        def job_status(self, **kwargs):
            result_id = kwargs["job_id"]
            state = store.get(key)["runtime"]["jobs"][result_id]
            return {
                "success": True,
                "job_id": result_id,
                "status": state["status"],
                "exit_code": 0 if state["terminal"] else None,
            }

        def delegate_status(self, **kwargs):
            result_id = kwargs["delegate_id"]
            state = store.get(key)["runtime"]["delegates"][result_id]
            return {"success": True, "delegate": {"delegate_id": result_id, **state}}

        def note_delegate_consumed(self, _result_id):
            pass

    registry = Registry()
    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "job_registry", registry)
    monkeypatch.setattr(server, "registry", registry)
    binding = session.bind_session(key)
    try:
        yield server, store, key
    finally:
        session.reset_session_binding(binding)


def _record(store, key, *, jobs=None, delegates=None):
    store.record_runtime(
        session_key=key,
        last_tool="job_status",
        jobs=jobs,
        delegates=delegates,
        next_action=STALE,
    )


@pytest.mark.parametrize("runtime", [None, {}, {"next_action": STALE}])
def test_idle_ignores_historical_action(runtime):
    result = resume_guidance({"runtime": runtime})
    assert result["resume_state"] == "runtime_checkpoint"
    assert result["pending_results"] == []
    assert result["next_action"] == CONTINUE_EXECUTION
    assert STALE not in result["next_action"]


@pytest.mark.parametrize("error", ["server_restart", "server_shutdown"])
def test_consumed_interruption_preserves_only_saved_plan(error):
    snapshot = _checkpoint(
        delegates={"old": _state(RESULT_CONSUMED, error)},
        plan="Continue the saved engineering task.",
    )
    result = resume_guidance(snapshot)
    assert result["resume_state"] == "semantic_checkpoint"
    assert result["pending_results"] == []
    assert result["next_action"] == snapshot["next_action"]


def test_mixed_pending_includes_every_kind_and_does_not_mutate_input():
    snapshot = _checkpoint(
        jobs={
            "same": _state(RESULT_CONSUMED),
            "ordinary": _state(RESULT_REQUIRES_CONSUMPTION),
            "live": _state(POLL_REQUIRED),
        },
        delegates={"same": _state(RESULT_REQUIRES_CONSUMPTION, "server_restart")},
        plan="Ship the reviewed change.",
    )
    before = deepcopy(snapshot)
    result = resume_guidance(snapshot)
    assert result["resume_state"] == "delegate_interrupted_by_server_restart"
    assert {(item["kind"], item["id"]) for item in result["pending_results"]} == {
        ("job", "ordinary"), ("delegate", "same")
    }
    assert "job:ordinary" in result["next_action"]
    assert "delegate:same" in result["next_action"]
    assert "partial work" in result["next_action"]
    assert snapshot["next_action"] in result["next_action"]
    assert STALE not in result["next_action"]
    result["pending_results"][1]["error"]["code"] = "changed-copy"
    assert snapshot == before


def test_active_work_supersedes_stale_consume_and_preserves_plan():
    snapshot = _checkpoint(
        jobs={"live": _state(POLL_REQUIRED)},
        delegates={"old": _state(RESULT_CONSUMED, "server_shutdown")},
        plan="Review the implementation.",
    )
    result = resume_guidance(snapshot)
    assert result["resume_state"] == "owned_work_in_progress"
    assert result["pending_results"] == []
    assert "job:live" in result["next_action"]
    assert "until terminal" in result["next_action"]
    assert "Review the implementation." in result["next_action"]
    assert STALE not in result["next_action"]
    assert "delegate:old" not in result["next_action"]


def test_pending_projection_preserves_store_order(tmp_path):
    store = SessionCheckpointStore(path=tmp_path / "checkpoint.json", ttl_seconds=86400)
    _record(store, "session", jobs={"b": _state(RESULT_REQUIRES_CONSUMPTION)})
    _record(store, "session", jobs={"a": _state(RESULT_REQUIRES_CONSUMPTION)})
    _record(store, "session", delegates={"b": _state(RESULT_REQUIRES_CONSUMPTION)})
    guidance = resume_guidance(store.get("session"))
    assert guidance["pending_results"] == store.pending_results("session")
    assert [(item["kind"], item["id"]) for item in guidance["pending_results"]] == [
        ("job", "b"), ("job", "a"), ("delegate", "b")
    ]


def test_registered_tools_consume_pending_then_active_then_saved_plan(owned_session):
    server, store, key = owned_session
    plan = "Run final engineering acceptance."
    store.put(session_key=key, checkpoint={"next_action": plan})
    _record(store, key, jobs={
        "a": _state(RESULT_REQUIRES_CONSUMPTION),
        "b": _state(RESULT_REQUIRES_CONSUMPTION),
        "live": _state(POLL_REQUIRED),
    })
    resumed = _call(server.session_resume)
    assert {item["id"] for item in resumed["pending_results"]} == {"a", "b"}

    consumed = _call(server.mark_result_consumed, kind="job", result_id="a")
    assert consumed["success"]
    assert [item["id"] for item in consumed["pending_results"]] == ["b"]
    repeated = _call(server.mark_result_consumed, kind="job", result_id="a")
    assert repeated["already_consumed"]
    assert "job:b" in repeated["next_action"]
    assert "job:a" not in repeated["next_action"]

    consumed = _call(server.mark_result_consumed, kind="job", result_id="b")
    assert consumed["resume_state"] == "owned_work_in_progress"
    assert "job:live" in consumed["next_action"]
    _record(store, key, jobs={"live": _state(RESULT_REQUIRES_CONSUMPTION)})
    assert "job:live" in _call(server.session_resume)["next_action"]
    consumed = _call(server.mark_result_consumed, kind="job", result_id="live")
    assert consumed["resume_state"] == "semantic_checkpoint"
    assert consumed["next_action"] == plan
    resumed = _call(server.session_resume)
    assert resumed["pending_results"] == []
    assert resumed["next_action"] == plan
    assert resumed["runtime"]["next_action"] == plan
    assert resumed["checkpoint"]["runtime"]["next_action"] == plan
    assert resumed["checkpoint"]["next_action"] == plan
    assert store.get(key)["runtime"]["next_action"] == STALE


@pytest.mark.parametrize("error", ["server_restart", "server_shutdown"])
def test_registered_resume_does_not_resurrect_consumed_interruption(owned_session, error):
    server, store, key = owned_session
    _record(store, key, delegates={"old": _state(RESULT_REQUIRES_CONSUMPTION, error)})
    assert _call(server.session_resume)["resume_state"] == "delegate_interrupted_by_server_restart"
    consumed = _call(server.mark_result_consumed, kind="delegate", result_id="old")
    assert consumed["success"]
    assert consumed["pending_results"] == []
    assert consumed["resume_state"] == "runtime_checkpoint"
    resumed = _call(server.session_resume)
    assert resumed["resume_state"] == "runtime_checkpoint"
    assert resumed["next_action"] == consumed["next_action"]
    assert resumed["runtime"]["next_action"] == resumed["next_action"]
    assert resumed["checkpoint"]["runtime"]["next_action"] == resumed["next_action"]
    assert STALE not in resumed["next_action"]


def test_resume_uses_post_refresh_snapshot_not_earlier_pending_list(owned_session, monkeypatch):
    from chatgpt_web_oauth_mcp import tools_core

    server, store, key = owned_session
    _record(store, key, jobs={"old": _state(RESULT_REQUIRES_CONSUMPTION)})
    earlier_pending = store.pending_results(key)

    def refresh(*_args, **_kwargs):
        store.mark_result_consumed(key, kind="job", result_id="old")
        _record(store, key, jobs={"new": _state(RESULT_REQUIRES_CONSUMPTION)})
        return {"jobs": [], "delegates": [], "pending_results": earlier_pending}

    monkeypatch.setattr(tools_core, "refresh_session_owned_work", refresh)
    resumed = _call(server.session_resume)
    assert [item["id"] for item in resumed["pending_results"]] == ["new"]
    assert "job:new" in resumed["next_action"]
    assert "job:old" not in resumed["next_action"]


def test_consumption_observes_new_completion_from_one_snapshot(owned_session, monkeypatch):
    from chatgpt_web_oauth_mcp import tools_core

    server, store, key = owned_session
    _record(store, key, jobs={"old": _state(RESULT_REQUIRES_CONSUMPTION)})
    original_consume = store.mark_result_consumed

    def consume_then_complete(*args, **kwargs):
        result = original_consume(*args, **kwargs)
        _record(store, key, jobs={"new": _state(RESULT_REQUIRES_CONSUMPTION)})
        return result

    def forbid_separate_pending_read(*_args, **_kwargs):
        pytest.fail("guidance and pending results must come from one checkpoint snapshot")

    monkeypatch.setattr(tools_core, "refresh_session_owned_work", lambda *_a, **_k: {})
    monkeypatch.setattr(store, "mark_result_consumed", consume_then_complete)
    monkeypatch.setattr(store, "pending_results", forbid_separate_pending_read)
    consumed = _call(server.mark_result_consumed, kind="job", result_id="old")
    assert consumed["success"]
    assert [item["id"] for item in consumed["pending_results"]] == ["new"]
    assert consumed["resume_state"] == "terminal_results_ready"
    assert "job:new" in consumed["next_action"]
    assert "job:old" not in consumed["next_action"]
