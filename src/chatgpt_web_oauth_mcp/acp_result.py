"""ACP outcome to the existing DelegateProcessRunner result boundary."""
from __future__ import annotations

import hashlib
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
        if finals:
            return ParsedHarnessOutput(error={"code": "acp_worker_protocol_invalid"})
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
    status = result.get("status")
    success = result.get("success")
    if (
        not isinstance(status, str)
        or status not in {"succeeded", "failed", "blocked", "cancelled", "timed_out"}
        or not isinstance(success, bool)
        or success != (status == "succeeded")
        or (success and result.get("outcome_uncertain") is True)
    ):
        return ParsedHarnessOutput(error={"code": "acp_worker_protocol_invalid"})
    if not isinstance(result.get("harness_metadata", {}), dict):
        return ParsedHarnessOutput(error={"code": "acp_worker_protocol_invalid"})
    text = result.get("answer")
    structured = extract_structured_output(text) if isinstance(text, str) else None
    metadata = dict(result.get("harness_metadata") or {})
    metadata["transport"] = "acp"
    metadata["turn_status"] = status
    metadata["session_ref"] = result.get("session_ref")
    metadata["turn_id"] = result.get("turn_id")
    metadata["outcome_uncertain"] = result.get("outcome_uncertain") is True
    metadata["usage"] = result.get("usage") or {}
    if isinstance(result.get("pending_request"), dict):
        metadata["pending_request"] = result["pending_request"]
    error = result.get("error")
    if result.get("success") is not True or result.get("status") != "succeeded" or error:
        code = error.get("code") if isinstance(error, dict) else None
        final_code = code if isinstance(code, str) and code.startswith("acp_") else "acp_turn_failed"
        if final_code == "acp_result_manifest_invalid":
            metadata["manifest_diagnostic"] = _build_manifest_diagnostic(text, structured)
            return ParsedHarnessOutput(
                metadata=metadata,
                error={"code": "acp_result_manifest_invalid"},
                terminal_status=status if status in {"cancelled", "timed_out"} else None,
            )
        return ParsedHarnessOutput(
            structured_output=structured, metadata=metadata,
            error={"code": final_code},
            terminal_status=status if status in {"cancelled", "timed_out"} else None,
        )
    if not isinstance(structured, dict) or _find_schema_violation_path(structured) is not None:
        metadata["manifest_diagnostic"] = _build_manifest_diagnostic(text, structured)
        return ParsedHarnessOutput(metadata=metadata, error={"code": "acp_result_manifest_invalid"})
    # Independent content/scope/test acceptance remains in the existing runner.
    return ParsedHarnessOutput(structured_output=structured, metadata=metadata)


def _find_schema_violation_path(structured: dict) -> str | None:
    status = structured.get("status")
    if not isinstance(status, str) or status not in {"succeeded", "partial", "blocked"}:
        return "status"

    summary = structured.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return "summary"

    if status == "succeeded" and not isinstance(structured.get("findings"), list):
        return "findings"

    if "findings" in structured:
        findings = structured["findings"]
        if not isinstance(findings, list):
            return "findings"
        for i, item in enumerate(findings):
            if not isinstance(item, str):
                return f"findings[{i}]"

    if "blockers" in structured:
        blockers = structured["blockers"]
        if not isinstance(blockers, list):
            return "blockers"
        for i, item in enumerate(blockers):
            if not isinstance(item, str):
                return f"blockers[{i}]"

    return None


def _build_manifest_diagnostic(text: object, structured: object) -> dict[str, object]:
    diagnostic: dict[str, object] = {"version": 1}

    raw_answer_ref: dict[str, object] | None = None
    if isinstance(text, str):
        try:
            raw_bytes = text.encode("utf-8")
            sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
            raw_answer_ref = {
                "source": "logs.stdout",
                "selector": "acp_result.result.answer",
                "sha256": sha256_hex,
                "bytes": len(raw_bytes),
            }
        except UnicodeEncodeError:
            raw_answer_ref = None

    if isinstance(structured, dict):
        path = _find_schema_violation_path(structured)
        if path is not None:
            diagnostic["reason"] = "schema_invalid"
            diagnostic["path"] = path
            if raw_answer_ref is not None:
                diagnostic["raw_answer_ref"] = raw_answer_ref
            return diagnostic

    if isinstance(text, str) and "```" not in text:
        stripped = text.strip()
        if stripped and text.lstrip().startswith(("{", "[")):
            try:
                json.loads(text)
            except json.JSONDecodeError as err:
                diagnostic["reason"] = "json_syntax"
                msg = err.msg[:160]
                diagnostic["msg"] = msg
                diagnostic["pos"] = err.pos
                diagnostic["lineno"] = err.lineno
                diagnostic["colno"] = err.colno
                if raw_answer_ref is not None:
                    diagnostic["raw_answer_ref"] = raw_answer_ref
                return diagnostic
            except (ValueError, RecursionError):
                pass

    diagnostic["reason"] = "invalid_manifest"
    if raw_answer_ref is not None:
        diagnostic["raw_answer_ref"] = raw_answer_ref
    return diagnostic
