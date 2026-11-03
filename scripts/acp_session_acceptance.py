#!/usr/bin/env python3
"""Explicit operator-only ACP live acceptance, never a default test suite step.

The launch spec is trusted operator configuration for an isolated QA profile.
Do not pass an unsandboxed server or production OAuth profile to this script.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
from pathlib import Path

from chatgpt_web_oauth_mcp.acp_result import parse_acp_worker_output
from chatgpt_web_oauth_mcp.acp_sdk_transport import AcpSdkTransport
from chatgpt_web_oauth_mcp.acp_session import AcpRunLimits, AcpSessionRunner
from chatgpt_web_oauth_mcp.acp_session_store import AcpSessionIdentity, AcpSessionStore


async def exercise(spec_path: Path, state_root: Path) -> None:
    spec = json.loads(spec_path.read_text())
    transport = AcpSdkTransport(
        argv=tuple(spec["argv"]), cwd=Path(spec["cwd"]), environment=spec["environment"],
    )
    identity = AcpSessionIdentity(
        owner="opss-acp-core-qa", account="authorized-acp-qa",
        workspace=str(transport.cwd.resolve()),
        scope_digest=hashlib.sha256(b"no tools; synthetic test only").hexdigest(),
        thinking="low",
    )
    store = AcpSessionStore(state_root)
    runner = AcpSessionRunner(store, transport)
    reference = secrets.token_hex(16)
    marker = "CORE-CONTEXT-" + secrets.token_hex(5).upper()
    first = await runner.run(
        reference=reference, identity=identity, turn_id="remember",
        prompt=f'Remember {marker}. Reply only JSON {{"status":"succeeded","summary":"ACK","findings":[]}}. No tools.',
    )
    print("LIVE_CORE_FIRST", json.dumps({"status": first["status"], "error": first.get("error"), "events": first.get("harness_metadata", {}).get("event_counts")}), flush=True)
    assert first["success"] and "ACK" in first["answer"]
    # The first connection/process is closed. Next run MUST load its session.
    second = await runner.run(
        reference=reference, identity=identity, turn_id="recall",
        prompt='Return only JSON {"status":"succeeded","summary":"<the exact marker from the prior turn>","findings":[]}. Do not use tools.',
    )
    decoded = parse_acp_worker_output(json.dumps({"event": "acp_result", "result": second}), "")
    correct = decoded.error is None and marker in str(decoded.structured_output.get("summary", ""))
    print("LIVE_CORE_RELOAD", json.dumps({"status": second["status"], "error": second.get("error"), "context_correct": correct, "manifest_valid": decoded.error is None, "usage": second.get("usage")}), flush=True)
    assert second["success"] and correct
    assert store.load(reference, identity)["turns_seen"] == ["remember", "recall"]

    cancel = asyncio.Event()
    def progress(update):
        if update.get("event") == "agent_message_chunk":
            cancel.set()
    cancelled = await runner.run(
        reference=reference, identity=identity, turn_id="cancel-test",
        prompt="Read-only cancellation test. Print integers 1 through 2000 one per line. No tools.",
        cancel=cancel, progress=progress,
        limits=AcpRunLimits(timeout_seconds=70, cancel_grace_seconds=8),
    )
    print("LIVE_CORE_CANCEL", json.dumps({"status": cancelled["status"], "error": cancelled.get("error"), "uncertain": cancelled["outcome_uncertain"]}), flush=True)
    assert cancelled["status"] == "cancelled" and not cancelled["outcome_uncertain"]
    assert store.load(reference, identity)["phase"] == "cancelled"
    proof_path = transport.cwd / "core-read-proof.txt"
    proof = "READ-PROOF-" + secrets.token_hex(7).upper()
    proof_path.write_text(proof + "\n")
    proof_path.chmod(0o600)
    reading = AcpSessionIdentity(
        owner="opss-acp-read-qa", account=identity.account,
        workspace=identity.workspace,
        scope_digest=hashlib.sha256(b"read-only:core-read-proof.txt").hexdigest(),
        thinking="low",
    )
    try:
        read_result = await runner.run(
            reference=secrets.token_hex(16), identity=reading, turn_id="read-file",
            prompt='Read ONLY core-read-proof.txt with your file-reading tool. Return JSON {"status":"succeeded","summary":"<the exact marker in the file>","findings":["core-read-proof.txt:1"]}. No shell or modifications.',
        )
        correct = read_result.get("success") is True and proof in read_result.get("answer", "")
        unchanged = proof_path.read_text().strip() == proof
        print("LIVE_CORE_FILE_READ", json.dumps({
            "status": read_result["status"], "error": read_result.get("error"),
            "source_marker_correct": correct, "source_unchanged": unchanged,
            "tool_calls": read_result.get("harness_metadata", {}).get("tool_calls"),
        }), flush=True)
        assert correct and unchanged
        assert read_result["harness_metadata"]["tool_calls"] >= 1
    finally:
        proof_path.unlink(missing_ok=True)
    print("ACP_CORE_LIVE_ACCEPTANCE_PASS", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-spec", required=True, type=Path)
    parser.add_argument("--state-root", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(exercise(args.launch_spec, args.state_root))
