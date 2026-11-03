from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.acp_session import (
    AcpRunLimits,
    AcpSessionRunner,
    AcpTurnClient,
)
from chatgpt_web_oauth_mcp.acp_control import AcpControlStore
from chatgpt_web_oauth_mcp.acp_session_store import (
    AcpSessionIdentity,
    AcpSessionStore,
    AcpStateError,
)

REF = "a" * 32
SID = "123e4567-e89b-12d3-a456-426614174000"
ANSWER = '{"status":"succeeded","summary":"checked","findings":["sample.py:1"]}'


def identity(tmp_path: Path) -> AcpSessionIdentity:
    return AcpSessionIdentity(
        owner="task-qa", account="acp-qa", workspace=str(tmp_path.resolve()),
        scope_digest=hashlib.sha256(b"read-only:sample.py").hexdigest(),
    )


class FakeAgent:
    def __init__(self, scenario="ok"):
        self.scenario = scenario
        self.starts = 0
        self.prompts = 0
        self.loads = 0
        self.cancels = 0
        self.reply = ANSWER
        self.load_supported = True
        self.fail_config = False
        self.configs = [
            {"id": "model", "current_value": "gemini-3.8-flash", "options": [{"value": "gemini-3.8-flash"}]},
            {"id": "thinking_level", "current_value": "high", "options": [{"value": v} for v in ("low", "medium", "high")]},
            {"id": "mode", "current_value": "default", "options": [{"value": "default"}, {"value": "yolo"}]},
        ]

    @asynccontextmanager
    async def connect(self, client):
        self.starts += 1
        self.client = client
        self.cancel_event = asyncio.Event()
        yield self

    async def initialize(self, **kwargs):
        return {"protocol_version": 1, "agent_capabilities": {"load_session": self.load_supported}}

    async def new_session(self, **kwargs):
        return {"session_id": SID, "config_options": deepcopy(self.configs)}

    async def load_session(self, **kwargs):
        self.loads += 1
        assert kwargs["session_id"] == SID
        # History MUST NOT appear in the next answer, tool budget or usage.
        await self.client.session_update(SID, {"session_update": "agent_message_chunk", "content": {"text": "OLD_SECRET_REPLAY"}})
        await self.client.session_update(SID, {"session_update": "tool_call", "tool_call_id": "old-tool"})
        return {"config_options": deepcopy(self.configs)}

    async def set_config_option(self, config_id, session_id, value):
        if not self.fail_config:
            next(o for o in self.configs if o["id"] == config_id)["current_value"] = value
        return {"config_options": deepcopy(self.configs)}

    async def prompt(self, **kwargs):
        self.prompts += 1
        assert kwargs["session_id"] == SID
        if self.scenario == "transport_error":
            raise RuntimeError("Bearer MUST_NOT_LEAK OAuth-private-data")
        if self.scenario in {"wait", "timeout", "ignore_cancel"}:
            await self.cancel_event.wait()
            return {"stop_reason": "cancelled"}
        if self.scenario in {"question", "permission"}:
            prefix = "interaction_" if self.scenario == "question" else "execute_"
            result = await self.client.request_permission(SID, {
                "tool_call_id": prefix + "1", "title": "A or B?", "kind": None if self.scenario == "question" else "execute",
                "raw_input": {} if self.scenario == "question" else {"credential": "MUST_NOT_PERSIST"},
            }, [{"option_id": "opt-a", "kind": "allow_always", "name": "A"}])
            assert result == {"outcome": {"outcome": "cancelled"}}
        if self.scenario == "overflow":
            await self.client.session_update(SID, {"session_update": "agent_message_chunk", "content": {"text": "x" * 1025}})
        elif self.scenario == "too_many_tools":
            for i in range(3):
                await self.client.session_update(SID, {"session_update": "tool_call", "tool_call_id": f"t{i}"})
        elif self.scenario == "empty":
            pass
        elif self.scenario == "unfinished_tool":
            await self.client.session_update(SID, {"session_update": "tool_call", "tool_call_id": "pending-command", "status": "in_progress"})
            await self.client.session_update(SID, {"session_update": "agent_message_chunk", "content": {"text": ANSWER}})
        else:
            await self.client.session_update("foreign-session", {"session_update": "agent_message_chunk", "content": {"text": "FOREIGN_SECRET"}})
            await self.client.session_update(SID, {"session_update": "agent_thought_chunk", "content": {"text": "PRIVATE_REASONING"}})
            await self.client.session_update(SID, {"session_update": "agent_message_chunk", "content": {"text": self.reply}})
        return {"stop_reason": "refusal" if self.scenario == "refusal" else "end_turn", "usage": {"total_tokens": 32, "input_tokens": 20, "output_tokens": 12}}

    async def cancel(self, **kwargs):
        self.cancels += 1
        if self.scenario != "ignore_cancel":
            self.cancel_event.set()


def setup(tmp_path, scenario="ok"):
    store = AcpSessionStore(tmp_path / "state")
    agent = FakeAgent(scenario)
    runner = AcpSessionRunner(store, agent.connect)
    return store, agent, runner


def test_native_execution_error_end_turn_is_not_success(tmp_path):
    _store,agent,runner=setup(tmp_path)
    agent.reply='Agent execution error: synthetic failure, not a model answer'
    result=asyncio.run(runner.run(reference=REF,identity=identity(tmp_path),turn_id='failed-helper',prompt='Inspect'))
    assert not result['success']
    assert result['error']['code']=='acp_native_execution_error'


def run(runner, tmp_path, turn_id="t1", **kwargs):
    return asyncio.run(runner.run(reference=REF, identity=identity(tmp_path), turn_id=turn_id, prompt="Read sample.py", **kwargs))


def test_turn_new_then_load_drops_replayed_and_foreign_events(tmp_path):
    store, agent, runner = setup(tmp_path)
    first = run(runner, tmp_path)
    second = run(runner, tmp_path, turn_id="t2")
    assert first["success"] and second["success"]
    assert second["answer"] == ANSWER
    assert agent.loads == 1 and agent.prompts == 2
    assert second["harness_metadata"]["tool_calls"] == 0
    assert second["usage"]["total_tokens"] == 32
    assert "OLD_SECRET_REPLAY" not in json.dumps(second)
    assert "FOREIGN_SECRET" not in json.dumps(second)
    assert "PRIVATE_REASONING" not in json.dumps(second)
    checkpoint = store.load(REF, identity(tmp_path))
    assert checkpoint["phase"] == "completed"
    assert (store.root / (REF + ".json")).stat().st_mode & 0o777 == 0o600
    assert store.root.stat().st_mode & 0o777 == 0o700


def test_resume_from_current_turn_preserves_native_session(tmp_path):
    store, agent, runner = setup(tmp_path)
    assert run(runner, tmp_path, turn_id="source")["success"]
    result = run(runner, tmp_path, turn_id="continued", expected_previous_turn="source")
    assert result["success"] and result["turn_id"] == "continued"
    assert agent.starts == 2 and agent.loads == 1 and agent.prompts == 2
    assert store.load(REF, identity(tmp_path))["turn"] == "continued"


@pytest.mark.parametrize("advance", [False, True])
def test_stale_resume_rejects_before_any_provider_connection(tmp_path, advance):
    store, agent, runner = setup(tmp_path)
    if advance:
        run(runner, tmp_path, turn_id="source")
        run(runner, tmp_path, turn_id="advanced", expected_previous_turn="source")
    checkpoint = store.load(REF, identity(tmp_path))
    calls = (agent.starts, agent.loads, agent.prompts)
    with pytest.raises(AcpStateError, match="^acp_resume_source_stale$"):
        run(runner, tmp_path, turn_id="stale-child", expected_previous_turn="source")
    assert (agent.starts, agent.loads, agent.prompts) == calls
    assert store.load(REF, identity(tmp_path)) == checkpoint


def test_queued_continuations_of_one_source_cannot_rewind_latest_history(tmp_path):
    store, agent, runner = setup(tmp_path)
    run(runner, tmp_path, turn_id="source")

    async def queued_continuations():
        # Both requests capture the source before the account slot is released.
        account_slot = asyncio.Lock()
        await account_slot.acquire()

        async def continue_turn(turn_id):
            async with account_slot:
                return await runner.run(reference=REF, identity=identity(tmp_path),
                                        turn_id=turn_id, prompt="Continue the captured source",
                                        expected_previous_turn="source")

        requests = [asyncio.create_task(continue_turn(turn)) for turn in ("child-a", "child-b")]
        account_slot.release()
        return await asyncio.gather(*requests, return_exceptions=True)

    results = asyncio.run(queued_continuations())
    successes = [result for result in results if isinstance(result, dict) and result["success"]]
    failures = [result for result in results if isinstance(result, AcpStateError)]
    assert len(successes) == len(failures) == 1
    assert failures[0].code == "acp_resume_source_stale"
    assert agent.starts == 2 and agent.loads == 1 and agent.prompts == 2
    checkpoint = store.load(REF, identity(tmp_path))
    assert checkpoint["turn"] == successes[0]["turn_id"]
    assert checkpoint["turns_seen"] == ["source", successes[0]["turn_id"]]


@pytest.mark.parametrize("phase,code", [
    ("running", "acp_turn_outcome_unknown"), ("abandoned", "acp_session_abandoned"),
])
def test_resume_source_check_preserves_frozen_session_guard(tmp_path, phase, code):
    store, agent, runner = setup(tmp_path)
    state = store.load(REF, identity(tmp_path))
    state.update(phase=phase, turn="newer-turn")
    store.save(REF, state)
    with pytest.raises(AcpStateError, match=f"^{code}$"):
        run(runner, tmp_path, expected_previous_turn="old-turn")
    assert agent.starts == 0


@pytest.mark.parametrize("previous", ["", "../source", "bad turn", "x" * 129, 4])
def test_resume_source_identifier_is_validated_before_connect(tmp_path, previous):
    _, agent, runner = setup(tmp_path)
    with pytest.raises(ValueError, match="Invalid ACP previous turn ID"):
        run(runner, tmp_path, expected_previous_turn=previous)
    assert agent.starts == 0


def test_duplicate_turn_is_rejected_without_spawning(tmp_path):
    _, agent, runner = setup(tmp_path)
    run(runner, tmp_path)
    with pytest.raises(AcpStateError, match="already_recorded"):
        run(runner, tmp_path)
    assert agent.starts == 1


def test_reuse_of_older_turn_id_is_also_rejected(tmp_path):
    _, agent, runner = setup(tmp_path)
    run(runner, tmp_path, turn_id="t1")
    run(runner, tmp_path, turn_id="t2")
    with pytest.raises(AcpStateError, match="already_recorded"):
        run(runner, tmp_path, turn_id="t1")
    assert agent.starts == 2


@pytest.mark.parametrize("key,value", [
    ("owner", "other-owner"), ("account", "other-account"),
    ("scope_digest", "f" * 64), ("thinking", "low"),
])
def test_binding_cannot_change(tmp_path, key, value):
    _, agent, runner = setup(tmp_path)
    run(runner, tmp_path)
    with pytest.raises(AcpStateError, match="binding_mismatch"):
        asyncio.run(runner.run(reference=REF, identity=replace(identity(tmp_path), **{key: value}), turn_id="t2", prompt="Another task"))
    assert agent.starts == 1


def test_cross_process_lease_and_invalid_path(tmp_path):
    store, _, _ = setup(tmp_path)
    with store.lease(REF), pytest.raises(AcpStateError, match="session_busy"), store.lease(REF):
        pytest.fail("Second lease acquired")
    with pytest.raises(AcpStateError, match="invalid_session_reference"):
        store.load("../bad", identity(tmp_path))


def test_state_permissions_fail_closed(tmp_path):
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    root.chmod(0o755)  # test independent of the MCP supervisor's umask
    with pytest.raises(AcpStateError, match="not_private"):
        AcpSessionStore(root)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(target)
    with pytest.raises(AcpStateError, match="symlink"):
        AcpSessionStore(tmp_path / "link")


def test_uncertain_transport_failure_never_replays_prompt(tmp_path):
    store, agent, runner = setup(tmp_path, "transport_error")
    result = run(runner, tmp_path)
    assert not result["success"] and result["outcome_uncertain"]
    assert result["error"]["code"] == "acp_transport_error"
    assert "MUST_NOT_LEAK" not in json.dumps(store.load(REF, identity(tmp_path)))
    with pytest.raises(AcpStateError, match="outcome_unknown"):
        run(runner, tmp_path, turn_id="t2")
    assert agent.prompts == 1


@pytest.mark.parametrize("stage", [
    "connect", "initialize", "new_session", "load_session", "configure", "prompt", "teardown",
])
def test_transport_diagnostic_reports_stage_without_exception_values(tmp_path, stage):
    secret = "EXCEPTION_SECRET_BEARER_AND_OAUTH_CODE"

    class DiagnosticFailure(RuntimeError):
        def __str__(self):
            pytest.fail("Transport diagnostics must never format the exception")

    class FailingAgent(FakeAgent):
        failure_stage = None

        def fail(self, current_stage):
            if self.failure_stage == current_stage:
                raise DiagnosticFailure(secret)

        @asynccontextmanager
        async def connect(self, client):
            self.fail("connect")
            async with super().connect(client):
                yield self
                self.fail("teardown")

        async def initialize(self, **kwargs):
            self.fail("initialize")
            return await super().initialize(**kwargs)

        async def new_session(self, **kwargs):
            self.fail("new_session")
            return await super().new_session(**kwargs)

        async def load_session(self, **kwargs):
            self.fail("load_session")
            return await super().load_session(**kwargs)

        async def set_config_option(self, **kwargs):
            self.fail("configure")
            return await super().set_config_option(**kwargs)

        async def prompt(self, **kwargs):
            self.fail("prompt")
            return await super().prompt(**kwargs)

    store = AcpSessionStore(tmp_path / "state")
    agent = FailingAgent()
    runner = AcpSessionRunner(store, agent.connect)
    if stage == "load_session":
        assert run(runner, tmp_path, turn_id="source")["success"]
    agent.failure_stage = stage
    result = run(runner, tmp_path, turn_id="diagnostic")
    assert result["error"]["code"] == "acp_transport_error"
    assert result["harness_metadata"]["transport_failure"] == {
        "stage": stage, "exception_type": "DiagnosticFailure",
    }
    assert result["prompt_sent"] is (stage in {"prompt", "teardown"})
    assert secret not in json.dumps(result)
    assert secret not in json.dumps(store.load(REF, identity(tmp_path)))


@pytest.mark.parametrize("exception_name", ["private/EXCEPTION_SECRET", "x" * 81])
def test_transport_diagnostic_sanitizes_non_identifier_exception_class(tmp_path, exception_name):
    exception_type = type(exception_name, (RuntimeError,), {})

    @asynccontextmanager
    async def fail_connect(client):
        raise exception_type("EXCEPTION_SECRET")
        yield  # pragma: no cover

    runner = AcpSessionRunner(AcpSessionStore(tmp_path / "state"), fail_connect)
    result = run(runner, tmp_path)
    assert result["harness_metadata"]["transport_failure"] == {
        "stage": "connect", "exception_type": "Exception",
    }
    assert "EXCEPTION_SECRET" not in json.dumps(result)


@pytest.mark.parametrize("scenario", ["ok", "refusal"])
def test_completed_protocol_exchange_has_no_transport_failure_diagnostic(tmp_path, scenario):
    _, _, runner = setup(tmp_path, scenario)
    result = run(runner, tmp_path)
    assert "transport_failure" not in result["harness_metadata"]


@pytest.mark.parametrize("scenario,code,status", [
    ("question", "acp_input_required", "blocked"),
    ("permission", "acp_permission_denied", "failed"),
    ("empty", "acp_empty_answer", "failed"),
    ("refusal", "acp_incomplete_turn", "failed"),
    ("unfinished_tool", "acp_unfinished_tools", "failed"),
])
def test_errors_and_questions_are_not_success(tmp_path, scenario, code, status):
    store, _agent, runner = setup(tmp_path, scenario)
    result = run(runner, tmp_path)
    assert result["status"] == status and not result["success"]
    assert result["error"]["code"] == code
    assert result["outcome_uncertain"] is (scenario == "unfinished_tool")
    assert "accepted" not in result
    assert "MUST_NOT_PERSIST" not in json.dumps(store.load(REF, identity(tmp_path)))
    if scenario == "question":
        assert result["pending_request"]["kind"] == "question"
        assert result["pending_request"]["disposition"] == "cancelled_requires_continuation"


def test_native_cancel_waits_for_terminal_response(tmp_path):
    _, agent, runner = setup(tmp_path, "wait")

    async def exercise():
        event = asyncio.Event()
        task = asyncio.create_task(runner.run(reference=REF, identity=identity(tmp_path), turn_id="t1", prompt="Read", cancel=event))
        while agent.prompts == 0:
            await asyncio.sleep(0.001)
        event.set()
        return await task

    result = asyncio.run(exercise())
    assert result["status"] == "cancelled"
    assert not result["outcome_uncertain"]
    assert agent.cancels == 1


def test_pre_cancelled_turn_never_connects(tmp_path):
    _, agent, runner = setup(tmp_path)

    async def exercise():
        event = asyncio.Event()
        event.set()
        return await runner.run(reference=REF, identity=identity(tmp_path), turn_id="t1", prompt="Read", cancel=event)

    result = asyncio.run(exercise())
    assert result["status"] == "cancelled"
    assert agent.starts == 0


@pytest.mark.parametrize("scenario,code", [("overflow", "acp_output_budget_exceeded"), ("too_many_tools", "acp_tool_budget_exceeded")])
def test_budgets_cannot_report_success(tmp_path, scenario, code):
    _, agent, runner = setup(tmp_path, scenario)
    result = run(runner, tmp_path, limits=AcpRunLimits(max_answer_bytes=1024, max_tool_calls=2))
    assert not result["success"]
    assert result["error"]["code"] == code
    assert agent.cancels == 1


def test_missing_load_capability_never_falls_back_to_new_session(tmp_path):
    _, agent, runner = setup(tmp_path)
    run(runner, tmp_path)
    agent.load_supported = False
    result = run(runner, tmp_path, turn_id="t2")
    assert result["error"]["code"] == "acp_session_load_unsupported"
    assert agent.prompts == 1


def test_unconfirmed_config_prevents_prompt(tmp_path):
    _, agent, runner = setup(tmp_path)
    agent.fail_config = True
    result = run(runner, tmp_path)
    assert result["error"]["code"] == "acp_config_not_confirmed"
    assert agent.prompts == 0


def test_timeout_uses_protocol_cancel_and_preserves_uncertainty(tmp_path):
    _, agent, runner = setup(tmp_path, "ignore_cancel")
    result = run(runner, tmp_path, limits=AcpRunLimits(timeout_seconds=1, cancel_grace_seconds=0.2))
    assert not result["success"] and result["outcome_uncertain"]
    assert agent.cancels == 1
    assert result["error"]["code"] in {"acp_timeout", "acp_cancel_unconfirmed"}


def test_cancel_grace_must_fit_total_deadline():
    with pytest.raises(ValueError, match="grace"):
        AcpRunLimits(timeout_seconds=1, cancel_grace_seconds=5)


def test_sdk_is_not_an_import_dependency():
    # This test file and core work in the existing Opss venv without acp.
    import sys
    assert "acp" not in sys.modules


def test_frozen_client_ignores_late_permission_and_message():
    async def exercise():
        client = AcpTurnClient(AcpRunLimits())
        client.session_id = SID
        client.closed = True
        await client.session_update(SID, {"session_update": "agent_message_chunk", "content": {"text": "LATE"}})
        result = await client.request_permission(SID, {"tool_call_id": "interaction_late", "title": "late"}, [])
        assert result["outcome"]["outcome"] == "cancelled"
        assert client.pending is None and client.text == ""
    asyncio.run(exercise())


def test_tool_progress_is_not_counted_twice():
    async def exercise():
        client = AcpTurnClient(AcpRunLimits())
        client.session_id = SID
        client.live = True
        for kind in ("tool_call", "tool_call_update", "tool_call_update"):
            await client.session_update(SID, {"session_update": kind, "tool_call_id": "one"})
        assert len(client.tool_ids) == 1
        assert not client.stop.is_set()
    asyncio.run(exercise())


def test_cancel_racing_with_end_turn_never_reports_success(tmp_path):
    store, agent, runner = setup(tmp_path)

    async def exercise():
        cancel = asyncio.Event()
        original = agent.prompt

        async def racing_prompt(**kwargs):
            answer = await original(**kwargs)
            cancel.set()
            return answer

        agent.prompt = racing_prompt
        return await runner.run(
            reference=REF, identity=identity(tmp_path), turn_id="raced",
            prompt="Read sample.py", cancel=cancel,
        )

    result = asyncio.run(exercise())
    assert result["status"] == "cancelled"
    assert not result["success"] and not result["outcome_uncertain"]
    assert result["answer"] == ANSWER  # retained only as advisory evidence
    assert agent.cancels == 1
    assert store.load(REF, identity(tmp_path))["phase"] == "cancelled"


def test_failed_setup_freezes_client_for_late_callbacks(tmp_path):
    _store, agent, runner = setup(tmp_path)
    agent.fail_config = True
    result = run(runner, tmp_path)

    async def late():
        await agent.client.session_update(SID, {
            "session_update": "agent_message_chunk", "content": {"text": "late"},
        })
        await agent.client.request_permission(SID, {"tool_call_id": "interaction_late"}, [])

    asyncio.run(late())
    assert result["error"]["code"] == "acp_config_not_confirmed"
    assert agent.client.text == "" and agent.client.pending is None


@pytest.mark.parametrize("first_cancel", ["drop", "reset_on_send"])
def test_early_native_cancel_is_reasserted_without_replaying_prompt(tmp_path, first_cancel):
    store, agent, runner = setup(tmp_path, "wait")

    async def exercise():
        cancel = asyncio.Event()
        first_notification = asyncio.Event()
        native_ready = asyncio.Event()

        async def initializing_prompt(**kwargs):
            agent.prompts += 1
            assert kwargs["session_id"] == SID
            cancel.set()
            await first_notification.wait()
            # Vendor ACP may ignore cancellation before its agent starts; its
            # native send also resets cancellation delivered before user_input.
            if first_cancel == "reset_on_send":
                assert agent.cancel_event.is_set()
                agent.cancel_event.clear()
            native_ready.set()
            await agent.cancel_event.wait()
            return {"stop_reason": "cancelled"}

        async def native_cancel(**kwargs):
            assert kwargs["session_id"] == SID
            agent.cancels += 1
            if agent.cancels == 1:
                if first_cancel == "reset_on_send":
                    agent.cancel_event.set()
                first_notification.set()
            else:
                assert native_ready.is_set()
                agent.cancel_event.set()

        agent.prompt = initializing_prompt
        agent.cancel = native_cancel
        return await asyncio.wait_for(runner.run(
            reference=REF, identity=identity(tmp_path), turn_id="early-cancel",
            prompt="Read sample.py", cancel=cancel,
            limits=AcpRunLimits(timeout_seconds=2, cancel_grace_seconds=1),
        ), 1.5)

    result = asyncio.run(exercise())
    assert result["status"] == "cancelled"
    assert result["error"]["code"] == "acp_cancelled"
    assert not result["outcome_uncertain"]
    assert agent.prompts == 1 and agent.cancels == 2 and agent.starts == 1
    assert result["harness_metadata"]["event_counts"] == {}
    assert store.load(REF, identity(tmp_path))["phase"] == "cancelled"


@pytest.mark.parametrize("hanging_notification", [False, True])
def test_native_cancel_without_terminal_ack_is_bounded_and_cannot_resume(tmp_path, hanging_notification):
    store, agent, runner = setup(tmp_path, "ignore_cancel")

    async def exercise():
        cancel = asyncio.Event()
        notification_closed = asyncio.Event()
        original_prompt = agent.prompt

        async def active_prompt(**kwargs):
            cancel.set()
            return await original_prompt(**kwargs)

        async def native_cancel(**kwargs):
            agent.cancels += 1
            if hanging_notification:
                try:
                    await asyncio.Event().wait()
                finally:
                    notification_closed.set()

        agent.prompt = active_prompt
        agent.cancel = native_cancel
        result = await asyncio.wait_for(runner.run(
            reference=REF, identity=identity(tmp_path), turn_id="unconfirmed-cancel",
            prompt="Read sample.py", cancel=cancel,
            limits=AcpRunLimits(timeout_seconds=2, cancel_grace_seconds=0.6),
        ), 1.5)
        assert notification_closed.is_set() is hanging_notification
        with pytest.raises(AcpStateError, match="acp_turn_outcome_unknown"):
            await runner.run(reference=REF, identity=identity(tmp_path), turn_id="unsafe-replay", prompt="Read")
        return result

    result = asyncio.run(exercise())
    assert result["status"] == "failed" and not result["success"]
    assert result["error"]["code"] == "acp_cancel_unconfirmed"
    assert result["outcome_uncertain"]
    assert agent.prompts == 1 and agent.starts == 1
    assert agent.cancels == (1 if hanging_notification else 3)
    assert store.load(REF, identity(tmp_path))["phase"] == "running"


async def _native_question(agent, **kwargs):
    agent.prompts += 1
    await agent.client.session_update(SID, {
        "session_update": "tool_call", "tool_call_id": "interaction_live", "status": "pending",
    })
    response = await agent.client.request_permission(SID, {
        "tool_call_id": "interaction_live", "title": "Choose the next file", "raw_input": {},
    }, [{"option_id": "first", "name": "First file", "kind": "allow_once"},
        {"option_id": "second", "name": "Second file", "kind": "allow_once"}])
    agent.selection = response
    if response["outcome"]["outcome"] == "cancelled":
        return {"stop_reason": "cancelled"}
    await agent.client.session_update(SID, {
        "session_update": "tool_call_update", "tool_call_id": "interaction_live", "status": "completed",
    })
    await agent.client.session_update(SID, {"session_update": "agent_message_chunk", "content": {"text": ANSWER}})
    return {"stop_reason": "end_turn"}


async def _wait_pending(control, who):
    async def poll():
        while True:
            request = control.snapshot(REF, who)["pending_request"]
            if request and request["status"] == "pending":
                return request
            await asyncio.sleep(0.01)
    return await asyncio.wait_for(poll(), 2)


def test_live_question_continues_same_prompt_after_controller_restart(tmp_path):
    store, agent, runner = setup(tmp_path)
    control = AcpControlStore(tmp_path / "control")
    agent.prompt = lambda **kw: _native_question(agent, **kw)

    async def exercise():
        task = asyncio.create_task(runner.run(reference=REF, identity=identity(tmp_path), turn_id="question-turn",
                                             prompt="Ask one question", control=control))
        request = await _wait_pending(control, identity(tmp_path))
        assert not task.done() and request["turn_id"] == "question-turn"
        assert store.load(REF, identity(tmp_path))["phase"] == "running"
        restarted_controller = AcpControlStore(control.root)
        restarted_controller.reply(REF, identity(tmp_path), turn_id="question-turn",
                                   request_id=request["request_id"], option_id="second")
        return await asyncio.wait_for(task, 2)

    result = asyncio.run(exercise())
    assert result["success"] and agent.starts == 1 and agent.prompts == 1
    assert agent.selection == {"outcome": {"outcome": "selected", "option_id": "second"}}
    assert "pending_request" not in result
    assert control.snapshot(REF, identity(tmp_path))["pending_request"]["status"] == "consumed"


def test_expired_live_question_closes_native_request_and_reports_blocked(tmp_path, monkeypatch):
    _, agent, runner = setup(tmp_path)
    control = AcpControlStore(tmp_path / "control")
    agent.prompt = lambda **kw: _native_question(agent, **kw)

    async def exercise():
        task = asyncio.create_task(runner.run(reference=REF, identity=identity(tmp_path), turn_id="expired-question",
                                             prompt="Ask one question", control=control))
        request = await _wait_pending(control, identity(tmp_path))
        monkeypatch.setattr("chatgpt_web_oauth_mcp.acp_control.time.time", lambda: request["expires_at"] + 1)
        return await asyncio.wait_for(task, 2)

    result = asyncio.run(exercise())
    assert result["status"] == "blocked" and result["error"]["code"] == "acp_input_expired"
    assert not result["outcome_uncertain"] and agent.cancels == 1
    assert control.snapshot(REF, identity(tmp_path))["pending_request"]["status"] == "expired"


def test_cancelling_turn_while_question_waits_closes_mailbox(tmp_path):
    _, agent, runner = setup(tmp_path)
    control = AcpControlStore(tmp_path / "control")
    agent.prompt = lambda **kw: _native_question(agent, **kw)

    async def exercise():
        cancel = asyncio.Event()
        task = asyncio.create_task(runner.run(reference=REF, identity=identity(tmp_path), turn_id="cancel-question",
                                             prompt="Ask one question", cancel=cancel, control=control))
        await _wait_pending(control, identity(tmp_path))
        cancel.set()
        return await asyncio.wait_for(task, 2)

    result = asyncio.run(exercise())
    assert result["status"] == "cancelled" and not result["outcome_uncertain"]
    assert agent.selection["outcome"]["outcome"] == "cancelled"
    assert control.snapshot(REF, identity(tmp_path))["pending_request"]["status"] == "cancelled"


def test_dead_worker_question_requires_explicit_abandon_and_fresh_reference(tmp_path):
    store, agent, runner = setup(tmp_path)
    control = AcpControlStore(tmp_path / "control")
    original = agent.prompt
    agent.prompt = lambda **kw: _native_question(agent, **kw)

    async def exercise():
        task = asyncio.create_task(runner.run(reference=REF, identity=identity(tmp_path), turn_id="dead-owner",
                                             prompt="Ask one question", control=control))
        await _wait_pending(control, identity(tmp_path))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(AcpStateError, match="acp_turn_outcome_unknown"):
            await runner.run(reference=REF, identity=identity(tmp_path), turn_id="unsafe-resume", prompt="Resume")
        store.abandon(REF, identity(tmp_path), turn_id="dead-owner", reason="Reviewed owner failure; start fresh.")
        with pytest.raises(AcpStateError, match="acp_session_abandoned"):
            await runner.run(reference=REF, identity=identity(tmp_path), turn_id="still-frozen", prompt="Resume")
        agent.prompt = original
        return await runner.run(reference="b" * 32, identity=identity(tmp_path), turn_id="reviewed-new", prompt="New task")

    result = asyncio.run(exercise())
    assert result["success"] and agent.prompts == 2
    assert store.load(REF, identity(tmp_path))["phase"] == "abandoned"
    assert control.snapshot(REF, identity(tmp_path))["turn_state"] == "uncertain"


def test_queued_steering_is_attached_to_one_later_prompt_only(tmp_path):
    _, agent, runner = setup(tmp_path)
    control = AcpControlStore(tmp_path / "control")
    prompts = []
    original = agent.prompt

    async def record_prompt(**kwargs):
        prompts.append(kwargs["prompt"][0]["text"])
        return await original(**kwargs)

    agent.prompt = record_prompt
    run(runner, tmp_path, turn_id="t1", control=control)
    receipt = AcpControlStore(control.root).enqueue(REF, identity(tmp_path), source_turn_id="t1",
                                                  message_id="steer-later", text="Inspect the timeout branch too.")
    assert receipt["delivery"] == "queued_next_turn" and "timeout branch" not in prompts[0]
    second = run(runner, tmp_path, turn_id="t2", control=control)
    third = run(runner, tmp_path, turn_id="t3", control=control)
    assert "Inspect the timeout branch too." in prompts[1] and "timeout branch" not in prompts[2]
    assert second["harness_metadata"]["steering_message_ids"] == ["steer-later"]
    assert third["harness_metadata"]["steering_message_ids"] == []


@pytest.mark.parametrize("enabled,options,tool,sid,selected", [
    (True, [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"}], {"tool_call_id": "execute_1", "kind": "execute"}, SID, True),
    (False, [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"}], {"tool_call_id": "execute_1", "kind": "execute"}, SID, False),
    (True, [{"option_id": "allow_always", "name": "Allow all", "kind": "allow_always"}], {"tool_call_id": "execute_1", "kind": "execute"}, SID, False),
    (True, [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"}], {"tool_call_id": "interaction_question", "raw_input": {}}, SID, False),
    (True, [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"}], {"tool_call_id": "interaction_spoof", "kind": "execute", "raw_input": {"command": "x"}}, SID, False),
    (True, [{"option_id": "allow", "name": "Allow once", "kind": "allow_once"}], {"tool_call_id": "execute_1", "kind": "execute"}, "foreign", False),
])
def test_writer_policy_allows_only_bound_native_single_use_permission(enabled, options, tool, sid, selected):
    async def exercise():
        client = AcpTurnClient(AcpRunLimits(), allow_native_tools=enabled)
        client.live, client.session_id = True, SID
        return await client.request_permission(sid, tool, options)
    result = asyncio.run(exercise())
    assert result["outcome"]["outcome"] == ("selected" if selected else "cancelled")
    if selected:
        assert result["outcome"]["option_id"] == "allow"


def test_readonly_permission_callback_never_grants_mcp_shaped_metadata():
    # The pinned runtime can derive this same metadata from a native tool's
    # argument envelope. Only trusted native static policies authorize gateway
    # calls; the callback must not promote this display metadata into a grant.
    client = AcpTurnClient(AcpRunLimits())
    client.session_id = SID
    client.live = True
    result = asyncio.run(client.request_permission(SID, {
        "tool_call_id": "a" * 32, "title": "opss_readonly_read_text", "kind": "other",
        "raw_input": {"path": "sample.py", "arguments": {"path": "sample.py"}},
        "field_meta": {"is_mcp_tool_call": True,
                       "mcp": {"server": "opss_readonly", "tool": "read_text"}},
    }, [{"option_id": "allow", "kind": "allow_once", "name": "Allow"}]))
    assert result == {"outcome": {"outcome": "cancelled"}}
    assert client.stop_code == "acp_permission_denied"


def test_http_gateway_requires_advertised_capability(tmp_path):
    _, agent, runner = setup(tmp_path)
    result = run(runner, tmp_path, mcp_servers=[{"type": "http", "name": "bounded", "url": "http://127.0.0.1:1/mcp", "headers": []}])
    assert result["error"]["code"] == "acp_mcp_http_unsupported" and agent.prompts == 0


def test_http_gateway_configuration_reaches_new_and_loaded_session(tmp_path):
    _, agent, runner = setup(tmp_path)
    calls = []
    new, loaded = agent.new_session, agent.load_session

    async def initialize(**kwargs):
        return {"protocol_version": 1, "agent_capabilities": {"load_session": True, "mcp_capabilities": {"http": True}}}

    async def new_session(**kwargs):
        calls.append(kwargs["mcp_servers"])
        return await new(**kwargs)

    async def load_session(**kwargs):
        calls.append(kwargs["mcp_servers"])
        return await loaded(**kwargs)

    agent.initialize, agent.new_session, agent.load_session = initialize, new_session, load_session
    servers = [{"type": "http", "name": "bounded", "url": "http://127.0.0.1:1/mcp", "headers": []}]
    assert run(runner, tmp_path, turn_id="gateway-new", mcp_servers=servers)["success"]
    assert run(runner, tmp_path, turn_id="gateway-loaded", mcp_servers=servers)["success"]
    assert calls == [servers, servers] and agent.loads == 1


def test_writer_permission_enforces_tool_budget_before_progress_event():
    async def exercise():
        client = AcpTurnClient(AcpRunLimits(max_tool_calls=1), allow_native_tools=True)
        client.live, client.session_id = True, SID
        options = [{"option_id": "allow", "name": "Once", "kind": "allow_once"}]
        first = await client.request_permission(SID, {"tool_call_id": "one", "kind": "edit"}, options)
        await client.session_update(SID, {"session_update": "tool_call", "tool_call_id": "one"})
        second = await client.request_permission(SID, {"tool_call_id": "two", "kind": "edit"}, options)
        assert first["outcome"]["outcome"] == "selected"
        assert second["outcome"]["outcome"] == "cancelled"
        assert client.stop_code == "acp_tool_budget_exceeded"
    asyncio.run(exercise())


def test_writer_completion_is_frozen_until_server_promotion_is_recorded(tmp_path):
    store, agent, runner = setup(tmp_path)
    control = AcpControlStore(tmp_path / "control")
    result = run(runner, tmp_path, turn_id="writer-native", control=control, defer_completion=True)
    assert result["success"] and not result["outcome_uncertain"]
    checkpoint = store.load(REF, identity(tmp_path))
    assert checkpoint["phase"] == "running"
    assert checkpoint["result"]["success"] is False and checkpoint["result"]["outcome_uncertain"]
    assert checkpoint["result"]["error"]["code"] == "acp_writer_postprocessing_incomplete"
    assert checkpoint["result"]["native_outcome"] == {"status": "succeeded", "success": True, "stop_reason": "end_turn"}
    assert checkpoint["result"]["answer"] == result["answer"] == ANSWER
    assert control.snapshot(REF, identity(tmp_path))["turn_state"] == "uncertain"
    # Model the crash before worker postprocessing: the lease is free, but a
    # fresh runner must not reinterpret native success as completed promotion.
    with store.lease(REF):
        pass
    with pytest.raises(AcpStateError, match="acp_turn_outcome_unknown"):
        run(runner, tmp_path, turn_id="unsafe-writer-resume", control=control)
    assert agent.prompts == 1


def test_writer_native_failure_does_not_claim_pending_promotion(tmp_path):
    store, _, runner = setup(tmp_path, "empty")
    result = run(runner, tmp_path, defer_completion=True)
    checkpoint = store.load(REF, identity(tmp_path))
    assert result["error"]["code"] == "acp_empty_answer"
    assert checkpoint["phase"] == "failed" and checkpoint["result"] == result
