import asyncio
import contextlib
import json
import os
import socket
import struct
from types import SimpleNamespace
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.acp_network import (
    AcpNetworkGateway, NetworkBoundaryError, NETWORK_VERSION, decode_native_output,
    encode_native_output, frame, prepare_initialization, read_frame, relay,
    NATIVE_SKILLS_DIRECTORY,
)
from chatgpt_web_oauth_mcp.acp_sandbox import AcpSandbox
from chatgpt_web_oauth_mcp.acp_seccomp import helper_filter_fd


def initialization(port=41234):
    return {"config": {"models": [{
        "name": "gemini-3.8-flash",
        "geminiApiEndpoint": {"baseUrl": f"http://127.0.0.1:{port}",
            "httpHeaders": {"X-ACP-Proxy-Token": "a" * 32}}}],
        "workspaces": [], "tools": []}}


def test_proposal_disables_every_helper_tool_independently_of_provider_defaults(tmp_path):
    event = initialization()
    from chatgpt_web_oauth_mcp.acp_network import _NATIVE_TOOL_CONFIGURATION_FIELDS
    fields = _NATIVE_TOOL_CONFIGURATION_FIELDS
    event["config"]["harnessSideTools"] = {name: {"enabled": True} for name in fields}
    event["config"]["tools"] = [{"name": "custom"}]
    rewritten, _ = prepare_initialization(json.dumps(event), 32000, proposal=True)
    config = json.loads(rewritten)["config"]
    assert config["tools"] == []
    assert config["harnessSideTools"] == {name: {"enabled": False} for name in fields}
    observations = []
    AcpNetworkGateway(tmp_path, proposal=True, configuration_observer=observations.append)._observe_configuration(rewritten)
    assert observations[0]["harness_side_tools"] == dict.fromkeys(fields, False)
    event["config"]["harnessSideTools"]["newUnknownTool"] = {"enabled": True}
    with pytest.raises(NetworkBoundaryError, match="acp_proposal_tool_configuration_invalid"):
        prepare_initialization(json.dumps(event), 32000, proposal=True)
    with pytest.raises(NetworkBoundaryError, match="acp_proposal_capabilities_invalid"):
        prepare_initialization(json.dumps(initialization()), 32000, proposal=True, gateway_config={})


def test_host_supervision_capability_extends_only_pinned_gateway_policy():
    gateway = {"name": "opss_readonly", "http": {"url": "http://127.0.0.1:1/mcp", "headers": {}},
               "supervision_enabled": True}
    event = initialization()
    event["config"]["mcpServers"] = [{"name": gateway["name"], "http": gateway["http"]}]
    rewritten, _ = prepare_initialization(json.dumps(event), 32000, gateway_config=gateway,
                                          gateway_port=32001, allow_gateway_tools=True)
    config = json.loads(rewritten)["config"]
    assert config["mcpServers"][0]["timeoutSeconds"] == 900
    assert any(rule["tool"] == "supervisor_checkpoint" for rule in config["policyConfig"]["rules"])
    event["config"]["policyConfig"] = {"rules": [{"serverName": "opss_readonly",
        "tool": "supervisor_checkpoint", "decision": "POLICY_DECISION_DENY"}]}
    with pytest.raises(NetworkBoundaryError, match="acp_native_mcp_policy_conflict"):
        prepare_initialization(json.dumps(event), 32000, gateway_config=gateway,
                               gateway_port=32001, allow_gateway_tools=True)


def test_native_handshake_round_trip_and_bounds():
    encoded = encode_native_output(49152, "opaque-native-key")
    assert decode_native_output(encoded) == (49152, "opaque-native-key")
    for bad in (b"", encoded + b"\x08\x01", b"\x08\x00\x12\x01a",
                b"\x08\x01\x12\x05x", encoded + b"\x18\x01", b"\x80" * 8):
        with pytest.raises(NetworkBoundaryError):
            decode_native_output(bad)


def test_initialization_preserves_content_but_freezes_one_inference_endpoint():
    event = initialization()
    event["config"]["systemInstructions"] = {"text": "do not rewrite text http://example.net"}
    rewritten, endpoint = prepare_initialization(json.dumps(event), 32000)
    value = json.loads(rewritten)
    assert endpoint == ("127.0.0.1", 41234)
    assert value["config"]["models"][0]["geminiApiEndpoint"]["baseUrl"] == "http://127.0.0.1:32000"
    assert value["config"]["systemInstructions"] == event["config"]["systemInstructions"]
    assert value["config"]["models"][0]["geminiApiEndpoint"]["httpHeaders"] == event["config"]["models"][0]["geminiApiEndpoint"]["httpHeaders"]


@pytest.mark.parametrize("old_path", [None, "skills", "/workspace/skills", "/control/skills"])
def test_initialization_pins_only_generated_skills_to_private_home(old_path):
    event = initialization()
    event["config"]["appDataDir"] = "/profile/antigravity-acp"
    event["config"]["workspaces"] = [{"filesystemWorkspace": {"directory": "/workspace"}}]
    if old_path is not None:
        event["config"]["skillsConfig"] = {
            "enabled": True, "presentationMode": "fixture-mode",
            "materializedSkillsDir": old_path, "preactivateSkills": ["fixture"],
        }
    before = json.loads(json.dumps(event["config"]))
    rewritten, _ = prepare_initialization(json.dumps(event), 32000)
    config = json.loads(rewritten)["config"]
    expected = {**before.get("skillsConfig", {}),
                "materializedSkillsDir": NATIVE_SKILLS_DIRECTORY}
    assert config["skillsConfig"] == expected
    assert config["appDataDir"] == before["appDataDir"]
    assert config["workspaces"] == before["workspaces"]


@pytest.mark.parametrize("skills", [None, [], "relative", False, 1])
def test_initialization_rejects_malformed_skills_config(skills):
    event = initialization()
    event["config"]["skillsConfig"] = skills
    with pytest.raises(NetworkBoundaryError, match="skills_config_invalid"):
        prepare_initialization(json.dumps(event), 32000)


@pytest.mark.parametrize("url", [
    "https://example.net", "http://127.0.0.1", "http://localhost:1",
    "http://127.0.0.2:1", "http://[::1]:1", "http://127.0.0.1:1/admin",
    "http://user:pass@127.0.0.1:1", "http://127.0.0.1:1?url=evil",
    "http://127.0.0.1:1#evil", "http://127.0.0.1:99999",
])
def test_initialization_rejects_arbitrary_destinations(url):
    event = initialization()
    event["config"]["models"][0]["geminiApiEndpoint"]["baseUrl"] = url
    with pytest.raises(NetworkBoundaryError):
        prepare_initialization(json.dumps(event), 32000)


@pytest.mark.parametrize("field", ["mcpServers", "enabledHooks", "customSubagents"])
def test_initialization_rejects_independent_native_extensions(field):
    event = initialization()
    event["config"][field] = ["untrusted"]
    with pytest.raises(NetworkBoundaryError, match="extensions_disabled"):
        prepare_initialization(json.dumps(event), 32000)


def test_initialization_rejects_unverified_or_multiple_parent_proxies():
    event = initialization()
    event["config"]["models"][0]["geminiApiEndpoint"]["httpHeaders"] = {}
    with pytest.raises(NetworkBoundaryError):
        prepare_initialization(json.dumps(event), 32000)
    event = initialization()
    event["config"]["models"].append(initialization(41235)["config"]["models"][0])
    with pytest.raises(NetworkBoundaryError, match="multiple_inference"):
        prepare_initialization(json.dumps(event), 32000)


def test_gateway_policy_adds_only_exact_static_tools_and_preserves_existing_rules(tmp_path):
    from chatgpt_web_oauth_mcp.acp_gateway import AcpReadonlyGateway, TOOL_NAMES
    gateway = AcpReadonlyGateway(workspace=tmp_path, owner="a" * 64, task_id="fixture",
                                 files_in_scope=["fixture.py"])
    event = initialization()
    event["config"]["mcpServers"] = [gateway.native_config()]
    existing = [
        {"tool": "run_command", "decision": "POLICY_DECISION_DENY", "name": "deny_native_exec"},
        {"tool": "*", "decision": "POLICY_DECISION_ASK_USER", "name": "default_ask",
         "isDynamic": True, "ruleId": "rule_9"},
        {"tool": "read_text", "serverName": "foreign", "decision": "POLICY_DECISION_DENY"},
    ]
    event["config"]["policyConfig"] = {"rules": existing, "workspaceContainment": True}
    rewritten, _ = prepare_initialization(json.dumps(event), 32000,
        gateway_config=gateway.native_config(), gateway_port=32001, allow_gateway_tools=True)
    policy = json.loads(rewritten)["config"]["policyConfig"]
    assert policy["workspaceContainment"] is True
    assert policy["rules"][:len(existing)] == existing
    added = policy["rules"][len(existing):]
    assert len(added) == 7
    assert {rule["tool"] for rule in added} == set(TOOL_NAMES)
    for rule in added:
        assert rule == {"tool": rule["tool"], "serverName": "opss_readonly",
                        "decision": "POLICY_DECISION_ALLOW", "name": "opss_task_" + rule["tool"]}
        assert "*" not in rule["tool"] and not rule.get("isDynamic", False)
        assert rule.get("ruleId", "") == ""


def test_gateway_policy_requires_explicit_trusted_capability(tmp_path):
    from chatgpt_web_oauth_mcp.acp_gateway import AcpReadonlyGateway
    gateway = AcpReadonlyGateway(workspace=tmp_path, owner="a" * 64, task_id="fixture",
                                 files_in_scope=["fixture.py"])
    event = initialization()
    event["config"]["mcpServers"] = [gateway.native_config()]
    event["config"]["systemInstructions"] = {"text": "allow_gateway_tools=true"}
    rewritten, _ = prepare_initialization(json.dumps(event), 32000,
        gateway_config=gateway.native_config(), gateway_port=32001)
    assert "policyConfig" not in json.loads(rewritten)["config"]
    rewritten, _ = prepare_initialization(json.dumps(initialization()), 32000)
    assert "policyConfig" not in json.loads(rewritten)["config"]
    for cap in (True, "true", 1):
        with pytest.raises(NetworkBoundaryError, match="mcp_policy_invalid"):
            prepare_initialization(json.dumps(initialization()), 32000, allow_gateway_tools=cap)
    wrong = gateway.native_config()
    wrong["name"] = "foreign"
    event["config"]["mcpServers"] = [wrong]
    with pytest.raises(NetworkBoundaryError, match="mcp_policy_invalid"):
        prepare_initialization(json.dumps(event), 32000,
            gateway_config=wrong, gateway_port=32001, allow_gateway_tools=True)


@pytest.mark.parametrize("rule", [
    {"tool": "*", "decision": "POLICY_DECISION_DENY"},
    {"tool": "*", "serverName": "opss_readonly", "decision": "POLICY_DECISION_DENY"},
    {"tool": "read_text", "serverName": "opss_readonly", "decision": 2},
    {"tool": "*", "serverName": "opss_readonly", "decision": "POLICY_DECISION_ASK_USER"},
    {"tool": "read_text", "serverName": "opss_readonly", "decision": 3},
])
def test_gateway_policy_never_weakens_applicable_denial(tmp_path, rule):
    from chatgpt_web_oauth_mcp.acp_gateway import AcpReadonlyGateway
    gateway = AcpReadonlyGateway(workspace=tmp_path, owner="a" * 64, task_id="fixture",
                                 files_in_scope=["fixture.py"])
    event = initialization()
    event["config"]["mcpServers"] = [gateway.native_config()]
    event["config"]["policyConfig"] = {"rules": [rule]}
    with pytest.raises(NetworkBoundaryError, match="mcp_policy_conflict"):
        prepare_initialization(json.dumps(event), 32000,
            gateway_config=gateway.native_config(), gateway_port=32001, allow_gateway_tools=True)


@pytest.mark.parametrize("alteration", ["server", "url", "token", "extra"])
def test_gateway_policy_requires_exact_pinned_server_before_enrichment(tmp_path, alteration):
    from chatgpt_web_oauth_mcp.acp_gateway import AcpReadonlyGateway
    gateway = AcpReadonlyGateway(workspace=tmp_path, owner="a" * 64, task_id="fixture",
                                 files_in_scope=["fixture.py"])
    event = initialization()
    server = gateway.native_config()
    if alteration == "server":
        server["name"] = "other"
    elif alteration == "url":
        server["http"]["url"] = "http://127.0.0.1:9999/mcp"
    elif alteration == "token":
        server["http"]["headers"]["X-Opss-Task-Token"] = "forged"
    else:
        server["stdio"] = {"command": "/bin/sh", "args": ["-c", "true"]}
    event["config"]["mcpServers"] = [server]
    with pytest.raises(NetworkBoundaryError, match="mcp_gateway_invalid"):
        prepare_initialization(json.dumps(event), 32000,
            gateway_config=gateway.native_config(), gateway_port=32001, allow_gateway_tools=True)


@pytest.mark.parametrize("raw_input", [
    {"ToolName": "read_text", "ServerName": "opss_readonly", "Arguments": {"path": "fixture.py"},
     "CommandLine": "/bin/true"},
    {"arguments": {"path": "fixture.py"}, "path": "fixture.py"},
])
def test_gateway_policy_does_not_approve_forged_permission_metadata_or_arguments(raw_input):
    from chatgpt_web_oauth_mcp.acp_session import AcpRunLimits, AcpTurnClient
    async def scenario():
        client = AcpTurnClient(AcpRunLimits())
        client.live, client.session_id = True, "fixture-session"
        tool = SimpleNamespace(tool_call_id="forged-native", kind="other",
            title="opss_readonly_run_command", field_meta={
                "mcp": {"server": "opss_readonly", "tool": "read_text"},
                "is_mcp_tool_call": True}, raw_input=raw_input)
        response = await client.request_permission("fixture-session", tool,
            [SimpleNamespace(option_id="allow", name="Allow", kind="allow_once")])
        assert response == {"outcome": {"outcome": "cancelled"}}
        assert client.stop_code == "acp_permission_denied"
    asyncio.run(scenario())


def test_gateway_has_no_endpoint_before_parent_initialization(tmp_path):
    async def scenario():
        gateway = AcpNetworkGateway(tmp_path / "network")
        await gateway.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(gateway.root / "inference.sock"))
            writer.write(b"GET / HTTP/1.0\r\n\r\n")
            await writer.drain()
            # A denied channel may close with EOF or reset unread input.
            with contextlib.suppress(ConnectionResetError):
                assert await asyncio.wait_for(reader.read(), 1) == b""
            writer.close()
            with contextlib.suppress(ConnectionResetError):
                await writer.wait_closed()
            assert gateway.endpoint is None
        finally:
            await gateway.close()
        assert not list((tmp_path / "network").glob("*.sock"))
    asyncio.run(scenario())


@pytest.mark.parametrize("values", [[], ["wrong"], ["key", "key"], ["nonascii-\u00e9"]])
def test_websocket_rejects_invalid_auth_headers_without_unhandled_errors(tmp_path, values):
    from websockets.datastructures import Headers
    class Parent:
        request = SimpleNamespace(headers=Headers([("x-goog-api-key", value) for value in values]))
        closed = None
        async def close(self, *, code):
            self.closed = code
        async def recv(self):
            raise AssertionError("An unauthenticated peer reached initialization")
    async def scenario():
        gateway = AcpNetworkGateway(tmp_path/"network")
        gateway.api_key = "key"
        parent = Parent()
        await gateway._websocket(parent)
        assert parent.closed == 1008
        assert gateway.endpoint is None and gateway.used is False
        assert gateway.error_code is None
    asyncio.run(scenario())


def test_normal_websocket_close_is_not_channel_failure(tmp_path):
    from websockets.datastructures import Headers
    from websockets.exceptions import ConnectionClosedOK
    from websockets.frames import Close
    class Parent:
        request = SimpleNamespace(headers=Headers([("x-goog-api-key", "key")]))
        async def recv(self):
            raise ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)
    async def scenario():
        gateway = AcpNetworkGateway(tmp_path/"network")
        gateway.api_key = "key"
        await gateway._websocket(Parent())
        assert gateway.error_code is None
    asyncio.run(scenario())


def test_failed_gateway_start_never_unlinks_preexisting_path(tmp_path):
    async def scenario():
        root = tmp_path/"network"
        root.mkdir(mode=0o700)
        existing = root/"native-ws.sock"
        existing.write_text("EXISTING_SENTINEL")
        gateway = AcpNetworkGateway(root)
        with pytest.raises(NetworkBoundaryError, match="socket_exists"):
            await gateway.start()
        await gateway.close()
        assert existing.read_text() == "EXISTING_SENTINEL"
    asyncio.run(scenario())


def test_gateway_shutdown_closes_idle_inference_before_waiting_for_server(tmp_path):
    async def scenario():
        connected = asyncio.Event()
        closed = asyncio.Event()
        async def inference(reader, writer):
            connected.set()
            try:
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()
        async with await asyncio.start_server(inference, "127.0.0.1", 0) as server:
            gateway = AcpNetworkGateway(tmp_path/"network")
            await gateway.start()
            gateway.endpoint = ("127.0.0.1", server.sockets[0].getsockname()[1])
            reader, writer = await asyncio.open_unix_connection(str(gateway.root/"inference.sock"))
            try:
                writer.write(b"I")
                await writer.drain()
                await asyncio.wait_for(connected.wait(), 1)
                # Keep this client open: wait_closed-before-handler-cancel deadlocks.
                await asyncio.wait_for(gateway.close(), 2)
                assert not gateway.connections
                assert gateway.error_code is None
                await asyncio.wait_for(closed.wait(), 1)
                assert await reader.read() == b""
            finally:
                writer.close()
                await writer.wait_closed()
                await gateway.close()
    asyncio.run(asyncio.wait_for(scenario(), 4))


@pytest.mark.parametrize("stage", ["opening", "opened_before_relay"])
def test_helper_cancel_during_connection_setup_closes_both_streams(tmp_path, monkeypatch, stage):
    import chatgpt_web_oauth_mcp.acp_network as module
    async def scenario():
        reached = asyncio.Event()
        remote_closed = asyncio.Event()
        handlers = set()
        original_open = asyncio.open_unix_connection
        async def remote(reader, writer):
            try:
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                remote_closed.set()
        async def open_remote(path):
            assert path == "/control/inference.sock"
            if stage == "opening":
                reached.set()
                await asyncio.Future()
            return await original_open(str(tmp_path/"remote.sock"))
        async def before_relay(*args):
            reached.set()
            await asyncio.Future()
        monkeypatch.setattr(module.asyncio, "open_unix_connection", open_remote)
        monkeypatch.setattr(module, "relay", before_relay)
        async with await asyncio.start_unix_server(remote, str(tmp_path/"remote.sock")):
            async def accepted(reader, writer):
                await module.helper_inference(reader, writer, handlers)
            server = await asyncio.start_server(accepted, "127.0.0.1", 0)
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            try:
                await asyncio.wait_for(reached.wait(), 1)
                task = next(iter(handlers))
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                assert not handlers
                # Keeping the original client open must not stall server teardown.
                server.close()
                await asyncio.wait_for(server.wait_closed(), 1)
                assert await asyncio.wait_for(reader.read(), 1) == b""
                if stage == "opened_before_relay":
                    await asyncio.wait_for(remote_closed.wait(), 1)
            finally:
                writer.close()
                await writer.wait_closed()
                server.close()
                await server.wait_closed()
    asyncio.run(asyncio.wait_for(scenario(), 4))


def test_gateway_rewrites_native_websocket_and_relays_only_admitted_inference(tmp_path):
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    async def scenario():
        gateway = AcpNetworkGateway(tmp_path / "network")
        await gateway.start()
        bridges = []
        native_messages = []
        received_inference = []
        async def inference(reader, writer):
            received_inference.append(await reader.readexactly(4))
            writer.write(b"PONG")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        async def native(ws):
            native_messages.append(json.loads(await ws.recv()))
            await ws.send("NATIVE_READY")
            assert await ws.recv() == "turn"
            await ws.send("end_turn")
        try:
            async with await asyncio.start_server(inference, "127.0.0.1", 0) as inf_server:
                inf_port = inf_server.sockets[0].getsockname()[1]
                async with serve(native, "127.0.0.1", 0, ping_interval=None) as native_server:
                    native_port = native_server.sockets[0].getsockname()[1]
                    host_r, host_w = await asyncio.open_unix_connection(str(gateway.root / "native-ws.sock"))
                    native_r, native_w = await asyncio.open_connection("127.0.0.1", native_port)
                    bridges.append(asyncio.create_task(relay(host_r, host_w, native_r, native_w)))
                    startup = json.dumps({"version": NETWORK_VERSION, "native_output": encode_native_output(native_port, "secret-native-key").hex(),
                                          "relay_port": 34567, "gateway_port": 34568}).encode()
                    port, key = decode_native_output(await gateway.ready(startup))
                    async with connect(f"ws://127.0.0.1:{port}", additional_headers={"x-goog-api-key": key}, proxy=None) as parent:
                        await parent.send(json.dumps(initialization(inf_port)))
                        assert await parent.recv() == "NATIVE_READY"
                        assert gateway.endpoint == ("127.0.0.1", inf_port)
                        endpoint = native_messages[0]["config"]["models"][0]["geminiApiEndpoint"]
                        assert endpoint["baseUrl"] == "http://127.0.0.1:34567"
                        reader, writer = await asyncio.open_unix_connection(str(gateway.root / "inference.sock"))
                        writer.write(b"IPING")
                        await writer.drain()
                        assert await asyncio.wait_for(reader.readexactly(4), 1) == b"PONG"
                        writer.close()
                        await writer.wait_closed()
                        await parent.send("turn")
                        assert await parent.recv() == "end_turn"
                    assert received_inference == [b"PING"]
                    assert gateway.error_code is None
        finally:
            await gateway.close()
            assert not gateway.websocket_tasks
            for task in bridges:
                task.cancel()
            await asyncio.gather(*bridges, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="Linux bwrap required")
def test_helper_network_namespace_cannot_reach_host_tcp_udp_or_internet(tmp_path):
    tcp = socket.socket()
    tcp.bind(("127.0.0.1", 0))
    tcp.listen(1)
    tcp.settimeout(.2)
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(.2)
    profile = tmp_path/"profile"; profile.mkdir(mode=0o700)
    workspace = tmp_path/"workspace"; workspace.mkdir()
    unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix.bind(str(workspace/"host.sock"))
    unix.listen(1)
    unix.settimeout(.2)
    control = tmp_path/"control"; control.mkdir(mode=0o700)
    helper = tmp_path/"helper"
    helper.write_text("#!/usr/bin/python3\n" + f"""
import socket,errno,os,ctypes
assert os.readlink('/proc/self/ns/net') != {os.readlink('/proc/self/ns/net')!r}
# The host socket is visible through RO workspace, but cannot be connected to.
assert os.path.exists({str(workspace/"host.sock")!r})
for factory in (lambda:socket.socket(socket.AF_UNIX,socket.SOCK_STREAM),
                lambda:socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM),
                lambda:socket.socketpair(socket.AF_UNIX,socket.SOCK_STREAM),
                lambda:socket.socketpair(socket.AF_UNIX,socket.SOCK_DGRAM)):
    try:
        factory()
        raise AssertionError('native AF_UNIX socket creation permitted')
    except OSError as e:
        assert e.errno == errno.EPERM
for name in os.listdir('/proc/self/fd'):
    try:
        target=os.readlink('/proc/self/fd/'+name)
        assert not target.startswith('socket:'), 'native inherited a socket'
    except FileNotFoundError:pass
sec=ctypes.CDLL('libseccomp.so.2');sec.seccomp_syscall_resolve_name.argtypes=[ctypes.c_char_p]
c=ctypes.CDLL(None,use_errno=True)
for name in (b'io_uring_setup',b'pidfd_getfd'):
    number=sec.seccomp_syscall_resolve_name(name)
    assert c.syscall(number,0,0,0,0)==-1 and ctypes.get_errno()==errno.EPERM
s=socket.socket();s.settimeout(.3)
try:
    s.connect(('127.0.0.1',{tcp.getsockname()[1]}))
    raise AssertionError('host TCP reachable')
except OSError: pass
s.close()
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.sendto(b'HOST_CANARY',('127.0.0.1',{udp.getsockname()[1]}))
s.close()
for family,addr in ((socket.AF_INET,('203.0.113.1',443)),(socket.AF_INET6,('2001:db8::1',443))):
    for kind in (socket.SOCK_STREAM,socket.SOCK_DGRAM):
        s=socket.socket(family,kind);s.settimeout(.3)
        try:
            s.connect(addr)
            raise AssertionError('external route exists')
        except OSError as e:
            assert e.errno in (errno.ENETUNREACH,errno.EHOSTUNREACH,errno.EAFNOSUPPORT)
        finally:s.close()
# The legitimate namespace-local loopback remains usable.
l=socket.socket();l.bind(('127.0.0.1',0));l.listen(1)
s=socket.socket();s.connect(l.getsockname())
a,_=l.accept();s.sendall(b'LOCAL');assert a.recv(5)==b'LOCAL'
s.close();a.close();l.close()
print('NAMESPACE_NETWORK_PASS')
""")
    helper.chmod(0o700)
    box = AcpSandbox(Path("/bin/true"), helper, profile, workspace, control)
    async def scenario():
        gateway = AcpNetworkGateway(control/"network")
        await gateway.start()
        fd = helper_filter_fd()
        try:
            argv = box.helper_command()
            argv = [argv[0], "--seccomp", str(fd), *argv[1:argv.index("--")],
                    "--", "/usr/bin/python3", "-I", "-B", "/opt/acp/acp_network.py", "--native"]
            child = await asyncio.create_subprocess_exec(*argv, pass_fds=(fd,),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(child.communicate(), 8)
            assert child.returncode == 0, err.decode()
            assert b"NAMESPACE_NETWORK_PASS" in out
        finally:
            os.close(fd)
            await gateway.close()
    try:
        asyncio.run(scenario())
        with pytest.raises(socket.timeout):
            tcp.accept()
        with pytest.raises(socket.timeout):
            udp.recvfrom(1024)
        with pytest.raises(socket.timeout):
            unix.accept()
    finally:
        tcp.close()
        udp.close()
        unix.close()
