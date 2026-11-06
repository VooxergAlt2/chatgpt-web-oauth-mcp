"""Explicit pre-inference probes are part of the ACP code execution contract."""
import asyncio
import json
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.delegate_code_verification import (
    build_code_contract, validate_verification_preflight_commands,
)
from chatgpt_web_oauth_mcp.executors import _delegate_request_fingerprint
from test_acp_admission import git
from test_acp_backend import registry


PROBES = ["/usr/bin/python3 -B -c 'import unittest'"]


@pytest.mark.parametrize("value", [
    "true", ("true",), {}, 1, True, [None], [1], [""], [" \n\t"],
    ["true\0false"], ["\ud800"], ["x" * 4097], ["é" * 2049], ["true"] * 5,
])
def test_preflight_validator_rejects_invalid_inputs(value):
    with pytest.raises(ValueError):
        validate_verification_preflight_commands(value)


@pytest.mark.parametrize("value", [None, []])
def test_absent_preflight_is_empty(value):
    assert validate_verification_preflight_commands(value) == []


def test_preflight_validator_preserves_exact_text_and_utf8_boundary():
    commands = ["  true\n", "é" * 2048, "x" * 4096, "echo ok"]
    result = validate_verification_preflight_commands(commands)
    assert result == commands and result is not commands


def make_contract(**kwargs):
    return build_code_contract(
        files_in_scope=["sample.py"], acceptance_criteria=[], done_means=[],
        verification_commands=["final behavioral check"], max_changed_files=1,
        max_added_lines=10, max_deleted_lines=10, **kwargs,
    )


def test_preflight_contract_preserves_old_hash_and_binds_exact_probe_order():
    old = make_contract()
    assert make_contract(verification_preflight_commands=None) == old
    assert make_contract(verification_preflight_commands=[]) == old
    assert "verification_preflight_commands" not in old
    commands = PROBES + ["test -r sample.py"]
    declared = make_contract(verification_preflight_commands=commands)
    assert declared["verification_preflight_commands"] == commands
    commands.append("another command")
    assert len(declared["verification_preflight_commands"]) == 2
    assert len({
        old["contract_hash"], declared["contract_hash"],
        make_contract(verification_preflight_commands=list(reversed(commands[:2])))["contract_hash"],
    }) == 3


def test_request_fingerprint_binds_probes_and_preserves_omitted_identity():
    args = dict(
        task="implement", goal=None, task_id=None, cwd=Path("/repo"), harness="antigravity",
        kind="code", group_id=None, files_in_scope=["sample.py"], out_of_scope=None,
        context_files=None, acceptance_criteria=None, done_means=None,
        verification_commands=["final check"], commit_mode="forbidden", model="flash",
        reasoning_effort="medium", complexity="low", max_tool_calls=12,
        output_schema=None, parse_structured_output=True, depends_on_group_ids=None,
        execution_backend="acp",
    )
    old = _delegate_request_fingerprint(**args)
    assert old == _delegate_request_fingerprint(**args, verification_preflight_commands=None)
    assert old == _delegate_request_fingerprint(**args, verification_preflight_commands=[])
    assert old != _delegate_request_fingerprint(**args, verification_preflight_commands=PROBES)
    assert _delegate_request_fingerprint(**args, verification_preflight_commands=PROBES) != (
        _delegate_request_fingerprint(**args, verification_preflight_commands=["different probe"]))


@pytest.mark.parametrize("changes,code", [
    ({"verification_preflight_commands": [""]}, "invalid_verification_preflight_commands"),
    ({"verification_preflight_commands": "true"}, "invalid_verification_preflight_commands"),
    ({"verification_commands": ["true\0false"]}, "acp_writer_verification_required"),
    ({"backend": "cli"}, "verification_preflight_requires_acp_code"),
    ({"kind": "explore"}, "verification_preflight_requires_acp_code"),
    ({"kind": "explore", "work_role": "proposal"}, "verification_preflight_requires_acp_code"),
])
def test_preflight_invalid_or_unsupported_request_is_not_scheduled(tmp_path, changes, code):
    scheduler, workspace = registry(tmp_path)
    response = scheduler.run_delegate(**{
        "task": "Change sample.py", "cwd": workspace, "harness": "antigravity",
        "backend": "acp", "kind": "code", "files_in_scope": ["sample.py"],
        "verification_commands": ["true"], "verification_preflight_commands": PROBES,
        "execution_timeout_seconds": 60, "wait_seconds": 0, **changes,
    })
    assert response["success"] is False
    assert response["error"]["code"] == code
    assert not scheduler.scheduler.tasks


@pytest.mark.parametrize("backend", ["acp", "auto"])
def test_preflight_contract_reaches_worker_request_without_model_call(tmp_path, monkeypatch, backend):
    scheduler, main = registry(tmp_path)
    git(main, "init", "-q")
    git(main, "add", "sample.py")
    git(main, "-c", "user.name=QA", "-c", "user.email=qa@invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    linked = tmp_path / "linked"
    git(main, "worktree", "add", "--detach", "-q", str(linked))
    monkeypatch.setattr(scheduler, "_start_codex_delegate_impl",
                        lambda **kwargs: {"success": True, "status": "succeeded"})
    response = scheduler.run_delegate(
        task="Change sample.py", cwd=linked, harness="antigravity", backend=backend,
        kind="code", files_in_scope=["sample.py"], verification_commands=["true"],
        verification_preflight_commands=PROBES, logical_session_id="preflight-owner",
        execution_timeout_seconds=60, wait_seconds=0,
    )
    assert response.get("delegate_id"), response
    task = scheduler.scheduler.get_task(response["delegate_id"])
    assert task.completed_event.wait(5)
    request = json.loads(scheduler._build_invocation_for_task(task).stdin)
    assert request["code_contract"]["verification_preflight_commands"] == PROBES
    assert request["code_contract"]["verification_commands"] == ["true"]


def test_public_tool_exposes_and_forwards_preflight_commands(tmp_path, monkeypatch):
    from chatgpt_web_oauth_mcp import server
    captured = {}

    class FakeRegistry:
        def run_delegate(self, **kwargs):
            captured.update(kwargs)
            return {"success": True, "status": "succeeded"}

    monkeypatch.setattr(server, "registry", FakeRegistry())
    tool = asyncio.run(server.mcp.get_tool("delegate_task"))
    assert "verification_preflight_commands" in tool.parameters["properties"]
    fn = server.delegate_task.fn if hasattr(server.delegate_task, "fn") else server.delegate_task
    result = fn(task="Implement", cwd=str(tmp_path), backend="acp", kind="code",
                harness="antigravity", verification_preflight_commands=PROBES)
    assert result["success"] is True
    assert captured["verification_preflight_commands"] == PROBES
