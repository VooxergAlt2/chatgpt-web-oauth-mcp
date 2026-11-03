"""Private-network native helper and its two fixed, task-owned channels.

The helper has no host IP interface. Its WebSocket and inference traffic cross
only pre-created Unix sockets mounted read-only by the trusted launcher. The
host never follows helper-created socket paths or accepts a connect address from
the helper. The sole inference endpoint comes from the authenticated runtime's
first native InitializeConversationEvent and is frozen for this process.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import re
import secrets
import socket
import stat
import struct
import sys
from pathlib import Path
from urllib.parse import urlsplit

HANDSHAKE_LIMIT = 4096
MESSAGE_LIMIT = 2 * 1024 * 1024
STREAM_LIMIT = 32 * 1024 * 1024
NETWORK_VERSION = 2
NATIVE_SKILLS_DIRECTORY = "/home/agent/.local/share/opss-native/skills"
_GATEWAY_SERVER = "opss_readonly"
_GATEWAY_TOOLS = ("read_text", "search", "code_map_symbols", "code_map_references",
                  "code_map_imports", "git_status", "git_diff")
_NATIVE_TOOL_CONFIGURATION_FIELDS = (
    "subagents", "runWorkflow", "find", "userQuestions", "runCommand",
    "manageTask", "schedule", "fileEdit", "viewFile", "writeToFile",
    "grepSearch", "listDir", "generateImage", "searchWeb", "readUrlContent",
)


class NetworkBoundaryError(ValueError):
    """A fixed diagnostic code; never include payloads, URLs or credentials."""


def _varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        if position >= len(data):
            break
        byte = data[position]
        position += 1
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, position
    raise NetworkBoundaryError("acp_native_handshake_invalid")


def _encode_varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def decode_native_output(data: bytes) -> tuple[int, str]:
    """Pinned native OutputConfig: int32 port=1, string api_key=2.

    Reject unknown/duplicate fields instead of forwarding a changed protocol.
    This two-field protobuf is intentionally decoded without importing the
    provider's Python 3.14 package into the system Python bootstrap.
    """
    if not 1 <= len(data) <= HANDSHAKE_LIMIT:
        raise NetworkBoundaryError("acp_native_handshake_invalid")
    values = {}
    position = 0
    while position < len(data):
        tag, position = _varint(data, position)
        if tag == 8 and "port" not in values:
            values["port"], position = _varint(data, position)
        elif tag == 18 and "api_key" not in values:
            size, position = _varint(data, position)
            if not 1 <= size <= 512 or position + size > len(data):
                raise NetworkBoundaryError("acp_native_handshake_invalid")
            try:
                values["api_key"] = data[position:position + size].decode("ascii")
            except UnicodeError:
                raise NetworkBoundaryError("acp_native_handshake_invalid") from None
            position += size
        else:
            raise NetworkBoundaryError("acp_native_handshake_invalid")
    if set(values) != {"port", "api_key"} or not 1 <= values["port"] <= 65535:
        raise NetworkBoundaryError("acp_native_handshake_invalid")
    if any(ord(c) < 33 or ord(c) > 126 for c in values["api_key"]):
        raise NetworkBoundaryError("acp_native_handshake_invalid")
    return values["port"], values["api_key"]


def encode_native_output(port: int, api_key: str) -> bytes:
    key = api_key.encode("ascii")
    data = b"\x08" + _encode_varint(port) + b"\x12" + _encode_varint(len(key)) + key
    decode_native_output(data)
    return data


async def read_frame(reader) -> bytes:
    size = struct.unpack("<I", await reader.readexactly(4))[0]
    if not 1 <= size <= HANDSHAKE_LIMIT:
        raise NetworkBoundaryError("acp_native_handshake_invalid")
    return await reader.readexactly(size)


def frame(data: bytes) -> bytes:
    if not 1 <= len(data) <= HANDSHAKE_LIMIT:
        raise NetworkBoundaryError("acp_native_handshake_invalid")
    return struct.pack("<I", len(data)) + data


async def pump(reader, writer, *, limit: int = STREAM_LIMIT):
    total = 0
    while chunk := await reader.read(65536):
        total += len(chunk)
        if total > limit:
            raise NetworkBoundaryError("acp_network_byte_limit")
        writer.write(chunk)
        await writer.drain()
    with contextlib.suppress(OSError, AttributeError, NotImplementedError):
        writer.write_eof()


async def relay(left_reader, left_writer, right_reader, right_writer):
    """Preserve half-close so a request EOF cannot truncate its response."""
    tasks = [asyncio.create_task(pump(left_reader, right_writer)),
             asyncio.create_task(pump(right_reader, left_writer))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for writer in (left_writer, right_writer):
            writer.close()
        await asyncio.gather(*(w.wait_closed() for w in (left_writer, right_writer)),
                             return_exceptions=True)


async def finish_cleanup(task):
    """Finish one owned cleanup even when its caller is cancelled again."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    result = task.result()
    if interrupted:
        raise asyncio.CancelledError
    return result


def _allow_task_gateway(config: dict) -> None:
    """Static native policy matches the actual MCP target, never display metadata.

    Pinned SDK policy.allow(server, mcp_tools=...) serializes these exact rules.
    Existing rule order/IDs are retained because dynamic callbacks refer to them.
    A specific allow would outrank a wildcard deny in the native engine, so an
    applicable explicit denial must fail closed instead of being weakened.
    Explicit server/tool ASK rules also remain operator gates; only the default
    global ASK fallback may be superseded for the seven scoped targets.
    """
    policy = config.get("policyConfig", {})
    if not isinstance(policy, dict):
        raise NetworkBoundaryError("acp_native_mcp_policy_invalid")
    rules = policy.get("rules", [])
    if not isinstance(rules, list) or len(rules) > 512:
        raise NetworkBoundaryError("acp_native_mcp_policy_invalid")
    for rule in rules:
        if not isinstance(rule, dict):
            raise NetworkBoundaryError("acp_native_mcp_policy_invalid")
        server, tool = rule.get("serverName", ""), rule.get("tool", "")
        if not isinstance(server, str) or not isinstance(tool, str):
            raise NetworkBoundaryError("acp_native_mcp_policy_invalid")
        applies = ((server == "" and tool == "*")
                   or (server == _GATEWAY_SERVER and tool in (*_GATEWAY_TOOLS, "*")))
        if applies and rule.get("decision") in ("POLICY_DECISION_DENY", 2):
            raise NetworkBoundaryError("acp_native_mcp_policy_conflict")
        if (applies and server == _GATEWAY_SERVER
                and rule.get("decision") in ("POLICY_DECISION_ASK_USER", 3)):
            raise NetworkBoundaryError("acp_native_mcp_policy_conflict")
    policy["rules"] = [*rules, *(
        {"tool": tool, "serverName": _GATEWAY_SERVER,
         "decision": "POLICY_DECISION_ALLOW", "name": "opss_task_" + tool}
        for tool in _GATEWAY_TOOLS
    )]
    config["policyConfig"] = policy


def prepare_initialization(message: str, relay_port: int, *, gateway_config=None,
                           gateway_port: int | None = None,
                           allow_gateway_tools: bool = False) -> tuple[str, tuple[str, int]]:
    """Admit one OAuth-personal inference proxy from the parent, never the model."""
    if not isinstance(message, str) or len(message.encode()) > MESSAGE_LIMIT:
        raise NetworkBoundaryError("acp_native_initialization_invalid")
    try:
        event = json.loads(message)
    except (ValueError, TypeError):
        raise NetworkBoundaryError("acp_native_initialization_invalid") from None
    if not isinstance(event, dict) or set(event) != {"config"} or not isinstance(event["config"], dict):
        raise NetworkBoundaryError("acp_native_initialization_invalid")
    config = event["config"]
    # Pinned SkillsConfig.materialized_skills_dir controls generated bundled
    # skills. Its unset native default is relative to cwd, which is the writer
    # mirror. Keep runtime artifacts in the namespace's private home instead;
    # never exempt generated files from the workspace promotion scope guard.
    skills = config.get("skillsConfig", {})
    if not isinstance(skills, dict):
        raise NetworkBoundaryError("acp_native_skills_config_invalid")
    skills["materializedSkillsDir"] = NATIVE_SKILLS_DIRECTORY
    config["skillsConfig"] = skills
    if type(allow_gateway_tools) is not bool or (allow_gateway_tools and gateway_config is None):
        raise NetworkBoundaryError("acp_native_mcp_policy_invalid")
    # Only the worker's exact task-owned HTTP MCP capability is admitted.
    # No address, command or header supplied by the model is accepted.
    if any(config.get(key) for key in ("enabledHooks", "customSubagents")):
        raise NetworkBoundaryError("acp_native_extensions_disabled")
    servers = config.get("mcpServers") or []
    if gateway_config is None:
        if servers:
            raise NetworkBoundaryError("acp_native_extensions_disabled")
    else:
        if (not isinstance(servers, list) or len(servers) != 1
                or not isinstance(servers[0], dict)
                or type(gateway_port) is not int or not 1 <= gateway_port <= 65535):
            raise NetworkBoundaryError("acp_native_mcp_gateway_invalid")
        server = servers[0]
        allowed = {"name", "http", "enabledTools", "disabledTools",
                   "timeoutSeconds", "forceAllToolsEager"}
        if (set(server) - allowed
                or server.get("name") != gateway_config["name"]
                or server.get("http") != gateway_config["http"]
                or server.get("enabledTools") or server.get("disabledTools")
                or type(server.get("forceAllToolsEager", False)) is not bool
                or type(server.get("timeoutSeconds", 0)) is not int
                or not 0 <= server.get("timeoutSeconds", 0) <= 60):
            raise NetworkBoundaryError("acp_native_mcp_gateway_invalid")
        if allow_gateway_tools and server["name"] != _GATEWAY_SERVER:
            raise NetworkBoundaryError("acp_native_mcp_policy_invalid")
        server["http"]["url"] = f"http://127.0.0.1:{gateway_port}/mcp"
    models = config.get("models")
    if not isinstance(models, list) or not 1 <= len(models) <= 16:
        raise NetworkBoundaryError("acp_native_inference_endpoint_invalid")
    endpoint = None
    proxy_token = None
    for model in models:
        if not isinstance(model, dict) or "vertexEndpoint" in model:
            raise NetworkBoundaryError("acp_native_inference_endpoint_invalid")
        api = model.get("geminiApiEndpoint")
        if not isinstance(api, dict):
            raise NetworkBoundaryError("acp_native_inference_endpoint_invalid")
        try:
            url = urlsplit(api.get("baseUrl", ""))
            port = url.port
        except (TypeError, ValueError):
            raise NetworkBoundaryError("acp_native_inference_endpoint_invalid") from None
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or not port
                or url.username or url.password or url.path not in ("", "/")
                or url.query or url.fragment):
            raise NetworkBoundaryError("acp_native_inference_endpoint_invalid")
        headers = api.get("httpHeaders")
        if not isinstance(headers, dict):
            raise NetworkBoundaryError("acp_native_inference_endpoint_invalid")
        tokens = [v for k, v in headers.items() if k.lower() == "x-acp-proxy-token"]
        if len(tokens) != 1 or not isinstance(tokens[0], str) or not re.fullmatch(r"[a-f0-9]{32}", tokens[0]):
            raise NetworkBoundaryError("acp_native_inference_endpoint_invalid")
        current = ("127.0.0.1", port)
        if endpoint is not None and (current != endpoint or tokens[0] != proxy_token):
            raise NetworkBoundaryError("acp_native_multiple_inference_endpoints")
        endpoint, proxy_token = current, tokens[0]
        api["baseUrl"] = f"http://127.0.0.1:{relay_port}"
    if allow_gateway_tools:
        _allow_task_gateway(config)
    rewritten = json.dumps(event, separators=(",", ":"))
    if len(rewritten.encode()) > MESSAGE_LIMIT:
        raise NetworkBoundaryError("acp_native_initialization_invalid")
    return rewritten, endpoint


class AcpNetworkGateway:
    """Host-side listener; no path, URL or destination is accepted from helper."""
    def __init__(self, root: Path, *, gateway=None, configuration_observer=None):
        self.root = root
        self.gateway = gateway
        self.configuration_observer = configuration_observer
        self.endpoint = None
        self.api_key = None
        self.relay_port = None
        self.gateway_port = None
        self.native_socket = None
        self.native_listener = None
        self.inference_server = None
        self.websocket_server = None
        self.connections = set()
        self.used = False
        self.error_code = None
        self.closing = False
        self.owned_paths = []
        self.websocket_tasks = set()
        self.close_task = None

    def _error(self, code):
        if not self.closing and self.error_code is None:
            self.error_code = code

    def _observe_configuration(self, message: str) -> None:
        """Report only fixed booleans from an initialization already forwarded.

        This is evidence of the helper configuration sent on the native channel,
        not a claim about the provider's complete tool inventory. Missing flags
        stay unknown; finish has no explicit HarnessSideTools flag in the pinned
        SDK. Observer failures cannot change transport or native finality.
        """
        if self.configuration_observer is None:
            return
        try:
            config = json.loads(message)["config"]
            fields = config.get("harnessSideTools")
            fields = fields if isinstance(fields, dict) else {}
            observed = {}
            for name in _NATIVE_TOOL_CONFIGURATION_FIELDS:
                node = fields.get(name)
                value = node.get("enabled") if isinstance(node, dict) else None
                observed[name] = value if type(value) is bool else None
            self.configuration_observer({
                "version": 1, "source": "forwarded_native_initialization",
                "harness_side_tools": observed,
                "unobservable_builtin_tools": ["finish"],
            })
        except Exception:  # noqa: BLE001
            # Never retain or report config text, URLs, credentials or arguments.
            # Cancellation and other BaseExceptions still propagate unchanged.
            pass

    async def _inference(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            if self.closing or self.endpoint is None or len(self.connections) > 16:
                return
            channel = await asyncio.wait_for(reader.readexactly(1), 10)
            if channel == b"M" and self.gateway is not None:
                await self.gateway.serve_http(reader, writer)
            elif channel == b"I":
                other_reader, other_writer = await asyncio.open_connection(*self.endpoint)
                await relay(reader, writer, other_reader, other_writer)
            else:
                raise NetworkBoundaryError("acp_network_channel_invalid")
        except (OSError, TimeoutError, asyncio.IncompleteReadError, NetworkBoundaryError):
            self._error("acp_inference_channel_failed")
        finally:
            self.connections.discard(task)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def start(self):
        self.root.mkdir(mode=0o700, exist_ok=True)
        metadata = self.root.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077):
            raise NetworkBoundaryError("acp_network_directory_unsafe")
        for name in ("native-ws.sock", "inference.sock"):
            if (self.root/name).exists() or (self.root/name).is_symlink():
                raise NetworkBoundaryError("acp_network_socket_exists")
        self.native_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.native_listener.setblocking(False)
        self.native_listener.bind(str(self.root/"native-ws.sock"))
        self.owned_paths.append(self.root/"native-ws.sock")
        self.native_listener.listen(1)
        (self.root/"native-ws.sock").chmod(0o600)
        self.inference_server = await asyncio.start_unix_server(
            self._inference, path=str(self.root/"inference.sock"), limit=MESSAGE_LIMIT)
        self.owned_paths.append(self.root/"inference.sock")
        (self.root/"inference.sock").chmod(0o600)

    async def ready(self, startup: bytes) -> bytes:
        try:
            metadata = json.loads(startup)
            if (not isinstance(metadata, dict) or set(metadata) != {"version", "native_output", "relay_port", "gateway_port"}
                    or metadata["version"] != NETWORK_VERSION
                    or not isinstance(metadata["native_output"], str)
                    or not isinstance(metadata["relay_port"], int)
                    or isinstance(metadata["relay_port"], bool)
                    or not 1 <= metadata["relay_port"] <= 65535
                    or type(metadata["gateway_port"]) is not int
                    or not 1 <= metadata["gateway_port"] <= 65535
                    or metadata["gateway_port"] == metadata["relay_port"]):
                raise ValueError
            _, self.api_key = decode_native_output(bytes.fromhex(metadata["native_output"]))
            self.relay_port = metadata["relay_port"]
            self.gateway_port = metadata["gateway_port"]
        except (ValueError, TypeError):
            raise NetworkBoundaryError("acp_network_handshake_invalid") from None
        self.native_socket, _ = await asyncio.wait_for(
            asyncio.get_running_loop().sock_accept(self.native_listener), 10)
        creds = self.native_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        if struct.unpack("3i", creds)[1] != os.getuid():
            raise NetworkBoundaryError("acp_network_peer_invalid")
        self.native_listener.close()
        self.native_listener = None
        # Optional dependency, imported only in an ACP worker.
        from websockets.asyncio.server import serve
        self.websocket_server = await serve(self._websocket, "127.0.0.1", 0,
            max_size=MESSAGE_LIMIT, max_queue=4, compression=None, open_timeout=10,
            close_timeout=2, ping_interval=None)
        port = self.websocket_server.sockets[0].getsockname()[1]
        return encode_native_output(port, self.api_key)

    async def _websocket(self, parent):
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
        handler_task = asyncio.current_task()
        self.websocket_tasks.add(handler_task)
        try:
            tokens = parent.request.headers.get_all("x-goog-api-key")
            if (self.closing or self.used or len(tokens) != 1
                    or not re.fullmatch(r"[!-~]{1,512}", tokens[0])
                    or not secrets.compare_digest(tokens[0], self.api_key)):
                await parent.close(code=1008)
                return
            self.used = True
            first = await asyncio.wait_for(parent.recv(), 10)
            rewritten, self.endpoint = prepare_initialization(
                first, self.relay_port, gateway_port=self.gateway_port,
                gateway_config=self.gateway.native_config() if self.gateway is not None else None,
                allow_gateway_tools=self.gateway is not None)
            native_socket, self.native_socket = self.native_socket, None
            async with connect("ws://localhost/", sock=native_socket, proxy=None,
                additional_headers={"x-goog-api-key": self.api_key},
                max_size=MESSAGE_LIMIT, max_queue=4, compression=None,
                open_timeout=10, close_timeout=2, ping_interval=None) as native:
                await native.send(rewritten)
                self._observe_configuration(rewritten)
                async def transfer(source, target):
                    total = 0
                    async for item in source:
                        total += len(item.encode() if isinstance(item, str) else item)
                        if total > STREAM_LIMIT:
                            raise NetworkBoundaryError("acp_network_byte_limit")
                        await target.send(item)
                tasks = [asyncio.create_task(transfer(parent, native)),
                         asyncio.create_task(transfer(native, parent))]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for transfer_task in done:
                        await transfer_task
                finally:
                    for transfer_task in tasks:
                        transfer_task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except NetworkBoundaryError as exc:
            self._error(str(exc))
            await parent.close(code=1008)
        except ConnectionClosedOK:
            pass
        except (OSError, TimeoutError, ConnectionClosedError):
            self._error("acp_native_channel_failed")
            await parent.close(code=1011)
        finally:
            self.websocket_tasks.discard(handler_task)

    async def close(self):
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close())
        await finish_cleanup(self.close_task)

    async def _close(self):
        self.closing = True
        if self.websocket_server is not None:
            self.websocket_server.close()
        if self.inference_server is not None:
            self.inference_server.close()
        for item in (self.native_socket, self.native_listener):
            if item is not None:
                item.close()
        # asyncio.Server.wait_closed waits for live transports on Python 3.12.
        # Close/cancel their owners first; otherwise shutdown deadlocks.
        tasks = list(self.connections | self.websocket_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.websocket_server is not None:
            await self.websocket_server.wait_closed()
        if self.inference_server is not None:
            await self.inference_server.wait_closed()
        for path in self.owned_paths:
            path.unlink(missing_ok=True)
        self.owned_paths.clear()


async def helper_inference(reader, writer, connection_tasks, *, channel=b"I"):
    """Own both accepted/remote streams even if opening or relay is cancelled."""
    task = asyncio.current_task()
    connection_tasks.add(task)
    remote_writer = None
    try:
        if len(connection_tasks) > 16:
            return
        remote_reader, remote_writer = await asyncio.open_unix_connection("/control/inference.sock")
        if channel not in (b"I", b"M"):
            raise NetworkBoundaryError("acp_network_channel_invalid")
        remote_writer.write(channel)
        await remote_writer.drain()
        await relay(reader, writer, remote_reader, remote_writer)
    except (OSError, NetworkBoundaryError):
        pass
    finally:
        connection_tasks.discard(task)
        streams = [writer] if remote_writer is None else [writer, remote_writer]
        for stream in streams:
            stream.close()
        await asyncio.gather(*(stream.wait_closed() for stream in streams),
                             return_exceptions=True)


async def isolated_helper():
    """Run only inside the helper's private filesystem and network namespace."""
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/python3", "-I", "-B", "/opt/acp/acp_network.py", "--native",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=MESSAGE_LIMIT, close_fds=True)
    stream = asyncio.StreamReader(limit=MESSAGE_LIMIT)
    protocol = asyncio.StreamReaderProtocol(stream)
    stdin_transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    input_task = asyncio.create_task(pump(stream, process.stdin))
    connection_tasks = set()
    inference_server = None
    gateway_server = None
    ws_task = None
    wait_task = None
    async def inference(reader, writer):
        await helper_inference(reader, writer, connection_tasks)
    async def gateway(reader, writer):
        await helper_inference(reader, writer, connection_tasks, channel=b"M")
    try:
        native_output = await asyncio.wait_for(read_frame(process.stdout), 10)
        native_port, _ = decode_native_output(native_output)
        inference_server = await asyncio.start_server(inference, "127.0.0.1", 0, limit=MESSAGE_LIMIT)
        local_port = inference_server.sockets[0].getsockname()[1]
        gateway_server = await asyncio.start_server(gateway, "127.0.0.1", 0, limit=65536)
        gateway_port = gateway_server.sockets[0].getsockname()[1]
        host_reader, host_writer = await asyncio.open_unix_connection("/control/native-ws.sock")
        # Native OutputConfig can precede its TCP listener becoming ready.
        # This retries only the local channel, never an inference request.
        for attempt in range(12):
            try:
                native_reader, native_writer = await asyncio.open_connection("127.0.0.1", native_port)
                break
            except ConnectionRefusedError:
                if attempt == 11:
                    raise
                await asyncio.sleep(min(.02 * (2 ** attempt), .25))
        ws_task = asyncio.create_task(relay(host_reader, host_writer, native_reader, native_writer))
        startup = json.dumps({"version": NETWORK_VERSION, "native_output": native_output.hex(),
                              "relay_port": local_port, "gateway_port": gateway_port}).encode()
        sys.stdout.buffer.write(frame(startup))
        sys.stdout.buffer.flush()
        wait_task = asyncio.create_task(process.wait())
        done, _ = await asyncio.wait([input_task, wait_task, ws_task], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task
    finally:
        stdin_transport.close()
        if inference_server is not None:
            inference_server.close()
        if gateway_server is not None:
            gateway_server.close()
        for task in [input_task, ws_task, wait_task, *connection_tasks]:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in [input_task, ws_task, wait_task, *connection_tasks] if t is not None),
                             return_exceptions=True)
        if inference_server is not None:
            await inference_server.wait_closed()
        if gateway_server is not None:
            await gateway_server.wait_closed()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()


def restricted_native_exec():
    """Install the stricter inherited native policy, with no borrowed Unix FDs."""
    # The bootstrap owns its loop's Unix self-pipe; it must not reach native
    # tools. close_fds already removes it; explicitly close all >=3 as a second
    # invariant before loading the immutable filter and execing the helper.
    os.closerange(3, os.sysconf("SC_OPEN_MAX"))
    data = Path("/opt/acp/native-policy.bpf").read_bytes()
    class SockFilter(ctypes.Structure):
        _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                    ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]
    class SockProgram(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ushort), ("filters", ctypes.POINTER(SockFilter))]
    if not data or len(data) > 65536 or len(data) % ctypes.sizeof(SockFilter):
        raise NetworkBoundaryError("acp_native_seccomp_invalid")
    filters = (SockFilter * (len(data)//ctypes.sizeof(SockFilter))).from_buffer_copy(data)
    program = SockProgram(len(filters), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(38, 1, 0, 0, 0) or libc.prctl(22, 2, ctypes.addressof(program), 0, 0):
        raise NetworkBoundaryError("acp_native_seccomp_failed")
    os.execv("/opt/acp/localharness_external", ["/opt/acp/localharness_external"])


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--helper"]:
            asyncio.run(isolated_helper())
        elif sys.argv[1:] == ["--native"]:
            restricted_native_exec()
        else:
            raise SystemExit(2)
    except Exception:
        # Neither native handshake nor model configuration belongs in stderr.
        raise SystemExit(1) from None
