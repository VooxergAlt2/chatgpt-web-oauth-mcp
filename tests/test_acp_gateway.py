import asyncio
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.acp_gateway import (
    AcpReadonlyGateway, GatewayError, REQUEST_LIMIT, RESPONSE_LIMIT, TOKEN_HEADER,
)
from chatgpt_web_oauth_mcp.acp_network import (
    AcpNetworkGateway, NetworkBoundaryError, helper_inference, prepare_initialization,
)


def gateway(tmp_path, **kwargs):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src" / "sample.py").write_text(
        "import pathlib\n\nclass Example:\n    def method(self):\n        return 'fixture value'\n"
    )
    return AcpReadonlyGateway(
        workspace=workspace, owner="a" * 64, task_id="test-task",
        files_in_scope=kwargs.pop("files_in_scope", ["src"]), **kwargs,
    )


async def call(target, name, arguments):
    await target.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-03-26"}})
    response = await target.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": name, "arguments": arguments}})
    result = response["result"]
    return result["isError"], json.loads(result["content"][0]["text"])


def test_read_search_and_code_map_produce_scoped_anchors(tmp_path):
    target = gateway(tmp_path)
    async def scenario():
        failed, result = await call(target, "read_text", {"path": "src/sample.py", "start_line": 3, "line_limit": 2})
        assert not failed and result["content"].startswith("3: class Example:")
        assert result["next_line"] == 5
        failed, result = await call(target, "search", {"query": "fixture", "path": "src"})
        assert not failed and result["results"][0]["line"] == 5
        failed, result = await call(target, "code_map_symbols", {})
        assert not failed
        assert {row["name"] for row in result["results"]} == {"Example", "method"}
        failed, result = await call(target, "code_map_references", {"symbol": "Example"})
        assert not failed and result["results"][0]["file"] == "src/sample.py"
        failed, result = await call(target, "code_map_imports", {})
        assert not failed and result["results"][0]["imports"] == ["pathlib"]
    asyncio.run(scenario())


def test_scope_alias_exclusions_and_nonregular_files(tmp_path):
    target = gateway(tmp_path, workspace_alias=Path("/logical/repo"), out_of_scope=["src/private"])
    (target.workspace / "outside.py").write_text("outside marker")
    (target.workspace / "src" / "private").mkdir()
    (target.workspace / "src" / "private" / "secret.py").write_text("excluded marker")
    (target.workspace / "src" / ".env").write_text("credential fixture")
    (target.workspace / "src" / "link.py").symlink_to(tmp_path / "other.py")
    (tmp_path / "other.py").write_text("outside marker")
    os.mkfifo(target.workspace / "src" / "pipe")
    async def scenario():
        failed, result = await call(target, "read_text", {"path": "/logical/repo/src/sample.py"})
        assert not failed
        for path in ("../other.py", "/etc/passwd", "outside.py", "src/private/secret.py",
                     "src/.env", "src/link.py", "src/pipe"):
            failed, result = await call(target, "read_text", {"path": path})
            assert failed and not any(marker in json.dumps(result) for marker in ("outside marker", "credential fixture", "excluded marker"))
        failed, result = await call(target, "search", {"query": "marker"})
        assert not failed and result["results"] == []
    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["delegate_task", "job_start", "run_command", "git_commit", "deploy", "write_file", "read_credentials"])
def test_only_fixed_read_only_tools_are_callable(tmp_path, name):
    target = gateway(tmp_path)
    failed, result = asyncio.run(call(target, name, {}))
    assert failed and result["error"]["code"] == "acp_gateway_tool_not_allowed"


def test_typed_arguments_budget_and_revocation(tmp_path):
    target = gateway(tmp_path, max_calls=2)
    async def scenario():
        failed, _ = await call(target, "read_text", {"path": "src/sample.py", "owner": "foreign"})
        assert failed
        failed, _ = await call(target, "read_text", {"path": "src/sample.py", "line_limit": True})
        assert failed
        failed, result = await call(target, "read_text", {"path": "src/sample.py"})
        assert failed and result["error"]["code"] == "acp_gateway_call_limit"
        target.revoke()
        response = await target.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert response["error"]["message"] == "acp_gateway_revoked"
        assert "token" not in json.dumps(target.summary()).lower()
    asyncio.run(scenario())


def test_output_is_bounded_and_diagnostics_never_echo_file_contents(tmp_path):
    target = gateway(tmp_path)
    (target.workspace / "src" / "large.txt").write_text("PRIVATE_FIXTURE" * 10000)
    failed, result = asyncio.run(call(target, "read_text", {"path": "src/large.txt"}))
    assert failed and result["error"]["code"] == "acp_gateway_output_limit"
    assert "PRIVATE_FIXTURE" not in json.dumps(result)
    (target.workspace / "src" / "larger.txt").write_bytes(b"x" * 524289)
    failed, result = asyncio.run(call(target, "read_text", {"path": "src/larger.txt"}))
    assert failed and result["error"]["code"] == "acp_gateway_file_too_large"


def test_writer_baseline_evidence_is_explicit_and_uses_logical_paths(tmp_path):
    baseline = tmp_path / "baseline"
    (baseline / "src").mkdir(parents=True)
    (baseline / "src" / "sample.py").write_text("old\n")
    (baseline / "src" / "deleted.py").write_text("deleted\n")
    target = gateway(tmp_path, baseline_root=baseline, workspace_alias=Path("/logical/repo"))
    async def scenario():
        failed, result = await call(target, "git_status", {})
        assert not failed and result["source"] == "workspace_baseline"
        assert {"path": "src/deleted.py", "status": " D"} in result["changes"]
        failed, result = await call(target, "git_diff", {})
        assert not failed and result["source"] == "workspace_baseline"
        assert "-old" in result["diff"] and "+import pathlib" in result["diff"]
        assert str(tmp_path) not in result["diff"]
        failed, result = await call(target, "git_diff", {"path": "src/deleted.py"})
        assert not failed and "-deleted" in result["diff"]
    asyncio.run(scenario())


def test_git_evidence_is_scoped_and_disables_external_programs(tmp_path):
    target = gateway(tmp_path, out_of_scope=["src/excluded.py"])
    def git(*args):
        return subprocess.run(["git", *args], cwd=target.workspace, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    git("init", "-q")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "add", "src/sample.py")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "fixture")
    forbidden = tmp_path / "external-ran"
    script = tmp_path / "external"
    script.write_text("#!/bin/sh\ntouch " + str(forbidden) + "\n")
    script.chmod(0o700)
    git("config", "core.fsmonitor", str(script))
    git("config", "diff.external", str(script))
    git("config", "filter.fixture.clean", str(script))
    (target.workspace / ".gitattributes").write_text("*.py filter=fixture\n")
    (target.workspace / "src" / "sample.py").write_text("changed\n")
    (target.workspace / "src" / "excluded.py").write_text("EXCLUDED_FIXTURE")
    (target.workspace / "src" / ".env").write_text("CREDENTIAL_FIXTURE")
    async def scenario():
        failed, result = await call(target, "git_status", {})
        assert not failed and result["source"] == "git"
        assert result["changes"] == [{"path": "src/sample.py", "status": " M"}]
        failed, result = await call(target, "git_diff", {})
        assert not failed and "+changed" in result["diff"]
        assert "EXCLUDED_FIXTURE" not in json.dumps(result)
        assert "CREDENTIAL_FIXTURE" not in json.dumps(result)
        assert not forbidden.exists()
    asyncio.run(scenario())


def initial():
    return {"config": {"models": [{"geminiApiEndpoint": {
        "baseUrl": "http://127.0.0.1:41234", "httpHeaders": {"X-ACP-Proxy-Token": "a" * 32}}}]}}


def test_network_allows_only_exact_worker_mcp_transport(tmp_path):
    target = gateway(tmp_path)
    event = initial()
    event["config"]["mcpServers"] = [target.native_config()]
    rewritten, endpoint = prepare_initialization(json.dumps(event), 32000,
        gateway_config=target.native_config(), gateway_port=32001)
    assert endpoint == ("127.0.0.1", 41234)
    assert json.loads(rewritten)["config"]["mcpServers"][0]["http"]["url"] == "http://127.0.0.1:32001/mcp"
    for change in ("url", "token", "name", "stdio", "second"):
        modified = copy.deepcopy(event)
        server = modified["config"]["mcpServers"][0]
        if change == "url":
            server["http"]["url"] = "http://127.0.0.1:4444/admin"
        elif change == "token":
            server["http"]["headers"][TOKEN_HEADER] = "b" * 64
        elif change == "name":
            server["name"] = "foreign"
        elif change == "stdio":
            server["stdio"] = {"command": "/bin/false"}
        else:
            modified["config"]["mcpServers"].append(copy.deepcopy(server))
        with pytest.raises(NetworkBoundaryError, match="mcp_gateway_invalid"):
            prepare_initialization(json.dumps(modified), 32000,
                gateway_config=target.native_config(), gateway_port=32001)
    with pytest.raises(NetworkBoundaryError, match="extensions_disabled"):
        prepare_initialization(json.dumps(event), 32000)


async def http_request(port, token, body, *, extra_headers=b"", declared_length=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    data = json.dumps(body).encode()
    length = len(data) if declared_length is None else declared_length
    writer.write((f"POST /mcp HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {length}\r\n{TOKEN_HEADER}: {token}\r\n").encode()
                 + extra_headers + b"\r\n" + data)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 3)
    writer.close()
    await writer.wait_closed()
    return response


def test_http_denies_foreign_capabilities_duplicate_headers_and_large_inputs(tmp_path):
    target = gateway(tmp_path)
    foreign = AcpReadonlyGateway(
        workspace=target.workspace, owner="b" * 64, task_id="foreign", files_in_scope=["src"])
    async def scenario():
        async with await asyncio.start_server(target.serve_http, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            response = await http_request(port, foreign.mcp_server()["headers"][0]["value"], request)
            assert response.startswith(b"HTTP/1.1 401")
            token = target.mcp_server()["headers"][0]["value"]
            response = await http_request(port, token, request,
                                          extra_headers=(f"{TOKEN_HEADER}: {token}\r\n").encode())
            assert response.startswith(b"HTTP/1.1 400")
            response = await http_request(port, token, request, declared_length=REQUEST_LIMIT + 1)
            assert response.startswith(b"HTTP/1.1 413")
            assert target.calls == 0
    asyncio.run(scenario())


def test_real_mcp_client_round_trip_uses_only_fixed_private_multiplex(tmp_path, monkeypatch):
    """Protocol fixture: real MCP SDK -> local helper proxy -> UDS -> scoped gateway."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import httpx2
    import chatgpt_web_oauth_mcp.acp_network as module
    target = gateway(tmp_path)
    async def scenario():
        network = AcpNetworkGateway(tmp_path / "network", gateway=target)
        await network.start()
        # MCP has no reason to connect to this fixed inference endpoint.
        network.endpoint = ("127.0.0.1", 1)
        original_open = asyncio.open_unix_connection
        async def open_channel(path):
            assert path == "/control/inference.sock"
            return await original_open(str(network.root / "inference.sock"))
        monkeypatch.setattr(module.asyncio, "open_unix_connection", open_channel)
        tasks = set()
        async def proxy(reader, writer):
            await helper_inference(reader, writer, tasks, channel=b"M")
        try:
            async with await asyncio.start_server(proxy, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                token = target.mcp_server()["headers"][0]["value"]
                async with httpx2.AsyncClient(headers={TOKEN_HEADER: token}, timeout=5) as client:
                    async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=client) as streams:
                        async with ClientSession(*streams) as session:
                            initialized = await session.initialize()
                            assert initialized.server_info.name == "opss_readonly"
                            available = await session.list_tools()
                            assert len(available.tools) == 7
                            response = await session.call_tool("read_text", {"path": "src/sample.py"})
                            assert response.is_error is False
                            assert "class Example" in response.content[0].text
                            response = await session.call_tool("code_map_symbols", {"path": "src/sample.py"})
                            assert response.is_error is False
                            assert "Example" in response.content[0].text
                            response = await session.call_tool("run_command", {"command": "ignored"})
                            assert response.is_error is True
                assert network.error_code is None
                assert target.calls == 3
                assert target.summary()["tool_calls"]["read_text"] == 1
                assert target.summary()["tool_calls"]["code_map_symbols"] == 1
                assert sum(target.summary()["tool_calls"].values()) == 2
        finally:
            await network.close()
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_http_cumulative_budget_revokes_only_its_task_capability(tmp_path, monkeypatch):
    import chatgpt_web_oauth_mcp.acp_gateway as module
    target = gateway(tmp_path)
    monkeypatch.setattr(module, "TOTAL_RESPONSE_LIMIT", 10)
    async def scenario():
        async with await asyncio.start_server(target.serve_http, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            token = target.mcp_server()["headers"][0]["value"]
            request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            response = await http_request(port, token, request)
            assert response.startswith(b"HTTP/1.1 413")
            assert target.revoked and target.response_bytes == 0
            response = await http_request(port, token, request)
            assert response.startswith(b"HTTP/1.1 401")
    asyncio.run(scenario())
