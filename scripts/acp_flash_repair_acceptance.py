#!/usr/bin/env python3
"""Operator-only Flash correction experiment against one frozen quota oracle.

Inputs and results stay private. One fresh proposal is allowed after the
reference passes and the seed reproduces the two designated numeric failures.
The same oracle checks the exact returned source in a separate offline sandbox.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError

if __package__:
    from .acp_mcp_acceptance import (
        AcceptanceError, QaServer, call, capture_implementation, prepare,
        require, select, status, terminal, write_private_json,
    )
else:
    from acp_mcp_acceptance import (
        AcceptanceError, QaServer, call, capture_implementation, prepare,
        require, select, status, terminal, write_private_json,
    )

EXPECTED_TESTS = 86
SOURCE_LIMIT = 8192
OUTPUT_LIMIT = 8192
ACCOUNT = "antigravity3"
MODEL = "gemini-3.8-flash-medium"
CONTRACT = """Return a complete Python 3.12 module defining
quota_delta(before, after, *, concurrent=False). Use only the standard library,
no tools or I/O, and preserve both input dictionaries. Always return a tuple
(delta_pp or None, reason), applying these rules in order:
1. concurrent true gives (None, 'concurrent').
2. account, group and window must be identical nonblank strings on both sides;
otherwise return reason 'identity'.
3. sampled_at must be finite int/float Unix seconds (bool excluded), strictly
increasing; otherwise return 'stale'. Python integers are arbitrarily large.
4. resets_at must be an identical nonblank string on both sides, parseable as
an aware ISO-8601 instant. Both observations must strictly precede the instant;
otherwise return 'reset'.
5. remaining_percent must be finite int/float in [0,100], bool excluded;
otherwise return 'unavailable'. A rise gives 'increased', equality gives
'below_resolution', a decline gives round(before minus after, 3), 'observed'.
All reasons other than observed must return None as their first tuple element.
The first implementation passed 84/86 tests. Both failures concern huge integer
sampled_at values with a future reset and a 10 percentage-point decline:
before=-10**400, after=0 must return (10.0, 'observed');
before=0, after=10**400 must return (None, 'reset').
Correct the supplied source. Return its complete replacement in the summary,
without Markdown. This is the only correction attempt.
"""


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def proposal_source(reply: dict) -> str:
    """Validate the exact bytes from the canonical review packet."""
    require(isinstance(reply, dict), "repair_packet_missing")
    packet = reply.get("packet") if isinstance(reply, dict) else None
    proposal = packet.get("proposal") if isinstance(packet, dict) else None
    require(reply.get("success") is True and isinstance(proposal, dict),
            "repair_packet_missing")
    require(proposal.get("available") is True and proposal.get("accepted") is False,
            "repair_proposal_unavailable")
    text = proposal.get("text")
    if isinstance(text, str):
        try:
            raw = text.encode("utf-8")
        except UnicodeError:
            raise AcceptanceError("repair_proposal_encoding") from None
    else:
        encoded = proposal.get("text_base64")
        require(isinstance(encoded, str), "repair_proposal_text_missing")
        try:
            raw = base64.b64decode(encoded, validate=True)
            text = raw.decode("utf-8")
        except (ValueError, UnicodeError, binascii.Error):
            raise AcceptanceError("repair_proposal_encoding") from None
    require(0 < len(raw) <= SOURCE_LIMIT, "repair_proposal_size")
    size = proposal.get("bytes")
    require(type(size) is int and size == len(raw), "repair_proposal_size")
    require(proposal.get("sha256") == digest(raw), "repair_proposal_digest")
    return text


def verify(source: bytes, oracle: bytes, directory: Path) -> dict:
    """Run exact source and oracle bytes with no repository or account mounts."""
    require(isinstance(source, bytes) and 0 < len(source) <= SOURCE_LIMIT,
            "repair_source_size")
    require(isinstance(oracle, bytes) and 0 < len(oracle) <= 65536,
            "repair_oracle_size")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    candidate = directory / "source.py"
    tests = directory / "oracle.py"
    candidate.write_bytes(source)
    tests.write_bytes(oracle)
    command = [
        "/usr/bin/prlimit", "--cpu=10", "--as=805306368",
        "--fsize=262144", "--nofile=128", "--",
        "/usr/bin/bwrap", "--die-with-parent", "--unshare-all", "--new-session",
        "--clearenv", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
        "--dir", "/app", "--dir", "/app/scripts", "--dir", "/app/tests",
        "--ro-bind", str(Path(sys.prefix).resolve()), "/venv",
        "--ro-bind", str(candidate.resolve()), "/app/scripts/acp_quota_pilot.py",
        "--ro-bind", str(tests.resolve()), "/app/tests/test_quota.py",
        "--chdir", "/app", "--setenv", "HOME", "/tmp",
        "--setenv", "PATH", "/venv/bin:/usr/bin",
        "--setenv", "PYTHONPATH", "/app",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1",
        "/usr/bin/prlimit", "--nproc=64", "--", "/venv/bin/python",
        "-m", "pytest", "-p", "no:cacheprovider",
        "-q", "--tb=no", "/app/tests/test_quota.py",
    ]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=stderr, timeout=25, check=False)
            exit_code = result.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            exit_code = None
            timed_out = True
        stdout.seek(0)
        output = stdout.read(262144).decode("utf-8", errors="replace")
        stderr.seek(0)
        errors = stderr.read(OUTPUT_LIMIT).decode("utf-8", errors="replace")
    def count(label):
        matches = re.findall(r"(\d+) " + label + r"(?:,| in |$)", output)
        return int(matches[-1]) if matches else 0
    failures = re.findall(r"^FAILED tests/test_quota\.py::([^\s]+)", output, re.MULTILINE)
    passed, failed = count("passed"), count("failed")
    return {
        "passed": exit_code == 0 and passed == EXPECTED_TESTS and failed == 0,
        "passed_count": passed, "failed_count": failed,
        "failed_tests": [name.split("[", 1)[0] for name in failures],
        "exit_code": exit_code, "timed_out": timed_out,
        "source_sha256": digest(source), "oracle_sha256": digest(oracle),
        "stdout_tail": output[-OUTPUT_LIMIT:], "stderr_tail": errors,
    }


def preflight(seed: bytes, reference: bytes, oracle: bytes, directory: Path) -> dict:
    reference_result = verify(reference, oracle, directory / "reference")
    require(reference_result["passed"], "repair_reference_rejected")
    seed_result = verify(seed, oracle, directory / "seed")
    require(seed_result["exit_code"] == 1 and not seed_result["timed_out"]
            and seed_result["passed_count"] == 84 and seed_result["failed_count"] == 2
            and seed_result["failed_tests"] == ["test_arbitrary_precision_integer_times"] * 2,
            "repair_seed_not_expected_failure")
    return {"reference": reference_result, "seed": seed_result}


async def exercise(config: Path, state: Path, workspace_root: Path,
                   seed_source: Path, reference_source: Path, oracle_path: Path) -> dict:
    from fastmcp import Client
    from chatgpt_web_oauth_mcp.shell import JobRegistry

    os.umask(0o077)
    implementation = capture_implementation(Path(__file__).resolve().parents[1])
    seed, reference, oracle = (path.read_bytes() for path in
                               (seed_source, reference_source, oracle_path))
    try:
        seed_text = seed.decode("utf-8")
    except UnicodeError:
        raise AcceptanceError("repair_source_encoding") from None
    state = state.resolve()
    require(not state.exists() and not state.is_symlink(), "qa_state_must_be_fresh")
    # Check all frozen inputs before creating an ACP server or invoking a model.
    with tempfile.TemporaryDirectory(prefix="flash-repair-preflight-") as scratch:
        checks = await asyncio.to_thread(preflight, seed, reference, oracle, Path(scratch))
    workspace, _, _, token, owner, environment = prepare(
        config, state, workspace_root, ACCOUNT,
    )
    service = QaServer(state=state, environment=environment)
    registry = JobRegistry()
    delegate_id = None
    job_id = None
    report = {
        "status": "FAIL", "implementation": implementation,
        "account": ACCOUNT, "model": MODEL, "max_attempts": 1,
        "seed_sha256": digest(seed), "oracle_sha256": digest(oracle),
        "reference_sha256": digest(reference), "preflight": checks, "attempts": [],
        "runner_sha256": digest(Path(__file__).read_bytes()),
    }
    try:
        report["qa_pid"] = await service.start()
        async with Client(service.transport(token, owner)) as client:
            await select(client)
            started = time.monotonic()
            submission = await call(client, "delegate_task", {
                "cwd": str(workspace), "harness": ACCOUNT, "backend": "acp",
                "kind": "explore", "work_role": "proposal", "model": MODEL,
                "task": CONTRACT + "\nSOURCE:\n" + seed_text,
                "max_tool_calls": 1, "execution_timeout_seconds": 180, "wait_seconds": 0,
            })
            delegate_id = submission.get("delegate_id")
            attempt = {"delegate_id": delegate_id, "submission_status": submission.get("status"),
                       "submission_error": submission.get("error")}
            report["attempts"].append(attempt)
            require(isinstance(delegate_id, str) and re.fullmatch(r"[a-f0-9]{12}", delegate_id),
                    "repair_submission_rejected")
            final = await terminal(client, delegate_id, seconds=205)
            job_id = final.get("durable_job_id")
            attempt.update(status=final.get("status"), error=final.get("error"),
                           seconds=round(time.monotonic() - started, 3),
                           usage=(final.get("harness_metadata") or {}).get("usage"))
            require(final.get("status") == "succeeded" and final.get("execution_backend") == "acp",
                    "repair_delegate_failed")
            require((final.get("harness_metadata") or {}).get("tool_calls") == 0,
                    "repair_tools_observed")
            packet = await call(client, "delegate_packet", {
                "delegate_id": delegate_id, "purpose": "review",
            })
            source = proposal_source(packet).encode("utf-8")
            (state / "candidate.py").write_bytes(source)
            result = await asyncio.to_thread(verify, source, oracle, state / "candidate-check")
            attempt["verification"] = result
            require(result.get("passed_count", 0) + result.get("failed_count", 0) == EXPECTED_TESTS,
                    "repair_verification_unavailable")
            require(result["passed"], "repair_candidate_rejected")
            receipt = await call(client, "mark_result_consumed", {
                "kind": "delegate", "result_id": delegate_id,
            })
            require(receipt.get("success") is True, "repair_result_not_consumed")
            report["status"] = "PASS"
        return report
    except Exception as exc:
        report["error"] = (exc.code if isinstance(exc, AcpStateError) else
                           str(exc) if isinstance(exc, AcceptanceError) else "repair_internal_error")
        raise
    finally:
        cleanup_errors = []
        if delegate_id:
            try:
                if service.process is None or service.process.poll() is not None:
                    await service.start()
                async with Client(service.transport(token, owner)) as client:
                    await select(client)
                    current = await status(client, delegate_id)
                    job_id = current.get("durable_job_id") or job_id
                    if not current.get("completed"):
                        await call(client, "delegate_cancel", {
                            "delegate_id": delegate_id, "force_running": True,
                            "rationale": "Disposable Flash correction cleanup",
                        })
                        current = await terminal(client, delegate_id, seconds=20)
                    if current.get("status") in {"timed_out", "interrupted"}:
                        await call(client, "delegate_reconcile", {
                            "delegate_id": delegate_id, "action": "abandon",
                            "reason": "Disposable single-attempt correction finished",
                        })
                    await call(client, "mark_result_consumed", {
                        "kind": "delegate", "result_id": delegate_id,
                    })
            except Exception:
                cleanup_errors.append("repair_delegate_cleanup_failed")
                if job_id:
                    current_job = registry.job_status(job_id=job_id, state_dir=state)
                    if current_job.get("status") == "running":
                        registry.kill_job(job_id=job_id, state_dir=state, signal_name="TERM")
        try:
            await service.close()
        except Exception:
            cleanup_errors.append("repair_service_cleanup_failed")
        try:
            shutil.rmtree(workspace)
        except OSError:
            cleanup_errors.append("repair_workspace_cleanup_failed")
        report["cleanup_errors"] = cleanup_errors
        cleanup_failed_after_success = cleanup_errors and report["status"] == "PASS"
        if cleanup_failed_after_success:
            report.update(status="FAIL", error="repair_cleanup_failed")
        write_private_json(state / "report.json", report)
        if cleanup_failed_after_success:
            raise AcceptanceError("repair_cleanup_failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    live = modes.add_parser("live", help="Run one fresh Flash correction after preflight")
    live.add_argument("--config", type=Path, required=True)
    live.add_argument("--state", type=Path, required=True,
                      help="New private result directory; must not already exist")
    live.add_argument("--workspace", type=Path, required=True,
                      help="Approved existing root for a disposable workspace")
    live.add_argument("--seed-source", type=Path, required=True)
    live.add_argument("--reference-source", type=Path, required=True)
    live.add_argument("--oracle", type=Path, required=True)
    replay = modes.add_parser("verify", help="Check retained source offline, with no model call")
    replay.add_argument("--source", type=Path, required=True)
    replay.add_argument("--oracle", type=Path, required=True)
    replay.add_argument("--state", type=Path, required=True,
                        help="New private result directory; must not already exist")
    args = parser.parse_args()
    try:
        if args.mode == "verify":
            os.umask(0o077)
            args.state.mkdir(mode=0o700, parents=True, exist_ok=False)
            result = verify(args.source.read_bytes(), args.oracle.read_bytes(),
                            args.state / "check")
            report = {"status": "PASS" if result["passed"] else "FAIL",
                      "model_calls": 0, "verification": result,
                      "runner_sha256": digest(Path(__file__).read_bytes())}
            write_private_json(args.state / "report.json", report)
        else:
            report = asyncio.run(exercise(args.config, args.state, args.workspace,
                                         args.seed_source, args.reference_source, args.oracle))
    except Exception as exc:
        code = (exc.code if isinstance(exc, AcpStateError) else
                str(exc) if isinstance(exc, AcceptanceError) else "repair_internal_error")
        print(json.dumps({"status": "FAIL", "error": code}), flush=True)
        return 1
    if args.mode == "verify":
        print(json.dumps({"status": report["status"], "model_calls": 0,
                          "source_sha256": result["source_sha256"],
                          "oracle_sha256": result["oracle_sha256"]}), flush=True)
    else:
        print(json.dumps({"status": report["status"], "attempts": len(report["attempts"]),
                          "seed_sha256": report["seed_sha256"],
                          "oracle_sha256": report["oracle_sha256"]}), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
