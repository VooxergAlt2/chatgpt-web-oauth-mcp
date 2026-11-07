"""Real HTTP/process acceptance with a fake Windows backend; not native QA."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest
import httpx2
from fastmcp import Client, FastMCP

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))


def _serve(role, root, port, gateway_port):
    from chatgpt_web_oauth_mcp.revit_workspace_backend import NodeWorkspaceBackend
    from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager
    from chatgpt_web_oauth_mcp.tool_groups import register_tool_groups
    from windows_revit_gateway import GatewayConfig, build_gateway
    if role == "gateway":
        backend = FastMCP("fake Windows HTTP acceptance")
        @backend.tool(name="Click")
        async def click(x: int):
            with (root / "executions.txt").open("a") as stream:
                stream.write(str(x) + "\n")
            (root / "started").write_text("started")
            async with asyncio.timeout(20):
                while not (root / "release").is_file():
                    await asyncio.sleep(0.05)
            (root / "finished").write_text("finished")
            return {"value": "private-http-result", "x": x}
        server = build_gateway(
            GatewayConfig(windows_url="http://127.0.0.1:8000/mcp",
                          node_executable="unused", revit_server_js=Path("unused"),
                          operation_state_dir=root / "journal"),
            windows_target=backend, revit_target=FastMCP("no Revit"), validate_files=False)
    else:
        url = f"http://127.0.0.1:{gateway_port}/mcp"
        manager = WorkspaceManager(root / "ledger.json", NodeWorkspaceBackend(lambda: url),
                                   owner_provider=lambda: "openai:http-acceptance")
        server = FastMCP("isolated Opss HTTP acceptance")
        register_tool_groups(server, transport_factory=lambda: url, workspace_manager=manager)
    server.run(transport="streamable-http", host="127.0.0.1", port=port,
               stateless_http=True, show_banner=False, allowed_hosts=["127.0.0.1"])


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start(role, root, port, gateway_port, logs):
    log = (root / f"{role}-{len(logs)}.log").open("wb")
    logs.append(log)
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TIKTOKEN_CACHE_DIR")
           if key in os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), str(REPO / "scripts")])
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--serve", role, str(root),
         str(port), str(gateway_port)], stdout=log, stderr=subprocess.STDOUT, env=env)


def _stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


async def _ready(proc, url):
    deadline = time.monotonic() + 20
    last_error = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError("Owned test server exited before readiness")
        try:
            async with asyncio.timeout(2), Client(url, timeout=1, init_timeout=1) as client:
                await client.list_tools()
                return
        except Exception as exc:
            last_error = type(exc).__name__
            await asyncio.sleep(0.1)
    raise AssertionError(f"Owned test server not ready: {last_error}")


async def _marker(path):
    async with asyncio.timeout(15):
        while not path.is_file():
            await asyncio.sleep(0.05)


async def _call(client, group, name, arguments=None):
    return await client.call_tool("tool_group_call",
                                 {"group": group, "name": name, "arguments": arguments or {}},
                                 raise_on_error=False)


def test_http_controller_death_recovers_result_without_replay(tmp_path):
    async def check():
        gateway_port, public_port = _free_port(), _free_port()
        while public_port == gateway_port:
            public_port = _free_port()
        gateway = controller = None
        logs = []
        url = f"http://127.0.0.1:{public_port}/mcp"
        try:
            gateway = _start("gateway", tmp_path, gateway_port, gateway_port, logs)
            await _ready(gateway, f"http://127.0.0.1:{gateway_port}/mcp")
            controller = _start("opss", tmp_path, public_port, gateway_port, logs)
            await _ready(controller, url)
            controller_killed = False
            try:
                async with Client(url, timeout=15, init_timeout=3) as client:
                    pending = asyncio.create_task(_call(client, "windows", "windows_Click", {"x": 42}))
                    await _marker(tmp_path / "started")
                    ledger = json.loads((tmp_path / "ledger.json").read_text())
                    fence_id = ledger["ui"]["fence_id"]
                    assert ledger["ui"]["durable"] is True
                    assert not (tmp_path / "finished").exists()
                    controller.kill()  # only this test's controller; gateway stays alive
                    controller.wait(timeout=5)
                    controller_killed = True
                    with pytest.raises(Exception):
                        await asyncio.wait_for(pending, 10)
            except httpx2.RemoteProtocolError:
                # FastMCP also reports the intentional disconnect when closing
                # its session. Never suppress errors before our proven kill.
                assert controller_killed
            (tmp_path / "release").write_text("controller stopped")
            await _marker(tmp_path / "finished")
            assert gateway.poll() is None
            controller = _start("opss", tmp_path, public_port, gateway_port, logs)
            await _ready(controller, url)
            async with Client(url, timeout=10, init_timeout=3) as client:
                async with asyncio.timeout(10):
                    while True:
                        state = await _call(client, "revit", "revit_workspace_ui_status")
                        assert not state.is_error
                        if state.structured_content["state"] == "clear":
                            break
                        await asyncio.sleep(0.1)
                recovered = await _call(client, "windows", "windows_operation_result",
                                        {"fence_id": fence_id})
                assert not recovered.is_error
                assert recovered.structured_content == {"value": "private-http-result", "x": 42}
            assert (tmp_path / "executions.txt").read_text().splitlines() == ["42"]
        finally:
            _stop(controller)
            _stop(gateway)
            for log in logs:
                log.close()
    asyncio.run(check())


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "--serve" or sys.argv[2] not in {"gateway", "opss"}:
        raise SystemExit("Internal test server mode only")
    _serve(sys.argv[2], Path(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))

