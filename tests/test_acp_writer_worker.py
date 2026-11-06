"""Writer worker integration: real copies/checkpoints/verifier, simulated native ACP."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
import subprocess
import threading
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
                 initial=BEFORE, mutate=True, preflight_commands=None):
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
        **({"verification_preflight_commands": preflight_commands}
           if preflight_commands is not None else {}),
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
        opened=0, active=False, views=[], checks=[], check_calls=[], session_calls=[], prompts=[], verification_started=None,
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

        def transport(self, *, gateway=None, configuration_observer=None):
            self.configuration_observer = configuration_observer
            assert gateway is not None
            self.gateway = gateway
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
            control.session_calls.append(("new", kwargs))
            assert kwargs["cwd"] == str(source)
            assert kwargs["mcp_servers"]
            return {"session_id": "native-writer-fixture", "config_options": self.options}

        async def load_session(self, **kwargs):
            control.session_calls.append(("load", kwargs))
            return {"session_id": "native-writer-fixture", "config_options": self.options}

        async def prompt(self, **kwargs):
            control.prompts.append(kwargs["prompt"][0]["text"])
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
        control.check_calls.append({"commands": list(args[1]), "opened": control.opened,
                                    "native_constructed": bool(control.views),
                                    "candidate": (args[0] / "calc.py").read_text()})
        rows = original_checks(*args, **kwargs)
        control.checks.extend(rows)
        return rows
    monkeypatch.setattr(acp_worker, "_run_verification_commands", checks)
    original_emit = acp_worker.emit
    def emit(event, value):
        progress = value.get("progress", {})
        if (progress.get("event") == "phase"
                and progress.get("runtime", {}).get("component") == "worker"
                and progress["runtime"].get("phase") == "verification"
                and control.verification_started is not None):
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


def _forbid_native_construction(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("preflight must finish before constructing native sandbox or runner")
    monkeypatch.setattr(acp_worker, "AcpSandbox", unexpected)
    monkeypatch.setattr(acp_worker, "AcpSessionRunner", unexpected)


def test_preflight_checks_environment_before_native_and_final_check_after_fix(tmp_path, monkeypatch):
    mount_namespace = os.readlink("/proc/self/ns/mnt")
    network_namespace = os.readlink("/proc/self/ns/net")
    program = (
        "import json, os, pathlib; "
        f"assert os.readlink('/proc/self/ns/mnt') != {mount_namespace!r}; "
        f"assert os.readlink('/proc/self/ns/net') != {network_namespace!r}; "
        "assert not pathlib.Path('/profile').exists(); "
        "assert not pathlib.Path('.git').exists(); print('PREFLIGHT_READY')"
    )
    command = "/usr/bin/python3 -I -c " + shlex.quote(program)
    fixture = setup_worker(tmp_path, monkeypatch, preflight_commands=[command])
    result = execute(fixture)
    assert result["success"] is True, result
    assert fixture.control.check_calls == [
        {"commands": [command], "opened": 0, "native_constructed": False, "candidate": BEFORE},
        {"commands": [CHECK], "opened": 1, "native_constructed": True, "candidate": AFTER},
    ]
    metadata = result["harness_metadata"]
    preflight = metadata["verification_preflight"]
    assert preflight["version"] == 1 and preflight["status"] == "passed"
    assert preflight["execution_boundary"] == "acp_verification_sandbox"
    assert preflight["commands"] == fixture.control.checks[:1]
    assert preflight["commands"][0]["passed"] is True
    assert preflight["commands"][0]["stdout"].strip() == "PREFLIGHT_READY"
    receipt = metadata["acp_code_receipt"]
    for key, raw_rows in (("verification_preflight_commands", preflight["commands"]),
                          ("verification_commands", fixture.control.checks[1:])):
        assert len(receipt[key]) == len(raw_rows) == 1
        assert receipt[key] == [{
            "command": raw_rows[0]["command"], "passed": True, "exit_code": 0,
            "stdout": raw_rows[0]["stdout"], "stderr": raw_rows[0]["stderr"],
            "stdout_truncated": False, "stderr_truncated": False,
        }]
    assert (fixture.source / "calc.py").read_text() == AFTER
    assert (fixture.main / "calc.py").read_text() == BEFORE
    assert "preflight" in metadata["acp_worker_diagnostics"]["phase_durations_seconds"]
    saved = checkpoint(fixture)["result"]["harness_metadata"]
    for rows in (saved["verification_preflight"]["commands"],
                 saved["acp_code_receipt"]["verification_preflight_commands"]):
        assert all("stdout" not in row and "stderr" not in row for row in rows)


@pytest.mark.parametrize(("command", "detail"), [
    ("/usr/bin/python3 -m no_such_module", "no_such_module"),
    ("printf forbidden > calc.py", "read-only"),
])
def test_preflight_failure_never_constructs_native_or_promotes(tmp_path, monkeypatch, command, detail):
    fixture = setup_worker(tmp_path, monkeypatch, preflight_commands=[command])
    _forbid_native_construction(monkeypatch)
    result = execute(fixture)
    assert result["success"] is False and result["status"] == "failed"
    assert result["error"]["code"] == "acp_verification_preflight_failed"
    metadata = result["harness_metadata"]
    preflight = metadata["verification_preflight"]
    assert preflight["version"] == 1 and preflight["status"] == "failed"
    assert preflight["execution_boundary"] == "acp_verification_sandbox"
    assert preflight["commands"] == fixture.control.checks
    assert len(preflight["commands"]) == 1 and preflight["commands"][0]["passed"] is False
    assert detail in preflight["commands"][0]["stderr"].lower()
    assert [call["commands"] for call in fixture.control.check_calls] == [[command]]
    assert not fixture.control.views and not fixture.control.session_calls and not fixture.control.prompts
    assert "acp_code_receipt" not in metadata
    runtime = metadata["acp_worker_diagnostics"]
    assert runtime["failure_phase"] == "preflight"
    assert not {"native", "verification", "promotion"} & runtime["phase_durations_seconds"].keys()
    assert_unpromoted(fixture)


@pytest.mark.parametrize(("timeout", "expected_budget"), [(45, 24.75), (120, 30)])
def test_preflight_timeout_uses_bounded_budget_and_stops_before_native(
        tmp_path, monkeypatch, timeout, expected_budget):
    fixture = setup_worker(tmp_path, monkeypatch, preflight_commands=["/bin/sleep 30"])
    fixture.request["timeout"] = timeout
    _forbid_native_construction(monkeypatch)
    monkeypatch.setattr(acp_worker, "time", SimpleNamespace(monotonic=lambda: 1000.0, time=time.time))
    original_checks = acp_worker._run_verification_commands
    budgets = []

    def short_real_check(*args, **kwargs):
        budgets.append(kwargs["timeout_seconds"])
        return original_checks(*args, **{**kwargs, "timeout_seconds": 0.05})

    monkeypatch.setattr(acp_worker, "_run_verification_commands", short_real_check)
    started = time.monotonic()
    result = execute(fixture)
    assert time.monotonic() - started < 5
    assert budgets == [expected_budget]
    assert result["success"] is False
    assert result["error"]["code"] == "acp_verification_preflight_failed"
    metadata = result["harness_metadata"]
    assert metadata["execution_budget"]["preflight_seconds"] == expected_budget
    assert metadata["verification_preflight"]["status"] == "timed_out"
    assert metadata["verification_preflight"]["commands"][0]["timed_out"] is True
    assert not metadata["verification_preflight"]["commands"][0].get("cleanup_failed")
    assert_unpromoted(fixture)


def test_preflight_exhausted_after_workspace_prepare_skips_all_launches(tmp_path, monkeypatch):
    commands = ["/usr/bin/true", "/usr/bin/python3 -V"]
    fixture = setup_worker(tmp_path, monkeypatch, preflight_commands=commands)
    _forbid_native_construction(monkeypatch)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(acp_worker, "time", SimpleNamespace(monotonic=lambda: clock.now, time=time.time))
    original_prepare = AcpWorkspace.prepare
    original_checks = acp_worker._run_verification_commands
    budgets = []

    def prepare_with_cost(*args, **kwargs):
        workspace = original_prepare(*args, **kwargs)
        clock.now += 25  # Exceeds 45 total - 3 safety - 11.25 reserve - 6 native floor.
        return workspace

    def unexpected(*args, **kwargs):
        pytest.fail("exhausted preflight reached argv construction or process launch")

    def checks_without_launch(*args, **kwargs):
        budgets.append(kwargs["timeout_seconds"])
        with monkeypatch.context() as guard:
            guard.setattr(subprocess, "Popen", unexpected)
            return original_checks(*args, **kwargs)

    monkeypatch.setattr(AcpWorkspace, "prepare", prepare_with_cost)
    monkeypatch.setattr(acp_worker, "verification_command", unexpected)
    monkeypatch.setattr(acp_worker, "_run_verification_commands", checks_without_launch)
    result = execute(fixture)
    assert budgets == [0]
    assert result["success"] is False
    assert result["error"]["code"] == "acp_verification_preflight_failed"
    metadata = result["harness_metadata"]
    assert metadata["execution_budget"]["preflight_seconds"] == 0
    assert metadata["execution_budget"]["native_session_seconds"] == 0
    preflight = metadata["verification_preflight"]
    assert preflight["status"] == "timed_out"
    assert [row["command"] for row in preflight["commands"]] == commands
    assert all(row["timed_out"] and row["not_run"] and not row["passed"]
               for row in preflight["commands"])
    assert not fixture.control.views and not fixture.control.session_calls
    assert_unpromoted(fixture)


def test_successful_preflight_cost_reduces_native_budget_and_preserves_final_reserve(tmp_path, monkeypatch):
    command = "/usr/bin/python3 -V"
    fixture = setup_worker(tmp_path, monkeypatch, preflight_commands=[command])
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(acp_worker, "time", SimpleNamespace(monotonic=lambda: clock.now, time=time.time))
    original_checks = acp_worker._run_verification_commands
    original_run = acp_worker.AcpSessionRunner.run
    check_budgets, native_budgets = [], []

    def checks_with_elapsed_time(*args, **kwargs):
        check_budgets.append((list(args[1]), kwargs["timeout_seconds"]))
        rows = original_checks(*args, **kwargs)
        if args[1] == [command]:
            clock.now += 4
        return rows

    async def native_uses_admitted_budget(self, **kwargs):
        native_budgets.append(kwargs["limits"].timeout_seconds)
        result = await original_run(self, **kwargs)
        clock.now += kwargs["limits"].timeout_seconds
        return result

    monkeypatch.setattr(acp_worker, "_run_verification_commands", checks_with_elapsed_time)
    monkeypatch.setattr(acp_worker.AcpSessionRunner, "run", native_uses_admitted_budget)
    result = execute(fixture)
    assert result["success"] is True, result
    assert native_budgets == [26.75]  # 45 total - 3 safety - 11.25 reserve - 4 preflight.
    assert check_budgets == [([command], 24.75), ([CHECK], 9.25)]
    budget = result["harness_metadata"]["execution_budget"]
    assert budget["setup_elapsed_seconds"] == 4
    assert budget["preflight_elapsed_seconds"] == 4
    assert budget["native_session_seconds"] == 26.75
    assert budget["verification_reserve_seconds"] == 11.25
    # Even a native turn using its entire allowance leaves the final reserve,
    # including two seconds which the final verifier keeps for postprocessing.
    assert clock.now + budget["verification_reserve_seconds"] == 1000 + 45 - 3
    assert check_budgets[-1][1] + 2 == budget["verification_reserve_seconds"]
    assert (fixture.source / "calc.py").read_text() == AFTER


@pytest.mark.parametrize("commands", [
    "not-a-list", [""], ["  "], [1], ["/bin/true"] * 5,
    ["x" * 4097], ["é" * 2049], ["/bin/true\0"],
])
def test_invalid_preflight_contract_is_rejected_before_workspace_or_provider(tmp_path, monkeypatch, commands):
    fixture = setup_worker(tmp_path, monkeypatch)
    fixture.request["code_contract"]["verification_preflight_commands"] = commands
    _forbid_native_construction(monkeypatch)

    def unexpected(*args, **kwargs):
        pytest.fail("invalid preflight contract reached workspace preparation")

    monkeypatch.setattr(AcpWorkspace, "prepare", unexpected)
    with pytest.raises(AcpStateError, match="acp_writer_contract_invalid"):
        execute(fixture)
    assert fixture.control.checks == []
    assert not (fixture.state / "workspaces").exists()
    assert (fixture.source / "calc.py").read_text() == BEFORE


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
    runtime = result["harness_metadata"]["acp_worker_diagnostics"]
    assert runtime["phase"] == "cleanup" and runtime["failure_phase"] is None
    assert set(runtime["phase_durations_seconds"]) == {
        "setup", "native", "validation", "verification", "promotion", "cleanup",
    }
    assert all(value >= 0 for value in runtime["phase_durations_seconds"].values())
    budget = result["harness_metadata"]["execution_budget"]
    assert budget["total_seconds"] == 45
    assert budget["worker_safety_seconds"] == 3
    assert budget["verification_reserve_seconds"] == 11.25
    assert budget["native_cancel_grace_seconds"] == 5
    assert 0 < budget["native_session_seconds"] <= 30.75
    assert budget["setup_elapsed_seconds"] >= 0
    profile = result["harness_metadata"]["native_tool_profile"]
    assert profile["id"] == "opss-code-questions-v1"
    assert profile["enabled_tools"] == ["ask_question", "create_file", "edit_file", "finish", "view_file"]
    assert checkpoint(fixture)["native_tool_profile"] == profile
    assert control_profile_kwargs(fixture) == [{"enabledTools": profile["enabled_tools"]}]


def control_profile_kwargs(fixture):
    assert all("_meta" not in kwargs for _, kwargs in fixture.control.session_calls)
    return [kwargs["antigravity"] for _, kwargs in fixture.control.session_calls]


def test_saved_draft_restores_in_second_native_turn_before_independent_verification(tmp_path, monkeypatch):
    from dataclasses import asdict
    from chatgpt_web_oauth_mcp.delegate_code_verification import contract_hash

    fixture = setup_worker(tmp_path, monkeypatch)
    channels = []
    original_channel = acp_worker.SupervisorChannel
    def channel(*args, **kwargs):
        instance = original_channel(*args, **kwargs)
        channels.append(instance)
        return instance
    monkeypatch.setattr(acp_worker, "SupervisorChannel", channel)
    fixture.control.after_native = lambda: channels[-1].capture()
    fixture.control.stop_reason = "refusal"
    first = execute(fixture)
    assert not first["success"] and not first["outcome_uncertain"]
    saved = first["harness_metadata"]["retained_draft"]
    assert not saved["accepted"]
    assert_unpromoted(fixture)
    def inspect_restored(view):
        assert (view / "calc.py").read_text() == AFTER
        assert (fixture.source / "calc.py").read_text() == BEFORE
    fixture.control.mutation = inspect_restored
    fixture.control.after_native = None
    fixture.control.stop_reason = "end_turn"
    contract = {k: v for k, v in fixture.request["code_contract"].items() if k != "contract_hash"}
    contract["draft_source"] = {"identity": asdict(fixture.identity), "session_ref": REFERENCE,
                                "source_execution_id": TURN, "snapshot_id": saved["snapshot_id"]}
    contract["contract_hash"] = contract_hash(contract)
    resumed = {**fixture.request, "delegate_id": "writer-fixture-2", "turn_id": "writer-fixture-2",
               "resume_from_turn": TURN, "code_contract": contract}
    second = asyncio.run(acp_worker.execute(fixture.backend.config_path, resumed))
    assert second["success"] is True, second
    assert second["harness_metadata"]["restored_draft"] == saved
    assert [method for method, _ in fixture.control.session_calls] == ["new", "load"]
    assert fixture.control.checks and all(row["passed"] for row in fixture.control.checks)
    assert (fixture.source / "calc.py").read_text() == AFTER
    assert "RESTORED UNACCEPTED DRAFT" in fixture.control.prompts[-1]


def test_failed_writer_retains_scoped_draft_only_after_native_teardown(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    fixture.control.stop_reason = "refusal"
    original = acp_worker.CandidateSnapshotStore.capture
    captures = []
    def capture(self, *args, **kwargs):
        assert not fixture.control.active
        captures.append(True)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(acp_worker.CandidateSnapshotStore, "capture", capture)
    result = execute(fixture)
    assert not result["success"] and result["error"]["code"] == "acp_incomplete_turn"
    saved = result["harness_metadata"]["retained_draft"]
    assert captures == [True] and saved["accepted"] is False
    assert fixture.control.checks == []
    assert_unpromoted(fixture)


def test_failed_writer_retention_error_preserves_original_failure(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    fixture.control.stop_reason = "refusal"
    def fail(*args, **kwargs):
        raise OSError("private internal error must not leak")
    monkeypatch.setattr(acp_worker.CandidateSnapshotStore, "capture", fail)
    result = execute(fixture)
    assert result["error"]["code"] == "acp_incomplete_turn"
    assert result["harness_metadata"]["draft_retention_warning"] == "draft_retention_failed"
    assert "private internal" not in json.dumps(result)
    assert_unpromoted(fixture)


def test_failed_writer_with_excluded_edit_is_not_retained(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    fixture.control.stop_reason = "refusal"
    fixture.control.mutation = lambda view: (view / "untouched.txt").write_text("invalid")
    result = execute(fixture)
    assert result["error"]["code"] == "acp_incomplete_turn"
    assert "retained_draft" not in result["harness_metadata"]
    assert result["harness_metadata"]["draft_retention_warning"]
    assert_unpromoted(fixture)


def test_automatic_early_gate_can_continue_in_same_native_turn(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    fixture.request["max_tool_calls"] = 16
    original = acp_worker.AcpSandbox.prompt
    async def prompt(self, **kwargs):
        for index in range(4):
            await self.client.session_update("native-writer-fixture", {
                "session_update": "tool_call", "tool_call_id": "read-" + str(index),
                "status": "completed",
            })
        ch = self.gateway.supervisor
        assert ch.store.snapshot(ch.binding)["state"] == "checkpoint_requested"
        async def release():
            for _ in range(100):
                state = ch.store.snapshot(ch.binding)
                if state["state"] == "paused_for_supervisor":
                    ch.store.command(ch.binding, action="continue", command_id="test-continue",
                        expected_revision=state["revision"], checkpoint_id=state["checkpoint"]["id"])
                    return
                await asyncio.sleep(0.01)
            pytest.fail("checkpoint was not published")
        control = asyncio.create_task(release())
        await ch.checkpoint(phase="plan", summary="edit calc.py only")
        await control
        return await original(self, **kwargs)
    monkeypatch.setattr(acp_worker.AcpSandbox, "prompt", prompt)
    result = execute(fixture)
    assert result["success"] is True, result
    assert result["harness_metadata"]["progress_checkpoints"]["issued"] == ["early_review"]
    assert "MANAGED IMPLEMENTATION" in fixture.control.prompts[0]
    assert (fixture.source / "calc.py").read_text() == AFTER


def test_writer_resumes_same_profile_then_verifies_and_promotes(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    fixture.control.stop_reason = "refusal"
    first = execute(fixture)
    assert first["success"] is False
    assert first["error"]["code"] == "acp_incomplete_turn"
    assert fixture.control.checks == []
    assert_unpromoted(fixture)
    fixture.control.stop_reason = "end_turn"
    next_turn = "writer-fixture-2"
    resumed = {**fixture.request, "delegate_id": next_turn, "turn_id": next_turn, "resume_from_turn": TURN}
    second = asyncio.run(acp_worker.execute(fixture.backend.config_path, resumed))
    assert second["success"] is True
    assert (fixture.source / "calc.py").read_text() == AFTER
    assert fixture.control.checks[0]["passed"] is True
    assert second["harness_metadata"]["acp_code_receipt"]["changed_files"] == ["calc.py"]
    assert fixture.control.opened == 2
    assert [method for method, _ in fixture.control.session_calls] == ["new", "load"]
    first_profile = first["harness_metadata"]["native_tool_profile"]
    assert second["harness_metadata"]["native_tool_profile"] == first_profile
    assert control_profile_kwargs(fixture) == [{"enabledTools": first_profile["enabled_tools"]}] * 2
    state = checkpoint(fixture)
    assert state["phase"] == "completed" and state["turn"] == next_turn
    assert state["native_tool_profile"] == first_profile
    assert not list((fixture.state / "workspaces").iterdir())


@pytest.mark.parametrize("stop_reason,success", [("end_turn", True), ("refusal", False)])
def test_writer_navigation_cost_reduces_native_budget_without_changing_finality(tmp_path, monkeypatch, stop_reason, success):
    fixture = setup_worker(tmp_path, monkeypatch)
    fixture.control.stop_reason = stop_reason
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(acp_worker, "time", SimpleNamespace(monotonic=lambda: clock.now, time=time.time))
    original_navigation = acp_worker._writer_navigation_prompt
    captured_navigation = []

    def navigation(**kwargs):
        workspace = kwargs["workspace"]
        assert (workspace.baseline_root / "calc.py").read_text() == BEFORE
        assert workspace.baseline_root != workspace.root
        assert workspace.baseline_root != fixture.source
        clock.now += 4
        augmented, metadata = original_navigation(**kwargs)
        captured_navigation.append((augmented, metadata))
        return augmented, metadata

    monkeypatch.setattr(acp_worker, "_writer_navigation_prompt", navigation)
    original_run = acp_worker.AcpSessionRunner.run
    native_budgets = []

    async def run_native(self, **kwargs):
        native_budgets.append(kwargs["limits"].timeout_seconds)
        return await original_run(self, **kwargs)

    monkeypatch.setattr(acp_worker.AcpSessionRunner, "run", run_native)
    result = execute(fixture)
    assert result["success"] is success
    assert native_budgets == [26.75]  # 45 total - 3 safety - 11.25 reserve - 4 navigation
    metadata = result["harness_metadata"]
    assert metadata["execution_budget"]["setup_elapsed_seconds"] == 4
    assert metadata["execution_budget"]["native_session_seconds"] == 26.75
    assert len(captured_navigation) == 1
    prompt, navigation_metadata = captured_navigation[0]
    assert fixture.control.prompts == [prompt]
    assert prompt.endswith(fixture.request["prompt"])
    prefix = prompt[:-len(fixture.request["prompt"])]
    assert metadata["source_navigation"] == navigation_metadata
    assert navigation_metadata["revision"] == fixture.request["code_baseline"]["head"]
    assert navigation_metadata["hint_bytes"] == len(prefix.encode("utf-8")) <= 8192
    assert navigation_metadata["hint_count"] == 1 and navigation_metadata["advisory"] is True
    rows = [json.loads(line) for line in prefix.splitlines() if line.startswith("{")]
    assert rows == [{"path": "calc.py", "start_line": 1, "end_line": 2, "kind": "function", "qualname": "add"}]
    assert metadata["native_tool_profile"]["id"] == "opss-code-questions-v1"
    if success:
        assert (fixture.source / "calc.py").read_text() == AFTER
        assert fixture.control.checks[0]["passed"] is True
        assert metadata["acp_code_receipt"]["changed_files"] == ["calc.py"]
        assert checkpoint(fixture)["phase"] == "completed"
    else:
        assert result["error"]["code"] == "acp_incomplete_turn"
        assert "acp_code_receipt" not in metadata
        assert fixture.control.checks == []
        assert_unpromoted(fixture)


def test_navigation_is_not_built_for_a_rejected_execution_baseline(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    fixture.request["code_baseline"] = {**fixture.request["code_baseline"], "head": "0" * 40}

    def unexpected_navigation(**kwargs):
        pytest.fail("Navigation must follow the execution-baseline identity check")

    monkeypatch.setattr(acp_worker, "_writer_navigation_prompt", unexpected_navigation)
    with pytest.raises(AcpStateError, match="acp_writer_baseline_changed"):
        execute(fixture)
    assert fixture.control.opened == 0 and fixture.control.prompts == []
    assert_unpromoted(fixture)


def test_navigation_cancellation_closes_private_copy_before_any_provider(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    cancellation = asyncio.CancelledError()

    def cancelled_navigation(**kwargs):
        raise cancellation

    monkeypatch.setattr(acp_worker, "scoped_navigation_hints", cancelled_navigation)
    with pytest.raises(asyncio.CancelledError) as captured:
        execute(fixture)
    assert captured.value is cancellation
    assert fixture.control.opened == 0 and fixture.control.prompts == []
    assert_unpromoted(fixture)


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
    failure = result["harness_metadata"]["verification_failure"]
    assert failure["check_index"] == 0 and failure["reason"] == "exit_nonzero"
    if command.startswith("printf"):
        assert "read-only" in failure["detail"].lower()
    else:
        assert failure["exit_code"] == 17
    assert checkpoint(fixture)["result"]["harness_metadata"]["verification_failure"] == failure
    runtime = result["harness_metadata"]["acp_worker_diagnostics"]
    assert runtime["failure_phase"] == "verification" and runtime["phase"] == "cleanup"
    assert "promotion" not in runtime["phase_durations_seconds"]
    assert fixture.control.checks[0]["passed"] is False
    assert "acp_code_receipt" not in result["harness_metadata"]
    assert checkpoint(fixture)["phase"] == "failed"
    assert_unpromoted(fixture)


@pytest.mark.parametrize(("flags", "reason"), [
    ({"timed_out": True}, "timed_out"),
    ({"not_run": True, "timed_out": True}, "not_run"),
    ({"cancelled": True}, "cancelled"),
    ({"cleanup_failed": True, "error": "cleanup"}, "cleanup_failed"),
    ({"error": "launch"}, "launch_error"),
])
def test_failed_check_diagnostic_is_bounded_and_keeps_the_first_failure(flags, reason):
    row = {"passed": False, "exit_code": None, "stderr": "x" * 300 + "TAIL", **flags}
    assert acp_worker._verification_failure([{"passed": True}, row, row]) == {
        "check_index": 1, "reason": reason, "exit_code": None, "detail": "x" * 252 + "TAIL"}
    assert acp_worker._verification_failure([{"passed": True}]) is None


def test_native_out_of_scope_candidate_is_rejected_before_checks(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    def mutate(view):
        (view / "calc.py").write_text(AFTER)
        (view / "untouched.txt").write_text("forbidden change\n")
    fixture.control.mutation = mutate
    result = execute(fixture)
    assert result["success"] is False
    assert result["error"]["code"] == "acp_writer_scope_violation"
    runtime = result["harness_metadata"]["acp_worker_diagnostics"]
    assert runtime["failure_phase"] == "validation" and runtime["phase"] == "cleanup"
    assert "verification" not in runtime["phase_durations_seconds"]
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
    assert result["harness_metadata"]["acp_worker_diagnostics"]["failure_phase"] == "validation"
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


@pytest.mark.parametrize(("phase", "origin"), [
    ("preflight", "mailbox"), ("preflight", "signal"),
    ("preflight", "task"), ("verification", "task"),
])
def test_check_cancellation_drains_real_process_before_private_workspace_close(
        tmp_path, monkeypatch, phase, origin):
    fixture = setup_worker(
        tmp_path, monkeypatch,
        preflight_commands=["/bin/sleep 30"] if phase == "preflight" else None,
        commands=[CHECK] if phase == "preflight" else ["/bin/sleep 30"],
    )
    if phase == "preflight":
        _forbid_native_construction(monkeypatch)
    original_popen, original_close = subprocess.Popen, AcpWorkspace.close
    original_checks = acp_worker._run_verification_commands
    processes, roots, closed, child_pids = [], [], [], set()
    reaped, finished, release = threading.Event(), threading.Event(), threading.Event()
    if origin != "task":
        release.set()

    def popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        if isinstance(args[0], list) and "opss-acp-verification" in args[0]:
            processes.append(process)
        return process

    def checks(*args, **kwargs):
        roots.append(args[0])
        try:
            return original_checks(*args, **kwargs)
        finally:
            reaped.set()
            assert release.wait(3), "test did not release drained verifier thread"
            finished.set()

    def close_after_checks(self):
        assert finished.is_set(), "cleanup raced the owned verification thread"
        assert processes
        for process in processes:
            assert process.poll() is not None
            with pytest.raises(ProcessLookupError):
                os.killpg(process.pid, 0)
        assert all(not Path(f"/proc/{pid}").exists() for pid in child_pids)
        closed.append(True)
        original_close(self)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(acp_worker, "_run_verification_commands", checks)
    monkeypatch.setattr(AcpWorkspace, "close", close_after_checks)

    def descendants(pid):
        try:
            children = {int(value) for value in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()}
        except FileNotFoundError:
            return set()
        return children | {child for parent in children for child in descendants(parent)}

    async def scenario():
        task = asyncio.create_task(acp_worker.execute(fixture.backend.config_path, fixture.request))
        try:
            while not child_pids and not task.done():
                for process in processes:
                    child_pids.update(descendants(process.pid))
                await asyncio.sleep(0.001)
            assert processes and child_pids, "verifier did not launch a real child process"
            if origin == "mailbox":
                fixture.backend.cancel_path(TURN).write_text("cancel")
            elif origin == "signal":
                os.kill(os.getpid(), signal.SIGTERM)
            else:
                task.cancel()
                while not reaped.is_set() and not task.done():
                    await asyncio.sleep(0.001)
                assert reaped.is_set() and not task.done() and not closed
                assert roots[0].exists()
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and not closed and roots[0].exists()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                return None
            return await task
        finally:
            release.set()

    result = asyncio.run(asyncio.wait_for(scenario(), 8))
    assert finished.is_set() and closed == [True]
    assert all(not row.get("cleanup_failed") for row in fixture.control.checks)
    if phase == "preflight":
        assert not fixture.control.views and not fixture.control.prompts
        if origin != "task":
            assert result["success"] is False and result["status"] == "cancelled"
            assert result["error"]["code"] == "acp_cancelled_during_preflight"
            preflight = result["harness_metadata"]["verification_preflight"]
            assert preflight["status"] == "cancelled" and preflight["commands"][0]["cancelled"]
    else:
        assert fixture.control.opened == 1
        assert checkpoint(fixture)["phase"] == "running"
        assert checkpoint(fixture)["result"]["outcome_uncertain"] is True
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


def test_persisted_cancel_at_promotion_entry_never_exports(tmp_path, monkeypatch):
    fixture = setup_worker(tmp_path, monkeypatch)
    original = AcpWorkspace.promote

    def cancel_before_export(self, **kwargs):
        fixture.backend.cancel_path(TURN).write_text("cancel")
        return original(self, **kwargs)

    monkeypatch.setattr(AcpWorkspace, "promote", cancel_before_export)
    result = execute(fixture)
    assert fixture.control.checks[0]["passed"] is True
    assert result["status"] == "cancelled" and result["success"] is False
    assert result["error"]["code"] == "acp_cancelled_during_promotion"
    runtime = result["harness_metadata"]["acp_worker_diagnostics"]
    assert runtime["failure_phase"] == "promotion" and runtime["phase"] == "cleanup"
    assert "promotion" in runtime["phase_durations_seconds"]
    assert "acp_code_receipt" not in result["harness_metadata"]
    assert checkpoint(fixture)["phase"] == "cancelled"
    assert_unpromoted(fixture)


@pytest.mark.parametrize("origin", ["mailbox", "signal"])
def test_promotion_keeps_mailbox_and_signal_cancellation_responsive(tmp_path, monkeypatch, origin):
    fixture = setup_worker(tmp_path, monkeypatch)
    entered = threading.Event()
    original = AcpWorkspace.promote

    def wait_for_cancellation(self, **kwargs):
        entered.set()
        deadline = time.monotonic() + 3
        while not kwargs["cancel_check"]() and time.monotonic() < deadline:
            time.sleep(0.005)
        assert kwargs["cancel_check"](), "promotion blocked the worker event loop"
        return original(self, **kwargs)

    monkeypatch.setattr(AcpWorkspace, "promote", wait_for_cancellation)

    async def scenario():
        task = asyncio.create_task(acp_worker.execute(fixture.backend.config_path, fixture.request))
        while not entered.is_set() and not task.done():
            await asyncio.sleep(0.001)
        assert entered.is_set()
        if origin == "mailbox":
            fixture.backend.cancel_path(TURN).write_text("cancel")
        else:
            os.kill(os.getpid(), signal.SIGTERM)
        return await task

    result = asyncio.run(asyncio.wait_for(scenario(), 5))
    assert result["status"] == "cancelled" and result["success"] is False
    assert checkpoint(fixture)["phase"] == "cancelled"
    assert_unpromoted(fixture)


def test_repeated_task_cancel_waits_for_promotion_rollback_before_cleanup(tmp_path, monkeypatch):
    import chatgpt_web_oauth_mcp.acp_workspace as workspace_module

    fixture = setup_worker(tmp_path, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_promote, original_close = AcpWorkspace.promote, AcpWorkspace.close
    original_replace = workspace_module._replace_checked
    closed = []

    def tracked_promotion(self, **kwargs):
        try:
            return original_promote(self, **kwargs)
        finally:
            finished.set()

    def pause_after_export(*args, **kwargs):
        original_replace(*args, **kwargs)
        if args[4].name == "promotion":
            entered.set()
            assert release.wait(3)

    def close_after_promotion(self):
        assert finished.is_set(), "cleanup raced the owned promotion thread"
        closed.append(True)
        original_close(self)

    monkeypatch.setattr(AcpWorkspace, "promote", tracked_promotion)
    monkeypatch.setattr(workspace_module, "_replace_checked", pause_after_export)
    monkeypatch.setattr(AcpWorkspace, "close", close_after_promotion)

    async def scenario():
        task = asyncio.create_task(acp_worker.execute(fixture.backend.config_path, fixture.request))
        try:
            while not entered.is_set() and not task.done():
                await asyncio.sleep(0.001)
            assert entered.is_set()
            assert (fixture.source / "calc.py").read_text() == AFTER
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and not closed
                assert fixture.control.views[0].exists()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert finished.is_set() and closed == [True]
    assert_unpromoted(fixture)
    assert checkpoint(fixture)["phase"] == "running"
    assert checkpoint(fixture)["result"]["outcome_uncertain"] is True


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
