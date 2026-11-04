#!/usr/bin/env python3
"""Explicit live managed-writer pilot on the existing isolated MCP QA harness."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from acp_mcp_acceptance import QaServer, call, capture_implementation, prepare, require, select, status, terminal, write_private_json


async def exercise(config, state, workspace_root):
    from fastmcp import Client
    os.umask(0o077)
    workspace, _, marker, token, owner, environment = prepare(config, state, workspace_root, "antigravity3")
    server = QaServer(state=state, environment=environment)
    report = {"implementation": capture_implementation(Path(__file__).resolve().parents[1])}
    owned = []
    target = "mcp-acceptance-proof.txt"

    def writer(name):
        path = workspace / name
        subprocess.run(["git", "-C", str(workspace), "worktree", "add", "--detach", str(path), "HEAD"],
                       check=True, capture_output=True, timeout=15)
        return path

    def check(value):
        return "python3 -c " + shlex.quote("from pathlib import Path; assert Path(" + repr(target) + ").read_text() == " + repr(value))

    async def submit(client, cwd, expected, **overrides):
        arguments = dict(cwd=str(cwd), harness="antigravity3", backend="acp", kind="code",
            files_in_scope=[target], verification_commands=[check(expected)],
            max_changed_files=1, max_added_lines=1, max_deleted_lines=1,
            max_tool_calls=12, execution_timeout_seconds=240, wait_seconds=0, complexity="low")
        arguments.update(overrides)
        result = await call(client, "delegate_task", arguments)
        require(isinstance(result.get("delegate_id"), str), "pilot_submit_failed")
        owned.append(result["delegate_id"])
        return result["delegate_id"]

    try:
        report["qa_pid"] = await server.start()
        async with Client(server.transport(token, owner)) as client:
            await select(client)
            first_workspace = writer("managed")
            expected = marker + "-MANAGED\n"
            identifier = await submit(client, first_workspace, expected, max_tool_calls=16, task=(
                "QA for early progress gates. Read mcp-acceptance-proof.txt exactly four times using native view_file "
                "(intentional repeated reads only for this acceptance). Then invoke scoped supervisor_checkpoint "
                "phase=plan, summary='Ready to add MANAGED suffix'. Wait for controller. After continue, "
                "write the original marker plus -MANAGED and one newline to that file; finish succeeded. "
                "No other exploration, no shell, no tests. Do not repeat the four reads after checkpoint."
            ))
            decisions = []
            deadline = time.monotonic() + 265
            while time.monotonic() < deadline:
                snap = await status(client, identifier)
                if snap.get("completed"):
                    break
                gate = (await call(client, "delegate_supervisor", {"delegate_id": identifier}))["supervision"]
                if gate.get("state") == "paused_for_supervisor":
                    decisions.append({"request": gate.get("request"), "checkpoint": gate["checkpoint"]})
                    await call(client, "delegate_supervisor", {
                        "delegate_id": identifier, "action": "continue",
                        "checkpoint_id": gate["checkpoint"]["id"], "expected_revision": gate["revision"],
                        "command_id": "pilot-continue-" + str(len(decisions)),
                        "instruction": "Proceed with the one suffix edit. No further audit."
                    })
                await asyncio.sleep(0.5)
            final = await terminal(client, identifier, seconds=20)
            write_private_json(state / "managed-result.json", final)
            require(final.get("status") == "succeeded" and final.get("code_verification", {}).get("verified"), "managed_writer_failed")
            require(decisions, "early_checkpoint_missing")
            policy = final.get("harness_metadata", {}).get("progress_checkpoints", {})
            require("early_review" in policy.get("issued", []), "automatic_early_signal_missing")
            require((first_workspace / target).read_text() == expected, "managed_promotion_missing")
            report["managed"] = {"delegate_id": identifier, "policy": policy, "decisions": len(decisions)}
            write_private_json(state / "managed-checkpoints.json", decisions)

            recovery_workspace = writer("recovery")
            completed = marker + "-RECOVERED\n"
            failed = await submit(client, recovery_workspace, completed, task=(
                "Intentional unaccepted-draft QA: change mcp-acceptance-proof.txt to its original marker "
                "plus -DRAFT and one newline. Then finish with structured status=blocked and blocker "
                "'Intentional QA handoff after draft edit'. Do not claim success, run tests, "
                "or call checkpoint. Only this one edit."
            ))
            failed_result = await terminal(client, failed, seconds=265)
            write_private_json(state / "failed-writer-result.json", failed_result)
            require(failed_result.get("status") == "failed", "intentional_failure_missing")
            require((recovery_workspace / target).read_text() == marker + "\n", "failed_candidate_promoted")
            drafts = await call(client, "delegate_draft", {"delegate_id": failed})
            write_private_json(state / "retained-drafts.json", drafts)
            saved = failed_result.get("harness_metadata", {}).get("retained_draft")
            require(saved and saved.get("accepted") is False, "automatic_failed_draft_missing")
            require(any(d.get("snapshot_id") == saved["snapshot_id"] for d in drafts.get("drafts", [])), "draft_not_discoverable")
            require(not failed_result.get("harness_metadata", {}).get("outcome_uncertain"), "unexpected_uncertain_failure")
            resumed = await submit(client, recovery_workspace, completed,
                draft_from_delegate_id=failed, draft_snapshot_id=saved["snapshot_id"],
                task="The saved unaccepted draft already contains the original marker plus -DRAFT. Read it once, replace only suffix -DRAFT with -RECOVERED, retain newline, finish succeeded. No further audit.")
            recovered = await terminal(client, resumed, seconds=265)
            write_private_json(state / "recovered-writer-result.json", recovered)
            require(recovered.get("status") == "succeeded" and recovered.get("code_verification", {}).get("verified"), "draft_recovery_failed")
            require((recovery_workspace / target).read_text() == completed, "recovered_promotion_missing")
            require(recovered.get("harness_metadata", {}).get("restored_draft", {}).get("snapshot_id") == saved["snapshot_id"], "wrong_draft_restored")
            report["recovery"] = {"failed_delegate": failed, "snapshot_id": saved["snapshot_id"], "recovered_delegate": resumed}
            for did in owned:
                await call(client, "mark_result_consumed", {"kind": "delegate", "result_id": did})
        report["status"] = "passed"
        write_private_json(state / "report.json", report)
        print(json.dumps(report, sort_keys=True), flush=True)
    finally:
        if report.get("status") != "passed":
            try:
                async with Client(server.transport(token, owner)) as cleanup:
                    await select(cleanup)
                    for did in owned:
                        snap = await status(cleanup, did)
                        if snap.get("in_progress"):
                            await call(cleanup, "delegate_cancel", {"delegate_id": did, "force_running": True,
                                                                  "rationale": "Isolated managed pilot cleanup"})
            except Exception:
                report["cleanup"] = "inspect_disposable_state"
            write_private_json(state / "failed-report.json", report)
        await server.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(exercise(args.config, args.state.resolve(), args.workspace_root))
