"""Regression checks for the controller policy; runnable with stdlib alone."""
from pathlib import Path
import runpy
import unittest


GUIDANCE = runpy.run_path(str(
    Path(__file__).resolve().parents[1] / "src/chatgpt_web_oauth_mcp/delegate_guidance.py"
))
GUIDE = GUIDANCE["DELEGATE_USE_GUIDE"]


class DelegateAdoptionGuidanceTests(unittest.TestCase):
    def test_eligible_implementation_gets_executor_before_deep_analysis(self):
        rules = GUIDE.split("## Critical rules", 1)[1].split("## Choose the tool", 1)[0]
        first_rule = rules.split("1. ", 1)[1].split("\n2. ", 1)[0].lower()
        self.assertIn("eligible nontrivial implementation", first_rule)
        self.assertIn("flash", first_rule)
        self.assertLess(first_rule.index("assign"), first_rule.index("deep analysis"))
        self.assertIn("before", first_rule)
        self.assertIn("known mechanical edits", first_rule)
        self.assertIn("git/status/process", first_rule)
        self.assertIn("do not force model calls", first_rule)
        self.assertIn("evade delegation", first_rule)

    def test_parallel_writers_are_scoped_and_capacity_limited(self):
        scheduling = GUIDE.split("## Understand scheduling", 1)[1].split("## Wait, monitor", 1)[0]
        self.assertIn("tasks and capacity allow", scheduling)
        self.assertIn("separate worktrees", scheduling)
        self.assertIn("independent scopes and checks", scheduling)
        self.assertIn("max_code_per_project", scheduling)
        self.assertIn("Team batches are read-only", scheduling)
        self.assertIn("Premium escalation is explicit", GUIDE)
        self.assertIn("Never switch automatically to Codex or Claude", GUIDE)

    def test_environment_claim_is_specific_to_acp_and_not_automatic_preflight(self):
        contract = GUIDE.split("- `verification_commands`:", 1)[1].split("- `max_changed_files`", 1)[0]
        self.assertIn("server-owned acceptance", contract)
        self.assertIn("only after the agent exits", contract)
        acp = contract.split("For ACP code only:", 1)[1]
        for required in ("read-only", "detached", "network-isolated", "clearenv",
                         "PATH=/usr/bin:/bin", "/usr/bin/python3", "observed 2026-11-06",
                         "no automatic preflight API", "Never fall back checks to the host"):
            self.assertIn(required, acp)
        self.assertIn("controller must verify", acp)
        self.assertIn("before spending", acp)

    def test_public_payload_and_index_share_guidance_revision(self):
        payload = GUIDANCE["delegate_use_payload"]()
        index = GUIDANCE["skill_index_payload"]()
        version = GUIDANCE["SKILL_GUIDANCE_VERSION"]
        self.assertGreaterEqual(tuple(map(int, version.split("."))), (1, 12))
        self.assertEqual(payload["content"], GUIDE)
        self.assertEqual(payload["version"], version)
        self.assertEqual(index["version"], version)
        self.assertIn("ATTENTION_REQUIRED", GUIDE)
        self.assertIn("never automatic termination limits", GUIDE)


if __name__ == "__main__":
    unittest.main()
