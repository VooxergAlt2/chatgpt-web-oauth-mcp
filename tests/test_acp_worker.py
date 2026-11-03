"""Worker-level continuation and lease checks without a provider or sandbox."""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
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
    barrier = {"started": None, "release": None}

    class NativeHistory:
        def __init__(self, _server, _helper, _profile, _workspace, _control, history):
            self.history = history
            self.history.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.options = [
                {"id": name, "current_value": value, "options": [{"value": value}]}
                for name, value in (
                    ("model", "gemini-3.8-flash"),
                    ("thinking_level", "medium"), ("mode", "default"),
                )
            ]

        def transport(self, *, gateway=None):
            @asynccontextmanager
            async def connect(client):
                opened.append(self.history)
                self.client = client
                yield self
            return connect

        async def initialize(self, **_kwargs):
            return {"protocol_version": 1, "agent_capabilities": {"load_session": True}}

        async def new_session(self, **_kwargs):
            (self.history / "provider-session.txt").write_text("previous turn's private marker")
            return {"session_id": "provider-session", "config_options": self.options}

        async def load_session(self, *, session_id, **_kwargs):
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


def test_new_delegate_continues_native_history_in_same_session(tmp_path, monkeypatch):
    backend, request, opened, _barrier = fixture(tmp_path, monkeypatch)
    first = asyncio.run(acp_worker.execute(backend.config_path, request("1" * 12)))
    second = asyncio.run(acp_worker.execute(backend.config_path, request("2" * 12)))
    assert first["success"] and second["success"]
    assert first["session_ref"] == second["session_ref"]
    assert first["turn_id"] != second["turn_id"]
    assert len(opened) == 2 and opened[0] == opened[1]
    assert json.loads(second["answer"])["summary"] == "native history retained"


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
