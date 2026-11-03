"""Provider-free provenance tests; runnable with stdlib unittest."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from scripts import acp_acceptance_provenance as provenance
from scripts import acp_final_acceptance as acceptance
from scripts import acp_mcp_acceptance as readonly_acceptance

SNAPSHOT = {
    "schema_version": 1, "captured_at_utc": "2026-11-03T00:00:00Z",
    "status": "captured", "head": "a" * 40, "tree": "b" * 40,
    "dirty": False, "error_code": None,
}
PRIVATE = "SYNTHETIC_PRIVATE_DATA"


class CaptureCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def assert_unavailable(self, snapshot):
        self.assertEqual(set(snapshot), set(SNAPSHOT))
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["status"], "unavailable")
        self.assertEqual(snapshot["error_code"], "qa_implementation_capture_failed")
        for key in ("head", "tree", "dirty"):
            self.assertIsNone(snapshot[key])
        self.assertNotIn(PRIVATE, json.dumps(snapshot))
        self.assertNotIn(str(self.root), json.dumps(snapshot))
        captured = datetime.fromisoformat(snapshot["captured_at_utc"])
        self.assertEqual(captured.utcoffset(), timedelta(0))


class CaptureTests(CaptureCase):
    def test_non_repository_and_missing_directory_are_unavailable(self):
        self.assert_unavailable(provenance.capture_implementation(self.root))
        self.assert_unavailable(provenance.capture_implementation(self.root / PRIVATE))

    def test_missing_git_timeout_and_git_errors_do_not_expose_diagnostics(self):
        failures = [
            FileNotFoundError(PRIVATE),
            subprocess.TimeoutExpired(PRIVATE, 5, output=PRIVATE, stderr=PRIVATE),
            subprocess.CompletedProcess([], 128, stdout=PRIVATE.encode(), stderr=PRIVATE.encode()),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                options = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(provenance.subprocess, "run", **options):
                    self.assert_unavailable(provenance.capture_implementation(self.root))

    def test_invalid_object_ids_and_head_race_are_unavailable(self):
        cases = [
            [b"g" * 40], [b"a" * 41], [b"a" * 40, b"b" * 64],
            [b"a" * 40, b"b" * 40, b"", b"c" * 40],
        ]
        for outputs in cases:
            with self.subTest(outputs=len(outputs)):
                responses = [subprocess.CompletedProcess([], 0, stdout=value) for value in outputs]
                with patch.object(provenance.subprocess, "run", side_effect=responses):
                    self.assert_unavailable(provenance.capture_implementation(self.root))

    def test_git_launch_uses_explicit_repository_and_sanitized_environment(self):
        responses = [subprocess.CompletedProcess([], 0, stdout=value)
                     for value in (b"a" * 40, b"b" * 40, b"", b"a" * 40)]
        with patch.dict(os.environ, {"GIT_DIR": PRIVATE, "SECRET_TOKEN": PRIVATE}), \
                patch.object(provenance.subprocess, "run", side_effect=responses) as run:
            snapshot = provenance.capture_implementation(self.root)
        self.assertEqual(snapshot["status"], "captured")
        self.assertEqual(run.call_count, 4)
        self.assertEqual(run.call_args_list[1].args[0][-1], "a" * 40 + "^{tree}")
        for call in run.call_args_list:
            argv, options = call.args[0], call.kwargs
            self.assertEqual(argv[:3], ["/usr/bin/git", "-C", str(self.root)])
            self.assertIn("core.hooksPath=/dev/null", argv)
            self.assertIn("core.fsmonitor=false", argv)
            self.assertGreater(options["timeout"], 0)
            self.assertLessEqual(options["timeout"], 5)
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertNotIn("GIT_DIR", options["env"])
            self.assertNotIn("SECRET_TOKEN", options["env"])
            self.assertEqual(options["env"]["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(options["env"]["GIT_CONFIG_GLOBAL"], "/dev/null")
            self.assertEqual(options["env"]["GIT_CONFIG_SYSTEM"], "/dev/null")


@unittest.skipUnless(Path("/usr/bin/git").is_file(), "fixed Git executable required")
class RealGitTests(CaptureCase):
    def git(self, *arguments):
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(self.root), "-c", "core.hooksPath=/dev/null",
             "-c", "commit.gpgsign=false", *arguments],
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            stdin=subprocess.DEVNULL, capture_output=True, timeout=5, check=True,
        )
        return result.stdout.decode().strip()

    def repository(self, object_format="sha1", commit=True):
        self.git("init", "-q", "--object-format=" + object_format)
        self.git("config", "user.name", "Provenance fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        if commit:
            (self.root / "tracked.txt").write_text("baseline\n")
            self.git("add", "tracked.txt")
            self.git("commit", "-qm", "baseline")

    def test_clean_repositories_with_sha1_and_sha256(self):
        for object_format in ("sha1", "sha256"):
            with self.subTest(object_format=object_format), tempfile.TemporaryDirectory() as temporary:
                prior, self.root = self.root, Path(temporary)
                try:
                    self.repository(object_format)
                    snapshot = provenance.capture_implementation(self.root)
                    self.assertEqual(snapshot["head"], self.git("rev-parse", "HEAD"))
                    self.assertEqual(snapshot["tree"], self.git("rev-parse", "HEAD^{tree}"))
                    self.assertEqual(snapshot["status"], "captured")
                    self.assertIs(snapshot["dirty"], False)
                    self.assertIsNone(snapshot["error_code"])
                    self.assertTrue(snapshot["captured_at_utc"].endswith("Z"))
                finally:
                    self.root = prior

    def test_tracked_staged_and_untracked_changes_are_dirty_without_filenames(self):
        self.repository()
        for change in ("tracked", "staged", "untracked"):
            with self.subTest(change=change):
                path = self.root / (PRIVATE if change == "untracked" else "tracked.txt")
                path.write_text(PRIVATE)
                if change == "staged":
                    self.git("add", "tracked.txt")
                snapshot = provenance.capture_implementation(self.root)
                self.assertEqual(snapshot["status"], "captured")
                self.assertIs(snapshot["dirty"], True)
                self.assertNotIn(PRIVATE, json.dumps(snapshot))
                self.assertNotIn(str(self.root), json.dumps(snapshot))
                self.git("reset", "--hard", "-q", "HEAD")
                if change == "untracked":
                    path.unlink()

    def test_unborn_repository_is_unavailable(self):
        self.repository(commit=False)
        self.assert_unavailable(provenance.capture_implementation(self.root))

    def test_local_fsmonitor_and_hostile_environment_do_not_run(self):
        self.repository()
        hook = self.root.parent / (self.root.name + "-hook")
        marker = self.root.parent / (self.root.name + "-marker")
        self.addCleanup(hook.unlink, missing_ok=True)
        self.addCleanup(marker.unlink, missing_ok=True)
        hook.write_text("#!/bin/sh\ntouch '" + str(marker) + "'\n")
        hook.chmod(0o700)
        self.git("config", "core.fsmonitor", str(hook))
        with patch.dict(os.environ, {"GIT_DIR": PRIVATE, "GIT_WORK_TREE": PRIVATE,
                                     "GIT_CONFIG_GLOBAL": PRIVATE}):
            snapshot = provenance.capture_implementation(self.root)
        self.assertEqual(snapshot["status"], "captured")
        self.assertFalse(marker.exists())


class WiringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.argv = ["acp_final_acceptance", "--config", str(self.root / "config"),
                     "--state", str(self.state), "--workspace", str(self.root)]
        # Imports inside exercise cannot start or even load provider dependencies.
        modules = {"fastmcp": SimpleNamespace(Client=Mock()),
                   "chatgpt_web_oauth_mcp.shell": SimpleNamespace(JobRegistry=Mock())}
        self.enterContext(patch.dict(sys.modules, modules))
        self.enterContext(patch.object(acceptance.os, "umask"))

    def test_main_captures_once_before_pass_and_fail_and_forwards_same_snapshot(self):
        for status in ("PASS", "FAIL"):
            with self.subTest(status=status), patch.object(
                    acceptance, "capture_implementation", return_value=dict(SNAPSHOT)) as capture:
                async def exercise(*args, **kwargs):
                    self.assertEqual(capture.call_count, 1)
                    self.assertIs(kwargs["implementation"], capture.return_value)
                    return {"status": status, "implementation": kwargs["implementation"]}
                output = io.StringIO()
                with patch.object(acceptance, "exercise", exercise), \
                        patch.object(sys, "argv", self.argv), contextlib.redirect_stdout(output):
                    code = acceptance.main()
                self.assertEqual(code, 0 if status == "PASS" else 1)
                self.assertEqual(json.loads(output.getvalue().split(" ", 1)[1])["implementation"], SNAPSHOT)
                capture.assert_called_once_with(Path(acceptance.__file__).resolve().parents[1])

    def test_setup_failure_keeps_snapshot_and_does_not_write_preexisting_state(self):
        self.state.mkdir()
        historical = self.state / "final-acceptance-report.json"
        historical.write_text("historical report")
        output = io.StringIO()
        with patch.object(acceptance, "capture_implementation", return_value=dict(SNAPSHOT)) as capture, \
                patch.object(acceptance, "prepare", side_effect=RuntimeError(PRIVATE)), \
                patch.object(acceptance, "write_private_json") as write, \
                patch.object(sys, "argv", self.argv), contextlib.redirect_stdout(output):
            self.assertEqual(acceptance.main(), 1)
        capture.assert_called_once()
        write.assert_not_called()
        self.assertEqual(historical.read_text(), "historical report")
        self.assertEqual(list(self.state.iterdir()), [historical])
        report = json.loads(output.getvalue().split(" ", 1)[1])
        self.assertEqual(report["implementation"], SNAPSHOT)
        self.assertEqual(report["error"], "qa_internal_error")
        self.assertNotIn(PRIVATE, output.getvalue())

    def test_readonly_main_keeps_one_snapshot_in_pass_and_exception_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), patch.object(
                    readonly_acceptance, "capture_implementation", return_value=dict(SNAPSHOT)) as capture:
                async def exercise(*args, **kwargs):
                    self.assertEqual(capture.call_count, 1)
                    self.assertIs(kwargs["implementation"], capture.return_value)
                    if fail:
                        raise RuntimeError(PRIVATE)
                    return {"status": "PASS", "implementation": kwargs["implementation"]}
                output = io.StringIO()
                with patch.object(readonly_acceptance, "exercise", exercise), \
                        patch.object(sys, "argv", self.argv), contextlib.redirect_stdout(output):
                    self.assertEqual(readonly_acceptance.main(), 1 if fail else 0)
                capture.assert_called_once_with(Path(readonly_acceptance.__file__).resolve().parents[1])
                result = json.loads(output.getvalue().split(" ", 1)[1])
                self.assertEqual(result["implementation"], SNAPSHOT)
                self.assertEqual(result["status"], "FAIL" if fail else "PASS")
                self.assertNotIn(PRIVATE, output.getvalue())

    def test_direct_exercise_captures_before_setup_and_persists_same_failure_snapshot(self):
        self.state.mkdir()
        for supplied in (False, True):
            with self.subTest(supplied=supplied), patch.object(
                    acceptance, "capture_implementation", return_value=dict(SNAPSHOT)) as capture:
                def prepare(*args):
                    self.assertEqual(capture.call_count, 0 if supplied else 1)
                    return self.root, self.root, "proof", "token", "owner", {}
                with patch.object(acceptance, "prepare", side_effect=prepare), \
                        patch.object(acceptance, "QaServer") as server, \
                        patch.object(acceptance, "prepare_writer",
                                     side_effect=acceptance.AcceptanceError("qa_fixture_failure")), \
                        patch.object(acceptance, "cleanup", new=AsyncMock(return_value=True)), \
                        patch.object(acceptance, "write_private_json") as write:
                    with self.assertRaisesRegex(acceptance.AcceptanceError, "qa_fixture_failure"):
                        asyncio.run(acceptance.exercise(
                            self.root / "config", self.state, self.root,
                            implementation=capture.return_value if supplied else None))
                report = write.call_args.args[1]
                self.assertIs(report["implementation"], capture.return_value)
                self.assertEqual(report["status"], "FAIL")
                self.assertEqual(report["error"], "qa_fixture_failure")
                server.return_value.start.assert_not_called()
                self.assertEqual(capture.call_count, 0 if supplied else 1)


if __name__ == "__main__":
    unittest.main()
