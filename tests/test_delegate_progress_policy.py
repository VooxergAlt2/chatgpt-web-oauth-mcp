"""Thresholds and existing supervisor CAS, no provider."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp.acp_supervision import ProgressCheckpoints
from chatgpt_web_oauth_mcp.delegate_progress_policy import DelegateProgressPolicy
from chatgpt_web_oauth_mcp.supervision import AgentEndpoint, SupervisorBinding, SupervisorChannel, SupervisorStore


@pytest.mark.parametrize("limit", [1, 8, 15])
def test_small_tasks_have_no_extra_gates(limit):
    assert DelegateProgressPolicy(limit).due(limit, 1) is None


@pytest.mark.parametrize("limit,early,late", [(16, 4, 12), (64, 8, 48)])
def test_thresholds_acknowledgement_and_replayed_counts(limit, early, late):
    policy = DelegateProgressPolicy(limit)
    assert policy.due(early - 1) is None
    assert policy.due(early) == "early_review"
    assert policy.due(early) == "early_review"  # No implicit delivery
    policy.acknowledge("early_review")
    assert policy.due(early) is None
    assert policy.due(1) is None
    assert policy.due(late) == "budget_review"
    policy.acknowledge("budget_review")
    assert policy.due(limit) is None


def test_late_signal_supersedes_early_and_time_requires_activity():
    policy = DelegateProgressPolicy(64)
    assert policy.due(0, 5) is None
    assert policy.due(1, 30) == "budget_review"
    policy.acknowledge("budget_review")
    assert policy.due(8, 100) is None


@pytest.mark.parametrize("value", [True, 0, 65, 2.5, "24"])
def test_invalid_budget(value):
    with pytest.raises(ValueError):
        DelegateProgressPolicy(value)


def channel(tmp_path):
    binding = SupervisorBinding(owner="b" * 64, execution_id="test-turn",
        session_ref="test-session", endpoint=AgentEndpoint("google", "acp", "test", "flash", "high"),
        workspace=str(tmp_path), scope_digest="a" * 64)
    store = SupervisorStore(tmp_path / "supervision")
    store.open(binding, expires_at=time.time() + 120)
    return SupervisorChannel(store, binding, deadline=time.monotonic() + 100,
                             cancel=asyncio.Event(), observe=lambda: {})


def test_requests_reuse_canonical_supervisor_and_preserve_operator_request(tmp_path):
    ch = channel(tmp_path)
    control = ProgressCheckpoints(ch, 64)
    ch.store.command(ch.binding, action="request_checkpoint", command_id="operator",
                     expected_revision=0, instruction="operator first")
    control.observe(8)
    view = ch.store.snapshot(ch.binding)
    assert view["request"]["command_id"] == "operator"
    assert control.snapshot()["issued"] == []
    state = ch.store.publish(ch.binding, report={"phase": "plan", "summary": "plan", "evidence": []}, observations={})
    ch.store.command(ch.binding, action="continue", command_id="continue-1",
        expected_revision=state["revision"], checkpoint_id=state["checkpoint"]["id"])
    ch.store.claim_decision(ch.binding, checkpoint_id=state["checkpoint"]["id"], observations={})
    control.observe(8)  # Existing completed checkpoint satisfies early review.
    assert ch.store.snapshot(ch.binding)["state"] == "running"
    control.observe(48)
    assert ch.store.snapshot(ch.binding)["request"]["command_id"] == "progress-budget_review"
    assert control.snapshot()["native_interception"] is False


def test_early_request_deduplicates_and_does_not_capture_draft(tmp_path):
    ch = channel(tmp_path)
    ch.capture = lambda: pytest.fail("unsafe native-time capture")
    control = ProgressCheckpoints(ch, 64)
    control.observe(8)
    revision = ch.store.snapshot(ch.binding)["revision"]
    control.observe(8)
    assert ch.store.snapshot(ch.binding)["revision"] == revision
    assert control.snapshot()["issued"] == ["early_review"]


def test_cas_race_does_not_consume_signal(tmp_path, monkeypatch):
    from chatgpt_web_oauth_mcp.supervision import SupervisionError
    ch = channel(tmp_path)
    control = ProgressCheckpoints(ch, 64)
    original = ch.store.command
    def race(*args, **kwargs):
        raise SupervisionError("supervisor_stale_revision")
    monkeypatch.setattr(ch.store, "command", race)
    control.observe(8)
    assert control.snapshot()["issued"] == []
    monkeypatch.setattr(ch.store, "command", original)
    control.observe(8)
    assert control.snapshot()["issued"] == ["early_review"]
