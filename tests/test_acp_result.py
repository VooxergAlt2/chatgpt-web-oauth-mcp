import json

import pytest

from chatgpt_web_oauth_mcp.acp_result import parse_acp_worker_output
from chatgpt_web_oauth_mcp.acp_sdk_transport import AcpSdkTransport


def envelope(**overrides):
    result = {
        "status": "succeeded", "success": True,
        "answer": json.dumps({"status": "succeeded", "summary": "review", "findings": []}),
        "session_ref": "a" * 32, "turn_id": "t1",
        "harness_metadata": {"conversation_id": "session", "tool_calls": 1},
    }
    result.update(overrides)
    return json.dumps({"event": "acp_result", "result": result})


def test_successful_manifest_uses_existing_acceptance_boundary():
    parsed = parse_acp_worker_output(envelope(), "")
    assert parsed.error is None
    assert parsed.structured_output["status"] == "succeeded"
    assert parsed.metadata["transport"] == "acp"
    assert "accepted" not in parsed.metadata


def test_advisory_success_cannot_override_failed_transport():
    parsed = parse_acp_worker_output(envelope(
        status="failed", success=False, outcome_uncertain=True,
        error={"code": "acp_transport_error"},
    ), "sensitive text must not be returned")
    assert parsed.error["code"] == "acp_transport_error"
    assert parsed.structured_output["status"] == "succeeded"
    assert parsed.metadata["outcome_uncertain"] is True
    assert "sensitive" not in str(parsed)


@pytest.mark.parametrize("raw", ["", "not json", "[]", envelope() + "\n" + envelope()])
def test_missing_malformed_and_duplicate_envelopes_fail(raw):
    assert parse_acp_worker_output(raw, "").error is not None


def test_partial_keeps_partial_semantics():
    parsed = parse_acp_worker_output(envelope(answer='{"status":"partial","summary":"some evidence"}'), "")
    assert parsed.error is None
    assert parsed.structured_output["status"] == "partial"


def test_question_remains_blocked_and_visible():
    parsed = parse_acp_worker_output(envelope(
        status="blocked", success=False, error={"code": "acp_input_required"},
        pending_request={"kind": "question", "title": "Need scope", "options": []},
    ), "")
    assert parsed.error["code"] == "acp_input_required"
    assert parsed.metadata["pending_request"]["kind"] == "question"


def test_transport_rejects_api_key_environment(tmp_path):
    with pytest.raises(ValueError, match="Unexpected"):
        AcpSdkTransport(argv=("/usr/bin/true",), cwd=tmp_path, environment={"GEMINI_API_KEY": "test"})


def test_transport_does_not_display_or_alias_environment(tmp_path):
    env = {"HOME": "/qa-home", "GEMINI_HOME": "/qa-auth", "PATH": "/usr/bin:/bin"}
    transport = AcpSdkTransport(argv=("/usr/bin/true",), cwd=tmp_path, environment=env)
    env["GEMINI_HOME"] = "/other"
    assert transport.environment["GEMINI_HOME"] == "/qa-auth"
    assert "/qa-auth" not in repr(transport)
