import copy
import math
import pytest
from chatgpt_web_oauth_mcp.desktop_recovery_policy import expired_idle_lease_reason


def test_eligible_expired_idle_lease():
    ui = {
        "workspace_id": "ws_1", "state": "idle", "expires_at": 100.0,
        "token": "tok_1", "operation_token": None, "activation_request_id": None,
    }
    ui_before = copy.deepcopy(ui)
    assert expired_idle_lease_reason(ui, now=100.0, native_outstanding=False, active_readers=0) == "expired_idle_lease"
    assert ui == ui_before


@pytest.mark.parametrize("now,expires_at", [(105.0, 100.0), (100, 100), (100.5, 100), (100.0, 99.5)])
def test_eligible_clock_variations(now, expires_at):
    ui = {"state": "idle", "expires_at": expires_at}
    assert expired_idle_lease_reason(ui, now=now, native_outstanding=False, active_readers=0) == "expired_idle_lease"


def test_unexpired_lease_rejected():
    ui = {"state": "idle", "expires_at": 100.1}
    assert expired_idle_lease_reason(ui, now=100.0) is None


@pytest.mark.parametrize("state", ["inflight", "draining", "verifying", "activation_failed", "ready", None, 1])
def test_non_idle_states_rejected(state):
    ui = {"state": state, "expires_at": 50.0}
    assert expired_idle_lease_reason(ui, now=100.0) is None


@pytest.mark.parametrize("field,value", [
    ("operation_token", "op_tok"), ("operation_token", ""),
    ("activation_request_id", "req_act"), ("durable", True), ("durable", "unresolved"),
])
def test_active_operation_or_request_rejected(field, value):
    ui = {"state": "idle", "expires_at": 50.0, field: value}
    assert expired_idle_lease_reason(ui, now=100.0) is None


@pytest.mark.parametrize("kwarg", [
    {"native_outstanding": True}, {"native_outstanding": 1}, {"native_outstanding": -1},
    {"native_outstanding": ["req_1"]}, {"active_readers": True}, {"active_readers": 1},
    {"active_readers": -1}, {"active_readers": ["r_1"]}, {"active_readers": {"r_1": {}}},
])
def test_active_readers_or_outstanding_rejected(kwarg):
    ui = {"state": "idle", "expires_at": 50.0}
    assert expired_idle_lease_reason(ui, now=100.0, **kwarg) is None


@pytest.mark.parametrize("now,expires_at", [
    (float("nan"), 50.0), (float("inf"), 50.0), (True, 50.0), ("100", 50.0),
    (100.0, float("nan")), (100.0, float("inf")), (100.0, True), (100.0, False),
    (100.0, None), (100.0, "50"),
])
def test_invalid_or_nonfinite_clocks_rejected(now, expires_at):
    ui = {"state": "idle", "expires_at": expires_at}
    assert expired_idle_lease_reason(ui, now=now) is None


@pytest.mark.parametrize("bad_ui", [None, "idle", [1], 42])
def test_non_dict_ui_rejected(bad_ui):
    assert expired_idle_lease_reason(bad_ui, now=100.0) is None


@pytest.mark.parametrize("field", ["now", "expires_at", "native_outstanding", "active_readers"])
def test_unrepresentable_numeric_metadata_never_releases_lease(field):
    ui = {"state": "idle", "expires_at": 100}
    arguments = {"now": 101, "native_outstanding": False, "active_readers": 0}
    if field == "expires_at":
        ui[field] = 10 ** 1000
    else:
        arguments[field] = 10 ** 1000
    assert expired_idle_lease_reason(ui, **arguments) is None
