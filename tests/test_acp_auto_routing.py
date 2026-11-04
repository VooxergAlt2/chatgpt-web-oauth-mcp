"""Default public delegate transport is ACP for admitted Gemini accounts."""
from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from chatgpt_web_oauth_mcp.acp_backend import AcpBackend
from chatgpt_web_oauth_mcp.acp_identity import verify_binding
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.shell import JobRegistry
from test_acp_backend import FixtureAgyHarness, registry as single_registry


def multi_registry(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sample.py").write_text("def result():\n    return 7\n")
    accounts = {}
    harnesses = []
    for index, name in enumerate(("antigravity", "antigravity3", "antigravity2"), 1):
        home = tmp_path / f"cli-{index}"
        profile = tmp_path / f"profile-{index}"
        nested = profile / "antigravity-acp"
        nested.mkdir(parents=True, mode=0o700)
        (nested / "settings.json").write_text('{"auth":{"type":"oauth-personal"}}')
        token = nested / "acp_token.json"
        token.write_text(json.dumps({
            "client_id": f"client-{index}", "client_secret": f"secret-{index}",
            "refresh_token": f"refresh-{index}",
        }))
        token.chmod(0o600)
        cli = home / ".gemini/antigravity-cli/antigravity-oauth-token"
        cli.parent.mkdir(parents=True)
        cli.write_text(json.dumps({
            "id_token_email": f"fixture-{index}@example.invalid",
            "token": {"refresh_token": f"cli-refresh-{index}"},
        }))
        cli.chmod(0o600)
        with patch("chatgpt_web_oauth_mcp.acp_identity._google_identity",
                   return_value=f"fixture-{index}@example.invalid"):
            verify_binding(profile=profile, account=name, cli_home=home,
                           proof_root=state / "account-bindings")
        accounts[name] = {
            "profile": str(profile), "cli_home": str(home), "workspace_roots": [str(workspace)],
        }
        harnesses.append(FixtureAgyHarness(name=name, command="true", home_dir=home))
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "version": 1, "python": sys.executable, "server": "/bin/true",
        "helper": "/bin/true", "state_root": str(state), "accounts": accounts,
    }))
    config.chmod(0o600)
    executor = ExecutorRegistry(
        default_harness="antigravity", harnesses=harnesses,
        acp_backend=AcpBackend.load(config),
        automatic_routing=True, agy_capacity_first=True,
        max_explore_per_project=8, max_explore_global=10,
        max_active_per_harness={name: 1 for name in accounts},
        durable_job_registry=JobRegistry(),
        durable_state_dir=tmp_path / "jobs",
        delegate_state_root=tmp_path / "delegates",
    )
    executor.quota_admission_gate = None
    return executor, workspace


@pytest.mark.parametrize(("busy", "expected"), [
    ((), "antigravity"),
    (("antigravity",), "antigravity3"),
    (("antigravity", "antigravity3"), "antigravity2"),
])
@pytest.mark.parametrize("complexity", ["low", "high"])
def test_auto_routes_each_gemini_account_with_shared_priority(
    tmp_path, monkeypatch, busy, expected, complexity,
):
    executor, workspace = multi_registry(tmp_path)
    monkeypatch.setattr(
        executor.scheduler, "harness_capacity",
        lambda name: {"active": 1 if name in busy else 0, "limit": 1},
    )
    seen = []
    def execute(*, delegate_task):
        seen.append(delegate_task)
        return {"status": "succeeded", "success": True,
                "execution_backend": delegate_task.execution_backend}
    monkeypatch.setattr(executor, "_start_codex_delegate_impl", execute)
    result = executor.run_delegate(
        task="Read sample.py and report the returned constant.",
        cwd=workspace, kind="explore", backend="auto",
        complexity=complexity, files_in_scope=["sample.py"], wait_seconds=0,
    )
    assert result.get("delegate_id"), result
    task = executor.scheduler.get_task(result["delegate_id"])
    assert task.completed_event.wait(5)
    assert task.execution_backend == "acp" and task.harness == expected
    assert task.model == f"gemini-3.8-flash-{'high' if complexity == 'high' else 'medium'}"
    assert task.reasoning_effort == ("high" if complexity == "high" else "medium")
    assert task.acp_resume_context is not None
    request = json.loads(executor._build_invocation_for_task(task).stdin)
    assert request["account"] == expected
    assert seen == [task]


@pytest.mark.parametrize("backend", ["auto", "acp", "cli"])
def test_explicit_cli_preserved_and_auto_acp_does_not_silently_fallback(tmp_path, monkeypatch, backend):
    executor, workspace = single_registry(tmp_path)
    if backend != "cli":
        executor.acp_backend = None
        result = executor.run_delegate(
            task="Read sample.py", cwd=workspace, kind="explore",
            harness="antigravity", backend=backend, wait_seconds=0,
        )
        assert result["error"]["code"] == "acp_backend_disabled"
        assert not executor.scheduler.tasks
    else:
        seen = []
        def execute(*, delegate_task):
            seen.append(delegate_task)
            return {"success": True, "status": "succeeded"}
        monkeypatch.setattr(executor, "_start_codex_delegate_impl", execute)
        result = executor.run_delegate(
            task="Read sample.py", cwd=workspace, kind="explore",
            harness="antigravity", backend="cli", wait_seconds=0,
        )
        task = executor.scheduler.get_task(result["delegate_id"])
        assert task.completed_event.wait(5)
        assert task.execution_backend == "cli" and seen == [task]


def test_auto_writer_refuses_missing_contract_without_cli_or_unchecked_edits(tmp_path):
    executor, workspace = single_registry(tmp_path)
    response = executor.run_delegate(
        task="Modify sample.py", cwd=workspace,
        harness="antigravity", backend="auto", kind="code", wait_seconds=0,
    )
    assert response["success"] is False
    assert response["error"]["code"] == "acp_writer_explicit_paths_required"
    assert not executor.scheduler.tasks
    assert (workspace / "sample.py").read_text() == "def sample():\n    return 2\n"


def test_auto_high_pinned_agy_is_allowed_and_transport_is_acp(tmp_path, monkeypatch):
    executor, workspace = single_registry(tmp_path)
    seen = []
    def execute(*, delegate_task):
        seen.append(delegate_task)
        return {"success": True, "status": "succeeded"}
    monkeypatch.setattr(executor, "_start_codex_delegate_impl", execute)
    result = executor.run_delegate(
        task="Review sample.py", cwd=workspace, harness="antigravity",
        backend="auto", kind="explore", complexity="high", wait_seconds=0,
    )
    task = executor.scheduler.get_task(result["delegate_id"])
    assert task.completed_event.wait(5)
    assert task.execution_backend == "acp" and task.reasoning_effort == "high"
    assert seen == [task]


def test_auto_writer_with_real_linked_worktree_and_verifier_uses_acp(tmp_path, monkeypatch):
    executor, workspace = multi_registry(tmp_path)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "add", "sample.py"], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=QA",
                    "-c", "user.email=qa@example.invalid", "-c", "commit.gpgsign=false",
                    "commit", "-qm", "base"], check=True)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(workspace), "worktree", "add",
                    "--detach", "-q", str(linked)], check=True)
    seen = []
    def execute(*, delegate_task):
        seen.append(delegate_task)
        return {"status": "succeeded", "success": True}
    monkeypatch.setattr(executor, "_start_codex_delegate_impl", execute)
    result = executor.run_delegate(
        task="Change sample.py in a private isolated writer", cwd=linked,
        kind="code", backend="auto", complexity="high", wait_seconds=0,
        files_in_scope=["sample.py"],
        verification_commands=["/usr/bin/python3 -B -c 'assert True'"],
        commit_mode="forbidden",
    )
    assert result.get("delegate_id"), result
    task = executor.scheduler.get_task(result["delegate_id"])
    assert task.completed_event.wait(5)
    assert task.execution_backend == "acp" and task.harness == "antigravity"
    assert task.model == "gemini-3.8-flash-high"
    assert task.max_tool_calls == 64
    request = json.loads(executor._build_invocation_for_task(task).stdin)
    assert request["code_contract"]["verification_commands"]
    assert request["account"] == "antigravity"
    assert seen == [task]
    assert subprocess.check_output(["git", "-C", str(linked), "status",
                                    "--porcelain"], text=True).strip() == ""
