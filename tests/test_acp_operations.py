from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp.acp_control import AcpControlStore
from chatgpt_web_oauth_mcp.acp_operations import control_status, enrich_control_snapshot, reconcile, send_message
from chatgpt_web_oauth_mcp.acp_session_store import AcpSessionIdentity, AcpSessionStore
from chatgpt_web_oauth_mcp.job_supervisor import TERMINAL_JOB_STATUSES
from chatgpt_web_oauth_mcp.shell import JobRegistry

DID = "d" * 12
REF = "e" * 32
OWNER = "logical-owner-stable"
OWNER_DIGEST = hashlib.sha256(OWNER.encode()).hexdigest()
SCOPE = hashlib.sha256(b"fixed-file-scope").hexdigest()


class Registry:
    def __init__(self, root, source):
        self.source = source
        self.task = None
        self.metadata = None
        self._persisted_delegate_paths = {}
        self.scheduler = SimpleNamespace(get_task=lambda did: self.task if did == DID else None)
        self.acp_backend = SimpleNamespace(state_root=root, accounts={"antigravity3": {}})
        self.durable_state_dir = root.parent / "jobs"
        self.worker_status = "running"
        self.durable_job_registry = SimpleNamespace(job_status=lambda **kw: {"success": True, "status": self.worker_status})
        self.snapshot_calls = 0

    def _resume_source_snapshot(self, did):
        self.snapshot_calls += 1
        return deepcopy(self.source) if did == DID else None

    def _read_persisted_delegate_metadata(self, path):
        return json.loads(Path(path).read_text()) if Path(path).exists() else None


def setup(tmp_path):
    root = tmp_path / "acp"
    root.mkdir(mode=0o700)
    who = AcpSessionIdentity(owner=OWNER_DIGEST, account="antigravity3", workspace=str(tmp_path.resolve()), scope_digest=SCOPE)
    sessions, control = AcpSessionStore(root / "sessions"), AcpControlStore(root / "control")
    state = sessions.load(REF, who)
    state.update(phase="running", turn=DID, result=None, turns_seen=[DID], session_id="native-session")
    sessions.save(REF, state)
    control.begin_turn(REF, who, turn_id=DID)
    control.publish_request(REF, who, turn_id=DID, request_id="interaction_pending", title="Which file?",
                            options=[{"id": "a", "name": "File A", "kind": "allow_once"}], expires_at=time.time() + 60)
    source = {"delegate_id": DID, "execution_backend": "acp", "harness": "antigravity3",
              "kind": "explore", "cwd": str(tmp_path.resolve()), "model": "gemini-3.8-flash-medium",
              "reasoning_effort": "medium", "logical_session_id": OWNER, "completed": False, "status": "running",
              "durable_job_id": "job-owned", "acp_resume_context": {
                  "owner_digest": OWNER_DIGEST, "scope_digest": SCOPE, "session_ref": REF}}
    registry = Registry(root, source)
    return registry, sessions, control, who


def reply(registry, owner=OWNER, **kwargs):
    args = {"action": "reply", "request_id": "interaction_pending", "option_id": "a"}
    return send_message(registry, DID, owner, **(args | kwargs))


def terminal(registry, sessions, who, *, status="failed", phase="running", uncertain=True):
    registry.source.update(completed=True, status=status)
    registry.worker_status = "succeeded"  # durable worker may exit 0 with a typed native failure
    state = sessions.load(REF, who)
    state.update(phase=phase, result={"success": status == "succeeded", "outcome_uncertain": uncertain})
    sessions.save(REF, state)


def test_owner_gate_covers_status_reply_steer_and_abandon(tmp_path):
    registry, sessions, control, who = setup(tmp_path)
    with sessions.lease(REF):
        operations = [
            control_status(registry, DID, "foreign"),
            reply(registry, owner="foreign"),
            send_message(registry, DID, "foreign", action="steer", message_id="m1", text="Steer"),
            reconcile(registry, DID, "foreign", reason="Wrong owner"),
        ]
    assert all(item["error"]["code"] == "delegate_owned_by_another_session" for item in operations)
    assert control.snapshot(REF, who)["pending_request"]["status"] == "pending"
    assert not control.snapshot(REF, who)["steering"]


def test_live_reply_requires_exact_turn_active_durable_worker_and_session_lease(tmp_path):
    registry, sessions, control, who = setup(tmp_path)
    assert reply(registry)["error"]["code"] == "acp_control_worker_not_active"
    with sessions.lease(REF):
        assert reply(registry, turn_id="wrong")["error"]["code"] == "acp_control_turn_mismatch"
        registry.worker_status = "failed"
        assert reply(registry)["error"]["code"] == "acp_control_worker_not_active"
        registry.worker_status = "running"
        result = reply(registry)
        assert result["success"] and result["control"]["delivery"] == "recorded"
        assert reply(registry)["error"]["code"] == "acp_control_reply_duplicate"
    assert control.consume_reply(REF, who, turn_id=DID, request_id="interaction_pending") == "a"


@pytest.mark.parametrize("status", ["failed", "cancelled", "succeeded", "timed_out"])
def test_terminal_delegate_cannot_reply_to_leftover_mailbox(tmp_path, status):
    registry, sessions, control, who = setup(tmp_path)
    terminal(registry, sessions, who, status=status)
    assert reply(registry)["error"]["code"] == "acp_control_delegate_not_active"
    assert control.snapshot(REF, who)["pending_request"]["status"] == "pending"


def test_reply_and_abandon_refuse_a_superseded_turn(tmp_path):
    registry, sessions, _, who = setup(tmp_path)
    state = sessions.load(REF, who)
    state["turn"] = "f" * 12
    sessions.save(REF, state)
    with sessions.lease(REF):
        assert reply(registry)["error"]["code"] == "acp_control_turn_mismatch"
    registry.source.update(completed=True, status="failed")
    registry.worker_status = "failed"
    assert reconcile(registry, DID, OWNER, reason="Old turn")["error"]["code"] == "acp_control_turn_mismatch"


def test_abandon_rejects_active_delegate_live_job_and_live_lease_then_freezes(tmp_path):
    registry, sessions, control, who = setup(tmp_path)
    assert reconcile(registry, DID, OWNER, reason="Premature")["error"]["code"] == "acp_abandon_requires_terminal_failed_delegate"
    terminal(registry, sessions, who)
    registry.worker_status = "running"
    assert reconcile(registry, DID, OWNER, reason="Worker active")["error"]["code"] == "acp_control_worker_not_terminal"
    registry.worker_status = "succeeded"
    with sessions.lease(REF):
        assert reconcile(registry, DID, OWNER, reason="Owner still active")["error"]["code"] == "acp_session_busy"
    result_before = sessions.load(REF, who)["result"]
    result = reconcile(registry, DID, OWNER, reason="Reviewed unknown outcome and retained artifacts.")
    assert result["success"] and result["reconciliation"]["requires_new_session"]
    assert not result["reconciliation"]["resume_allowed"]
    assert sessions.load(REF, who)["result"] == result_before
    assert sessions.load(REF, who)["phase"] == "abandoned"
    assert control.snapshot(REF, who)["turn_state"] == "abandoned"


@pytest.mark.parametrize("signal_name,exit_code", [("KILL", -9), ("TERM", -15)])
def test_abandon_accepts_real_job_registry_killed_status(tmp_path, signal_name, exit_code):
    registry, sessions, control, who = setup(tmp_path)
    jobs = JobRegistry()
    registry.durable_job_registry = jobs
    started = jobs.start_job(command="exec /bin/sleep 20", cwd=tmp_path,
                             state_dir=registry.durable_state_dir, timeout_seconds=10)
    assert started["success"] is True
    job_id = started["job_id"]
    registry.source.update(durable_job_id=job_id, status="cancelled", completed=True)
    before = sessions.load(REF, who)
    try:
        assert reconcile(registry, DID, OWNER, reason="Worker still running")["error"]["code"] == "acp_control_worker_not_terminal"
        killed = jobs.kill_job(job_id=job_id, state_dir=registry.durable_state_dir, signal_name=signal_name)
        assert killed["success"] is True and killed["signal_sent"] is True
        deadline = time.monotonic() + 5
        while True:
            job = jobs.job_status(job_id=job_id, state_dir=registry.durable_state_dir)
            if job.get("status") in TERMINAL_JOB_STATUSES or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        assert job["status"] == "killed" and job["exit_code"] == exit_code
        with sessions.lease(REF):
            assert reconcile(registry, DID, OWNER, reason="Lease still held")["error"]["code"] == "acp_session_busy"
        result = reconcile(registry, DID, OWNER, reason="Reviewed exact terminated worker and retained history.")
        assert result["success"] is True
        assert result["reconciliation"]["status"] == "abandoned"
        assert result["reconciliation"]["resume_allowed"] is False
        assert result["reconciliation"]["requires_new_session"] is True
        after = sessions.load(REF, who)
        assert after["phase"] == "abandoned"
        assert {k: v for k, v in after.items() if k not in {"phase", "abandonment"}} == {
            k: v for k, v in before.items() if k not in {"phase", "abandonment"}}
        assert control.snapshot(REF, who)["turn_state"] == "abandoned"
    finally:
        if jobs.job_status(job_id=job_id, state_dir=registry.durable_state_dir).get("status") not in TERMINAL_JOB_STATUSES:
            jobs.kill_job(job_id=job_id, state_dir=registry.durable_state_dir, signal_name="KILL")


@pytest.mark.parametrize("job_status", ["starting", "running", "unknown", "cancelled", "terminated", "log_limit_exceeded", None])
def test_control_rejects_noncanonical_or_unfinished_job_statuses(tmp_path, job_status):
    registry, sessions, control, who = setup(tmp_path)
    terminal(registry, sessions, who, status="cancelled", phase="cancelled", uncertain=False)
    registry.worker_status = job_status
    before = sessions.load(REF, who)
    assert reconcile(registry, DID, OWNER, reason="Not proven terminal")["error"]["code"] == "acp_control_worker_not_terminal"
    result = send_message(registry, DID, OWNER, action="steer", message_id="unsafe-next", text="Must not queue.")
    assert result["error"]["code"] == "acp_control_worker_not_terminal"
    assert sessions.load(REF, who) == before
    assert control.snapshot(REF, who)["steering"] == []


@pytest.mark.parametrize("job_status", ["succeeded", "failed", "interrupted", "timed_out"])
def test_abandon_accepts_other_canonical_terminal_job_states(tmp_path, job_status):
    registry, sessions, _, who = setup(tmp_path)
    terminal(registry, sessions, who)
    registry.worker_status = job_status
    assert reconcile(registry, DID, OWNER, reason="Reviewed uncertain terminal outcome")["success"] is True
    assert sessions.load(REF, who)["phase"] == "abandoned"


def test_known_cancelled_turn_can_queue_after_job_registry_reports_killed(tmp_path):
    registry, sessions, control, who = setup(tmp_path)
    terminal(registry, sessions, who, status="cancelled", phase="cancelled", uncertain=False)
    registry.worker_status = "killed"
    control.finish_turn(REF, who, turn_id=DID, status="cancelled")
    result = send_message(registry, DID, OWNER, action="steer", message_id="next-after-cancel", text="Inspect next file.")
    assert result["success"] is True and result["control"]["delivery"] == "queued_next_turn"


def test_abandon_cannot_relabel_success_or_recover_automatically(tmp_path):
    registry, sessions, _, who = setup(tmp_path)
    terminal(registry, sessions, who, status="succeeded", phase="completed", uncertain=False)
    result = reconcile(registry, DID, OWNER, reason="Already successful")
    assert result["error"]["code"] == "acp_abandon_requires_terminal_failed_delegate"
    assert sessions.load(REF, who)["phase"] == "completed"
    result = reconcile(registry, DID, OWNER, action="replay", reason="Do not replay")
    assert result["error"]["code"] == "acp_reconcile_action_invalid"


def test_terminal_known_turn_can_queue_next_turn_but_uncertain_turn_cannot(tmp_path):
    registry, sessions, control, who = setup(tmp_path)
    terminal(registry, sessions, who, status="succeeded", phase="completed", uncertain=False)
    control.finish_turn(REF, who, turn_id=DID, status="completed")
    result = send_message(registry, DID, OWNER, action="steer", message_id="next-1", text="Inspect next file.")
    assert result["success"] and result["control"]["delivery"] == "queued_next_turn"
    terminal(registry, sessions, who, status="failed", phase="running", uncertain=True)
    assert send_message(registry, DID, OWNER, action="steer", message_id="next-2", text="Do not replay.")["error"]["code"] == "acp_control_session_frozen"


@pytest.mark.parametrize("location", ["harness_metadata", "persisted_context", "live_task"])
def test_identity_fallbacks_support_post_restart_and_active_snapshots(tmp_path, location):
    registry, _, _, _ = setup(tmp_path)
    context = registry.source.pop("acp_resume_context")
    if location == "harness_metadata":
        registry.source["harness_metadata"] = context | {"model": "gemini-3.8-flash", "reasoning_effort": "medium"}
    elif location == "persisted_context":
        metadata = tmp_path / "metadata.json"
        metadata.write_text(json.dumps(registry.source | {"acp_resume_context": context}))
        registry._persisted_delegate_paths[DID] = metadata
    else:
        registry.task = SimpleNamespace(**(registry.source | {
            "cwd": tmp_path, "prompt": "Stable prompt", "acp_resume_context": context, "log_paths": None,
        }))
    result = control_status(registry, DID, OWNER)
    assert result["success"] and result["control"]["available"]
    assert result["control"]["pending_request"]["request_id"] == "interaction_pending"


def test_conflicting_identity_metadata_is_not_silently_preferred(tmp_path):
    registry, _, _, _ = setup(tmp_path)
    registry.source["harness_metadata"] = {"owner_digest": "f" * 64, "scope_digest": SCOPE, "session_ref": REF}
    result = control_status(registry, DID, OWNER)
    assert result["error"]["code"] == "acp_control_identity_conflict"


def test_status_enrichment_is_nonrecursive_and_old_records_are_unchanged(tmp_path):
    registry, _, _, _ = setup(tmp_path)
    view = enrich_control_snapshot(registry, registry.source)
    assert view is not registry.source and view["acp_control"]["available"]
    assert registry.snapshot_calls == 0 and "acp_control" not in registry.source
    (registry.acp_backend.state_root / "control" / (REF + ".json")).unlink()
    before = list((registry.acp_backend.state_root / "control").iterdir())
    view = enrich_control_snapshot(registry, registry.source)
    assert view["acp_control"]["available"] is False
    assert list((registry.acp_backend.state_root / "control").iterdir()) == before
    plain = registry.source | {"execution_backend": "cli"}
    assert enrich_control_snapshot(registry, plain) is plain


def test_missing_legacy_identity_is_status_unavailable_not_a_crash_or_new_record(tmp_path):
    registry, _, _, _ = setup(tmp_path)
    registry.source.pop("acp_resume_context")
    view = enrich_control_snapshot(registry, registry.source)
    assert view["status"] == "running" and view["acp_control"]["reason"] == "acp_control_identity_unavailable"
    assert control_status(registry, "a" * 12, OWNER)["error"]["code"] == "delegate_not_found"


def test_empty_owner_and_wrong_backend_cannot_operate_controls(tmp_path):
    registry, _, _, _ = setup(tmp_path)
    assert control_status(registry, DID, "")["error"]["code"] == "acp_control_owner_required"
    registry.source["execution_backend"] = "cli"
    assert reply(registry)["error"]["code"] == "acp_backend_required"


@pytest.mark.parametrize("recovered", [False, True])
def test_status_enrichment_reads_advisory_progress_for_live_and_recovered_tasks(tmp_path, recovered):
    registry, _, _, _ = setup(tmp_path)
    stdout = tmp_path / "stdout.log"
    stdout.write_text(json.dumps({"event": "acp_progress", "progress": {
        "event": "phase", "runtime": {"version": 1, "component": "session",
            "phase": "prompt", "elapsed_seconds": 2, "observed_at_epoch": time.time()},
        "tools": 1, "answer_bytes": 0, "content": "must not leave the worker log",
    }}) + "\n")
    registry.source.update(logs={"stdout": str(stdout)}, recovered_from_disk=recovered,
                           activity_state="starting_or_quiet")
    before = deepcopy(registry.source)
    view = enrich_control_snapshot(registry, registry.source)
    assert view["acp_progress"]["available"]
    assert view["acp_progress"]["runtime"]["phase"] == "prompt"
    assert view["acp_progress"]["seconds_since_progress"] >= 0
    assert view["status"] == "running" and view["activity_state"] == "starting_or_quiet"
    assert view["recovered_from_disk"] is recovered and "content" not in view["acp_progress"]
    assert registry.source == before and registry.snapshot_calls == 0


def test_progress_enrichment_is_available_without_a_legacy_control_mailbox(tmp_path):
    registry, _, _, _ = setup(tmp_path)
    registry.source.pop("acp_resume_context")
    registry.source.update(status="failed", completed=True, harness_metadata={
        "acp_worker_diagnostics": {"version": 1, "component": "worker", "phase": "cleanup",
                                   "elapsed_seconds": 5, "failure_phase": "setup"},
    })
    view = enrich_control_snapshot(registry, registry.source)
    assert not view["acp_control"]["available"]
    assert view["acp_progress"]["source"] == "final_metadata"
    assert view["acp_progress"]["diagnostics"]["worker"]["failure_phase"] == "setup"
    assert view["status"] == "failed" and view["completed"] is True


def test_composition_overflow_preserves_public_continuation_and_queue(tmp_path):
    import asyncio
    from contextlib import asynccontextmanager

    from chatgpt_web_oauth_mcp.acp_result import parse_acp_worker_output
    from chatgpt_web_oauth_mcp.acp_session import AcpSessionRunner
    from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
    from chatgpt_web_oauth_mcp.acp_tool_profiles import native_tool_profile
    from chatgpt_web_oauth_mcp.executors import ExecutorRegistry

    registry, sessions, control, who = setup(tmp_path)
    terminal(registry, sessions, who, status="succeeded", phase="completed", uncertain=False)
    control.finish_turn(REF, who, turn_id=DID, status="completed")
    state = sessions.load(REF, who)
    state.update(version=2, native_tool_profile=native_tool_profile(writer=False, questions=True))
    sessions.save(REF, state)
    project = SimpleNamespace(project_key="composition-project")
    registry.source.update(
        project={"project_key": project.project_key},
        acp_scope={"files_in_scope": ["sample.py"], "out_of_scope": []},
        harness_metadata=dict(registry.source["acp_resume_context"]),
    )
    sources = {DID: deepcopy(registry.source)}
    registry._resume_source_snapshot = lambda did: deepcopy(sources.get(did))
    overflow_id, next_id = "b" * 12, "c" * 12
    messages = [("first", "Keep Unicode café."), ("second", "Inspect the boundary.")]
    for message_id, text in messages:
        receipt = send_message(registry, DID, OWNER, action="steer", message_id=message_id, text=text)
        assert receipt["success"] and receipt["control"]["delivery"] == "queued_next_turn"
    queued = control_status(registry, DID, OWNER)["control"]["steering"]

    class OfflineAgent:
        def __init__(self):
            self.prompts = []
            self.loads = 0

        @asynccontextmanager
        async def connect(self, client):
            self.client = client
            yield self

        async def initialize(self, **kwargs):
            return {"protocol_version": 1, "agent_capabilities": {"load_session": True}}

        async def load_session(self, **kwargs):
            assert kwargs["session_id"] == "native-session"
            self.loads += 1
            return {"config_options": [
                {"id": key, "current_value": value, "options": [{"value": value}]}
                for key, value in (("model", who.model), ("thinking_level", who.thinking), ("mode", "default"))
            ]}

        async def prompt(self, **kwargs):
            self.prompts.append(kwargs["prompt"][0]["text"])
            await self.client.session_update("native-session", {
                "session_update": "agent_message_chunk",
                "content": {"text": '{"status":"succeeded","summary":"checked","findings":[]}'},
            })
            return {"stop_reason": "end_turn"}

    def publish(turn_id, result):
        parsed = parse_acp_worker_output(json.dumps({"event": "acp_result", "result": result}) + "\n", "")
        sources[turn_id] = {
            **sources[DID], "delegate_id": turn_id, "completed": True,
            "status": result["status"], "harness_metadata": parsed.metadata,
        }
        return parsed

    agent = OfflineAgent()
    runner = AcpSessionRunner(sessions, agent.connect)
    failed = asyncio.run(runner.run(
        reference=REF, identity=who, turn_id=overflow_id, prompt="x" * 131072,
        control=control, expected_previous_turn=DID,
    ))
    parsed = publish(overflow_id, failed)
    assert parsed.error["code"] == "acp_prompt_composition_too_large"
    assert failed["status"] == "failed" and failed["prompt_sent"] is False
    assert parsed.metadata["outcome_uncertain"] is False
    assert agent.prompts == []
    checkpoint = sessions.load(REF, who)
    assert checkpoint["phase"] == "failed" and checkpoint["turn"] == overflow_id
    view = control_status(registry, overflow_id, OWNER)["control"]
    assert view["for_delegate"] and view["turn_id"] == overflow_id and view["turn_state"] == "failed"
    assert view["pending_request"] is None and view["steering"] == queued
    assert all("text" not in item for item in view["steering"])
    snapshot = enrich_control_snapshot(registry, sources[overflow_id])
    assert snapshot["completed"] and snapshot["status"] == "failed"
    assert snapshot["acp_control"] == view
    assert control_status(registry, DID, OWNER)["control"]["for_delegate"] is False
    assert send_message(registry, DID, OWNER, action="steer", message_id="stale", text="Old source.")[
        "error"]["code"] == "acp_control_turn_mismatch"
    assert send_message(registry, overflow_id, OWNER, action="reply",
                        request_id="interaction_pending", option_id="a")[
        "error"]["code"] == "acp_control_delegate_not_active"
    assert reconcile(registry, overflow_id, OWNER, reason="Known unsent overflow.")[
        "error"]["code"] == "acp_abandon_requires_uncertain_turn"
    assert send_message(registry, overflow_id, OWNER, action="steer", message_id="first",
                        text=messages[0][1])["error"]["code"] == "acp_control_message_duplicate"
    messages.append(("after-overflow", "Preserve the review scope."))
    assert send_message(registry, overflow_id, OWNER, action="steer",
                        message_id=messages[-1][0], text=messages[-1][1])["success"]

    # Exercise the actual admission resolver used by public delegate_task.
    context, inherited_scope, error = ExecutorRegistry._resolve_acp_resume_context(
        registry, source_id=overflow_id, harness="antigravity3", kind="explore",
        project=project, cwd=tmp_path.resolve(), logical_session_id=OWNER,
        files_in_scope=None, out_of_scope=None, model="gemini-3.8-flash-medium",
        reasoning_effort="medium", timeout=120,
    )
    assert error is None and context == sources[DID]["acp_resume_context"]
    assert inherited_scope == sources[DID]["acp_scope"]
    with pytest.raises(AcpStateError, match="acp_resume_source_stale"):
        asyncio.run(runner.run(
            reference=context["session_ref"], identity=who, turn_id=next_id, prompt="Short task.",
            control=control, expected_previous_turn=DID,
        ))
    assert agent.loads == 1 and agent.prompts == []
    succeeded = asyncio.run(runner.run(
        reference=context["session_ref"], identity=who, turn_id=next_id, prompt="Short task.",
        control=control, expected_previous_turn=overflow_id,
    ))
    assert succeeded["success"] and succeeded["prompt_sent"] is True
    assert succeeded["outcome_uncertain"] is False
    assert publish(next_id, succeeded).error is None
    expected = "Short task.\n\nAdditional instructions queued by the session owner for this turn:\n"
    expected += "\n\n".join(text for _, text in messages)
    assert agent.prompts == [expected]
    assert succeeded["harness_metadata"]["steering_message_ids"] == [key for key, _ in messages]
    final = control_status(registry, next_id, OWNER)["control"]
    assert final["for_delegate"] and final["turn_state"] == "completed"
    assert all(item["status"] == "claimed" and item["target_turn_id"] == next_id for item in final["steering"])
    assert control_status(registry, overflow_id, OWNER)["control"]["for_delegate"] is False
