"""Bounded client for the gateway-owned Windows execution journal."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from fastmcp import Client
from mcp.types import CallToolResult

PROTOCOL_VERSION = 1
MAX_POLL_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.25

UNKNOWN_ERROR_CODES = frozenset({
    "TIMEOUT", "UPSTREAM_ERROR", "RESULT_OVERFLOW", "GATEWAY_RESTARTED",
    "TASK_CANCELLED", "JOURNAL_WRITE_FAILED", "JOURNAL_WORKER_FAILED",
    "NOT_DISPATCHED",
})


def unknown_operation_diagnostic(receipt: dict) -> dict[str, str]:
    """Expose only stable reason codes, never arbitrary upstream error text."""
    code = receipt.get("error_code")
    if not isinstance(code, str) or code not in UNKNOWN_ERROR_CODES:
        code = "UNKNOWN_OUTCOME"
    action = "operator_inspection_required"
    if receipt.get("execution_status") == "finished":
        action = "retry_as_new_request_if_needed" if code == "NOT_DISPATCHED" else "verify_outcome_before_retry"
    return {"error_code": code, "next_action": action}


def compute_request_digest(name: str, arguments: dict[str, Any]) -> str:
    raw = json.dumps({"name": name, "arguments": arguments}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_operation_receipt(receipt: Any, operation_id: str, digest: str) -> dict:
    if not isinstance(receipt, dict):
        raise ValueError("INVALID_OPERATION_RECEIPT")
    if type(receipt.get("protocol_version")) is not int or receipt["protocol_version"] != 1:
        raise ValueError("UNSUPPORTED_PROTOCOL_VERSION")
    if receipt.get("operation_id") != operation_id:
        raise ValueError("OPERATION_ID_MISMATCH")
    if receipt.get("request_digest") != digest:
        raise ValueError("REQUEST_DIGEST_MISMATCH")
    if receipt.get("state") not in {"running", "completed", "unknown"}:
        raise ValueError("INVALID_OPERATION_STATE")
    execution = receipt.get("execution_status", "unconfirmed")
    if not isinstance(execution, str) or execution not in {"running", "finished", "unconfirmed"}:
        raise ValueError("INVALID_EXECUTION_STATUS")
    if ((receipt["state"] == "running" and execution == "finished")
            or (receipt["state"] == "completed" and execution == "running")):
        raise ValueError("INCONSISTENT_EXECUTION_STATUS")
    if receipt["state"] == "completed":
        result = receipt.get("result")
        if (not isinstance(result, dict) or not isinstance(result.get("content"), list)
                or result.get("isError") is not False
                or result.get("resultType", "complete") != "complete"):
            raise ValueError("INVALID_TERMINAL_RESULT")
        try:
            CallToolResult.model_validate(result, strict=True)
        except Exception:
            raise ValueError("INVALID_TERMINAL_RESULT") from None
    return receipt


def extract_receipt(result: Any) -> dict:
    if result.is_error:
        raise ValueError("WINDOWS_OPERATION_STATUS_UNAVAILABLE")
    if isinstance(result.structured_content, dict):
        return result.structured_content
    if len(result.content) == 1 and result.content[0].type == "text":
        try:
            value = json.loads(result.content[0].text)
        except json.JSONDecodeError:
            value = None
        if isinstance(value, dict):
            return value
    raise ValueError("MISSING_STRUCTURED_RECEIPT")


class WindowsOperationClient:
    def __init__(self, client: Client):
        self.client = client

    async def submit(self, operation_id: str, name: str, arguments: dict,
                     expected_digest: str) -> dict:
        result = await self.client.call_tool(
            "windows_operation_submit",
            {"operation_id": operation_id, "name": name, "arguments": arguments},
            timeout=5, raise_on_error=False)
        return validate_operation_receipt(extract_receipt(result), operation_id, expected_digest)

    async def status(self, operation_id: str, expected_digest: str) -> dict:
        result = await self.client.call_tool(
            "windows_operation_status", {"operation_id": operation_id},
            timeout=5, raise_on_error=False)
        return validate_operation_receipt(extract_receipt(result), operation_id, expected_digest)

    async def reconcile(self, operation_id: str, expected_digest: str) -> dict:
        result = await self.client.call_tool(
            "windows_operation_reconcile",
            {"operation_id": operation_id, "request_digest": expected_digest},
            timeout=5, raise_on_error=False)
        return validate_operation_receipt(extract_receipt(result), operation_id, expected_digest)

    async def submit_and_poll(self, operation_id: str, name: str, arguments: dict,
                              expected_digest: str, *,
                              poll_timeout: float = MAX_POLL_TIMEOUT_SECONDS) -> dict:
        try:
            receipt = await self.submit(operation_id, name, arguments, expected_digest)
        except Exception:
            # Never resend an uncertain action. Reconcile/cancel its admission
            # atomically; a late delivery cannot execute past the tombstone.
            receipt = await self.reconcile(operation_id, expected_digest)
        if receipt["state"] != "running":
            return receipt
        try:
            async with asyncio.timeout(poll_timeout):
                while True:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    receipt = await self.status(operation_id, expected_digest)
                    if receipt["state"] != "running":
                        return receipt
        except TimeoutError:
            # The gateway task continues; return its last confirmed state.
            return receipt
