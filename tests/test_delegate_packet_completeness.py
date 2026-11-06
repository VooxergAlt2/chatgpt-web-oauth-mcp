"""Behavioral packet regressions runnable with the isolated system Python."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_packets import (
    MAX_PACKET_BYTES, build_delegate_packet, handoff_context, restore_packet_claims,
)


class PacketCompletenessTests(unittest.TestCase):
    def packet(self, source, purpose="review"):
        return build_delegate_packet(source, purpose=purpose, freshness={"state": "unknown"})

    def test_single_long_finding_is_not_reported_complete(self):
        source = {"structured_output": {"summary": "ok", "findings": ["я" * 600]}}
        for purpose in ("review", "handoff"):
            p = self.packet(source, purpose)
            self.assertEqual(len(p["agent_claims"]["findings"][0]), 512)
            self.assertTrue(p["truncated"])
            self.assertIn("agent_claims.findings[0]", p["truncated_fields"])

    def test_exact_limits_and_short_values_are_complete(self):
        for text in ("short", "x" * 512):
            p = self.packet({"structured_output": {"summary": "ok", "findings": [text]}})
            self.assertFalse(p["truncated"])
            self.assertEqual(p["truncated_fields"], [])

    def test_text_fields_report_individual_loss(self):
        cases = [
            ({"structured_output": {"summary": "ю" * 2049}}, "agent_claims.summary"),
            ({"structured_output": {"next_execution_prompt": "ю" * 1025}}, "agent_claims.next_execution_prompt"),
            ({"acp_scope": {"files_in_scope": ["я" * 513]}}, "binding.scope.files_in_scope[0]"),
            ({"code_verification": {"verification_commands": [{"command": "я" * 1025}]}},
             "server_observations.verification.checks[0].command"),
            ({"code_verification": {"review_evidence": {"diff": "x" * 12289}}},
             "server_observations.review_evidence.diff"),
        ]
        for source, field in cases:
            p = self.packet(source)
            self.assertTrue(p["truncated"], field)
            self.assertIn(field, p["truncated_fields"])
        self.assertTrue(p["server_observations"]["review_evidence"]["truncated"])

    def test_upstream_diff_omission_is_propagated(self):
        p = self.packet({"code_verification": {"review_evidence": {"diff": "x", "truncated": True}}})
        self.assertTrue(p["truncated"])
        self.assertIn("server_observations.review_evidence.diff", p["truncated_fields"])

    def test_list_cuts_are_reported_without_long_items(self):
        p = self.packet({"structured_output": {"findings": ["x"] * 13},
                         "code_verification": {"verification_commands": [{"passed": True}] * 17}})
        self.assertIn("agent_claims.findings", p["truncated_fields"])
        self.assertIn("server_observations.verification.checks", p["truncated_fields"])

    def test_existing_reference_is_bounded_and_source_is_unchanged(self):
        source = {"structured_output": {"findings": ["x" * 600]},
                  "structured_result_ref": {"name": "structured-result.json", "bytes": 100, "sha256": "a" * 64},
                  "logs": {"stdout": "/actual/stdout.log", "metadata": "/actual/metadata.json", "prompt": "private"}}
        before = copy.deepcopy(source)
        p = self.packet(source)
        self.assertEqual(source, before)
        self.assertEqual(p["full_result"]["stdout"], "/actual/stdout.log")
        self.assertEqual(p["full_result"]["structured_result_ref"], source["structured_result_ref"])
        self.assertNotIn("prompt", p["full_result"])
        self.assertNotIn("full_result", self.packet({}))

    def test_corrupt_claim_artifact_reports_known_unavailability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = json.dumps({"delegate_id": "a" * 12,
                               "structured_output": {"summary": "claim"}}).encode()
            source = {"delegate_id": "a" * 12, "structured_result_ref": {
                "name": "structured-result.json", "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()}}
            artifact = root / "structured-result.json"
            artifact.write_bytes(data.replace(b"claim", b"other"))
            artifact.chmod(0o600)
            restored = restore_packet_claims(source, root / "metadata.json")
            self.assertEqual(restored["structured_result_unavailable"], "artifact_validation_failed")
            p = self.packet(restored)
            self.assertEqual(p["full_result"]["availability"], "unavailable")
            self.assertEqual(p["full_result"]["reason"], "artifact_validation_failed")
            self.assertEqual(p["full_result"]["structured_result_ref"], source["structured_result_ref"])
            self.assertEqual(p["agent_claims"]["summary"], "")

    def test_triage_metadata_and_claim_cuts_are_distinct(self):
        p = self.packet({"model": "x" * 81}, "triage")
        self.assertTrue(p["truncated"])
        self.assertFalse(p["claims_truncated"])
        self.assertIn("model", p["truncated_fields"])
        p = self.packet({"structured_output": {"findings": ["x", "y"]}}, "triage")
        self.assertTrue(p["truncated"] and p["claims_truncated"])

    def test_handoff_three_finding_filter_is_not_silent(self):
        source = {"execution_backend": "acp", "completed": True,
                  "harness": "antigravity", "cwd": "/unused-packet-source",
                  "harness_metadata": {"owner_digest": hashlib.sha256(b"owner").hexdigest()},
                  "acp_scope": {"files_in_scope": ["a.py"], "out_of_scope": []},
                  "structured_output": {"findings": ["a", "b", "c", "d"]}}
        text = handoff_context(source, owner="owner", harness="antigravity",
                               cwd=Path(source["cwd"]), files_in_scope=["a.py"], out_of_scope=[])
        p = json.loads(text.split("\n")[-3])
        self.assertEqual(p["agent_claims"]["findings"], ["a", "b", "c"])
        self.assertTrue(p["truncated"])
        self.assertIn("agent_claims.findings", p["truncated_fields"])

    def test_adversarial_unicode_stays_within_budgets(self):
        source = {"cwd": "😀" * 2000, "harness": "😀" * 200, "model": "😀" * 200,
                  "reasoning_effort": "😀" * 200,
                  "structured_output": {"summary": "😀" * 5000, "findings": ["😀" * 2000] * 100,
                                        "blockers": ["😀" * 2000] * 20, "next_execution_prompt": "😀" * 2000},
                  "acp_scope": {"files_in_scope": ["😀" * 2000] * 100},
                  "code_verification": {"changed_files": ["😀" * 2000] * 100,
                      "verification_commands": [{"command": "😀" * 2000}] * 20,
                      "review_evidence": {"diff": "😀" * 20000}},
                  "harness_metadata": {"scoped_gateway": {"diagnostics": {"large": "x" * 100000}}}}
        before = copy.deepcopy(source)
        for purpose in ("triage", "review", "handoff"):
            p = self.packet(source, purpose)
            self.assertLessEqual(len(json.dumps(p, ensure_ascii=True, sort_keys=True).encode()),
                                 4096 if purpose == "triage" else MAX_PACKET_BYTES)
            self.assertTrue(p["truncated"])
            self.assertLessEqual(len(p["truncated_fields"]), 32)
        self.assertEqual(source, before)

    def test_proposal_exact_candidate_is_preserved(self):
        text = "x" * 3000
        p = self.packet({"work_role": "proposal", "execution_backend": "acp", "completed": True,
                         "status": "succeeded", "structured_output": {"status": "succeeded", "summary": text,
                             "findings": [], "blockers": [], "next_execution_prompt": None}})
        self.assertEqual(p["proposal"]["text"], text)
        self.assertFalse(p["proposal"]["accepted"])
        self.assertFalse(p["truncated"])


if __name__ == "__main__":
    unittest.main()
