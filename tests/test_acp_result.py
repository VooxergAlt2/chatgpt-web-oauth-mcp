import json
import sys

import pytest

from chatgpt_web_oauth_mcp.acp_result import parse_acp_worker_output
from chatgpt_web_oauth_mcp.acp_sdk_transport import AcpSdkTransport
from chatgpt_web_oauth_mcp.delegate_process import extract_structured_output


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


@pytest.mark.parametrize("example", ["```123```", "```null```", "```[]```", "```false```"])
def test_whole_json_manifest_preserves_fenced_literals_in_claims(example):
    manifest = {"status": "succeeded", "summary": "Reviewed source example " + example,
                "findings": ["Literal text remains advisory " + example]}
    answer = json.dumps(manifest)

    assert extract_structured_output(answer) == manifest
    parsed = parse_acp_worker_output(envelope(answer=answer), "")
    assert parsed.error is None
    assert parsed.structured_output == manifest


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "Review complete.\n```json\n{}\n```"])
def test_plain_and_final_fenced_manifests_still_parse(wrapper):
    manifest = {"status": "succeeded", "summary": "review", "findings": []}
    answer = wrapper.format(json.dumps(manifest))
    assert extract_structured_output(answer) == manifest
    parsed = parse_acp_worker_output(envelope(answer=answer), "")
    assert parsed.error is None
    assert parsed.structured_output == manifest


@pytest.mark.parametrize("prefix", ["", "Review complete.\n", "Earlier example:\n```json\n[1,2]\n```\nFinal report:\n"])
def test_final_fenced_manifest_preserves_literal_fences_inside_json_strings(prefix):
    manifest = {"status": "succeeded", "summary": "Literal source: ```json [1,2]```",
                "findings": []}
    answer = prefix + "```json\n" + json.dumps(manifest) + "\n```"

    assert extract_structured_output(answer) == manifest
    parsed = parse_acp_worker_output(envelope(answer=answer), "")
    assert parsed.error is None
    assert parsed.structured_output == manifest


def test_oversized_integer_in_noisy_output_does_not_hide_a_later_manifest():
    digit_limit = sys.get_int_max_str_digits()
    if not digit_limit:
        pytest.skip("Integer string limit disabled")
    oversized = "9" * (digit_limit + 1)
    answer = oversized + "\n```json\n" + oversized + "\n```\n```json\n123\n```"
    assert extract_structured_output(answer) == 123


def test_invalid_final_fenced_manifest_does_not_fall_back_to_earlier_valid_block():
    manifest = {"status": "succeeded", "summary": "earlier example", "findings": []}
    answer = "```json\n" + json.dumps(manifest) + "\n```\nFinal report:\n```json\n{broken\n```"

    assert extract_structured_output(answer) is None
    assert parse_acp_worker_output(envelope(answer=answer), "").error["code"] == "acp_result_manifest_invalid"


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


@pytest.mark.parametrize("status,code", [
    ("timed_out", "acp_timeout"), ("cancelled", "acp_cancelled"),
])
def test_adapter_preserves_typed_terminal_outcome(status, code):
    parsed = parse_acp_worker_output(envelope(
        status=status, success=False, error={"code": code},
    ), "")
    assert parsed.terminal_status == status
    assert parsed.metadata["turn_status"] == status
    assert parsed.error["code"] == code


@pytest.mark.parametrize("changes", [
    {"status": "succeeded", "success": False},
    {"status": "failed", "success": True},
    {"status": []}, {"success": 1}, {"outcome_uncertain": True},
])
def test_inconsistent_worker_outcome_is_never_success(changes):
    assert parse_acp_worker_output(envelope(**changes), "").error is not None


def test_progress_after_final_is_not_an_accepted_worker_stream():
    raw = envelope() + '\n' + json.dumps({"event": "acp_progress", "progress": {}})
    assert parse_acp_worker_output(raw, "").error["code"] == "acp_worker_protocol_invalid"


@pytest.mark.parametrize("manifest", [
    {"status": "succeeded"},
    {"status": "succeeded", "summary": "", "findings": []},
    {"status": "succeeded", "summary": "review", "findings": "not a list"},
    {"status": "succeeded", "summary": "review", "findings": [{}]},
    {"status": "succeeded", "summary": "review", "findings": [], "blockers": False},
    {"status": [], "summary": "review"},
])
def test_empty_or_malformed_manifest_does_not_complete_research(manifest):
    parsed = parse_acp_worker_output(envelope(answer=json.dumps(manifest)), "")
    assert parsed.error["code"] == "acp_result_manifest_invalid"
