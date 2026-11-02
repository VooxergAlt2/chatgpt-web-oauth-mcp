"""ACP outcome to the existing DelegateProcessRunner result boundary."""
from __future__ import annotations

import json

from .delegate_process import ParsedHarnessOutput, extract_structured_output


def parse_acp_worker_output(stdout: str, stderr: str) -> ParsedHarnessOutput:
    """Read only the server-owned final envelope, never infer CLI success.

    Native ACP messages are handled by the SDK, not emitted on worker stdout.
    This envelope is the internal adapter-to-durable-runner contract, not a
    replacement JSON-RPC implementation.
    """
    if len(stdout.encode("utf-8")) > 2 * 1024 * 1024:
        return ParsedHarnessOutput(error={"code": "acp_worker_output_too_large"})
    finals: list[dict] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return ParsedHarnessOutput(error={"code": "acp_worker_protocol_invalid"})
        if not isinstance(event, dict):
            return ParsedHarnessOutput(error={"code": "acp_worker_protocol_invalid"})
        if event.get("event") == "acp_result":
            finals.append(event)
        elif event.get("event") != "acp_progress":
            return ParsedHarnessOutput(error={"code": "acp_worker_protocol_invalid"})
    if len(finals) != 1 or not isinstance(finals[0].get("result"), dict):
        return ParsedHarnessOutput(error={"code": "acp_worker_result_missing_or_duplicate"})
    result = finals[0]["result"]
    if not isinstance(result.get("harness_metadata", {}), dict):
        return ParsedHarnessOutput(error={"code": "acp_worker_protocol_invalid"})
    text = result.get("answer")
    structured = extract_structured_output(text) if isinstance(text, str) else None
    metadata = dict(result.get("harness_metadata") or {})
    metadata["transport"] = "acp"
    metadata["session_ref"] = result.get("session_ref")
    metadata["turn_id"] = result.get("turn_id")
    metadata["outcome_uncertain"] = result.get("outcome_uncertain") is True
    metadata["usage"] = result.get("usage") or {}
    if isinstance(result.get("pending_request"), dict):
        metadata["pending_request"] = result["pending_request"]
    error = result.get("error")
    if result.get("success") is not True or result.get("status") != "succeeded" or error:
        code = error.get("code") if isinstance(error, dict) else None
        return ParsedHarnessOutput(
            structured_output=structured, metadata=metadata,
            error={"code": code if isinstance(code, str) and code.startswith("acp_") else "acp_turn_failed"},
        )
    if not isinstance(structured, dict) or structured.get("status") not in {"succeeded", "partial", "blocked"}:
        return ParsedHarnessOutput(metadata=metadata, error={"code": "acp_result_manifest_invalid"})
    # Independent content/scope/test acceptance remains in the existing runner.
    return ParsedHarnessOutput(structured_output=structured, metadata=metadata)
