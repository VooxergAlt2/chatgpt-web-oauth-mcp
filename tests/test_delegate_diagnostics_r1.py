"""Standard unittest suite for Release 1 delegate diagnostics accumulator.

Verifies pure stdlib accumulator behavior, repetition streaks, changed response
breaks, repeated identical errors, last novel observation elapsed timings,
secret absence, and bounded storage.
"""
from __future__ import annotations

import json
import unittest

from chatgpt_web_oauth_mcp.delegate_diagnostics import (
    DEFAULT_LIMITATIONS,
    DIAGNOSTICS_VERSION,
    DelegateDiagnosticsAccumulator,
)


class TestDelegateDiagnosticsAccumulator(unittest.TestCase):
    def setUp(self):
        self.time_val = 100.0
        self.clock = lambda: self.time_val
        self.accumulator = DelegateDiagnosticsAccumulator(clock=self.clock, max_window=64)

    def test_initial_summary_contract(self):
        summary = self.accumulator.summary()
        self.assertEqual(summary["version"], DIAGNOSTICS_VERSION)
        self.assertIs(summary["advisory"], True)
        self.assertEqual(summary["observation_scope"], "gateway")
        self.assertEqual(summary["completeness"], "incomplete")
        self.assertEqual(summary["native_visibility"], "incomplete")
        self.assertEqual(summary["total_calls"], 0)
        self.assertEqual(summary["same_request_response_repeats"], 0)
        self.assertEqual(summary["repeated_identical_errors"], 0)
        self.assertEqual(summary["repeat_streak"], 0)
        self.assertEqual(summary["max_repeat_streak"], 0)
        self.assertEqual(summary["error_streak"], 0)
        self.assertIsNone(summary["last_novel_observation_elapsed"])
        self.assertEqual(summary["window_size"], 0)
        for expected in DEFAULT_LIMITATIONS:
            self.assertIn(expected, summary["limitations"])


    def test_same_request_same_response_repetitions(self):
        # Call 1: first observation
        self.time_val = 101.0
        self.accumulator.record_success("read_text", {"path": "src/main.py"}, {"content": "code v1"})
        s1 = self.accumulator.summary()
        self.assertEqual(s1["total_calls"], 1)
        self.assertEqual(s1["same_request_response_repeats"], 0)
        self.assertEqual(s1["repeat_streak"], 0)
        self.assertEqual(s1["last_novel_observation_elapsed"], 1.0)

        # Call 2: identical request and response
        self.time_val = 103.0
        self.accumulator.record_success("read_text", {"path": "src/main.py"}, {"content": "code v1"})
        s2 = self.accumulator.summary()
        self.assertEqual(s2["total_calls"], 2)
        self.assertEqual(s2["same_request_response_repeats"], 1)
        self.assertEqual(s2["repeat_streak"], 1)
        self.assertEqual(s2["max_repeat_streak"], 1)
        # Novel observation elapsed timestamp must NOT advance on repeat
        self.assertEqual(s2["last_novel_observation_elapsed"], 1.0)

        # Call 3: third identical call
        self.time_val = 105.0
        self.accumulator.record_success("read_text", {"path": "src/main.py"}, {"content": "code v1"})
        s3 = self.accumulator.summary()
        self.assertEqual(s3["total_calls"], 3)
        self.assertEqual(s3["same_request_response_repeats"], 2)
        self.assertEqual(s3["repeat_streak"], 2)
        self.assertEqual(s3["max_repeat_streak"], 2)
        self.assertEqual(s3["last_novel_observation_elapsed"], 1.0)

    def test_changed_response_breaks_repeat_streak(self):
        # Call 1 & 2 establish a streak of 1 repeat
        self.time_val = 101.0
        self.accumulator.record_success("read_text", {"path": "src/main.py"}, {"content": "code v1"})
        self.time_val = 102.0
        self.accumulator.record_success("read_text", {"path": "src/main.py"}, {"content": "code v1"})
        self.assertEqual(self.accumulator.summary()["repeat_streak"], 1)

        # Call 3: same request, but changed content/response
        self.time_val = 104.0
        self.accumulator.record_success("read_text", {"path": "src/main.py"}, {"content": "code v2"})
        s3 = self.accumulator.summary()
        self.assertEqual(s3["total_calls"], 3)
        # Repeat counter must not increment when response changed
        self.assertEqual(s3["same_request_response_repeats"], 1)
        # Changed response breaks repeat streak
        self.assertEqual(s3["repeat_streak"], 0)
        self.assertEqual(s3["max_repeat_streak"], 1)
        # Novel observation elapsed updates to current elapsed (4.0)
        self.assertEqual(s3["last_novel_observation_elapsed"], 4.0)

    def test_different_operation_resets_repeat_streak(self):
        self.time_val = 101.0
        self.accumulator.record_success("read_text", {"path": "src/a.py"}, {"content": "a"})
        self.time_val = 102.0
        self.accumulator.record_success("read_text", {"path": "src/a.py"}, {"content": "a"})
        self.assertEqual(self.accumulator.summary()["repeat_streak"], 1)

        # Different operation
        self.time_val = 103.0
        self.accumulator.record_success("search", {"query": "foo"}, {"results": []})
        s = self.accumulator.summary()
        self.assertEqual(s["repeat_streak"], 0)
        self.assertEqual(s["last_novel_observation_elapsed"], 3.0)

    def test_repeated_identical_errors(self):
        # Call 1: first error
        self.time_val = 101.0
        self.accumulator.record_error("read_text", {"path": "missing.py"}, "acp_gateway_file_unavailable")
        s1 = self.accumulator.summary()
        self.assertEqual(s1["total_calls"], 1)
        self.assertEqual(s1["repeated_identical_errors"], 0)
        self.assertEqual(s1["error_streak"], 1)

        # Call 2: same error on different tool/path
        self.time_val = 102.0
        self.accumulator.record_error("search", {"query": "test"}, "acp_gateway_file_unavailable")
        s2 = self.accumulator.summary()
        self.assertEqual(s2["total_calls"], 2)
        self.assertEqual(s2["repeated_identical_errors"], 1)
        self.assertEqual(s2["error_streak"], 2)

        # Call 3: different error code breaks error streak
        self.time_val = 103.0
        self.accumulator.record_error("read_text", {"path": "bad"}, "acp_gateway_path_invalid")
        s3 = self.accumulator.summary()
        self.assertEqual(s3["total_calls"], 3)
        self.assertEqual(s3["repeated_identical_errors"], 1)
        self.assertEqual(s3["error_streak"], 1)

        # Call 4: successful call resets error streak
        self.time_val = 104.0
        self.accumulator.record_success("code_map_symbols", {}, {"results": []})
        s4 = self.accumulator.summary()
        self.assertEqual(s4["error_streak"], 0)

    def test_same_request_repeated_error_increments_both(self):
        self.time_val = 101.0
        self.accumulator.record_error("read_text", {"path": "err.py"}, "acp_gateway_file_unavailable")
        self.time_val = 102.0
        self.accumulator.record_error("read_text", {"path": "err.py"}, "acp_gateway_file_unavailable")
        s = self.accumulator.summary()
        self.assertEqual(s["same_request_response_repeats"], 1)
        self.assertEqual(s["repeat_streak"], 1)
        self.assertEqual(s["repeated_identical_errors"], 1)
        self.assertEqual(s["error_streak"], 2)

    def test_secret_absence_and_raw_data_never_persisted(self):
        secret_content = "super_confidential_token_12345"
        raw_path = "secret/credentials/passwords.txt"
        raw_query = "confidential_query_marker"

        self.accumulator.record_success(
            "search",
            {"path": raw_path, "query": raw_query},
            {"content": secret_content},
        )
        summary = self.accumulator.summary()
        dumped = json.dumps(summary)

        # Neither raw path, raw query, nor secret content may appear
        self.assertNotIn(secret_content, dumped)
        self.assertNotIn(raw_path, dumped)
        self.assertNotIn(raw_query, dumped)
        self.assertNotIn("secret", dumped)
        self.assertNotIn("token", dumped.lower())

        # HMAC secret attribute is private and not exposed in summary or repr
        self.assertNotIn("secret", summary)
        self.assertNotIn(self.accumulator._secret.hex(), dumped)
        self.assertNotIn(self.accumulator._secret.hex(), repr(self.accumulator))

    def test_per_instance_keyed_hmac_isolation(self):
        # Two instances with the exact same inputs must have different internal digests
        # due to independent per-instance random secrets
        acc1 = DelegateDiagnosticsAccumulator(clock=self.clock)
        acc2 = DelegateDiagnosticsAccumulator(clock=self.clock)

        self.assertNotEqual(acc1._secret, acc2._secret)

        acc1.record_success("read_text", {"path": "src/sample.py"}, {"content": "abc"})
        acc2.record_success("read_text", {"path": "src/sample.py"}, {"content": "abc"})

        # Compare internal HMAC digests stored in the bounded window
        op1, res1, _, _ = acc1._window[0]
        op2, res2, _, _ = acc2._window[0]
        self.assertNotEqual(op1, op2)
        self.assertNotEqual(res1, res2)

    def test_bounded_storage_memory_ceiling(self):
        acc = DelegateDiagnosticsAccumulator(clock=self.clock, max_window=10)
        for i in range(250):
            acc.record_success("read_text", {"line": i}, {"text": f"val_{i}"})

        summary = acc.summary()
        self.assertEqual(summary["total_calls"], 250)
        self.assertEqual(summary["window_size"], 10)
        self.assertEqual(len(acc._window), 10)

    def test_advisory_robustness_on_unusual_arguments(self):
        # Accumulator must never raise on None, circular, or non-primitive arguments
        acc = DelegateDiagnosticsAccumulator(clock=self.clock)
        acc.record_call("custom_tool", None, result=None)
        acc.record_call("custom_tool", "string_arg", error=TypeError("bad arg"))
        acc.record_call("custom_tool", {"func": lambda x: x}, result=object())
        self.assertEqual(acc.total_calls, 3)


if __name__ == "__main__":
    unittest.main()
