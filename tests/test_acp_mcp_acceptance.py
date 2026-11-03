"""No-provider checks for the operator-only subprocess HTTP acceptance harness."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shlex
import signal
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "acp_mcp_acceptance.py"
SPEC = importlib.util.spec_from_file_location("acp_mcp_acceptance", SCRIPT)
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)


def environment(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("CHATGPT_MCP_")}
    env.update({
        "PYTHONPATH": str(SCRIPT.parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CHATGPT_MCP_STATE_DIR": str(tmp_path),
        "CHATGPT_MCP_DELEGATE_STATE_DIR": str(tmp_path / "delegates"),
        "CHATGPT_MCP_WORKSPACE_ROOT": str(tmp_path),
        "CHATGPT_MCP_AUTH_MODE": "shared_token",
        "CHATGPT_MCP_AUTH_TOKEN": "local-fixture-token",
        "CHATGPT_MCP_TOOL_GROUP_MODE": "exclusive",
        "CHATGPT_MCP_TOOL_PROFILE": "lean",
        "CHATGPT_MCP_ACP_BACKEND_CONFIG": "",
        "CHATGPT_MCP_CODE_GRAPH_ENABLED": "false",
        "CHATGPT_MCP_CODEX_COMMAND": "/bin/false",
        "CHATGPT_MCP_ANTIGRAVITY_COMMAND": "",
        "CHATGPT_MCP_ANTIGRAVITY2_ENABLED": "false",
        "CHATGPT_MCP_ANTIGRAVITY3_ENABLED": "false",
        "CHATGPT_MCP_LOCAL_DELEGATE_ENABLED": "false",
        "CHATGPT_MCP_LOCAL2_DELEGATE_ENABLED": "false",
        "CHATGPT_MCP_HEALTH_USAGE_LIMITS_ENABLED": "false",
        "CHATGPT_MCP_QUOTA_PRIMING_ENABLED": "false",
    })
    return env


def test_crash_signals_only_qa_pid_and_keeps_bound_port(tmp_path):
    # The fake helper is in the same process group as its parent. A killpg()
    # implementation would kill it, which this regression must detect.
    helper_path = tmp_path / "owned-helper.pid"
    fake = tmp_path / "fake_server.py"
    fake.write_text(
        "import os,subprocess,sys,time\n"
        f"path={str(helper_path)!r}\n"
        "if not os.path.exists(path):\n"
        "    p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        "    open(path,'w').write(str(p.pid))\n"
        "fd=int(os.environ['CHATGPT_MCP_READY_FD'])\n"
        "os.write(fd,b'ready\\n');os.close(fd)\n"
        "time.sleep(30)\n"
    )

    async def exercise():
        service = qa.QaServer(
            state=tmp_path, environment=environment(tmp_path),
            command=[sys.executable, str(fake)],
        )
        helper_pid = None
        try:
            first = await service.start()
            helper_pid = int(helper_path.read_text())
            port = service.port
            await service.crash()
            os.kill(helper_pid, 0)
            second = await service.start()
            assert second != first and service.port == port
        finally:
            await service.close()
            if helper_pid is not None:
                try:
                    os.kill(helper_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    asyncio.run(exercise())


def test_real_http_job_survives_disposable_server_restart(tmp_path):
    """Exercise socket, ready pipe, logical header and durable job via real MCP.

    This starts no ACP profile, CLI agent, model endpoint or provider request.
    """
    from fastmcp import Client
    from chatgpt_web_oauth_mcp.shell import JobRegistry

    async def exercise():
        service = qa.QaServer(state=tmp_path, environment=environment(tmp_path))
        owner, token = "fixture-logical-session", "local-fixture-token"
        released = tmp_path / "release-worker"
        worker = (
            "import pathlib,time; p=pathlib.Path(" + repr(str(released)) + "); "
            "deadline=time.monotonic()+30\n"
            "while not p.exists() and time.monotonic()<deadline: time.sleep(0.05)\n"
            "print('LOCAL_DURABLE_PASS',flush=True)\n"
        )
        registry = JobRegistry()
        job_id = None
        try:
            first_pid = await service.start()
            async with Client(service.transport(token, owner)) as client:
                await qa.select(client)
                started = await qa.call(client, "job_start", {
                    "command": shlex.join([sys.executable, "-c", worker]),
                    "cwd": str(tmp_path), "timeout_seconds": 45,
                })
                job_id = started["job_id"]
                assert started["status"] == "running"
            await service.crash()
            observed = registry.job_status(job_id=job_id, state_dir=tmp_path)
            assert observed["status"] == "running" and observed["pid"] == started["pid"]
            second_pid = await service.start()
            assert second_pid != first_pid
            released.touch()
            async with Client(service.transport(token, owner)) as client:
                await qa.select(client)
                response = await qa.call(client, "await_job", {"job_id": job_id, "wait_seconds": 10})
                assert response["terminal"] and response["result"]["status"] == "succeeded"
                output = await qa.call(client, "job_output", {"job_id": job_id, "max_bytes": 1024})
                assert "LOCAL_DURABLE_PASS" in output["content"]
                consumed = await qa.call(client, "mark_result_consumed", {"kind": "job", "result_id": job_id})
                assert consumed["success"]
        finally:
            released.touch()
            if job_id and registry.job_status(job_id=job_id, state_dir=tmp_path).get("status") == "running":
                registry.kill_job(job_id=job_id, state_dir=tmp_path, signal_name="TERM")
            await service.close()

    asyncio.run(exercise())


def test_artifact_verification_rejects_cross_state_and_hash_corruption(tmp_path):
    directory = tmp_path / "delegates" / "fixture"
    directory.mkdir(parents=True)
    artifact = directory / "structured-result.json"
    raw = json.dumps({"delegate_id": "a" * 12, "structured_output": {
        "status": "succeeded", "summary": "fixture", "findings": [],
    }}).encode()
    artifact.write_bytes(raw)
    final = {
        "delegate_id": "a" * 12, "logs": {"metadata": str(directory / "metadata.json")},
        "structured_result_ref": {"name": artifact.name, "sha256": hashlib.sha256(raw).hexdigest()},
    }
    assert qa.private_artifact(final, tmp_path)["summary"] == "fixture"
    artifact.write_bytes(raw + b"\n")
    with pytest.raises(qa.AcceptanceError, match="hash_mismatch"):
        qa.private_artifact(final, tmp_path)
    with pytest.raises(qa.AcceptanceError, match="outside_state"):
        qa.private_artifact({**final, "logs": {"metadata": str(tmp_path / "other.json")}}, tmp_path)


def test_running_gate_requires_native_checkpoint_not_just_scheduler_state(tmp_path, monkeypatch):
    calls = 0
    native = 0

    async def current(_client, _did):
        nonlocal calls
        calls += 1
        return {
            "status": "running", "completed": False, "pid": 1234,
            "durable_job_id": "fixture-job", "durable_job_status": "running",
        }

    def checkpoint(*_args):
        nonlocal native
        native += 1
        return {"phase": "ready" if native < 2 else "running"}, "a" * 32

    monkeypatch.setattr(qa, "status", current)
    monkeypatch.setattr(qa, "checkpoint", checkpoint)
    snapshot, reference = asyncio.run(qa.running_turn(None, "a" * 12, tmp_path, tmp_path))
    assert calls == 2 and reference == "a" * 32
    assert snapshot["durable_job_id"] == "fixture-job"
