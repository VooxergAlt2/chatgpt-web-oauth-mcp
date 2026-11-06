"""Unittest regression suite for bounded ACP manifest diagnostics."""
from __future__ import annotations

import hashlib
import json
import unittest

from chatgpt_web_oauth_mcp.acp_result import parse_acp_worker_output


def envelope(**overrides) -> str:
    result = {
        "status": "succeeded",
        "success": True,
        "answer": json.dumps({"status": "succeeded", "summary": "review", "findings": []}),
        "session_ref": "a" * 32,
        "turn_id": "t1",
        "harness_metadata": {"conversation_id": "session", "tool_calls": 1},
    }
    result.update(overrides)
    return json.dumps({"event": "acp_result", "result": result})


class TestAcpManifestDiagnostics(unittest.TestCase):
    def test_syntax_coordinates_original_whitespace_and_unicode(self):
        # Leading whitespace and newline followed by unicode with a syntax error
        raw = "  \n  {\"status\": \"succeeded\", \"tag\": \"\U0001F600\", \"summary\": \"review\", \"findings\": [\"a\" \"b\"]}"
        parsed = parse_acp_worker_output(envelope(answer=raw), "")
        self.assertIsNotNone(parsed.error)
        self.assertEqual(parsed.error["code"], "acp_result_manifest_invalid")
        self.assertIsNone(parsed.structured_output)

        diag = parsed.metadata.get("manifest_diagnostic")
        self.assertIsInstance(diag, dict)
        self.assertEqual(diag["version"], 1)
        self.assertEqual(diag["reason"], "json_syntax")
        self.assertEqual(diag["lineno"], 2)
        self.assertGreater(diag["colno"], 0)
        self.assertGreater(diag["pos"], 0)
        self.assertLessEqual(len(diag["msg"]), 160)

        # Exact known failure fixture check
        fixture = '{"status":"succeeded","summary":"review","findings":["claim" interpretation="audit"]}'
        parsed_fixture = parse_acp_worker_output(envelope(answer=fixture), "")
        self.assertEqual(parsed_fixture.error["code"], "acp_result_manifest_invalid")
        self.assertIsNone(parsed_fixture.structured_output)

        diag_fix = parsed_fixture.metadata.get("manifest_diagnostic")
        self.assertIsInstance(diag_fix, dict)
        self.assertEqual(diag_fix["version"], 1)
        self.assertEqual(diag_fix["reason"], "json_syntax")
        self.assertEqual(diag_fix["pos"], 61)
        self.assertEqual(diag_fix["lineno"], 1)
        self.assertEqual(diag_fix["colno"], 62)
        self.assertNotIn("message", diag_fix)

    def test_rawref_digest_and_bytes(self):
        answer = '{"status": "succeeded", "summary": "Unicode: \u2705 \U0001f389", "findings": [invalid'
        raw_bytes = answer.encode("utf-8")
        expected_sha = hashlib.sha256(raw_bytes).hexdigest()
        expected_len = len(raw_bytes)

        parsed = parse_acp_worker_output(envelope(answer=answer), "")
        diag = parsed.metadata.get("manifest_diagnostic")
        self.assertIsInstance(diag, dict)

        raw_ref = diag.get("raw_answer_ref")
        self.assertIsInstance(raw_ref, dict)
        self.assertEqual(raw_ref["source"], "logs.stdout")
        self.assertEqual(raw_ref["selector"], "acp_result.result.answer")
        self.assertEqual(raw_ref["sha256"], expected_sha)
        self.assertEqual(raw_ref["bytes"], expected_len)
        self.assertNotIn("digest", raw_ref)
        self.assertNotIn("byte_length", raw_ref)

        # When answer is None or missing, raw_answer_ref must be absent
        parsed_no_answer = parse_acp_worker_output(envelope(answer=None), "")
        diag_no_answer = parsed_no_answer.metadata.get("manifest_diagnostic")
        self.assertIsInstance(diag_no_answer, dict)
        self.assertEqual(diag_no_answer["version"], 1)
        self.assertEqual(diag_no_answer["reason"], "invalid_manifest")
        self.assertNotIn("raw_answer_ref", diag_no_answer)

    def test_no_raw_text_in_metadata(self):
        secret = "SENSITIVE_SECRET_ADVISORY_CLAIM_DATA_12345"
        raw = f'{{"status": "succeeded", "summary": "review", "findings": ["{secret}" "broken"]}}'
        parsed = parse_acp_worker_output(envelope(answer=raw), "")
        metadata_str = json.dumps(parsed.metadata)
        self.assertNotIn(secret, metadata_str)
        self.assertNotIn("findings", parsed.metadata["manifest_diagnostic"].get("msg", ""))
        self.assertNotIn("raw_answer", parsed.metadata["manifest_diagnostic"])

    def test_required_fields_and_typed_lists(self):
        cases = [
            ({"summary": "s"}, "status"),
            ({"status": "invalid_status", "summary": "s"}, "status"),
            ({"status": "succeeded", "findings": []}, "summary"),
            ({"status": "succeeded", "summary": "   ", "findings": []}, "summary"),
            ({"status": "succeeded", "summary": "s"}, "findings"),
            ({"status": "partial", "summary": "s", "findings": "not_a_list"}, "findings"),
            ({"status": "succeeded", "summary": "s", "findings": [], "blockers": False}, "blockers"),
        ]
        for manifest, expected_path in cases:
            with self.subTest(manifest=manifest, expected_path=expected_path):
                parsed = parse_acp_worker_output(envelope(answer=json.dumps(manifest)), "")
                self.assertEqual(parsed.error["code"], "acp_result_manifest_invalid")
                self.assertIsNone(parsed.structured_output)
                diag = parsed.metadata.get("manifest_diagnostic")
                self.assertIsInstance(diag, dict)
                self.assertEqual(diag["version"], 1)
                self.assertEqual(diag["reason"], "schema_invalid")
                self.assertEqual(diag["path"], expected_path)

    def test_bounded_invalid_index(self):
        cases = [
            ({"status": "succeeded", "summary": "s", "findings": [123]}, "findings[0]"),
            ({"status": "succeeded", "summary": "s", "findings": ["ok", "ok2", None]}, "findings[2]"),
            ({"status": "partial", "summary": "s", "blockers": [{}]}, "blockers[0]"),
            ({"status": "partial", "summary": "s", "blockers": ["b0", "b1", 456]}, "blockers[2]"),
        ]
        for manifest, expected_path in cases:
            with self.subTest(manifest=manifest, expected_path=expected_path):
                parsed = parse_acp_worker_output(envelope(answer=json.dumps(manifest)), "")
                self.assertEqual(parsed.error["code"], "acp_result_manifest_invalid")
                self.assertIsNone(parsed.structured_output)
                diag = parsed.metadata.get("manifest_diagnostic")
                self.assertIsInstance(diag, dict)
                self.assertEqual(diag["reason"], "schema_invalid")
                self.assertEqual(diag["path"], expected_path)
                self.assertNotIn("field", diag)

    def test_good_result_unchanged(self):
        valid = {"status": "succeeded", "summary": "All good", "findings": ["verified"]}
        parsed = parse_acp_worker_output(envelope(answer=json.dumps(valid)), "")
        self.assertIsNone(parsed.error)
        self.assertEqual(parsed.structured_output, valid)
        self.assertNotIn("manifest_diagnostic", parsed.metadata)

        valid_partial = {"status": "partial", "summary": "Partial work", "blockers": ["blocked on X"]}
        parsed_partial = parse_acp_worker_output(envelope(answer=json.dumps(valid_partial)), "")
        self.assertIsNone(parsed_partial.error)
        self.assertEqual(parsed_partial.structured_output, valid_partial)
        self.assertNotIn("manifest_diagnostic", parsed_partial.metadata)

    def test_malformed_last_fenced_block_does_not_fall_back(self):
        valid = {"status": "succeeded", "summary": "earlier example", "findings": []}
        answer = f"```json\n{json.dumps(valid)}\n```\nFinal report:\n```json\n{{broken\n```"
        parsed = parse_acp_worker_output(envelope(answer=answer), "")
        self.assertEqual(parsed.error["code"], "acp_result_manifest_invalid")
        self.assertIsNone(parsed.structured_output)

        diag = parsed.metadata.get("manifest_diagnostic")
        self.assertIsInstance(diag, dict)
        self.assertEqual(diag["version"], 1)
        self.assertEqual(diag["reason"], "invalid_manifest")
        self.assertNotIn("pos", diag)
        self.assertNotIn("lineno", diag)
        self.assertNotIn("colno", diag)
        self.assertNotIn("msg", diag)
        self.assertIn("raw_answer_ref", diag)
        self.assertEqual(diag["raw_answer_ref"]["bytes"], len(answer.encode("utf-8")))

    def test_transport_and_envelope_priorities(self):
        # Transport error takes priority even if answer is broken
        parsed_transport = parse_acp_worker_output(envelope(
            status="failed",
            success=False,
            outcome_uncertain=True,
            error={"code": "acp_transport_error"},
            answer="{broken json",
        ), "")
        self.assertEqual(parsed_transport.error["code"], "acp_transport_error")
        self.assertNotIn("manifest_diagnostic", parsed_transport.metadata)

        # Timeout preserves terminal status and error
        parsed_timeout = parse_acp_worker_output(envelope(
            status="timed_out",
            success=False,
            error={"code": "acp_timeout"},
            answer="{broken json",
        ), "")
        self.assertEqual(parsed_timeout.error["code"], "acp_timeout")
        self.assertEqual(parsed_timeout.terminal_status, "timed_out")
        self.assertNotIn("manifest_diagnostic", parsed_timeout.metadata)

        # Pending question request preserves error and metadata
        parsed_question = parse_acp_worker_output(envelope(
            status="blocked",
            success=False,
            error={"code": "acp_input_required"},
            pending_request={"kind": "question", "title": "Scope?"},
            answer="{broken json",
        ), "")
        self.assertEqual(parsed_question.error["code"], "acp_input_required")
        self.assertEqual(parsed_question.metadata["pending_request"]["kind"], "question")
        self.assertNotIn("manifest_diagnostic", parsed_question.metadata)

    def test_failed_worker_bearing_acp_result_manifest_invalid(self):
        fixture = '{"status":"succeeded","summary":"review","findings":["claim" interpretation="audit"]}'
        parsed = parse_acp_worker_output(envelope(
            status="failed",
            success=False,
            error={"code": "acp_result_manifest_invalid"},
            answer=fixture,
        ), "")
        self.assertEqual(parsed.error["code"], "acp_result_manifest_invalid")
        self.assertIsNone(parsed.structured_output)
        self.assertIn("manifest_diagnostic", parsed.metadata)
        diag = parsed.metadata["manifest_diagnostic"]
        self.assertEqual(diag["version"], 1)
        self.assertEqual(diag["reason"], "json_syntax")
        self.assertEqual(diag["pos"], 61)
        self.assertEqual(diag["colno"], 62)

    def test_escaped_lone_surrogate_answer(self):
        # Escaped lone surrogate in answer via json.dumps envelope
        raw_env = envelope(answer="lone surrogate \ud800 in raw text")
        parsed = parse_acp_worker_output(raw_env, "")
        self.assertEqual(parsed.error["code"], "acp_result_manifest_invalid")
        self.assertIsNone(parsed.structured_output)
        diag = parsed.metadata.get("manifest_diagnostic")
        self.assertIsInstance(diag, dict)
        self.assertEqual(diag["version"], 1)
        self.assertEqual(diag["reason"], "invalid_manifest")
        self.assertNotIn("raw_answer_ref", diag)

        # Escaped lone surrogate in schema-invalid answer via json.dumps envelope
        schema_invalid_answer = json.dumps({"status": "succeeded", "summary": "test \ud800", "findings": 123})
        parsed_schema = parse_acp_worker_output(envelope(answer=schema_invalid_answer), "")
        self.assertEqual(parsed_schema.error["code"], "acp_result_manifest_invalid")
        self.assertIsNone(parsed_schema.structured_output)
        diag_schema = parsed_schema.metadata.get("manifest_diagnostic")
        self.assertIsInstance(diag_schema, dict)
        self.assertEqual(diag_schema["version"], 1)
        self.assertEqual(diag_schema["reason"], "schema_invalid")
        self.assertEqual(diag_schema["path"], "findings")
        self.assertIn("raw_answer_ref", diag_schema)

    def test_deeply_nested_json_array_recursion(self):
        # 2000 nested array levels: json.loads probe raises RecursionError, must not crash
        deep_answer = "[" * 2000 + "]" * 2000
        parsed = parse_acp_worker_output(envelope(answer=deep_answer), "")
        self.assertEqual(parsed.error["code"], "acp_result_manifest_invalid")
        self.assertIsNone(parsed.structured_output)
        diag = parsed.metadata.get("manifest_diagnostic")
        self.assertIsInstance(diag, dict)
        self.assertEqual(diag["version"], 1)
        self.assertEqual(diag["reason"], "invalid_manifest")
        self.assertIn("raw_answer_ref", diag)
        self.assertEqual(diag["raw_answer_ref"]["bytes"], 4000)
        self.assertEqual(diag["raw_answer_ref"]["sha256"], hashlib.sha256(deep_answer.encode("utf-8")).hexdigest())
        self.assertNotIn("pos", diag)
        self.assertNotIn("msg", diag)


if __name__ == "__main__":
    unittest.main()
