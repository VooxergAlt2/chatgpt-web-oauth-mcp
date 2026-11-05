"""Independent no-provider regressions for one Flash repair attempt."""
from __future__ import annotations

import base64
import hashlib

import pytest

from scripts import acp_flash_repair_acceptance as qa


def packet(source="def example():\n    return 'ё'\n"):
    raw = source.encode("utf-8")
    return {"success": True, "packet": {"proposal": {
        "available": True, "accepted": False,
        "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "text": source,
    }}}


def test_proposal_source_preserves_exact_multibyte_source():
    value = packet()
    assert qa.proposal_source(value) == value["packet"]["proposal"]["text"]


def test_proposal_source_decodes_exact_base64_bytes():
    value = packet()
    proposal = value["packet"]["proposal"]
    text = proposal.pop("text")
    proposal["text_base64"] = base64.b64encode(text.encode("utf-8")).decode("ascii")
    assert qa.proposal_source(value) == text


@pytest.mark.parametrize(("key", "value"), [
    ("available", False), ("available", 1),
    ("accepted", True), ("accepted", None),
    ("bytes", 1), ("bytes", True), ("bytes", "32"),
    ("sha256", "0" * 64), ("sha256", None),
    ("text", "def changed():\n    pass\n"),
])
def test_proposal_rejects_unverified_flags_and_changed_artifact(key, value):
    reply = packet()
    reply["packet"]["proposal"][key] = value
    with pytest.raises((qa.AcceptanceError, ValueError)):
        qa.proposal_source(reply)


def test_proposal_does_not_extract_source_from_failed_tool_response():
    value = packet()
    value["success"] = False
    with pytest.raises((qa.AcceptanceError, ValueError)):
        qa.proposal_source(value)


@pytest.mark.parametrize("encoded", ["%%%", "a", "////", "ZGVm!"])
def test_proposal_rejects_invalid_base64_or_utf8(encoded):
    value = packet()
    del value["packet"]["proposal"]["text"]
    value["packet"]["proposal"]["text_base64"] = encoded
    with pytest.raises((qa.AcceptanceError, ValueError, UnicodeError)):
        qa.proposal_source(value)

@pytest.mark.parametrize("value", [None, [], "bad"])
def test_nonobject_packet_is_a_classified_failure(value):
    with pytest.raises((qa.AcceptanceError, ValueError)):
        qa.proposal_source(value)


def test_preflight_reference_failure_prevents_seed_and_submission(monkeypatch, tmp_path):
    import asyncio

    seen = []
    def verify(source, oracle, directory):
        seen.append(source)
        return {"passed": False}
    def forbidden_prepare(*args):
        pytest.fail("provider setup happened before reference passed")
    monkeypatch.setattr(qa, "verify", verify)
    monkeypatch.setattr(qa, "prepare", forbidden_prepare)
    monkeypatch.setattr(qa, "capture_implementation", lambda path: {})
    paths = []
    for name in ("seed", "reference", "oracle"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        paths.append(path)
    with pytest.raises(qa.AcceptanceError, match="repair_reference_rejected"):
        asyncio.run(qa.exercise(tmp_path / "config", tmp_path / "state",
                                tmp_path, *paths))
    assert seen == [b"reference"]
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("bad", [
    {"exit_code": 2},
    {"timed_out": True},
    {"passed_count": 83, "failed_count": 3},
    {"failed_tests": ["test_unrelated"] * 2},
])
def test_seed_infrastructure_or_other_failure_never_qualifies(monkeypatch, tmp_path, bad):
    results = iter([
        {"passed": True},
        {"passed": False, "exit_code": 1, "timed_out": False,
         "passed_count": 84, "failed_count": 2,
         "failed_tests": ["test_arbitrary_precision_integer_times"] * 2} | bad,
    ])
    monkeypatch.setattr(qa, "verify", lambda *args: next(results))
    with pytest.raises(qa.AcceptanceError, match="repair_seed_not_expected_failure"):
        qa.preflight(b"seed", b"reference", b"oracle", tmp_path)


def test_preflight_returns_the_two_exact_checks(monkeypatch, tmp_path):
    seen = []
    reference = {"passed": True}
    seed = {
        "passed": False, "exit_code": 1, "timed_out": False,
        "passed_count": 84, "failed_count": 2,
        "failed_tests": ["test_arbitrary_precision_integer_times"] * 2,
    }
    def verify(source, oracle, directory):
        seen.append((source, oracle, directory.name))
        return reference if source == b"reference" else seed
    monkeypatch.setattr(qa, "verify", verify)
    assert qa.preflight(b"seed", b"reference", b"oracle", tmp_path) == {
        "reference": reference, "seed": seed,
    }
    assert seen == [(b"reference", b"oracle", "reference"), (b"seed", b"oracle", "seed")]


@pytest.mark.parametrize(("terminal_status", "close_fails", "candidate_passes", "expected_error"), [
    ("failed", False, True, "repair_delegate_failed"),
    ("timed_out", False, True, "repair_delegate_failed"),
    ("succeeded", False, False, "repair_candidate_rejected"),
    ("succeeded", False, None, "repair_verification_unavailable"),
    ("succeeded", True, True, "repair_cleanup_failed"),
])
def test_terminal_outcome_is_recorded_and_cleanup_cannot_hide_failure(
    monkeypatch, tmp_path, terminal_status, close_fails, candidate_passes, expected_error
):
    import asyncio
    import json
    import fastmcp

    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    operations = []
    owned = "a" * 12
    final = {"status": terminal_status, "completed": True,
             "execution_backend": "acp", "durable_job_id": "job_owned",
             "harness_metadata": {"tool_calls": 0},
             "error": None if terminal_status == "succeeded" else
                 {"code": "acp_timeout" if terminal_status == "timed_out" else "transport_error"}}

    class Client:
        def __init__(self, transport): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass

    class Service:
        process = type("Process", (), {"poll": lambda self: None})()
        def __init__(self, **kwargs): pass
        async def start(self):
            operations.append("start")
            return 123
        def transport(self, *args): return None
        async def close(self):
            operations.append("close")
            if close_fails:
                raise RuntimeError("simulated shutdown failure")

    def prepare(*args):
        state.mkdir()
        workspace.mkdir()
        (workspace / "fixture").write_text("disposable")
        return workspace, None, None, "token", "owner", {}

    async def call(client, name, arguments):
        operations.append((name, arguments))
        if name == "delegate_task":
            return {"delegate_id": owned, "status": "running"}
        if name == "delegate_packet":
            return packet()
        return {"success": True}

    async def current(*args, **kwargs): return dict(final)
    async def select(*args): pass

    monkeypatch.setattr(fastmcp, "Client", Client)
    monkeypatch.setattr(qa, "QaServer", Service)
    monkeypatch.setattr(qa, "prepare", prepare)
    monkeypatch.setattr(qa, "preflight", lambda *args: {})
    monkeypatch.setattr(qa, "verify", lambda *args: {
        "passed": bool(candidate_passes),
        "passed_count": 0 if candidate_passes is None else (86 if candidate_passes else 84),
        "failed_count": 0 if candidate_passes is None or candidate_passes else 2,
    })
    monkeypatch.setattr(qa, "capture_implementation", lambda path: {})
    monkeypatch.setattr(qa, "call", call)
    monkeypatch.setattr(qa, "status", current)
    monkeypatch.setattr(qa, "terminal", current)
    monkeypatch.setattr(qa, "select", select)
    paths = []
    for name in ("seed", "reference", "oracle"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        paths.append(path)
    with pytest.raises(qa.AcceptanceError, match=expected_error):
        asyncio.run(qa.exercise(tmp_path / "config", state, tmp_path, *paths))
    report = json.loads((state / "report.json").read_text())
    assert report["status"] == "FAIL"
    assert report["attempts"][0]["status"] == terminal_status
    assert report["attempts"][0]["error"] == final["error"]
    submissions = [
        item[1] for item in operations if isinstance(item, tuple) and item[0] == "delegate_task"
    ]
    assert len(submissions) == 1
    assert submissions[0]["model"] == "gemini-3.8-flash-medium"
    assert submissions[0]["work_role"] == "proposal"
    assert "resume_from_delegate_id" not in submissions[0]
    assert ("mark_result_consumed", {"kind": "delegate", "result_id": owned}) in operations
    assert operations[-1] == "close"
    assert not workspace.exists()


@pytest.mark.parametrize(("error", "expected"), [
    (qa.AcpStateError("acp_workspace_not_allowed"), "acp_workspace_not_allowed"),
    (RuntimeError("private provider payload"), "repair_internal_error"),
])
def test_cli_classifies_setup_errors_without_leaking_payload(monkeypatch, capsys, error, expected):
    import json
    import sys

    async def fail(*args):
        raise error
    monkeypatch.setattr(qa, "exercise", fail)
    monkeypatch.setattr(sys, "argv", ["repair", "live", "--config", "config", "--state", "state",
        "--workspace", "root", "--seed-source", "seed", "--reference-source", "ref",
        "--oracle", "oracle"])
    assert qa.main() == 1
    assert json.loads(capsys.readouterr().out) == {"status": "FAIL", "error": expected}


def test_process_limit_is_applied_inside_namespace(monkeypatch, tmp_path):
    import subprocess

    commands = []
    def run(command, *, stdin, stdout, stderr, timeout, check):
        commands.append(command)
        stdout.write(b"86 passed in 0.01s\n")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(qa.subprocess, "run", run)
    result = qa.verify(b"source", b"oracle", tmp_path / "check")
    assert result["passed"]
    command = commands[0]
    boundary = command.index("/usr/bin/bwrap")
    assert "--nproc=64" not in command[:boundary]
    limit = command.index("--nproc=64")
    assert limit > command.index("--unshare-all")
    assert command[limit - 1:limit + 3] == [
        "/usr/bin/prlimit", "--nproc=64", "--", "/venv/bin/python",
    ]
    assert (tmp_path / "check" / "source.py").read_bytes() == b"source"
    assert (tmp_path / "check" / "oracle.py").read_bytes() == b"oracle"


@pytest.mark.parametrize("passed", [True, False])
def test_verify_cli_never_invokes_provider(monkeypatch, tmp_path, capsys, passed):
    import json
    import sys

    def forbidden(*args):
        pytest.fail("offline recheck invoked provider")
    monkeypatch.setattr(qa, "exercise", forbidden)
    monkeypatch.setattr(qa, "verify", lambda *args: {
        "passed": passed, "source_sha256": "source", "oracle_sha256": "oracle",
    })
    source, oracle, state = tmp_path / "source", tmp_path / "oracle", tmp_path / "state"
    source.write_bytes(b"source")
    oracle.write_bytes(b"oracle")
    monkeypatch.setattr(sys, "argv", ["repair", "verify", "--source", str(source),
        "--oracle", str(oracle), "--state", str(state)])
    assert qa.main() == (0 if passed else 1)
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    report = json.loads((state / "report.json").read_text())
    assert report["status"] == ("PASS" if passed else "FAIL")
    assert report["model_calls"] == 0
