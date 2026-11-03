"""Writer worker integration: real copies/checkpoints/verifier, simulated native ACP."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from chatgpt_web_oauth_mcp import acp_worker
from chatgpt_web_oauth_mcp.acp_session_store import AcpSessionIdentity, AcpSessionStore, AcpStateError
from chatgpt_web_oauth_mcp.acp_workspace import AcpWorkspace, capture_source_snapshot
from chatgpt_web_oauth_mcp.delegate_code_verification import (
    build_code_contract, capture_code_baseline,
)

REFERENCE = "c" * 32
TURN = "writer-fixture-1"
BEFORE = "def add(a, b):\n    return a - b\n"
AFTER = "def add(a, b):\n    return a + b\n"
CHECK = """/usr/bin/python3 -I -c "import runpy; assert runpy.run_path('calc.py')['add'](2, 3) == 5" """

pytestmark = pytest.mark.skipif(not Path("/usr/bin/bwrap").exists(), reason="Linux bwrap required")


def setup_worker(tmp_path, monkeypatch, *, commands=None, max_changed_files=2,
                 initial=BEFORE, mutate=True):
    main = tmp_path / "main"
    main.mkdir()
    def git(cwd, *args):
        return subprocess.check_output(["/usr/bin/git", "-C", str(cwd), *args],
                                       stderr=subprocess.DEVNULL).decode().strip()
    git(main, "init", "-q")
    git(main, "config", "user.name", "ACP fixture")
    git(main, "config", "user.email", "fixture@example.invalid")
    (main / "calc.py").write_text(initial)
    (main / "untouched.txt").write_text("source sentinel\n")
    git(main, "add", ".")
    git(main, "commit", "-qm", "fixture baseline")
    source = tmp_path / "writer"
    git(main, "worktree", "add", "-qb", "writer-fixture", str(source))
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    (profile / "antigravity-acp").mkdir(mode=0o700)
    backend = SimpleNamespace(
        config_path=tmp_path / "operator.json", server=Path("/bin/true"), helper=Path("/bin/true"),
        state_root=state, accounts={"fixture": {"profile": profile}},
        validate=lambda **kwargs: None,
        cancel_path=lambda delegate_id: state / (delegate_id + ".cancel"),
    )
    monkeypatch.setattr(acp_worker.AcpBackend, "load", lambda path: backend)
    contract = build_code_contract(
        files_in_scope=["calc.py"], acceptance_criteria=["addition is correct"],
        done_means=["independent checks pass"], verification_commands=commands or [CHECK],
        max_changed_files=max_changed_files, max_added_lines=20, max_deleted_lines=20,
    )
    task = SimpleNamespace(kind="code", execution_backend="acp", cwd=source, code_contract=contract)
    baseline, error = capture_code_baseline(task)
    assert error is None
    request = {
        "delegate_id": TURN, "account": "fixture", "owner": "a" * 64,
        "workspace": str(source), "thinking": "medium", "scope_digest": "b" * 64,
        "session_ref": REFERENCE, "turn_id": TURN, "prompt": "Fix addition in calc.py",
        "timeout": 45, "max_tool_calls": 8, "kind": "code",
        "scope": {"files_in_scope": ["calc.py"], "out_of_scope": []},
        "code_contract": contract, "code_baseline": baseline,
    }
    control = SimpleNamespace(
        opened=0, active=False, views=[], checks=[], verification_started=None,
        mutation=(lambda view: (view / "calc.py").write_text(AFTER)) if mutate else lambda view: None,
        stop_reason="end_turn", manifest_status="succeeded", after_native=None,
    )

    class SimulatedNative:
        def __init__(self, _server, _helper, _profile, original, _control, _history,
                     *, workspace_view, writable_workspace):
            assert original == source and writable_workspace is True
            assert workspace_view != source and not (workspace_view / ".git").exists()
            self.view = workspace_view
            control.views.append(workspace_view)
            self.options = [
                {"id": key, "current_value": value, "options": [{"value": value}]}
                for key, value in (("model", "gemini-3.8-flash"),
                                   ("thinking_level", "medium"), ("mode", "default"))
            ]

        def transport(self, *, gateway=None):
            assert gateway is not None
            @asynccontextmanager
            async def connection(client):
                control.opened += 1
                control.active = True
                self.client = client
                try:
                    yield self
                finally:
                    control.active = False
            return connection

        async def initialize(self, **kwargs):
            return {"protocol_version": 1, "agent_capabilities": {
                "load_session": True, "mcp_capabilities": {"http": True},
            }}

        async def new_session(self, **kwargs):
            assert kwargs["cwd"] == str(source)
            assert kwargs["mcp_servers"]
            return {"session_id": "native-writer-fixture", "config_options": self.options}

        async def load_session(self, **kwargs):
            return {"session_id": "native-writer-fixture", "config_options": self.options}

        async def prompt(self, **kwargs):
            permission = await self.client.request_permission(
                "native-writer-fixture",
                {"tool_call_id": "native-edit", "kind": "edit", "title": "edit detached copy"},
                [{"option_id": "allow", "kind": "allow_once", "name": "Allow once"}],
            )
            assert permission["outcome"] == {"outcome": "selected", "option_id": "allow"}
            control.mutation(self.view)
            if control.after_native is not None:
                control.after_native()
            report = {"status": control.manifest_status, "summary": "candidate result", "findings": []}
            await self.client.session_update("native-writer-fixture", {
                "session_update": "agent_message_chunk", "content": {"text": json.dumps(report)},
            })
            return {"stop_reason": control.stop_reason}

        async def cancel(self, **kwargs):
            return None

    monkeypatch.setattr(acp_worker, "AcpSandbox", SimulatedNative)
    original_checks = acp_worker._run_verification_commands
    def checks(*args, **kwargs):
        assert not control.active, "native must be torn down before verification"
        assert (source / "calc.py").read_text() == initial
        assert kwargs.get("argv_factory") is not None
        rows = original_checks(*args, **kwargs)
        control.checks.extend(rows)
        return rows
    monkeypatch.setattr(acp_worker, "_run_verification_commands", checks)
    original_emit = acp_worker.emit
    def emit(event, value):
        if value.get("progress", {}).get("event") == "verifying" and control.verification_started is not None:
            control.verification_started.set()
        original_emit(event, value)
    monkeypatch.setattr(acp_worker, "emit", emit)
    identity = AcpSessionIdentity(owner=request["owner"], account=request["account"],
                                  workspace=str(source), scope_digest=request["scope_digest"],
                                  thinking=request["thinking"])
    return SimpleNamespace(backend=backend, request=request, source=source, main=main, state=state,
                           control=control, identity=identity, initial=initial)


def checkpoint(fixture):
    return AcpSessionStore(fixture.state / "sessions").load(REFERENCE, fixture.identity)


def execute(fixture):
    return asyncio.run(acp_worker.execute(fixture.backend.config_path, fixture.request))


def assert_unpromoted(fixture):
    assert (fixture.source / "calc.py").read_text() == fixture.initial
    assert (fixture.source / "untouched.txt").read_text() == "source sentinel\n"
    assert not fixture.control.active
    assert not list((fixture.state / "workspaces").iterdir())


def test_writer_success_promotes_only_after_real_independent_check(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    result = execute(fixture)
    assert result["success"] is True, result
    assert (fixture.source / "calc.py").read_text() == AFTER
    assert (fixture.main / "calc.py").read_text() == BEFORE
    receipt = result["harness_metadata"]["acp_code_receipt"]
    assert receipt["changed_files"] == ["calc.py"]
    assert receipt["snapshot_digest"] == capture_source_snapshot(fixture.source, require_clean=False)["digest"]
    assert fixture.control.checks[0]["passed"] is True
    assert checkpoint(fixture)["phase"] == "completed"
    assert not list((fixture.state / "workspaces").iterdir())


def test_writer_zero_change_checks_pass_means_already_satisfied(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch, initial=AFTER, mutate=False)
    result = execute(fixture)
    assert result["success"] is True
    assert result["harness_metadata"]["acp_code_receipt"]["changed_files"] == []
    assert_unpromoted(fixture)


@pytest.mark.parametrize("command", ["/bin/sh -c 'exit 17'", "printf forbidden > calc.py"])
def test_failed_or_mutating_independent_check_never_promotes(tmp_path, monkeypatch, command):
    fixture = setup_worker(tmp_path, monkeypatch, commands=[command])
    result = execute(fixture)
    assert result["success"] is False
    assert result["error"]["code"] == "acp_code_verification_failed"
    assert fixture.control.checks[0]["passed"] is False
    assert "acp_code_receipt" not in result["harness_metadata"]
    assert checkpoint(fixture)["phase"] == "failed"
    assert_unpromoted(fixture)


def test_native_out_of_scope_candidate_is_rejected_before_checks(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    def mutate(view):
        (view / "calc.py").write_text(AFTER)
        (view / "untouched.txt").write_text("forbidden change\n")
    fixture.control.mutation = mutate
    result = execute(fixture)
    assert result["success"] is False
    assert result["error"]["code"] == "acp_writer_scope_violation"
    rejection = result["harness_metadata"]["writer_rejection"]
    assert rejection["available"] is True
    assert rejection["changed_file_count"] == 2
    assert rejection["outside_scope_count"] == 1
    assert rejection["changes"][0]["path"] == "untouched.txt"
    assert rejection["changes"][0]["in_scope"] is False
    assert "forbidden change" not in json.dumps(rejection)
    assert checkpoint(fixture)["result"]["harness_metadata"]["writer_rejection"] == rejection
    assert fixture.control.checks == []
    assert_unpromoted(fixture)


def test_zero_file_budget_rejects_real_native_edit_before_checks(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch, max_changed_files=0)
    result = execute(fixture)
    assert result["success"] is False
    assert result["error"]["code"] == "acp_code_change_budget_exceeded"
    assert fixture.control.checks == []
    assert_unpromoted(fixture)


def test_partial_native_manifest_is_not_promoted(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    fixture.control.manifest_status = "partial"
    result = execute(fixture)
    assert result["success"] is False
    assert result["error"]["code"] == "acp_writer_incomplete_manifest"
    assert fixture.control.checks == []
    assert_unpromoted(fixture)


def test_persisted_cancel_before_fast_checks_prevents_promotion(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    fixture.control.after_native = lambda: fixture.backend.cancel_path(TURN).write_text("cancel")
    # The watcher polls at 100ms. A completed check is deliberately faster: the
    # persisted cancellation must be re-read at the promotion boundary itself.
    monkeypatch.setattr(acp_worker, "_run_verification_commands", lambda cwd, commands, **kwargs: [
        {"command": command, "passed": True, "exit_code": 0, "stdout": "", "stderr": ""}
        for command in commands
    ])
    result = execute(fixture)
    assert result["status"] == "cancelled", result
    assert result["success"] is False
    assert checkpoint(fixture)["phase"] == "cancelled"
    assert_unpromoted(fixture)


def test_cancel_during_real_verifier_reaps_command_and_preserves_source(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch, commands=["/bin/sleep 30"])
    async def scenario():
        fixture.control.verification_started = asyncio.Event()
        task = asyncio.create_task(acp_worker.execute(fixture.backend.config_path, fixture.request))
        await fixture.control.verification_started.wait()
        fixture.backend.cancel_path(TURN).write_text("cancel")
        return await task
    started = time.monotonic()
    result = asyncio.run(asyncio.wait_for(scenario(), 5))
    assert time.monotonic() - started < 5
    assert result["status"] == "cancelled"
    assert result["success"] is False
    assert all(row.get("cleanup_failed") is not True for row in fixture.control.checks)
    assert checkpoint(fixture)["phase"] == "cancelled"
    assert_unpromoted(fixture)


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("stage", ["before_finalization", "during_checks", "after_promotion"])
def test_postprocessing_crash_is_uncertain_and_abandonable_without_replay(tmp_path, monkeypatch, stage):
    fixture = setup_worker(tmp_path, monkeypatch)
    if stage == "before_finalization":
        async def crash(**kwargs):
            raise SimulatedCrash("crash after native teardown")
        monkeypatch.setattr(acp_worker, "_finish_writer", crash)
    elif stage == "during_checks":
        def crash(*args, **kwargs):
            raise SimulatedCrash("crash during independent checks")
        monkeypatch.setattr(acp_worker, "_run_verification_commands", crash)
    else:
        original = AcpWorkspace.promote
        def crash(self, **kwargs):
            original(self, **kwargs)
            raise SimulatedCrash("crash after source export")
        monkeypatch.setattr(AcpWorkspace, "promote", crash)
    with pytest.raises(SimulatedCrash):
        execute(fixture)
    state = checkpoint(fixture)
    assert state["phase"] == "running"
    assert state["result"]["outcome_uncertain"] is True
    if stage == "after_promotion":
        assert (fixture.source / "calc.py").read_text() == AFTER
    else:
        assert_unpromoted(fixture)
        replay = {**fixture.request, "delegate_id": "writer-fixture-2", "turn_id": "writer-fixture-2"}
        with pytest.raises(AcpStateError, match="outcome_unknown"):
            asyncio.run(acp_worker.execute(fixture.backend.config_path, replay))
        assert fixture.control.opened == 1
    store = AcpSessionStore(fixture.state / "sessions")
    outcome = store.abandon(REFERENCE, fixture.identity, turn_id=TURN,
                            reason="Review interrupted worker output before a new task")
    assert outcome["resume_allowed"] is False
    assert checkpoint(fixture)["phase"] == "abandoned"
