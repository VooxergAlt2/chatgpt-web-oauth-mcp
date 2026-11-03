#!/usr/bin/env python3
"""Operator-only final native ACP acceptance on a fresh disposable QA server.

Nothing runs on import. The operator explicitly supplies a fresh state directory
and an already approved fixture root. The configured OAuth profile is not copied
or modified: prepare() copies only its previously validated account attestation.
All model turns are sequential. Fault injection addresses only our Popen server
or an exact owned job in this run's private JobRegistry.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import subprocess
import time
from pathlib import Path

try:
    from .acp_acceptance_provenance import capture_implementation
    from .acp_mcp_acceptance import (
        AcceptanceError, QaServer, call, checkpoint, prepare, private_artifact,
        read_json, require, running_turn, select, status, terminal, write_private_json,
    )
except ImportError:
    from acp_acceptance_provenance import capture_implementation
    from acp_mcp_acceptance import (
        AcceptanceError, QaServer, call, checkpoint, prepare, private_artifact,
        read_json, require, running_turn, select, status, terminal, write_private_json,
    )

BUGGY = b"def add(left, right):\n    return left - right\n"
FIXED = b"def add(left, right):\n    return left + right\n"
PROOF = "mcp-acceptance-proof.txt"
TERMINAL = {"succeeded", "failed", "cancelled", "timed_out", "unknown", "interrupted"}
_DELEGATE_ID = re.compile(r"[a-f0-9]{12}")
_SAFE_CODE = re.compile(r"[a-zA-Z0-9_:-]{1,160}")


def progress(stage, **values):
    """Only controlled stages, booleans, counts and fixed diagnostic codes."""
    print("ACP_FINAL_STAGE", json.dumps({"stage": stage, **values}, sort_keys=True), flush=True)


def git_fixture(workspace, *arguments):
    env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_TERMINAL_PROMPT": "0"}
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(workspace), "-c", "core.hooksPath=/dev/null",
         "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", *arguments],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=15, check=False,
    )
    require(result.returncode == 0, "qa_final_git_fixture_failed")
    return result.stdout


def prepare_writer(workspace, state, account):
    """The only commits made by this script are inside its own new fixture."""
    from chatgpt_web_oauth_mcp.acp_backend import AcpBackend

    calculator = workspace / "calculator.py"
    require(not calculator.exists() and not calculator.is_symlink(), "qa_calculator_already_exists")
    calculator.write_bytes(BUGGY)
    calculator.chmod(0o644)
    git_fixture(workspace, "add", "--", "calculator.py")
    git_fixture(workspace, "-c", "user.name=ACP QA", "-c", "user.email=acp-qa@invalid",
                "commit", "-q", "-m", "Disposable native writer acceptance fixture")
    writer = state / "writer"
    require(not writer.exists() and not writer.is_symlink(), "qa_writer_path_already_exists")
    git_fixture(workspace, "worktree", "add", "--detach", str(writer))
    backend = AcpBackend.load(state / "acp-config.json")
    require(backend.workspace_allowed(account, writer), "qa_linked_worktree_not_approved")
    backend.validate(account=account, workspace=writer, kind="code", model=None, timeout=180)
    require((writer / "calculator.py").read_bytes() == BUGGY, "qa_writer_baseline_wrong")
    return writer


def own_metadata(final, state):
    path = Path((final.get("logs") or {}).get("metadata", "")).resolve()
    require(path.is_relative_to((state / "delegates").resolve()), "qa_metadata_outside_state")
    require(path.name == "metadata.json" and not path.is_symlink(), "qa_metadata_path_invalid")
    value = read_json(path)
    require(value.get("delegate_id") == final.get("delegate_id"), "qa_metadata_delegate_mismatch")
    return value


def successful(final, state):
    if final.get("status") != "succeeded":
        code = (final.get("error") or {}).get("code")
        if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
            progress("delegate_failed", error_code=code)
        raise AcceptanceError("qa_final_delegate_did_not_succeed")
    require(final.get("execution_backend") == "acp", "qa_wrong_execution_backend")
    require(final.get("durable") is True, "qa_delegate_not_durable")
    stored = own_metadata(final, state)
    native = stored.get("harness_metadata") or final.get("harness_metadata") or {}
    require(native.get("transport") == "acp", "qa_wrong_result_parser")
    require(final.get("acceptance_status") != "accepted", "qa_agent_self_accepted")
    return private_artifact(final, state), native, stored


def own_checkpoint(reference, acp_state):
    require(isinstance(reference, str) and re.fullmatch(r"[a-f0-9]{32}", reference),
            "qa_session_ref_invalid")
    path = acp_state / "sessions" / (reference + ".json")
    require(path.is_file() and not path.is_symlink(), "qa_checkpoint_missing")
    return read_json(path)


def history_fingerprints(reference, acp_state):
    """Hash only this run's native history after its exact worker is dead."""
    require(isinstance(reference, str) and re.fullmatch(r"[a-f0-9]{32}", reference),
            "qa_session_ref_invalid")
    directory = acp_state / "conversations" / reference
    require(directory.is_dir() and not directory.is_symlink(), "qa_native_history_missing")
    manifest = {}
    total = 0
    for path in sorted(directory.rglob("*")):
        require(not path.is_symlink(), "qa_native_history_symlink")
        if path.is_dir():
            continue
        require(path.is_file(), "qa_native_history_file_type")
        total += path.stat().st_size
        require(len(manifest) < 128 and total <= 64 * 1024 * 1024, "qa_native_history_limit")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(131072):
                digest.update(chunk)
        manifest[path.relative_to(directory).as_posix()] = digest.hexdigest()
    require(bool(manifest), "qa_native_history_missing")
    return manifest


async def pending_question(client, delegate_id, state, acp_state, jobs, seconds=110):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        snapshot = await status(client, delegate_id)
        if snapshot.get("durable_job_id"):
            jobs.add(snapshot["durable_job_id"])
        if snapshot.get("completed"):
            code = (snapshot.get("error") or {}).get("code")
            if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
                progress("question_turn_ended_early", error_code=code)
            raise AcceptanceError("qa_actual_question_not_published")
        control = snapshot.get("acp_control") or {}
        request = control.get("pending_request")
        if isinstance(request, dict) and request.get("status") == "pending":
            require(control.get("available") is True and control.get("for_delegate") is True,
                    "qa_question_control_unbound")
            require(request.get("kind") == "question" and request.get("turn_id") == delegate_id,
                    "qa_question_kind_invalid")
            options = request.get("options")
            require(isinstance(options, list), "qa_question_options_invalid")
            labels = [row.get("name") for row in options if isinstance(row, dict)]
            require(labels.count("AMBER") == 1 and labels.count("VIOLET") == 1 and len(labels) == 2,
                    "qa_question_choices_wrong")
            choice = next(row for row in options if row.get("name") == "VIOLET")
            require(isinstance(choice.get("id"), str) and choice["id"], "qa_question_option_id_missing")
            saved, reference = checkpoint(snapshot, state, acp_state)
            require(saved and saved.get("phase") == "running", "qa_question_checkpoint_not_running")
            require(snapshot.get("durable_job_status") == "running"
                    and isinstance(snapshot.get("pid"), int) and snapshot["pid"] > 1,
                    "qa_question_worker_missing")
            return snapshot, reference, request, choice["id"]
        await asyncio.sleep(0.2)
    raise AcceptanceError("qa_question_wait_timeout")


async def consume(client, delegate_id):
    result = await call(client, "mark_result_consumed", {"kind": "delegate", "result_id": delegate_id})
    require(result.get("success") is not False, "qa_result_consumption_failed")


async def cleanup(service, token, owner, state, owned, jobs, registry):
    """Bounded cleanup of exact IDs in this fresh QA state; no process discovery."""
    from fastmcp import Client

    clean = True
    try:
        async with asyncio.timeout(45):
            if owned:
                if service.process is None or service.process.poll() is not None:
                    await service.start()
                async with Client(service.transport(token, owner)) as client:
                    await select(client)
                    for delegate_id in owned:
                        try:
                            snapshot = await status(client, delegate_id)
                            if snapshot.get("durable_job_id"):
                                jobs.add(snapshot["durable_job_id"])
                            if not snapshot.get("completed"):
                                await call(client, "delegate_cancel", {
                                    "delegate_id": delegate_id, "force_running": True,
                                    "rationale": "Disposable final ACP acceptance cleanup",
                                })
                            else:
                                await consume(client, delegate_id)
                        except Exception:
                            clean = False
    except Exception:
        clean = False
    # SIGTERM/KILL are only JobRegistry operations on exact observed QA job IDs.
    for job_id in jobs:
        try:
            snapshot = registry.job_status(job_id=job_id, state_dir=state)
            if snapshot.get("status") == "running":
                registry.kill_job(job_id=job_id, state_dir=state, signal_name="TERM")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if registry.job_status(job_id=job_id, state_dir=state).get("status") != "running":
                        break
                    await asyncio.sleep(0.1)
                if registry.job_status(job_id=job_id, state_dir=state).get("status") == "running":
                    stopped = registry.kill_job(job_id=job_id, state_dir=state, signal_name="KILL")
                    clean = clean and stopped.get("success") is not False
        except Exception:
            clean = False
    try:
        if owned and service.process is not None and service.process.poll() is None:
            async with asyncio.timeout(15):
                async with Client(service.transport(token, owner)) as client:
                    await select(client)
                    for delegate_id in owned:
                        try:
                            snapshot = await status(client, delegate_id)
                            if snapshot.get("completed"):
                                await consume(client, delegate_id)
                        except Exception:
                            clean = False
    except Exception:
        clean = False
    try:
        await service.close()
    except Exception:
        clean = False
    progress("cleanup", complete=clean)
    return clean


async def exercise(config: Path, state: Path, workspace_root: Path, account="antigravity3",
                   *, start_at="question", implementation=None):
    if implementation is None:
        implementation = capture_implementation(Path(__file__).resolve().parents[1])
    from fastmcp import Client
    from chatgpt_web_oauth_mcp.shell import JobRegistry

    os.umask(0o077)
    require(start_at in {"question", "writer", "reconcile"}, "qa_start_phase_invalid")
    state = state.resolve()
    workspace, acp_state, proof_marker, token, owner, env = prepare(
        config, state, workspace_root, account)
    service = QaServer(state=state, environment=env)
    registry = JobRegistry()
    owned = []
    jobs = set()
    report = {"status": "RUNNING", "account": account, "start_at": start_at,
              "implementation": implementation}
    phase = "prepare"
    base = {
        "cwd": str(workspace), "harness": account, "backend": "acp", "kind": "explore",
        "files_in_scope": [PROOF, "calculator.py"], "max_tool_calls": 8,
        "model": "gemini-3.8-flash-medium", "reasoning_effort": "medium",
        "execution_timeout_seconds": 180, "wait_seconds": 0,
    }

    async def submit(client, *, inherit=False, **changes):
        arguments = {**base, **changes}
        if inherit:
            for key in ("model", "reasoning_effort", "files_in_scope", "out_of_scope"):
                arguments.pop(key, None)
        response = await call(client, "delegate_task", arguments)
        delegate_id = response.get("delegate_id")
        if not isinstance(delegate_id, str) or not _DELEGATE_ID.fullmatch(delegate_id):
            code = (response.get("error") or {}).get("code")
            if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
                progress("submission_rejected", error_code=code)
            raise AcceptanceError("qa_final_submission_rejected")
        owned.append(delegate_id)
        if response.get("durable_job_id"):
            jobs.add(response["durable_job_id"])
        return delegate_id

    try:
        writer = prepare_writer(workspace, state, account)
        report["fixture_ready"] = True
        progress("prepared")
        server_pid = await service.start()
        if start_at == "question":
            phase = "question_restart_gateway"
            progress(phase)
            steering_id = "qa-steer-" + secrets.token_hex(12)
            # This unpredictable value is never placed in any delegate_task prompt.
            steering_summary = "QUEUED-" + secrets.token_hex(16).upper()
            async with asyncio.timeout(180):
                async with Client(service.transport(token, owner)) as client:
                    await select(client)
                    first_id = await submit(client, task=(
                        "Native ACP acceptance. Your FIRST operation MUST call the native ask_question tool. "
                        "Ask exactly 'Which fixture color?' with exactly two choices named AMBER and VIOLET. "
                        "Do not infer an answer and do not finish before receiving the selected option. "
                        "After the selected answer, call the supplied MCP server opss_readonly tool read_text "
                        "on mcp-acceptance-proof.txt, and its code_map_symbols tool on calculator.py "
                        "(language python). These must be actual custom MCP calls, not built-in file reads. "
                        "Do not run commands or modify files. Return the required JSON with status succeeded, "
                        "summary exactly '<selected choice>|<exact marker read from the proof file>', "
                        "one finding citing mcp-acceptance-proof.txt:1 and its marker, one finding citing "
                        "calculator.py:1-2 about add, and blockers=[]."
                    ))
                    before, reference, request, violet = await pending_question(
                        client, first_id, state, acp_state, jobs)
                    first_job = before["durable_job_id"]
                    before_job = registry.job_status(job_id=first_job, state_dir=state)
                    require(before_job.get("status") == "running"
                            and before_job.get("pid") == before["pid"] and before["pid"] != server_pid,
                            "qa_pending_worker_identity_mismatch")
                    queued = await call(client, "delegate_message", {
                        "delegate_id": first_id, "action": "steer", "message_id": steering_id,
                        "text": (
                            "For the next explicit continuation ONLY: use no tools and do not reread files. "
                            "Return the normal JSON with status succeeded, findings=[], blockers=[], "
                            "and summary exactly " + steering_summary + "."
                        ),
                    })
                    require(queued.get("success") is True
                            and queued.get("control", {}).get("status") == "queued"
                            and queued.get("control", {}).get("delivery") == "queued_next_turn",
                            "qa_steering_not_queued")
                    async with Client(service.transport(token, owner + "-foreign")) as foreign:
                        await select(foreign)
                        denied = await call(foreign, "delegate_message", {
                            "delegate_id": first_id, "action": "reply", "turn_id": first_id,
                            "request_id": request["request_id"], "option_id": violet,
                        })
                        require(denied.get("error", {}).get("code") in {
                            "result_owned_by_another_session", "acp_control_owner_mismatch"},
                            "qa_foreign_reply_not_denied")
                await service.crash()
                surviving = registry.job_status(job_id=first_job, state_dir=state)
                require(surviving.get("status") == "running" and surviving.get("pid") == before["pid"],
                        "qa_pending_worker_did_not_survive")
                replacement_pid = await service.start()
                require(replacement_pid != server_pid, "qa_server_not_restarted")
                async with Client(service.transport(token, owner)) as client:
                    await select(client)
                    recovered, recovered_ref, pending, recovered_violet = await pending_question(
                        client, first_id, state, acp_state, jobs, seconds=20)
                    require(recovered["durable_job_id"] == first_job and recovered["pid"] == before["pid"],
                            "qa_pending_recovery_changed_job")
                    require(recovered_ref == reference and pending["request_id"] == request["request_id"]
                            and recovered_violet == violet, "qa_pending_recovery_changed_question")
                    require(recovered.get("recovered_from_disk") is True, "qa_pending_recovery_not_adopted")
                    replied = await call(client, "delegate_message", {
                        "delegate_id": first_id, "action": "reply", "turn_id": first_id,
                        "request_id": pending["request_id"], "option_id": violet,
                    })
                    require(replied.get("success") is True
                            and replied.get("control", {}).get("option_id") == violet,
                            "qa_exact_option_reply_failed")
                    first = await terminal(client, first_id, seconds=150)
                    first_report, first_native, _ = successful(first, state)
                    require(first_report.get("summary") == "VIOLET|" + proof_marker, "qa_question_or_file_marker_wrong")
                    gateway_counts = first_native.get("scoped_gateway", {}).get("tool_calls", {})
                    require(gateway_counts.get("read_text", 0) >= 1
                            and gateway_counts.get("code_map_symbols", 0) >= 1,
                            "qa_custom_mcp_tools_not_observed")
                    require(first_native.get("steering_message_ids") == [], "qa_steering_applied_to_inflight_turn")
                    require(first.get("research_evidence", {}).get("status") == "all_findings_anchored",
                            "qa_first_findings_not_anchored")
                    require((workspace / PROOF).read_text().strip() == proof_marker
                            and (workspace / "calculator.py").read_bytes() == BUGGY, "qa_readonly_source_changed")
                    first_control = first.get("acp_control") or {}
                    require(first_control.get("pending_request", {}).get("status") == "consumed",
                            "qa_question_reply_not_consumed")
                    await consume(client, first_id)
                report.update(question_replied=True, foreign_reply_denied=True, pending_restart_same_job=True,
                              pending_request_preserved=True, custom_mcp_read_text=True, custom_mcp_code_map=True,
                              native_readonly_preserved=True)
                progress("question_restart_gateway_passed",
                         read_text_calls=gateway_counts["read_text"],
                         code_map_symbols_calls=gateway_counts["code_map_symbols"])

            phase = "queued_continuation"
            progress(phase)
            async with asyncio.timeout(120):
                async with Client(service.transport(token, owner)) as client:
                    await select(client)
                    resumed_id = await submit(
                        client, inherit=True, resume_from_delegate_id=first_id,
                        execution_timeout_seconds=120, max_tool_calls=1,
                        task=("Apply the owner's queued instructions for this continuation. "
                              "Use no tools. Return the standard JSON with findings=[] and blockers=[]."),
                    )
                    resumed = await terminal(client, resumed_id, seconds=110)
                    resumed_report, resumed_native, _ = successful(resumed, state)
                    require(resumed_report.get("summary") == steering_summary, "qa_queued_instruction_not_delivered")
                    require(resumed_native.get("steering_message_ids") == [steering_id],
                            "qa_queued_message_identity_missing")
                    require(resumed_native.get("session_ref") == reference
                            and resumed_native.get("conversation_id") == first_native.get("conversation_id")
                            and bool(resumed_native.get("conversation_id")), "qa_continuation_provider_changed")
                    require(resumed.get("model") == first.get("model")
                            and resumed.get("reasoning_effort") == first.get("reasoning_effort"),
                            "qa_omitted_model_or_thinking_not_inherited")
                    require(resumed_native.get("tool_calls") == 0
                            and resumed_native.get("scoped_gateway", {}).get("calls") == 0,
                            "qa_queued_continuation_used_tools")
                    await consume(client, resumed_id)
                report.update(queued_instruction_delivered=True, continuation_same_provider=True,
                              omitted_model_thinking_inherited=True, continuation_zero_tools=True)
                progress("queued_continuation_passed")

        else:
            report["question_continuation_skipped"] = True
            progress("question_continuation_skipped")

        if start_at != "reconcile":
            phase = "native_writer"
            progress(phase)
            check = (
                "from pathlib import Path; ns={}; "
                "exec(compile(Path('calculator.py').read_text(), 'calculator.py', 'exec'), ns); "
                "add=ns['add']; assert add(8,5)==13; assert add(-8,5)==-3; "
                "assert add(-8,-5)==-13; assert add(0,0)==0; print('ACP_WRITER_CHECKS_PASSED')"
            )
            verification = "/usr/bin/python3 -B -c " + shlex.quote(check)
            async with asyncio.timeout(180):
                async with Client(service.transport(token, owner)) as client:
                    await select(client)
                    writer_id = await submit(
                        client, cwd=str(writer), kind="code", files_in_scope=["calculator.py"],
                        out_of_scope=[PROOF], commit_mode="forbidden", max_tool_calls=16,
                        verification_commands=[verification], max_changed_files=1,
                        max_added_lines=1, max_deleted_lines=1,
                        acceptance_criteria=["add returns the sum for positive, negative and zero inputs."],
                        done_means=["Only calculator.py is changed; independent declared checks pass."],
                        task=(
                            "Fix the deliberate arithmetic bug in calculator.py using native file-edit tools. "
                            "The final file must contain exactly these two lines and a trailing newline:\n"
                            "def add(left, right):\n"
                            "    return left + right\n"
                            "Change only calculator.py. Preserve all other files. Do not commit, invoke other "
                            "agents or run the declared verification command; the server runs it independently "
                            "after your native process exits. Return the required final JSON when the edit is saved."
                        ),
                    )
                    written = await terminal(client, writer_id, seconds=175)
                    _, writer_native, stored = successful(written, state)
                    code_verification = stored.get("code_verification") or written.get("code_verification") or {}
                    require(code_verification.get("verified") is True
                            and code_verification.get("verification_level") == "checks_passed",
                            "qa_writer_not_independently_verified")
                    require(written.get("acceptance_status") == "checks_passed", "qa_writer_acceptance_status_wrong")
                    receipt = writer_native.get("acp_code_receipt") or {}
                    require(receipt.get("delegate_id") == writer_id
                            and receipt.get("execution_boundary") == "acp_verification_sandbox"
                            and receipt.get("changed_files") == ["calculator.py"],
                            "qa_writer_receipt_invalid")
                    checks = receipt.get("verification_commands")
                    require(isinstance(checks, list) and len(checks) == 1
                            and checks[0].get("command") == verification
                            and checks[0].get("passed") is True and checks[0].get("exit_code") == 0,
                            "qa_writer_isolated_check_missing")
                    require(writer_native.get("promoted_paths") == ["calculator.py"], "qa_writer_export_scope_wrong")
                    require((writer / "calculator.py").read_bytes() == FIXED, "qa_writer_expected_bytes_wrong")
                    require((workspace / "calculator.py").read_bytes() == BUGGY
                            and (workspace / PROOF).read_text().strip() == proof_marker,
                            "qa_writer_changed_main_fixture")
                    require(receipt.get("snapshot_digest") == code_verification.get("post_verification_result_fingerprint"),
                            "qa_writer_canonical_fingerprint_mismatch")
                    await consume(client, writer_id)
                report.update(writer_accepted=True, writer_exact_bytes=True, writer_isolated_checks=True,
                              writer_receipt_verified=True, writer_main_fixture_unchanged=True)
                progress("native_writer_passed")

        else:
            report["native_writer_skipped"] = True
            progress("native_writer_skipped")

        phase = "worker_loss_reconciliation"
        progress(phase)
        async with asyncio.timeout(120):
            async with Client(service.transport(token, owner)) as client:
                await select(client)
                lost_id = await submit(
                    client, execution_timeout_seconds=120, max_tool_calls=1,
                    task=(
                        "Without using tools, prepare a detailed catalogue of 500 distinct software testing "
                        "ideas. Keep every idea in the final JSON summary. Use findings=[] and blockers=[]. "
                        "Do not finish early with an acknowledgement; complete all 500 ideas."
                    ),
                )
                active, lost_ref = await running_turn(client, lost_id, state, acp_state, seconds=75)
                lost_job = active["durable_job_id"]
                jobs.add(lost_job)
                registered = registry.job_status(job_id=lost_job, state_dir=state)
                require(registered.get("status") == "running" and registered.get("pid") == active["pid"],
                        "qa_fault_target_not_own_running_worker")
                killed = registry.kill_job(job_id=lost_job, state_dir=state, signal_name="KILL")
                require(killed.get("success") is True and killed.get("signal_sent") is True,
                        "qa_exact_worker_kill_failed")
                failed = await terminal(client, lost_id, seconds=35)
                require(failed.get("status") in TERMINAL - {"succeeded"}
                        and failed.get("durable_job_id") == lost_job, "qa_worker_loss_terminal_wrong")
                require(registry.job_status(job_id=lost_job, state_dir=state).get("status") != "running",
                        "qa_killed_worker_still_running")
                prior = own_checkpoint(lost_ref, acp_state)
                require(prior.get("phase") == "running" or (prior.get("result") or {}).get("outcome_uncertain") is True,
                        "qa_worker_loss_was_not_uncertain")
                native_history = history_fingerprints(lost_ref, acp_state)
                reconciled = await call(client, "delegate_reconcile", {
                    "delegate_id": lost_id, "action": "abandon",
                    "reason": ("Reviewed this exact disposable QA worker's terminal loss, private checkpoint "
                               "and unchanged read-only fixture. Preserve its uncertain history; do not replay."),
                })
                reconciliation = reconciled.get("reconciliation") or {}
                reconciliation_error = (reconciled.get("error") or {}).get("code")
                if isinstance(reconciliation_error, str) and _SAFE_CODE.fullmatch(reconciliation_error):
                    report["reconciliation_error_code"] = reconciliation_error
                    progress("reconciliation_rejected", error_code=reconciliation_error)
                require(reconciled.get("success") is True and reconciliation.get("status") == "abandoned"
                        and reconciliation.get("resume_allowed") is False
                        and reconciliation.get("requires_new_session") is True,
                        "qa_public_reconciliation_failed")
                abandoned = own_checkpoint(lost_ref, acp_state)
                require(abandoned.get("phase") == "abandoned", "qa_abandoned_checkpoint_missing")
                before_payload = {key: value for key, value in prior.items() if key not in {"phase", "abandonment"}}
                after_payload = {key: value for key, value in abandoned.items() if key not in {"phase", "abandonment"}}
                require(before_payload == after_payload, "qa_abandonment_changed_prior_checkpoint")
                require(history_fingerprints(lost_ref, acp_state) == native_history, "qa_abandonment_changed_native_history")
                frozen = await status(client, lost_id)
                require(frozen.get("durable_job_id") == lost_job
                        and frozen.get("acp_control", {}).get("turn_state") == "abandoned",
                        "qa_abandoned_status_not_frozen")
                require(own_checkpoint(lost_ref, acp_state).get("turns_seen") == prior.get("turns_seen"),
                        "qa_uncertain_turn_replayed")
                require((workspace / "calculator.py").read_bytes() == BUGGY
                        and (writer / "calculator.py").read_bytes() == (BUGGY if start_at == "reconcile" else FIXED),
                        "qa_fault_changed_fixture")
                await consume(client, lost_id)
            report.update(exact_worker_loss=True, public_reconciliation=True,
                          abandoned_reference_frozen=True, native_history_preserved=True, no_replay=True)
            progress("worker_loss_reconciliation_passed")

        report["status"] = "PASS"
        return report
    except BaseException as exc:
        code = str(exc) if isinstance(exc, AcceptanceError) else (
            "qa_phase_timeout" if isinstance(exc, TimeoutError) else "qa_internal_error")
        report.update(status="FAIL", phase=phase, error=code)
        raise
    finally:
        report["cleanup_complete"] = await cleanup(service, token, owner, state, owned, jobs, registry)
        if report.get("status") == "PASS" and not report["cleanup_complete"]:
            report.update(status="FAIL", error="qa_cleanup_incomplete")
        write_private_json(state / "final-acceptance-report.json", report)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path, help="Fresh private QA state; must not exist")
    parser.add_argument("--workspace", required=True, type=Path, help="Already approved root for a fresh disposable Git fixture")
    parser.add_argument("--account", choices=["antigravity3"], default="antigravity3")
    parser.add_argument("--start-at", choices=["question", "writer", "reconcile"], default="question",
                        help="question: all phases; writer: fresh writer then reconciliation; reconcile: recovery only")
    args = parser.parse_args()
    implementation = capture_implementation(Path(__file__).resolve().parents[1])
    try:
        result = asyncio.run(exercise(args.config, args.state, args.workspace, args.account,
                                      start_at=args.start_at, implementation=implementation))
    except BaseException as exc:
        code = str(exc) if isinstance(exc, AcceptanceError) else (
            "qa_phase_timeout" if isinstance(exc, TimeoutError) else "qa_internal_error")
        # Setup may have rejected a preexisting --state. Emit evidence without
        # writing into a directory whose ownership was not established.
        print("ACP_FINAL_ACCEPTANCE_FAIL", json.dumps({
            "status": "FAIL", "error": code, "implementation": implementation,
        }), flush=True)
        return 1
    if result.get("status") != "PASS":
        print("ACP_FINAL_ACCEPTANCE_FAIL", json.dumps(result, sort_keys=True), flush=True)
        return 1
    print("ACP_FINAL_ACCEPTANCE_PASS", json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
