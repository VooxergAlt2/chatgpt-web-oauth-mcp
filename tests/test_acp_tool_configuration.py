"""Provider-free checks for observation of forwarded native tool configuration."""
from __future__ import annotations

import asyncio
import json
import os
import struct
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp.acp_helper_broker import AcpHelperBroker
from chatgpt_web_oauth_mcp.acp_network import AcpNetworkGateway, NetworkBoundaryError
from chatgpt_web_oauth_mcp.acp_sandbox import AcpSandbox


FIELDS = {
    "subagents", "runWorkflow", "find", "userQuestions", "runCommand",
    "manageTask", "schedule", "fileEdit", "viewFile", "writeToFile",
    "grepSearch", "listDir", "generateImage", "searchWeb", "readUrlContent",
}


def initialization(fields=None):
    return {"config": {
        "models": [{"name": "gemini-3.8-flash", "geminiApiEndpoint": {
            "baseUrl": "http://127.0.0.1:41234",
            "httpHeaders": {"X-ACP-Proxy-Token": "a" * 32},
        }}],
        "harnessSideTools": fields if fields is not None else {},
        "systemInstructions": {"text": "PRIVATE_PROMPT"},
        "workspaces": [{"path": "PRIVATE_PATH"}],
    }}


def test_observation_has_only_fixed_boolean_fields_and_unknown_finish(tmp_path):
    observations = []
    target = AcpNetworkGateway(tmp_path, configuration_observer=observations.append)
    event = initialization({
        "viewFile": {"enabled": True, "path": "PRIVATE_PATH"},
        "fileEdit": {"enabled": False, "arguments": "PRIVATE_ARGUMENTS"},
        "unknown PRIVATE_LABEL": {"enabled": True},
        "finish": {"enabled": True},
    })
    before = json.dumps(event)
    target._observe_configuration(before)
    assert json.dumps(event) == before
    assert len(observations) == 1
    view = observations[0]
    assert view == {
        "version": 1, "source": "forwarded_native_initialization",
        "harness_side_tools": {key: True if key == "viewFile" else False if key == "fileEdit" else None
                               for key in FIELDS},
        "unobservable_builtin_tools": ["finish"],
    }
    serialized = json.dumps(view)
    assert len(serialized) < 1024
    assert "PRIVATE" not in serialized and "http" not in serialized
    assert "X-ACP-Proxy-Token" not in serialized


@pytest.mark.parametrize("node", [
    None, {}, [], False, True, "PRIVATE", {"enabled": None},
    {"enabled": "true"}, {"enabled": 0}, {"enabled": 1},
    {"enabled": []}, {"enabled": {}}, {"enabled": float("nan")},
])
def test_missing_or_malformed_enabled_is_unknown_never_disabled(tmp_path, node):
    observations = []
    target = AcpNetworkGateway(tmp_path, configuration_observer=observations.append)
    target._observe_configuration(json.dumps(initialization({"viewFile": node})))
    assert observations[0]["harness_side_tools"] == {key: None for key in FIELDS}


@pytest.mark.parametrize("fields", [None, [], "PRIVATE", 1, False])
def test_missing_or_nonmapping_harness_side_tools_is_unknown(tmp_path, fields):
    observations = []
    target = AcpNetworkGateway(tmp_path, configuration_observer=observations.append)
    event = initialization()
    if fields is None:
        event["config"].pop("harnessSideTools")
    else:
        event["config"]["harnessSideTools"] = fields
    target._observe_configuration(json.dumps(event))
    assert observations[0]["harness_side_tools"] == {key: None for key in FIELDS}


def test_all_known_enabled_flags_are_independently_observed(tmp_path):
    observations = []
    target = AcpNetworkGateway(tmp_path, configuration_observer=observations.append)
    for enabled in (False, True):
        target._observe_configuration(json.dumps(initialization(
            {key: {"enabled": enabled} for key in FIELDS})))
    assert observations[0]["harness_side_tools"] == {key: False for key in FIELDS}
    assert observations[1]["harness_side_tools"] == {key: True for key in FIELDS}
    observations[0]["harness_side_tools"]["viewFile"] = "mutated"
    assert observations[1]["harness_side_tools"]["viewFile"] is True


@pytest.mark.parametrize("message", ["invalid", "null", "[]", "{}", '{"config":null}'])
def test_malformed_observation_input_is_best_effort(tmp_path, message):
    observations = []
    target = AcpNetworkGateway(tmp_path, configuration_observer=observations.append)
    target._observe_configuration(message)
    assert observations == [] and target.error_code is None


def test_observer_exceptions_do_not_change_transport_state_but_cancellation_propagates(tmp_path):
    def failed_observer(value):
        raise RuntimeError("PRIVATE")

    target = AcpNetworkGateway(tmp_path, configuration_observer=failed_observer)
    target._observe_configuration(json.dumps(initialization()))
    assert target.error_code is None

    def cancelled_observer(value):
        raise asyncio.CancelledError()

    target.configuration_observer = cancelled_observer
    with pytest.raises(asyncio.CancelledError):
        target._observe_configuration(json.dumps(initialization()))


class Parent:
    def __init__(self, message):
        self.message = message
        self.request = SimpleNamespace(headers=SimpleNamespace(get_all=lambda key: ["fixture-key"]))
        self.closed = []

    async def recv(self):
        return self.message

    async def close(self, code):
        self.closed.append(code)

    async def send(self, message):
        raise AssertionError("No native payload expected in this fixture")

    async def __aiter__(self):
        if False:
            yield None


class Native:
    def __init__(self, order, fail=False):
        self.sent = []
        self.order = order
        self.fail = fail

    async def send(self, message):
        self.order.append("send")
        if self.fail:
            raise OSError("fixture send failure")
        self.sent.append(message)

    async def __aiter__(self):
        if False:
            yield None


@pytest.mark.parametrize("observer_fails", [False, True])
def test_websocket_observes_only_after_send_and_keeps_forwarded_config_unchanged(tmp_path, monkeypatch, observer_fails):
    import websockets.asyncio.client

    order, observations = [], []
    native = Native(order)
    event = initialization({"viewFile": {"enabled": True}, "fileEdit": {"enabled": False}})
    parent = Parent(json.dumps(event))

    def observe(value):
        order.append("observe")
        assert len(native.sent) == 1
        observations.append(value)
        if observer_fails:
            raise RuntimeError("fixture observer failure")

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield native

    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)
    target = AcpNetworkGateway(tmp_path, configuration_observer=observe)
    target.api_key, target.relay_port = "fixture-key", 32000
    target.native_socket = object()
    asyncio.run(target._websocket(parent))
    assert order == ["send", "observe"] and len(observations) == 1
    sent = json.loads(native.sent[0])
    assert sent["config"]["harnessSideTools"] == event["config"]["harnessSideTools"]
    assert sent["config"]["systemInstructions"] == event["config"]["systemInstructions"]
    assert sent["config"]["models"][0]["geminiApiEndpoint"]["baseUrl"] == "http://127.0.0.1:32000"
    assert target.error_code is None and parent.closed == [] and not target.websocket_tasks


@pytest.mark.parametrize("reject_at", ["validation", "send"])
def test_rejected_or_unsent_initialization_is_not_observed(tmp_path, monkeypatch, reject_at):
    import websockets.asyncio.client

    observations, order = [], []
    native = Native(order, fail=reject_at == "send")
    event = initialization({"viewFile": {"enabled": True}})
    if reject_at == "validation":
        event["config"]["models"] = []
    parent = Parent(json.dumps(event))

    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield native

    monkeypatch.setattr(websockets.asyncio.client, "connect", connect)
    target = AcpNetworkGateway(tmp_path, configuration_observer=observations.append)
    target.api_key, target.relay_port = "fixture-key", 32000
    target.native_socket = object()
    asyncio.run(target._websocket(parent))
    assert observations == [] and native.sent == []
    assert target.error_code is not None and parent.closed and not target.websocket_tasks


@pytest.mark.parametrize("proposal", [False, True])
def test_helper_broker_passes_observer_without_launching_native_process(tmp_path, monkeypatch, proposal):
    import chatgpt_web_oauth_mcp.acp_helper_broker as module

    observer = lambda value: None
    captured = []

    class Gateway:
        error_code = None
        closing = False

        def __init__(self, root, *, gateway=None, configuration_observer=None, proposal=False):
            captured.append((configuration_observer, proposal))

        async def start(self):
            raise NetworkBoundaryError("fixture_stop_before_launch")

        async def close(self):
            pass

    class Writer:
        def get_extra_info(self, key):
            return SimpleNamespace(getsockopt=lambda *args: struct.pack("3i", 1, os.getuid(), 1))

        def close(self):
            pass

        async def wait_closed(self):
            pass

    monkeypatch.setattr(module, "AcpNetworkGateway", Gateway)
    broker = AcpHelperBroker(tmp_path / "helper.sock", ("unused",), configuration_observer=observer,
                             proposal=proposal)
    asyncio.run(broker._serve(None, Writer()))
    assert captured == [(observer, proposal)]
    assert broker.error_code == "fixture_stop_before_launch"
    assert not broker.connections and not broker.processes


@pytest.mark.parametrize("proposal", [False, True])
def test_sandbox_passes_observer_without_changing_sdk_connection(tmp_path, monkeypatch, proposal):
    import chatgpt_web_oauth_mcp.acp_sandbox as module

    observer = lambda value: None
    gateway = object()
    client = object()
    connection = object()
    captured = []

    class Broker:
        error_code = None

        def __init__(self, path, argv, *, gateway=None, configuration_observer=None, proposal=False):
            self.path = path
            captured.append((gateway, configuration_observer, proposal))

        @asynccontextmanager
        async def running(self):
            yield self

    @asynccontextmanager
    async def sdk_transport(received_client):
        assert received_client is client
        yield connection

    box = SimpleNamespace(control=tmp_path, helper_command=lambda: ["fixed-helper"],
                          runtime_transport=lambda path: sdk_transport)
    monkeypatch.setattr(module, "AcpHelperBroker", Broker)

    async def scenario():
        async with AcpSandbox.transport(box, gateway=gateway, configuration_observer=observer, proposal=proposal)(client) as result:
            assert result is connection

    asyncio.run(scenario())
    assert captured == [(gateway, observer, proposal)]
