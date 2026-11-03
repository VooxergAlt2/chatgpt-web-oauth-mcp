#!/usr/bin/env python3
"""Operator-only HTTP MCP/ACP acceptance with a real disposable server crash.

The supplied config/profile is never changed. Each run creates a private state,
a narrower derived config and a fresh Git fixture under an approved workspace
root. Only the exact QA server PID is killed during the restart experiment;
the detached worker must survive. No provider work runs on import or in tests.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from .acp_acceptance_provenance import capture_implementation
except ImportError:
    from acp_acceptance_provenance import capture_implementation


class AcceptanceError(RuntimeError):
    """A fixed diagnostic code, safe to print without logs, prompts or tokens."""


def require(condition, code):
    if not condition:
        raise AcceptanceError(code)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_private_json(path, value):
    from chatgpt_web_oauth_mcp.state_io import atomic_write_bytes
    atomic_write_bytes(path, (json.dumps(value, indent=2) + "\n").encode(), mode=0o600)


class QaServer:
    """Own only our Popen object and inherited socket; never signal a group."""

    def __init__(self, *, state: Path, environment: dict[str, str], command=None):
        self.state = state
        self.environment = dict(environment)
        self.command = command
        self.process = None
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(32)
        self.port = self.socket.getsockname()[1]
        self.log = (state / "qa-server.log").open("ab", buffering=0)
        os.chmod(state / "qa-server.log", 0o600)

    async def start(self):
        require(self.process is None or self.process.poll() is not None, "qa_server_already_running")
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
        env = dict(self.environment, CHATGPT_MCP_READY_FD=str(writer), CHATGPT_MCP_PORT=str(self.port))
        command = self.command or [
            sys.executable, "-m", "chatgpt_web_oauth_mcp.server", "--fd", str(self.socket.fileno()),
        ]
        try:
            self.process = subprocess.Popen(
                command, env=env, cwd=self.state, stdin=subprocess.DEVNULL,
                stdout=self.log, stderr=self.log, start_new_session=True,
                pass_fds=(writer, self.socket.fileno()),
            )
        finally:
            os.close(writer)
        try:
            deadline = time.monotonic() + 30
            ready = b""
            while time.monotonic() < deadline:
                require(self.process.poll() is None, "qa_server_start_failed")
                try:
                    ready += os.read(reader, 32)
                except BlockingIOError:
                    pass
                if ready == b"ready\n":
                    return self.process.pid
                require(len(ready) <= 32, "qa_ready_protocol_invalid")
                await asyncio.sleep(0.05)
            raise AcceptanceError("qa_server_start_timeout")
        finally:
            os.close(reader)

    async def crash(self):
        require(self.process is not None and self.process.poll() is None, "qa_server_not_running")
        pid = self.process.pid
        self.process.kill()  # SIGKILL only the disposable MCP process.
        await asyncio.to_thread(self.process.wait, 5)
        return pid

    async def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 12)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, 5)

    async def close(self):
        await self.stop()
        self.socket.close()
        self.log.close()

    def transport(self, token, owner):
        from fastmcp.client.transports import StreamableHttpTransport
        return StreamableHttpTransport(f"http://127.0.0.1:{self.port}/mcp", headers={
            "Authorization": "Bearer " + token, "x-openai-session": owner,
        })


async def call(client, name, arguments):
    response = await client.call_tool(
        "tool_group_call", {"group": "opss", "name": name, "arguments": arguments},
        raise_on_error=False,
    )
    data = response.structured_content
    if not isinstance(data, dict):
        try:
            data = json.loads(response.content[0].text)
        except (ValueError, IndexError, AttributeError):
            raise AcceptanceError("mcp_response_not_structured") from None
    require(isinstance(data, dict), "mcp_response_not_object")
    return data


async def select(client):
    result = await client.call_tool("tool_group_select", {"group": "opss", "limit": 1}, raise_on_error=False)
    require(not result.is_error, "qa_group_selection_failed")


async def status(client, delegate_id):
    response = await call(client, "delegate_status", {"delegate_id": delegate_id, "limit": 1})
    require(isinstance(response.get("delegate"), dict), "qa_delegate_unavailable")
    return response["delegate"]


async def terminal(client, delegate_id, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        final = await status(client, delegate_id)
        if final.get("completed"):
            return final
        await asyncio.sleep(0.3)
    raise AcceptanceError("qa_delegate_wait_timeout")


def private_artifact(final, state):
    logs = final.get("logs") or {}
    metadata = Path(logs.get("metadata", "")).resolve()
    require(metadata.is_relative_to((state / "delegates").resolve()), "qa_artifact_outside_state")
    ref = final.get("structured_result_ref") or {}
    name = ref.get("name")
    require(isinstance(name, str) and name == Path(name).name, "qa_artifact_name_invalid")
    path = metadata.parent / name
    require(not path.is_symlink(), "qa_artifact_symlink")
    raw = path.read_bytes()
    require(bool(raw) and hashlib.sha256(raw).hexdigest() == ref.get("sha256"), "qa_artifact_hash_mismatch")
    document = json.loads(raw)
    require(document.get("delegate_id") == final.get("delegate_id"), "qa_artifact_delegate_mismatch")
    report = document.get("structured_output")
    require(isinstance(report, dict), "qa_artifact_manifest_missing")
    return report


def checkpoint(snapshot, state, acp_state):
    metadata = Path((snapshot.get("logs") or {}).get("metadata", "")).resolve()
    require(metadata.is_relative_to((state / "delegates").resolve()), "qa_metadata_outside_state")
    request_path = metadata.parent / "invocation-stdin.bin"
    if not request_path.exists():
        return None, None
    request = read_json(request_path)
    reference = request.get("session_ref")
    require(isinstance(reference, str) and re.fullmatch(r"[a-f0-9]{32}", reference), "qa_session_ref_invalid")
    path = acp_state / "sessions" / (reference + ".json")
    return (read_json(path) if path.exists() else None), reference


async def running_turn(client, delegate_id, state, acp_state, seconds=75):
    """Do not equate a queued/running scheduler row with a dispatched ACP turn."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        snapshot = await status(client, delegate_id)
        require(not snapshot.get("completed"), "qa_turn_finished_before_fault_injection")
        saved, reference = checkpoint(snapshot, state, acp_state)
        if saved and saved.get("phase") == "running" and snapshot.get("durable_job_id"):
            require(snapshot.get("durable_job_status") == "running", "qa_durable_worker_not_running")
            require(isinstance(snapshot.get("pid"), int) and snapshot["pid"] > 1, "qa_worker_pid_missing")
            return snapshot, reference
        await asyncio.sleep(0.1)
    raise AcceptanceError("qa_acp_turn_did_not_start")


def prepare(config, state, workspace_root, account):
    from chatgpt_web_oauth_mcp.acp_backend import AcpBackend

    require(account == "antigravity3", "qa_requires_attested_third_account")
    backend = AcpBackend.load(config)
    require(account in backend.accounts, "qa_account_not_configured")
    workspace_root = workspace_root.resolve(strict=True)
    require(workspace_root.is_dir(), "qa_workspace_root_invalid")
    backend.validate(account=account, workspace=workspace_root, kind="explore", model=None, timeout=120)
    require(not state.exists() and not state.is_symlink(), "qa_state_must_be_fresh")
    state.mkdir(mode=0o700, parents=True)
    workspace = Path(tempfile.mkdtemp(prefix="acp-mcp-", dir=workspace_root)).resolve()
    raw = read_json(config)
    account_settings = dict(raw["accounts"][account])
    require(isinstance(account_settings.get("cli_home"), str), "qa_cli_account_binding_missing")
    acp_state = state / "acp"
    acp_state.mkdir(mode=0o700)
    proof_dir = acp_state / "account-bindings"
    proof_dir.mkdir(mode=0o700)
    proof = backend.state_root / "account-bindings" / (account + ".json")
    require(proof.is_file() and not proof.is_symlink() and not proof.stat().st_mode & 0o077, "qa_binding_proof_unavailable")
    # Copy only an already validated attestation, never OAuth credentials.
    from chatgpt_web_oauth_mcp.state_io import atomic_write_bytes
    atomic_write_bytes(proof_dir / proof.name, proof.read_bytes(), mode=0o600)
    account_settings["workspace_roots"] = [str(workspace)]
    raw["accounts"] = {account: account_settings}
    raw["state_root"] = str(acp_state)
    derived = state / "acp-config.json"
    write_private_json(derived, raw)
    AcpBackend.load(derived)
    marker = "MCP-ACP-" + secrets.token_hex(12).upper()
    source = workspace / "mcp-acceptance-proof.txt"
    source.write_text(marker + "\n", encoding="utf-8")
    source.chmod(0o600)
    for command in (
        ["git", "init", "-q", str(workspace)],
        ["git", "-C", str(workspace), "add", source.name],
        ["git", "-C", str(workspace), "-c", "user.name=ACP QA", "-c", "user.email=acp-qa@invalid",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", "Disposable ACP evidence baseline"],
    ):
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
        require(result.returncode == 0, "qa_git_fixture_failed")
    token, owner = secrets.token_hex(24), "acp-qa-" + secrets.token_hex(12)
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "CHATGPT_MCP_STATE_DIR": str(state),
        "CHATGPT_MCP_DELEGATE_STATE_DIR": str(state / "delegates"),
        "CHATGPT_MCP_ACP_BACKEND_CONFIG": str(derived),
        "CHATGPT_MCP_WORKSPACE_ROOT": str(workspace),
        "CHATGPT_MCP_HOST": "127.0.0.1",
        "CHATGPT_MCP_TOOL_GROUP_MODE": "exclusive",
        "CHATGPT_MCP_TOOL_PROFILE": "lean",
        "CHATGPT_MCP_AUTH_MODE": "shared_token",
        "CHATGPT_MCP_AUTH_TOKEN": token,
        "CHATGPT_MCP_HEALTH_TOKEN": token,
        "CHATGPT_MCP_ANTIGRAVITY3_ENABLED": "true",
        "CHATGPT_MCP_ANTIGRAVITY3_HOME": account_settings["cli_home"],
        "CHATGPT_MCP_QUOTA_PRIMING_ENABLED": "false",
        "CHATGPT_MCP_CODE_GRAPH_ENABLED": "false",
    })
    return workspace, acp_state, marker, token, owner, env


def check_success(final, state):
    require(final.get("status") == "succeeded", "qa_delegate_did_not_succeed")
    require(final.get("execution_backend") == "acp", "qa_wrong_execution_backend")
    require(final.get("harness_metadata", {}).get("transport") == "acp", "qa_wrong_result_parser")
    require(final.get("durable") is True, "qa_delegate_not_durable")
    require(final.get("acceptance_status") != "accepted", "qa_agent_self_accepted")
    return private_artifact(final, state)


async def exercise(config: Path, state: Path, workspace_root: Path, account="antigravity3", *, implementation=None):
    if implementation is None:
        implementation = capture_implementation(Path(__file__).resolve().parents[1])
    from fastmcp import Client
    from chatgpt_web_oauth_mcp.shell import JobRegistry

    os.umask(0o077)
    state = state.resolve()
    workspace, acp_state, marker, token, owner, env = prepare(config, state, workspace_root, account)
    service = QaServer(state=state, environment=env)
    owned = []
    jobs = set()
    report = {"implementation": implementation}
    registry = JobRegistry()
    base = {
        "cwd": str(workspace), "harness": account, "backend": "acp", "kind": "explore",
        "files_in_scope": ["mcp-acceptance-proof.txt"], "max_tool_calls": 6,
        "model": "gemini-3.8-flash-medium", "reasoning_effort": "medium",
        "execution_timeout_seconds": 120, "wait_seconds": 0,
    }

    async def submit(client, **changes):
        response = await call(client, "delegate_task", {**base, **changes})
        did = response.get("delegate_id")
        require(isinstance(did, str) and re.fullmatch(r"[a-f0-9]{12}", did), "qa_submission_rejected")
        owned.append(did)
        return did

    try:
        before_pid = await service.start()
        async with Client(service.transport(token, owner)) as client:
            await select(client)
            first_id = await submit(client, task=(
                "Read ONLY mcp-acceptance-proof.txt. Remember its exact marker for a later continuation. "
                "Return the exact marker as summary and one finding citing mcp-acceptance-proof.txt:1 "
                "and the marker. No commands or changes."
            ))
            before, reference = await running_turn(client, first_id, state, acp_state)
        job_id = before["durable_job_id"]
        jobs.add(job_id)
        before_job = registry.job_status(job_id=job_id, state_dir=state)
        require(before_job.get("status") == "running", "qa_restart_window_missed")
        require(before_job.get("pid") == before["pid"] and before["pid"] != before_pid, "qa_worker_identity_mismatch")
        await service.crash()
        surviving = registry.job_status(job_id=job_id, state_dir=state)
        require(surviving.get("status") == "running" and surviving.get("pid") == before["pid"], "qa_worker_did_not_survive")
        after_pid = await service.start()
        require(after_pid != before_pid, "qa_server_not_replaced")

        async with Client(service.transport(token, owner)) as client:
            await select(client)
            recovered = await status(client, first_id)
            require(recovered.get("durable_job_id") == job_id, "qa_recovery_changed_job")
            require(recovered.get("recovered_from_disk") is True, "qa_recovery_not_adopted")
            final = await terminal(client, first_id)
            first_report = check_success(final, state)
            require(first_report.get("summary") == marker, "qa_source_marker_wrong")
            evidence = final.get("research_evidence") or {}
            require(evidence.get("status") == "all_findings_anchored", "qa_evidence_not_anchored")
            require(bool(evidence.get("source_commit")) and evidence.get("anchored_finding_count", 0) >= 1, "qa_git_evidence_missing")
            require((workspace / "mcp-acceptance-proof.txt").read_text().strip() == marker, "qa_source_changed")
            require(final.get("durable_job_id") == job_id, "qa_terminal_job_changed")
            await call(client, "mark_result_consumed", {"kind": "delegate", "result_id": first_id})
            report.update(restart_same_job=True, worker_survived=True, durable_artifact_verified=True,
                          source_unchanged=True, source_evidence_anchored=True)

            # Same account/context and logical owner; marker is deliberately not
            # included in the new prompt or reread from the fixture.
            resumed_id = await submit(client, resume_from_delegate_id=first_id, max_tool_calls=1, task=(
                "Recall the exact marker read in the prior turn from conversation memory only. "
                "Do NOT call tools or reread files. Return it as summary, with findings=[] and blockers=[]."
            ))
            resumed = await terminal(client, resumed_id)
            recalled = check_success(resumed, state)
            native = resumed.get("harness_metadata") or {}
            require(resumed_id != first_id and native.get("session_ref") == reference, "qa_resume_session_changed")
            require(recalled.get("summary") == marker, "qa_resume_memory_lost")
            require(native.get("tool_calls") == 0, "qa_resume_used_tools")
            require(native.get("conversation_id") == final.get("harness_metadata", {}).get("conversation_id"), "qa_resume_provider_session_changed")
            await call(client, "mark_result_consumed", {"kind": "delegate", "result_id": resumed_id})
            report["resume_memory_without_reads"] = True

            denied = await call(client, "delegate_task", {**base, "task": "Do not execute", "kind": "code"})
            require(denied.get("error", {}).get("code") == "acp_writer_verification_required", "qa_writer_guard_failed")
            denied = await call(client, "delegate_task", {
                **base, "task": "Do not execute", "backend": "cli", "resume_from_delegate_id": resumed_id,
            })
            require(denied.get("error", {}).get("code") == "delegate_resume_backend_mismatch", "qa_transport_guard_failed")
            report.update(writer_denied=True, cross_backend_denied=True)

            async with Client(service.transport(token, owner + "-foreign")) as foreign:
                await select(foreign)
                denied = await call(foreign, "delegate_status", {"delegate_id": resumed_id})
                require(denied.get("error", {}).get("code") == "result_owned_by_another_session", "qa_foreign_read_allowed")
                denied = await call(foreign, "delegate_task", {
                    **base, "task": "Do not execute", "resume_from_delegate_id": resumed_id,
                })
                require(denied.get("error", {}).get("code") in {
                    "acp_resume_owner_mismatch", "result_owned_by_another_session",
                }, "qa_foreign_resume_allowed")
            report["wrong_owner_denied"] = True

            cancel_id = await submit(client, max_tool_calls=1, task=(
                "Cancellation acceptance fixture. Without tools, prepare a detailed catalogue of "
                "500 software testing ideas. Keep all ideas in the final JSON summary. "
                "Use findings=[] and blockers=[]; do not finish early with an acknowledgement."
            ))
            cancelling, cancel_ref = await running_turn(client, cancel_id, state, acp_state)
            jobs.add(cancelling["durable_job_id"])
            cancelled = await call(client, "delegate_cancel", {
                "delegate_id": cancel_id, "force_running": True,
                "rationale": "Explicit cancellation of this disposable ACP acceptance turn",
            })
            require(cancelled.get("success") is not False, "qa_cancel_request_failed")
            cancelled = await terminal(client, cancel_id, seconds=40)
            native = cancelled.get("harness_metadata") or {}
            require(cancelled.get("status") == "cancelled", "qa_cancel_terminal_wrong")
            require(native.get("turn_status") == "cancelled" and native.get("outcome_uncertain") is False,
                    "qa_native_cancel_not_confirmed")
            saved = read_json(acp_state / "sessions" / (cancel_ref + ".json"))
            require(saved.get("phase") == "cancelled", "qa_cancel_checkpoint_wrong")
            require(saved.get("result", {}).get("error", {}).get("code") == "acp_cancelled", "qa_cancel_used_signal_fallback")
            cancelled_job = registry.job_status(job_id=cancelling["durable_job_id"], state_dir=state)
            require(cancelled_job.get("exit_code") == 0 and cancelled_job.get("termination_reason") is None,
                    "qa_cancel_worker_forced")
            await call(client, "mark_result_consumed", {"kind": "delegate", "result_id": cancel_id})
            report["native_cancel_confirmed"] = True
        write_private_json(state / "acceptance-report.json", {"status": "PASS", "account": account, **report})
        return {"status": "PASS", "account": account, **report}
    finally:
        # Reattach only this disposable server/state if a failed gate left an
        # owned worker alive. Never search for or signal a production process.
        if owned:
            try:
                if service.process is None or service.process.poll() is not None:
                    await service.start()
                async with Client(service.transport(token, owner)) as client:
                    await select(client)
                    for delegate_id in owned:
                        current = await status(client, delegate_id)
                        if not current.get("completed"):
                            await call(client, "delegate_cancel", {
                                "delegate_id": delegate_id, "force_running": True,
                                "rationale": "Disposable ACP acceptance cleanup",
                            })
            except Exception:
                # Exact observed job IDs in the freshly created registry only.
                for job_id in jobs:
                    if registry.job_status(job_id=job_id, state_dir=state).get("status") == "running":
                        registry.kill_job(job_id=job_id, state_dir=state, signal_name="TERM")
        await service.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True, help="New private directory; must not already exist")
    parser.add_argument("--workspace", type=Path, required=True, help="Approved existing root for a fresh Git fixture")
    parser.add_argument("--account", choices=["antigravity3"], default="antigravity3")
    args = parser.parse_args()
    implementation = capture_implementation(Path(__file__).resolve().parents[1])
    try:
        result = asyncio.run(exercise(args.config, args.state, args.workspace, args.account, implementation=implementation))
    except Exception as exc:
        code = str(exc) if isinstance(exc, AcceptanceError) else "qa_internal_error"
        print("ACP_MCP_ACCEPTANCE_FAIL", json.dumps({"status": "FAIL", "error": code, "implementation": implementation}), flush=True)
        return 1
    print("ACP_MCP_ACCEPTANCE_PASS", json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
