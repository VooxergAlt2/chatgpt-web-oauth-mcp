"""Worker-level continuation and lease checks without a provider or sandbox."""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from chatgpt_web_oauth_mcp import acp_worker
from chatgpt_web_oauth_mcp.acp_backend import AcpBackend
from chatgpt_web_oauth_mcp.acp_identity import verify_binding
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError


REFERENCE = "a" * 32


def fixture(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    native_profile = profile / "antigravity-acp"
    native_profile.mkdir(mode=0o700)
    token = native_profile / "acp_token.json"
    token.write_text(json.dumps({
        "client_id": "fixture-client", "client_secret": "fixture-secret", "refresh_token": "fixture-acp",
    }))
    token.chmod(0o600)
    cli_home = tmp_path / "cli-home"
    cli_token = cli_home / ".gemini/antigravity-cli/antigravity-oauth-token"
    cli_token.parent.mkdir(parents=True)
    cli_token.write_text(json.dumps({
        "id_token_email": "qa@example.invalid", "token": {"refresh_token": "fixture-cli"},
    }))
    cli_token.chmod(0o600)
    # Exercise the real offline binding validator; only the provider identity
    # lookup is replaced while creating this synthetic attestation.
    with patch("chatgpt_web_oauth_mcp.acp_identity._google_identity", return_value="qa@example.invalid"):
        verify_binding(profile=profile, account="antigravity", cli_home=cli_home,
                       proof_root=state / "account-bindings")
    backend = AcpBackend(
        config_path=tmp_path / "operator.json", python=Path(sys.executable),
        server=Path("/bin/true"), helper=Path("/bin/true"),
        state_root=state,
        accounts={"antigravity": {"profile": profile, "cli_home": cli_home, "workspace_roots": (workspace,)}},
    )
    monkeypatch.setattr(acp_worker.AcpBackend, "load", lambda _path: backend)
    opened = []
    barrier = {"started": None, "release": None, "session_calls": []}

    class NativeHistory:
        def __init__(self, _server, _helper, _profile, _workspace, _control, history, **kwargs):
            barrier["sandbox_kwargs"] = kwargs
            if "workspace_view" in kwargs:
                view = kwargs["workspace_view"]
                assert view.is_dir() and not list(view.iterdir())
                assert view.stat().st_mode & 0o077 == 0
            self.history = history
            self.history.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.options = [
                {"id": name, "current_value": value, "options": [{"value": value}]}
                for name, value in (
                    ("model", "gemini-3.8-flash"),
                    ("thinking_level", "medium"), ("mode", "default"),
                )
            ]

        def transport(self, *, gateway=None, configuration_observer=None, proposal=False):
            barrier["gateway"] = gateway
            barrier["proposal"] = proposal
            self.configuration_observer = configuration_observer
            @asynccontextmanager
            async def connect(client):
                opened.append(self.history)
                self.client = client
                yield self
            return connect

        async def initialize(self, **_kwargs):
            return {"protocol_version": 1, "agent_capabilities": {"load_session": True}}

        async def new_session(self, **_kwargs):
            barrier["session_calls"].append(("new", _kwargs))
            (self.history / "provider-session.txt").write_text("previous turn's private marker")
            return {"session_id": "provider-session", "config_options": self.options}

        async def load_session(self, *, session_id, **_kwargs):
            barrier["session_calls"].append(("load", _kwargs))
            assert session_id == "provider-session"
            # Real native storage survives a new worker. A process-local fake
            # agent alone cannot catch a directory keyed by the new delegate ID.
            assert (self.history / "provider-session.txt").read_text() == "previous turn's private marker"
            return {"config_options": self.options}

        async def prompt(self, **_kwargs):
            if barrier["started"] is not None:
                barrier["started"].set()
                await barrier["release"].wait()
            report = {"status": "succeeded", "summary": "native history retained", "findings": []}
            await self.client.session_update("provider-session", {
                "session_update": "agent_message_chunk",
                "content": {"text": json.dumps(report)},
            })
            return {"stop_reason": "end_turn"}

    monkeypatch.setattr(acp_worker, "AcpSandbox", NativeHistory)

    def request(delegate_id):
        return {
            "delegate_id": delegate_id, "account": "antigravity", "owner": "fixture-owner",
            "workspace": str(workspace), "thinking": "medium", "scope_digest": "b" * 64,
            "session_ref": REFERENCE, "turn_id": delegate_id, "prompt": "Inspect sample",
            "timeout": 15, "max_tool_calls": 4,
            "kind": "explore", "scope": {"files_in_scope": [], "out_of_scope": []},
            "code_contract": None, "code_baseline": None,
        }

    return backend, request, opened, barrier


def test_proposal_has_no_project_gateway_or_native_read_tools(tmp_path, monkeypatch):
    backend, request, opened, barrier = fixture(tmp_path, monkeypatch)
    values = {**request("1" * 12), "proposal": True,
              "scope": {"files_in_scope": ["private.py"], "out_of_scope": []}}
    (Path(values["workspace"]) / "private.py").write_text("private project canary")
    def forbidden(*args, **kwargs):
        pytest.fail("proposal opened project gateway or supervision")
    monkeypatch.setattr(acp_worker, "AcpReadonlyGateway", forbidden)
    monkeypatch.setattr(acp_worker, "SupervisorStore", forbidden)
    result = asyncio.run(acp_worker.execute(backend.config_path, values))
    assert result["success"]
    assert barrier["proposal"] is True and barrier["gateway"] is None
    view = barrier["sandbox_kwargs"]["workspace_view"]
    assert not view.exists()
    assert not barrier["sandbox_kwargs"].get("writable_workspace", False)
    assert barrier["session_calls"][0][1]["mcp_servers"] == []
    assert barrier["session_calls"][0][1]["antigravity"] == {"enabledTools": ["finish"]}
    assert result["harness_metadata"]["native_tool_profile"]["id"] == "opss-proposal-v1"
    assert not (backend.state_root / "supervision").exists()
    assert (Path(values["workspace"]) / "private.py").read_text() == "private project canary"
    with pytest.raises(AcpStateError, match="acp_proposal_requires_new_session"):
        asyncio.run(acp_worker.execute(backend.config_path, {**values, "delegate_id": "2" * 12,
                                                           "turn_id": "2" * 12}))
    assert len(opened) == 1


@pytest.mark.parametrize("extra", [
    {"proposal": "true"}, {"proposal": 1}, {"proposal": None},
    {"proposal": True, "kind": "code"}, {"proposal": True, "resume_from_turn": None},
    {"proposal": True, "resume_from_turn": "1" * 12},
])
def test_worker_rejects_invalid_proposal_before_provider(tmp_path, monkeypatch, extra):
    backend, request, opened, _ = fixture(tmp_path, monkeypatch)
    with pytest.raises(AcpStateError, match="acp_worker_request_invalid"):
        asyncio.run(acp_worker.execute(backend.config_path, {**request("1" * 12), **extra}))
    assert opened == []


def test_new_delegate_continues_native_history_in_same_session(tmp_path, monkeypatch):
    backend, request, opened, barrier = fixture(tmp_path, monkeypatch)
    first = asyncio.run(acp_worker.execute(backend.config_path, request("1" * 12)))
    resumed = {**request("2" * 12), "resume_from_turn": "1" * 12}
    second = asyncio.run(acp_worker.execute(backend.config_path, resumed))
    assert first["success"] and second["success"]
    assert first["session_ref"] == second["session_ref"]
    assert first["turn_id"] != second["turn_id"]
    assert len(opened) == 2 and opened[0] == opened[1]
    assert json.loads(second["answer"])["summary"] == "native history retained"
    expected_tools = ["ask_question", "finish", "view_file"]
    assert [method for method, _ in barrier["session_calls"]] == ["new", "load"]
    for _, kwargs in barrier["session_calls"]:
        assert kwargs["antigravity"] == {"enabledTools": expected_tools}
        assert "_meta" not in kwargs
    profile = first["harness_metadata"]["native_tool_profile"]
    assert profile["id"] == "opss-explore-questions-v1"
    assert profile["enabled_tools"] == expected_tools
    assert second["harness_metadata"]["native_tool_profile"] == profile


def test_legacy_worker_session_refuses_continuation_before_provider(tmp_path, monkeypatch):
    backend, request, opened, barrier = fixture(tmp_path, monkeypatch)
    first = asyncio.run(acp_worker.execute(backend.config_path, request("1" * 12)))
    assert first["success"]
    path = backend.state_root / "sessions" / (REFERENCE + ".json")
    state = json.loads(path.read_text())
    state.pop("native_tool_profile")
    state["version"] = 1  # exact pre-profile checkpoint schema
    path.write_text(json.dumps(state))
    before = path.read_text()
    resumed = {**request("2" * 12), "resume_from_turn": "1" * 12}
    with pytest.raises(AcpStateError, match="acp_tool_profile_legacy_requires_new_session"):
        asyncio.run(acp_worker.execute(backend.config_path, resumed))
    assert len(opened) == 1
    assert [method for method, _ in barrier["session_calls"]] == ["new"]
    assert path.read_text() == before


def test_worker_request_cannot_supply_custom_native_profile(tmp_path, monkeypatch):
    backend, request, opened, _ = fixture(tmp_path, monkeypatch)
    values = {**request("1" * 12), "native_tool_profile": {"enabled_tools": ["execute"]}}
    with pytest.raises(AcpStateError, match="acp_worker_request_invalid"):
        asyncio.run(acp_worker.execute(backend.config_path, values))
    assert opened == []
    assert not (backend.state_root / "conversations").exists()


@pytest.mark.parametrize("stop_reason,success", [("end_turn", True), ("refusal", False)])
def test_forwarded_native_configuration_is_reported_without_changing_finality(tmp_path, monkeypatch, stop_reason, success):
    backend, request, opened, _ = fixture(tmp_path, monkeypatch)
    original = acp_worker.AcpSandbox.prompt
    keys = (
        "subagents", "runWorkflow", "find", "userQuestions", "runCommand",
        "manageTask", "schedule", "fileEdit", "viewFile", "writeToFile",
        "grepSearch", "listDir", "generateImage", "searchWeb", "readUrlContent",
    )
    initial = {
        "version": 1, "source": "forwarded_native_initialization",
        "harness_side_tools": dict.fromkeys(keys),
        "unobservable_builtin_tools": ["finish"],
    }
    observed = {
        **initial, "harness_side_tools": {
            **dict.fromkeys(keys, False), "userQuestions": True, "viewFile": True,
        },
    }

    async def prompt(self, **kwargs):
        assert self.configuration_observer is not None
        self.configuration_observer(initial)
        self.configuration_observer(observed)
        await original(self, **kwargs)
        return {"stop_reason": stop_reason}

    monkeypatch.setattr(acp_worker.AcpSandbox, "prompt", prompt)
    result = asyncio.run(acp_worker.execute(backend.config_path, request("1" * 12)))
    assert result["success"] is success
    assert len(opened) == 1
    if not success:
        assert result["error"]["code"] == "acp_incomplete_turn"
    metadata = result["harness_metadata"]
    assert metadata["native_tool_configuration"] == {**observed, "observation_count": 2}
    assert metadata["native_tool_profile"]["enabled_tools"] == ["ask_question", "finish", "view_file"]
    assert "finish" not in metadata["native_tool_configuration"]["harness_side_tools"]


def test_same_session_lease_blocks_parallel_native_helpers(tmp_path, monkeypatch):
    backend, request, opened, barrier = fixture(tmp_path, monkeypatch)

    async def exercise():
        barrier["started"] = asyncio.Event()
        barrier["release"] = asyncio.Event()
        first = asyncio.create_task(acp_worker.execute(backend.config_path, request("1" * 12)))
        await asyncio.wait_for(barrier["started"].wait(), 2)
        try:
            with pytest.raises(AcpStateError, match="acp_session_busy"):
                await acp_worker.execute(backend.config_path, request("2" * 12))
            assert len(opened) == 1
        finally:
            barrier["release"].set()
        assert (await first)["success"]

    asyncio.run(exercise())


@pytest.mark.parametrize("reference", ["../escape", "../../outside", None, []])
def test_invalid_reference_is_rejected_before_native_history_creation(tmp_path, monkeypatch, reference):
    backend, request, opened, _barrier = fixture(tmp_path, monkeypatch)
    values = {**request("1" * 12), "session_ref": reference}
    with pytest.raises(AcpStateError, match="acp_invalid_session_reference"):
        asyncio.run(acp_worker.execute(backend.config_path, values))
    assert opened == []
    assert not (backend.state_root / "conversations").exists()


def test_setup_cost_reduces_native_budget_and_final_snapshot_includes_cleanup(tmp_path, monkeypatch):
    backend, request, opened, _ = fixture(tmp_path, monkeypatch)
    clock = SimpleNamespace(now=1000.0)
    # Replacing the worker's module reference leaves asyncio's clock untouched.
    monkeypatch.setattr(acp_worker, "time", SimpleNamespace(monotonic=lambda: clock.now))
    original_validate = backend.validate

    def load(path):
        clock.now += 2
        return backend

    def validate(self, **kwargs):
        original_validate(**kwargs)
        clock.now += 3

    monkeypatch.setattr(acp_worker.AcpBackend, "load", load)
    monkeypatch.setattr(acp_worker.AcpBackend, "validate", validate)
    original_run = acp_worker.AcpSessionRunner.run
    native_budgets = []

    async def run_native(self, **kwargs):
        native_budgets.append(kwargs["limits"].timeout_seconds)
        result = await original_run(self, **kwargs)
        clock.now += 4
        return result

    monkeypatch.setattr(acp_worker.AcpSessionRunner, "run", run_native)
    values = request("1" * 12)
    cancel_path = backend.cancel_path(values["delegate_id"])
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == cancel_path:
            clock.now += 2
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    events = []
    monkeypatch.setattr(acp_worker, "emit", lambda event, value: events.append((event, value)))
    diagnostics = {}
    result = asyncio.run(acp_worker.execute(backend.config_path, values, diagnostics=diagnostics))
    assert result["success"] is True
    assert len(opened) == 1 and native_budgets == [7]
    metadata = result["harness_metadata"]
    assert metadata["execution_budget"] == {
        "version": 1, "total_seconds": 15, "worker_safety_seconds": 3,
        "verification_reserve_seconds": 0, "native_cancel_grace_seconds": 5,
        "setup_elapsed_seconds": 5, "native_session_seconds": 7,
    }
    runtime = metadata["acp_worker_diagnostics"]
    assert runtime["phase"] == "cleanup" and runtime["failure_phase"] is None
    assert runtime["phase_durations_seconds"] == {"setup": 5, "native": 4, "cleanup": 2}
    assert runtime["elapsed_seconds"] == 11
    worker_progress = [
        value["progress"] for event, value in events
        if event == "acp_progress" and value["progress"]["runtime"]["component"] == "worker"
    ]
    assert [row["runtime"]["phase"] for row in worker_progress] == ["setup", "native", "cleanup"]
    assert worker_progress[1]["execution_budget"] == metadata["execution_budget"]
    assert diagnostics == {key: metadata[key] for key in ("acp_worker_diagnostics", "execution_budget")}
    assert runtime["event_count"] == 0  # worker phases do not claim native activity
    assert metadata["acp_session_diagnostics"]["event_count"] == 1


def test_setup_exhaustion_never_starts_native_transport(tmp_path, monkeypatch):
    backend, request, opened, _ = fixture(tmp_path, monkeypatch)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(acp_worker, "time", SimpleNamespace(monotonic=lambda: clock.now))
    original_validate = backend.validate

    def validate(self, **kwargs):
        original_validate(**kwargs)
        clock.now += 10

    monkeypatch.setattr(acp_worker.AcpBackend, "validate", validate)
    diagnostics = {}
    with pytest.raises(AcpStateError, match="acp_start_deadline_exhausted"):
        asyncio.run(acp_worker.execute(backend.config_path, request("1" * 12), diagnostics=diagnostics))
    assert opened == []
    runtime = diagnostics["acp_worker_diagnostics"]
    assert runtime["failure_phase"] == "setup"
    assert runtime["phase"] == "cleanup"
    assert "native" not in runtime["phase_durations_seconds"]
    assert diagnostics["execution_budget"]["setup_elapsed_seconds"] == 10
    assert diagnostics["execution_budget"]["native_session_seconds"] == 2


@pytest.mark.parametrize("stage", ["load", "validate"])
def test_setup_exception_keeps_original_error_and_diagnostic_sink(tmp_path, monkeypatch, stage):
    backend, request, opened, _ = fixture(tmp_path, monkeypatch)
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(acp_worker, "time", SimpleNamespace(monotonic=lambda: clock.now))
    failure = RuntimeError("PRIVATE_SETUP_FAILURE")

    def fail(*args, **kwargs):
        clock.now += 3
        raise failure

    monkeypatch.setattr(acp_worker.AcpBackend, stage, fail)
    diagnostics = {}
    with pytest.raises(RuntimeError) as captured:
        asyncio.run(acp_worker.execute(backend.config_path, request("1" * 12), diagnostics=diagnostics))
    assert captured.value is failure
    assert opened == []
    runtime = diagnostics["acp_worker_diagnostics"]
    assert runtime["failure_phase"] == runtime["phase"] == "setup"
    assert runtime["phase_durations_seconds"] == {"setup": 3}
    assert "execution_budget" not in diagnostics
    assert "PRIVATE_SETUP_FAILURE" not in json.dumps(diagnostics)


def test_worker_native_failure_retains_origin_through_cleanup(tmp_path, monkeypatch):
    backend, request, opened, _ = fixture(tmp_path, monkeypatch)
    original = acp_worker.AcpSandbox.prompt

    async def refusal(self, **kwargs):
        await original(self, **kwargs)
        return {"stop_reason": "refusal"}

    monkeypatch.setattr(acp_worker.AcpSandbox, "prompt", refusal)
    result = asyncio.run(acp_worker.execute(backend.config_path, request("1" * 12)))
    assert result["success"] is False
    assert result["error"]["code"] == "acp_incomplete_turn"
    assert len(opened) == 1
    metadata = result["harness_metadata"]
    assert metadata["acp_worker_diagnostics"]["failure_phase"] == "native"
    assert metadata["acp_worker_diagnostics"]["phase"] == "cleanup"
    assert metadata["acp_session_diagnostics"]["failure_phase"] == "prompt"
    assert 0 < metadata["execution_budget"]["native_session_seconds"] <= 12


def test_progress_sink_failure_cannot_change_worker_success(tmp_path, monkeypatch):
    backend, request, opened, _ = fixture(tmp_path, monkeypatch)
    attempted = []

    def broken_emit(event, value):
        attempted.append(event)
        raise RuntimeError("PRIVATE_PROGRESS_FAILURE")

    monkeypatch.setattr(acp_worker, "emit", broken_emit)
    result = asyncio.run(acp_worker.execute(backend.config_path, request("1" * 12)))
    assert result["success"] is True
    assert len(opened) == 1 and attempted
    assert result["harness_metadata"]["acp_worker_diagnostics"]["failure_phase"] is None
    assert result["harness_metadata"]["acp_worker_diagnostics"]["phase"] == "cleanup"
    assert "PRIVATE_PROGRESS_FAILURE" not in json.dumps(result)
