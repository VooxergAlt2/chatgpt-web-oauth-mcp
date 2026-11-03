"""Subprocess descriptor ownership at the optional SDK adapter boundary."""
import asyncio
import contextlib
import gc
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp.acp_sdk_transport import AcpSdkTransport


def transport(tmp_path, script):
    return AcpSdkTransport(
        argv=('/usr/bin/python3', '-c', script), cwd=tmp_path,
        environment={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path),
                     'GEMINI_HOME': str(tmp_path)},
    )


def assert_closed(process):
    assert process.returncode is not None
    assert process._transport.is_closing()
    assert process._transport._loop is None
    assert process.stdin.is_closing()
    for descriptor in (0, 1):
        pipe = process._transport.get_pipe_transport(descriptor)
        assert pipe.is_closing()
        assert pipe.get_extra_info('pipe').closed


def sdk_fixture(monkeypatch, *, close=None):
    import chatgpt_web_oauth_mcp.acp_sdk_transport as module
    monkeypatch.setattr(module, 'version', lambda name: '0.12.1')
    created = []
    sdk = ModuleType('acp')
    connection = SimpleNamespace(closed=False)
    async def close_connection():
        connection.closed = True
        if close is not None:
            await close(created[0])
    connection.close = close_connection

    @asynccontextmanager
    async def spawn_agent_process(client, executable, *args, **kwargs):
        process = await asyncio.create_subprocess_exec(
            executable, *args, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=64, env=kwargs['env'], cwd=kwargs['cwd'],
        )
        created.append(process)
        try:
            yield connection, process
        finally:
            # This emulates the pinned SDK's outer finalizer. The adapter must
            # already have settled every pipe; a PID-only wait is insufficient.
            try:
                assert connection.closed
                assert_closed(process)
            finally:
                process._transport.close()
                await asyncio.sleep(.01)  # test-only cleanup for a failing implementation
    sdk.spawn_agent_process = spawn_agent_process
    monkeypatch.setitem(sys.modules, 'acp', sdk)
    return created


def test_sdk_adapter_closes_reaped_process_with_paused_stdout(tmp_path, monkeypatch):
    created = sdk_fixture(monkeypatch)
    adapter = transport(tmp_path, 'import os; os.write(1, b"x"*4096)')
    async def scenario():
        async with adapter(object()):
            process = created[0]
            while process.returncode is None or not process.stdout._paused:
                await asyncio.sleep(.001)
        assert_closed(process)
    asyncio.run(asyncio.wait_for(scenario(), 4))
    assert_closed(created[0])
    gc.collect()


def test_sdk_adapter_finishes_owned_cleanup_despite_repeated_cancellation(tmp_path, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        async def delayed_close(process):
            started.set()
            await release.wait()
        created = sdk_fixture(monkeypatch, close=delayed_close)
        adapter = transport(tmp_path, 'import time; time.sleep(30)')
        async def owner():
            async with adapter(object()):
                pass
        task = asyncio.create_task(owner())
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert_closed(created[0])
    asyncio.run(asyncio.wait_for(scenario(), 4))


def test_sdk_blocked_close_still_reaps_process_and_discards_queued_stdin(tmp_path, monkeypatch):
    async def blocked_close(process):
        process.stdin.write(b'x' * (1024 * 1024))
        await process.stdin.drain()
    created = sdk_fixture(monkeypatch, close=blocked_close)
    adapter = transport(tmp_path, 'import time; time.sleep(30)')
    async def scenario():
        with pytest.raises(TimeoutError):
            async with adapter(object()):
                pass
        assert_closed(created[0])
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_pinned_sdk_transport_smoke_without_provider(tmp_path):
    """When ACP is installed, exercise its real reader and nested finalizers."""
    import importlib.metadata
    try:
        installed = importlib.metadata.version('agent-client-protocol')
    except importlib.metadata.PackageNotFoundError:
        pytest.skip('Optional pinned ACP SDK is not installed')
    if installed != '0.12.1':
        pytest.skip('Requires the production pinned ACP SDK')
    from chatgpt_web_oauth_mcp.acp_session import AcpRunLimits, AcpTurnClient
    adapter = transport(tmp_path, 'import sys; [None for line in sys.stdin]')
    async def scenario():
        for _ in range(3):
            client = AcpTurnClient(AcpRunLimits(timeout_seconds=10))
            async with adapter(client):
                await asyncio.sleep(.01)
    asyncio.run(asyncio.wait_for(scenario(), 5))
    gc.collect()

def test_native_profiles_reach_pinned_sdk_wire_without_double_meta(tmp_path):
    """Use OPSS_ACP_TEST_PYTHON for a separately installed optional worker SDK."""
    import json
    import os
    import subprocess

    from chatgpt_web_oauth_mcp.acp_tool_profiles import native_tool_profile, profile_sdk_kwargs

    profiles = [native_tool_profile(writer=writer, questions=questions)
                for writer in (False, True) for questions in (False, True)]
    payload = [{"kwargs": profile_sdk_kwargs(profile)} for profile in profiles]
    interpreter = os.environ.get("OPSS_ACP_TEST_PYTHON", sys.executable)
    probe = r"""
import asyncio
import importlib.metadata
import json
import sys
from types import SimpleNamespace

try:
    installed = importlib.metadata.version("agent-client-protocol")
except importlib.metadata.PackageNotFoundError:
    installed = None
if installed != "0.12.1":
    print(json.dumps({"status": "optional_sdk_unavailable", "version": installed}))
    raise SystemExit(0)

from acp.client.connection import ClientSideConnection

calls = []
class Spy:
    async def send_request(self, method, params):
        calls.append({"method": method, "params": params})
        return {"sessionId": "offline-fixture"} if method == "session/new" else {}

async def main():
    for item in json.load(sys.stdin):
        connection = SimpleNamespace(_conn=Spy())
        await ClientSideConnection.new_session(
            connection, cwd="/fixture/workspace", mcp_servers=[], **item["kwargs"])
        await ClientSideConnection.load_session(
            connection, session_id="offline-fixture", cwd="/fixture/workspace",
            mcp_servers=[], **item["kwargs"])

asyncio.run(main())
print(json.dumps({"status": "ready", "version": installed, "calls": calls}))
"""
    completed = subprocess.run(
        [interpreter, "-I", "-B", "-c", probe], cwd=tmp_path,
        input=json.dumps(payload), text=True, capture_output=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    if observed["status"] != "ready":
        if "OPSS_ACP_TEST_PYTHON" in os.environ:
            pytest.fail("Configured test interpreter does not have ACP SDK 0.12.1")
        pytest.skip("Optional pinned ACP SDK is not installed; set OPSS_ACP_TEST_PYTHON")
    assert observed["version"] == "0.12.1"
    assert len(observed["calls"]) == 8
    for profile, rows in zip(profiles, [observed["calls"][i:i + 2] for i in range(0, 8, 2)], strict=True):
        assert [row["method"] for row in rows] == ["session/new", "session/load"]
        for row in rows:
            params = row["params"]
            assert params["_meta"] == {"antigravity": {"enabledTools": profile["enabled_tools"]}}
            assert params["cwd"] == "/fixture/workspace"
            assert params["mcpServers"] == []
            assert "enabled_tools" not in params and "antigravity" not in params
        assert rows[1]["params"]["sessionId"] == "offline-fixture"
