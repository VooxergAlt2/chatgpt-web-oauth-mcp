#!/usr/bin/env python3
"""Live R1 acceptance on a disposable MCP server and Git fixture.

Uses the existing ACP QA harness; never changes production config or kills
a production PID. Explicit invocation only; no provider calls on import.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

from acp_mcp_acceptance import (
    QaServer, call, capture_implementation, prepare, require, select, terminal,
    write_private_json,
)


def unwrap(result):
    require(result.get("success") is not False, "r1_packet_failed")
    packet = result.get("packet", result)
    require(isinstance(packet, dict), "r1_packet_invalid")
    return packet


async def exercise(config, state, workspace_root):
    from fastmcp import Client

    os.umask(0o077)
    workspace, _, marker, token, owner, environment = prepare(
        config, state, workspace_root, "antigravity3")
    service = QaServer(state=state, environment=environment)
    report = {"implementation": capture_implementation(Path(__file__).resolve().parents[1])}
    owned = []
    base = {
        "cwd": str(workspace), "harness": "antigravity3", "backend": "acp",
        "kind": "explore", "files_in_scope": ["mcp-acceptance-proof.txt"],
        "max_tool_calls": 12, "execution_timeout_seconds": 180,
        "wait_seconds": 0, "complexity": "low",
    }

    async def submit(client, **changes):
        result = await call(client, "delegate_task", {**base, **changes})
        require(isinstance(result.get("delegate_id"), str), "r1_submit_failed")
        owned.append(result["delegate_id"])
        return result["delegate_id"]

    try:
        report["pid_before"] = await service.start()
        async with Client(service.transport(token, owner)) as client:
            await select(client)
            identifier = await submit(client, task=(
                "This is an intentional diagnostics acceptance scenario. Use the scoped Opss "
                "read_text tool to read mcp-acceptance-proof.txt with start_line=1 and "
                "line_limit=1 exactly four separate times with identical arguments. "
                "Do not use native reads or shell. Report the exact marker as summary "
                "and cite mcp-acceptance-proof.txt:1. These repetitions are requested QA, "
                "not an invitation to continue repeating. Then finish."
            ))
            final = await terminal(client, identifier, seconds=210)
            require(final.get("status") == "succeeded", "r1_reader_failed")
            require(marker in json.dumps(final.get("structured_output")), "r1_marker_missing")
            packet = unwrap(await call(client, "delegate_packet", {
                "delegate_id": identifier, "purpose": "review"}))
            handoff = unwrap(await call(client, "delegate_packet", {
                "delegate_id": identifier, "purpose": "handoff"}))
            write_private_json(state / "reader-packet.json", packet)
            write_private_json(state / "reader-handoff.json", handoff)
            write_private_json(state / "reader-result.json", final)
            report["reader_delegate"] = identifier
            report["reader_packet_sha256"] = hashlib.sha256(
                json.dumps(packet, sort_keys=True).encode()).hexdigest()
            report["gateway"] = final.get("harness_metadata", {}).get("scoped_gateway")
            diagnostics = (report["gateway"] or {}).get("diagnostics", {})
            require(diagnostics.get("total_calls", 0) >= 4, "r1_gateway_reads_missing")
            require(diagnostics.get("same_request_response_repeats", 0) >= 3,
                    "r1_repeated_calls_not_observed")
            require(diagnostics.get("native_visibility") == "incomplete", "r1_visibility_overclaimed")
            require(packet.get("reuse_eligible") is False, "r1_unproven_reuse")
            info = await call(client, "delegate_harnesses", {})
            write_private_json(state / "harness-health.json", info)
            health = info["harnesses"]["antigravity3"]["execution_backends"]["acp"]["health"]
            require(health["state"] == "healthy", "r1_observed_acp_health_missing")
            report["health_observed_at"] = health["observed_at_epoch"]

        async with Client(service.transport(token, owner + "-foreign")) as foreign:
            await select(foreign)
            denied = await call(foreign, "delegate_packet", {
                "delegate_id": identifier, "purpose": "review"})
            require(denied.get("success") is False, "r1_foreign_packet_not_denied")
            report["foreign_owner_denied"] = True

        # No worker is active here. Verify durable reads across a real restart.
        await service.stop()
        report["pid_after"] = await service.start()
        require(report["pid_before"] != report["pid_after"], "r1_restart_pid_unchanged")
        async with Client(service.transport(token, owner)) as client:
            await select(client)
            recovered = unwrap(await call(client, "delegate_packet", {
                "delegate_id": identifier, "purpose": "handoff"}))
            write_private_json(state / "recovered-handoff.json", recovered)
            require(recovered == handoff, "r1_recovered_evidence_changed")
            restored_info = await call(client, "delegate_harnesses", {})
            restored_health = restored_info["harnesses"]["antigravity3"]["execution_backends"]["acp"]["health"]
            require(restored_health["observed_at_epoch"] == report["health_observed_at"],
                    "r1_health_replay_refreshed_or_lost")
            followup = await submit(client, resume_from_delegate_id=identifier, task=(
                "Continue the completed QA turn. Read mcp-acceptance-proof.txt once with "
                "the scoped gateway and report its marker. Do not repeat the earlier "
                "four-read test. Do not run shell or edit files."
            ))
            continued = await terminal(client, followup, seconds=210)
            require(continued.get("status") == "succeeded", "r1_continuation_failed")
            require(marker in json.dumps(continued.get("structured_output")), "r1_resume_marker_missing")
            require(continued.get("harness") == "antigravity3", "r1_account_changed")
            require(continued.get("execution_backend") == "acp", "r1_transport_changed")
            report["continuation_delegate"] = followup
            write_private_json(state / "continuation-result.json", continued)

            writer = workspace / "writer"
            process = subprocess.run(
                ["git", "-C", str(workspace), "worktree", "add", "--detach", str(writer), "HEAD"],
                capture_output=True, timeout=15)
            require(process.returncode == 0, "r1_writer_fixture_failed")
            expected = marker + "-R1\n"
            command = (
                "python3 -c " + __import__("shlex").quote(
                    "from pathlib import Path; assert Path('mcp-acceptance-proof.txt').read_text() == "
                    + repr(expected))
            )
            code_id = await submit(
                client, cwd=str(writer), kind="code",
                task="Change mcp-acceptance-proof.txt to the same marker with suffix -R1 followed by one newline. No other edits. Finish.",
                verification_commands=[command], max_changed_files=1,
                max_added_lines=1, max_deleted_lines=1, execution_timeout_seconds=240)
            code = await terminal(client, code_id, seconds=270)
            require(code.get("status") == "succeeded", "r1_writer_failed")
            require(code.get("code_verification", {}).get("verified") is True, "r1_writer_not_verified")
            require((writer / "mcp-acceptance-proof.txt").read_text() == expected, "r1_promotion_missing")
            code_packet = unwrap(await call(client, "delegate_packet", {
                "delegate_id": code_id, "purpose": "review"}))
            write_private_json(state / "writer-packet.json", code_packet)
            write_private_json(state / "writer-result.json", code)
            observations = code_packet["server_observations"]
            require(observations["source_freshness"]["state"] == "current", "r1_current_receipt_mismatch")
            require(observations["review_evidence"].get("available") is True, "r1_diff_missing")
            require("+" + marker + "-R1" in observations["review_evidence"].get("diff", ""), "r1_diff_wrong")
            (writer / "mcp-acceptance-proof.txt").write_text(expected + "changed\n")
            stale_packet = unwrap(await call(client, "delegate_packet", {
                "delegate_id": code_id, "purpose": "review"}))
            write_private_json(state / "stale-writer-packet.json", stale_packet)
            require(stale_packet["server_observations"]["source_freshness"]["state"] == "stale",
                    "r1_stale_candidate_not_observed")
            require(stale_packet["server_observations"]["verification"]["historical_verified"] is True,
                    "r1_historical_evidence_lost")
            require(stale_packet["reuse_eligible"] is False, "r1_stale_reuse")
            report["writer_delegate"] = code_id
            report["writer_verified"] = True
            report["source_drift_observed"] = True
            for did in owned:
                await call(client, "mark_result_consumed", {"kind": "delegate", "result_id": did})
        report["status"] = "passed"
        write_private_json(state / "report.json", report)
        print(json.dumps(report, sort_keys=True), flush=True)
        return report
    finally:
        if report.get("status") != "passed":
            try:
                async with Client(service.transport(token, owner)) as cleanup:
                    await select(cleanup)
                    for did in owned:
                        status = await call(cleanup, "delegate_status", {"delegate_id": did})
                        if status.get("delegate", {}).get("in_progress"):
                            await call(cleanup, "delegate_cancel", {"delegate_id": did,
                                "force_running": True, "rationale": "R1 isolated acceptance cleanup"})
            except (OSError, RuntimeError, ValueError):
                report["cleanup"] = "inspect_disposable_state"
            write_private_json(state / "failed-report.json", report)
        await service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(exercise(args.config, args.state.resolve(), args.workspace_root))


if __name__ == "__main__":
    main()
