from __future__ import annotations

from chatgpt_web_oauth_mcp.delegate_health import DelegateHealth
from test_acp_backend import registry


def event(now, *, transport="acp", status="succeeded", code=None, **kw):
    return {"delegate_id": "a" * 12, "harness": "antigravity",
            "execution_backend": transport, "completed_at_epoch": now,
            "status": status, "error": {"code": code} if code else None, **kw}


def test_unknown_stale_and_replay_do_not_refresh_health():
    clock = [1000.0]
    health = DelegateHealth(clock=lambda: clock[0])
    assert health.snapshot("antigravity", "acp")["state"] == "unknown"
    success = event(999)
    health.observe(success)
    assert health.snapshot("antigravity", "acp")["state"] == "healthy"
    assert health.snapshot("antigravity", "cli")["state"] == "unknown"
    clock[0] = 2000
    health.observe(success)
    result = health.snapshot("antigravity", "acp")
    assert result["state"] == "stale"
    assert result["observed_at_epoch"] == 999
    health.observe(event(998, status="failed", code="authentication_failed"))
    assert health.snapshot("antigravity", "acp")["last_observed_state"] == "healthy"


def test_transport_failure_is_isolated_and_auth_shared():
    health = DelegateHealth(clock=lambda: 1000)
    health.observe(event(990, transport="cli", status="failed", code="acp_transport_error"))
    assert health.snapshot("antigravity", "cli")["state"] == "transport_error"
    assert health.snapshot("antigravity", "acp")["state"] == "unknown"
    health.observe(event(995, status="failed", code="authentication_failed"))
    assert health.snapshot("antigravity", "cli")["state"] == "auth_required"
    assert health.snapshot("antigravity", "acp")["state"] == "auth_required"
    health.observe(event(999, transport="cli"))
    assert health.snapshot("antigravity", "cli")["state"] == "healthy"
    assert health.snapshot("antigravity", "acp")["state"] == "unknown"


def test_uncertain_writer_is_not_healthy_and_missing_time_not_fresh():
    health = DelegateHealth(clock=lambda: 1000)
    health.observe(event(999, status="failed", code="acp_writer_finalization_failed", outcome_uncertain=True))
    assert health.snapshot("antigravity", "acp")["state"] == "reconcile_required"
    health.observe(event(None))
    assert health.snapshot("antigravity", "acp")["state"] == "reconcile_required"


def test_registry_cooldowns_partition_transport_and_share_account(tmp_path):
    import time
    r, workspace = registry(tmp_path)
    now = time.time()
    r._note_routing_terminal(event(now - 1, transport="cli", status="failed", code="delegate_harness_unavailable"))
    assert r._routing_block("antigravity", "cli") is not None
    assert r._routing_block("antigravity", "acp") is None
    r._note_routing_terminal(event(now, status="failed", code="authentication_failed"))
    assert r._routing_block("antigravity", "cli")["failure_domain"] == "account"
    assert r._routing_block("antigravity", "acp")["failure_domain"] == "account"


def test_registry_info_keeps_static_availability_separate(tmp_path):
    import time
    r, workspace = registry(tmp_path)
    before = r.harness_info()["antigravity"]["execution_backends"]
    assert before["cli"]["enabled"] is True
    assert before["cli"]["health"]["state"] == "unknown"
    r._note_routing_terminal(event(time.time() - 1))
    after = r.harness_info()["antigravity"]["execution_backends"]
    assert after["acp"]["health"]["state"] == "healthy"
    assert after["cli"]["health"]["state"] == "unknown"


def test_make_task_adds_only_bound_handoff(tmp_path, monkeypatch):
    import hashlib
    r, workspace = registry(tmp_path)
    source = event(999)
    source.update(completed=True, cwd=str(workspace), structured_output={"summary": "prior finding"},
        harness_metadata={"owner_digest": hashlib.sha256(b"owner").hexdigest()},
        acp_scope={"files_in_scope": ["sample.py"], "out_of_scope": []})
    monkeypatch.setattr(r, "_resume_source_snapshot", lambda _: source)
    task = r._make_task(
        harness="antigravity", project=r.project_resolver.resolve(workspace), cwd=workspace,
        kind="explore", task="Current instruction", goal=None, task_id=None, group_id=None,
        model="gemini-3.8-flash-medium", reasoning_effort="medium", sandbox_mode="acp-native-read-only",
        commit_mode="forbidden", execution_timeout_seconds=90, depends_on_group_ids=(),
        files_in_scope=["sample.py"], out_of_scope=[], context_files=[], acceptance_criteria=[],
        done_means=[], verification_commands=[], output_schema=None, parse_structured_output=True,
        request_fingerprint="handoff-test", execution_backend="acp", logical_session_id="owner",
        resume_from_delegate_id="a" * 12, acp_resume_context={"session_ref": "b" * 32})
    assert "prior finding" in task.prompt
    assert "Current instruction" in task.prompt
    assert "current task and scope take precedence" in task.prompt


def test_single_terminal_health_restored_without_timestamp_refresh(tmp_path):
    import json
    import time
    from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
    state = tmp_path / "delegates"
    log = state / "record"
    log.mkdir(parents=True)
    original = time.time() - 55
    payload = event(original)
    payload.update(completed=True, cwd=str(tmp_path))
    (log / "metadata.json").write_text(json.dumps(payload))
    r = ExecutorRegistry(codex_command="true", delegate_state_root=state)
    r.recover_persisted_delegates(roots=[state])
    health = r._delegate_health.snapshot("antigravity", "acp")
    assert health["observed_at_epoch"] == original
    assert health["state"] == "healthy"


def test_scheduler_callback_preserves_persisted_terminal_epoch(tmp_path, monkeypatch):
    import time
    from test_acp_backend import stored_acp_task
    r, workspace = registry(tmp_path)
    task = stored_acp_task(r, workspace)
    original = time.time() - 1
    task.result = event(original)
    task.result.update(completed=True, delegate_id=task.delegate_id, failure_analysis={}, replan={})
    task.state = 'succeeded'
    task.completed_at = original + 0.05
    observed = []
    monkeypatch.setattr(r, '_record_terminal_telemetry', lambda snapshot, **kw: observed.append(kw['completed_at_epoch']))
    monkeypatch.setattr(r, 'maintain_persisted_delegates', lambda: None)
    r._on_task_terminal(task)
    assert task.result['completed_at_epoch'] == original
    assert observed == [original]
    assert r._delegate_health.snapshot('antigravity', 'acp')['observed_at_epoch'] == original
