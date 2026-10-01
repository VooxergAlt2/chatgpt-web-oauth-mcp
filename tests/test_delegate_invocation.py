from __future__ import annotations

from pathlib import Path
import subprocess
import time

from chatgpt_web_oauth_mcp import executors
import chatgpt_web_oauth_mcp.delegate_harnesses as delegate_harnesses
from chatgpt_web_oauth_mcp.delegate_harnesses import (
    AntigravityHarness,
    CodexHarness,
    GenericCliHarness,
)
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def test_explore_invocation_is_hard_readonly_and_uses_task_defaults(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="codex")

    invocation = registry._build_invocation(
        command="codex",
        task="inspect the scheduler",
        goal=None,
        cwd=tmp_path,
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="allowed",
        model="gpt-5.6-luna",
        reasoning_effort="low",
        kind="explore",
    )

    assert invocation.args[1:6] == [
        "exec",
        "--model",
        "gpt-5.6-luna",
        "-c",
        'model_reasoning_effort="low"',
    ]
    assert "--sandbox" in invocation.args
    assert invocation.args[invocation.args.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in invocation.args
    assert "--dangerously-bypass-approvals-and-sandbox" not in invocation.args
    prompt = (invocation.stdin or b"").decode("utf-8")
    assert "This is a read-only exploration task." in prompt
    assert "Commit mode: forbidden" in prompt


def test_codex_output_parser_extracts_total_token_usage() -> None:
    parsed = delegate_harnesses.parse_codex_output(
        '{"status":"succeeded","summary":"ok"}',
        "codex diagnostic output\ntokens used\n39,035\n",
    )

    assert parsed.structured_output == {"status": "succeeded", "summary": "ok"}
    assert parsed.metadata == {"usage": {"total_tokens": 39035}}


def test_code_defaults_are_sol_xhigh_and_full_access(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('done')\"")

    result = registry.run_codex(task="implement", cwd=tmp_path, wait_seconds=2)

    assert result["status"] == "succeeded"
    assert result["model"] == "gpt-5.6-sol"
    assert result["reasoning_effort"] == "xhigh"
    assert result["sandbox_mode"] == "danger-full-access"
    assert result["kind"] == "code"


def test_explore_forces_permissions_even_with_model_override(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="python3 -c \"print('done')\"",
        allow_unsafe_explore_command=True,
    )

    result = registry.run_codex(
        task="inspect",
        kind="explore",
        cwd=tmp_path,
        model="custom-model",
        reasoning_effort="medium",
        commit_mode="required",
        wait_seconds=2,
    )

    assert result["status"] == "succeeded"
    assert result["model"] == "custom-model"
    assert result["reasoning_effort"] == "medium"
    assert result["sandbox_mode"] == "read-only"
    assert result["commit_mode"] == "forbidden"


def test_explore_fails_closed_when_command_cannot_enforce_codex_sandbox(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="python3 -c \"print('unsafe')\"")

    result = registry.run_codex(
        task="inspect",
        kind="explore",
        cwd=tmp_path,
        wait_seconds=0,
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "readonly_sandbox_unavailable"


def test_pi_explore_invocation_enforces_read_only_tool_allowlist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("CHATGPT_MCP_AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("CHATGPT_MCP_HEALTH_TOKEN", "health-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        returncode = 0

        def __init__(self, args, **kwargs) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

        def communicate(self, timeout=None):
            return b'{"ok": true}', b""

    registry = ExecutorRegistry(
        codex_command=None,
        pi_command="pi",
        default_harness="pi",
    )
    monkeypatch.setattr(executors, "_command_available", lambda command: True)
    monkeypatch.setattr(executors.subprocess, "Popen", FakeProcess)

    result = registry.run_delegate(
        kind="explore",
        task="inspect the scheduler",
        cwd=tmp_path,
        wait_seconds=2,
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert args[0] == "pi"
    assert "--print" in args
    assert "--no-session" in args
    assert "--no-approve" in args
    assert "--no-extensions" in args
    assert "--no-skills" in args
    assert "--no-context-files" in args
    assert args[args.index("--tools") + 1] == "read,grep,find,ls"
    assert "--approve" not in args
    assert "--model" not in args
    assert "--thinking" not in args
    assert result["executor"] == "pi"
    assert result["harness"] == "pi"
    assert result["sandbox_mode"] == "tool-allowlist-read-only"
    assert result["commit_mode"] == "forbidden"
    assert result["structured_output"] == {"ok": True}
    assert "pi-delegates" in result["logs"]["log_dir"]
    child_env = captured["kwargs"]["env"]
    assert "CHATGPT_MCP_AUTH_TOKEN" not in child_env
    assert "CHATGPT_MCP_HEALTH_TOKEN" not in child_env
    assert child_env["OPENAI_API_KEY"] == "provider-secret"


def test_pi_code_invocation_maps_model_and_reasoning_flags(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        returncode = 0

        def __init__(self, args, **kwargs) -> None:
            captured["args"] = args

        def communicate(self, timeout=None):
            return b"done", b""

    registry = ExecutorRegistry(codex_command=None, pi_command="pi")
    monkeypatch.setattr(executors, "_command_available", lambda command: True)
    monkeypatch.setattr(executors.subprocess, "Popen", FakeProcess)

    result = registry.run_delegate(
        harness="pi",
        kind="code",
        task="implement the change",
        cwd=tmp_path,
        model="anthropic/claude-sonnet-4",
        reasoning_effort="high",
        wait_seconds=2,
    )

    args = captured["args"]
    assert isinstance(args, list)
    assert args[args.index("--model") + 1] == "anthropic/claude-sonnet-4"
    assert args[args.index("--thinking") + 1] == "high"
    assert "--approve" in args
    assert "--tools" not in args
    assert result["status"] == "succeeded"
    assert result["executor"] == "pi"
    assert result["sandbox_mode"] == "full-tool-access"


def test_delegate_rejects_unknown_harness_with_available_names(tmp_path: Path) -> None:
    registry = ExecutorRegistry(codex_command="codex", pi_command="pi")

    result = registry.run_delegate(
        harness="missing-agent",
        task="inspect",
        cwd=tmp_path,
    )

    assert result["error"]["code"] == "unsupported_delegate_harness"
    assert result["error"]["available_harnesses"] == ["codex", "pi"]
    assert result["harness"] == "missing-agent"


def test_codex_read_only_sandbox_probe_uses_local_non_llm_command(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class Completed:
        returncode = 1

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Completed()

    delegate_harnesses.codex_read_only_sandbox_available.cache_clear()
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(delegate_harnesses.subprocess, "run", fake_run)

    available = delegate_harnesses.codex_read_only_sandbox_available("codex")

    assert available is False
    assert captured["args"] == ["codex", "sandbox", "/bin/true"]
    assert captured["kwargs"]["timeout"] == 5
    assert captured["kwargs"]["close_fds"] is True
    delegate_harnesses.codex_read_only_sandbox_available.cache_clear()


def test_codex_harness_info_gates_explore_on_runtime_sandbox(
    monkeypatch,
) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(
        delegate_harnesses,
        "codex_read_only_sandbox_available",
        lambda _command: False,
    )

    info = CodexHarness(command="codex").info()

    assert info["available"] is True
    assert info["read_only_supported"] is True
    assert info["explore_available"] is False
    assert info["read_only_runtime_available"] is False
    assert info["read_only_runtime_reason"] == "codex_sandbox_unavailable"


def test_antigravity_second_account_uses_isolated_home(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "agy2-home"
    credential = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    credential.parent.mkdir(parents=True)
    credential.write_text("token", encoding="utf-8")
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    harness = AntigravityHarness(
        name="antigravity2",
        display_name="Antigravity 2",
        command="agy",
        home_dir=home,
    )

    info = harness.info()
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[harness],
        durable_harnesses=("antigravity2",),
    )
    project = registry.project_resolver.resolve(tmp_path)
    task = registry._make_task(
        harness="antigravity2",
        project=project,
        cwd=tmp_path,
        kind="explore",
        task="inspect",
        goal=None,
        task_id=None,
        group_id=None,
        model="gemini-3.8-flash",
        reasoning_effort="high",
        sandbox_mode="plan+sandbox",
        commit_mode="forbidden",
        execution_timeout_seconds=30,
        depends_on_group_ids=(),
        files_in_scope=(),
        out_of_scope=(),
        context_files=(),
        acceptance_criteria=(),
        done_means=(),
        verification_commands=(),
        output_schema=None,
        parse_structured_output=True,
        request_fingerprint="agy2-isolation",
        logical_session_id=None,
    )
    invocation = harness.build_invocation(task)

    assert info["available"] is True
    assert info["account_authenticated"] is True
    assert invocation.env_overrides == {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
    }


def test_antigravity_second_account_is_unavailable_until_authenticated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    harness = AntigravityHarness(
        name="antigravity2",
        display_name="Antigravity 2",
        command="agy",
        home_dir=tmp_path / "agy2-home",
    )

    info = harness.info()

    assert info["available"] is False
    assert info["explore_available"] is False
    assert info["account_authenticated"] is False
    assert info["availability_reason"] == "account_authentication_required"


def test_registry_accepts_programmatic_generic_cli_harness() -> None:
    registry = ExecutorRegistry(
        harnesses=[
            GenericCliHarness(
                name="custom",
                command="custom-agent run",
                explore_command="custom-agent inspect --read-only",
            )
        ]
    )

    info = registry.harness_info()["custom"]
    assert info["read_only_supported"] is True
    assert info["explore"]["sandbox_mode"] == "adapter-enforced-read-only"

def test_routing_guidance_prefers_codex_for_bounded_work_and_antigravity_for_review(
    monkeypatch,
) -> None:
    registry = ExecutorRegistry(codex_command=None, default_harness="antigravity")
    monkeypatch.setattr(
        registry,
        "harness_info",
        lambda: {
            "codex": {
                "available": True,
                "explore_available": True,
                "read_only_supported": True,
            },
            "antigravity": {
                "available": True,
                "explore_available": True,
                "read_only_supported": True,
            },
        },
    )

    guidance = registry.routing_guidance()

    assert guidance["automatic_routing"] is False
    assert guidance["explicit_harness_override_preserved"] is True
    assert guidance["profiles"]["bounded_explore"]["preferred_harness"] == "codex"
    assert guidance["profiles"]["independent_review"]["preferred_harness"] == "antigravity"
    assert guidance["profiles"]["implementation"]["preferred_harness"] == "codex"
    assert guidance["continuation"]["prefer_resume_for_same_review"] is True


def test_routing_guidance_falls_back_only_to_available_harnesses(monkeypatch) -> None:
    registry = ExecutorRegistry(codex_command=None, default_harness="antigravity")
    monkeypatch.setattr(
        registry,
        "harness_info",
        lambda: {
            "codex": {
                "available": False,
                "explore_available": False,
                "read_only_supported": True,
            },
            "antigravity": {
                "available": False,
                "explore_available": False,
                "read_only_supported": True,
            },
            "pi": {
                "available": True,
                "explore_available": True,
                "read_only_supported": True,
            },
        },
    )

    guidance = registry.routing_guidance()

    assert guidance["profiles"]["bounded_explore"]["preferred_harness"] == "pi"
    assert (
        guidance["profiles"]["bounded_explore"]["reason"]
        == "available read-only fallback for bounded repository discovery"
    )
    assert guidance["profiles"]["independent_review"]["preferred_harness"] == "pi"
    assert guidance["profiles"]["implementation"]["preferred_harness"] == "pi"
    assert guidance["continuation"]["prefer_resume_for_same_review"] is False

    monkeypatch.setattr(
        registry,
        "harness_info",
        lambda: {
            "codex": {
                "available": False,
                "explore_available": False,
                "read_only_supported": True,
            },
            "antigravity": {
                "available": True,
                "explore_available": False,
                "read_only_supported": True,
            },
        },
    )
    unavailable_review = registry.routing_guidance()
    assert unavailable_review["profiles"]["bounded_explore"]["preferred_harness"] is None
    assert (
        unavailable_review["profiles"]["bounded_explore"]["reason"]
        == "no compatible read-only delegate harness available"
    )
    assert unavailable_review["profiles"]["independent_review"]["preferred_harness"] is None
    assert (
        unavailable_review["profiles"]["implementation"]["preferred_harness"]
        == "antigravity"
    )
    assert unavailable_review["continuation"]["prefer_resume_for_same_review"] is False

    registry.default_harness = "claude"
    monkeypatch.setattr(
        registry,
        "harness_info",
        lambda: {
            "codex": {
                "available": True,
                "explore_available": True,
                "read_only_supported": True,
            },
            "antigravity": {
                "available": False,
                "explore_available": False,
                "read_only_supported": True,
            },
            "claude": {
                "available": True,
                "explore_available": True,
                "read_only_supported": True,
            },
        },
    )
    configured_default = registry.routing_guidance()
    assert (
        configured_default["profiles"]["bounded_explore"]["preferred_harness"]
        == "codex"
    )
    assert (
        configured_default["profiles"]["independent_review"]["preferred_harness"]
        == "claude"
    )


def test_project_prompt_context_is_compact_and_includes_submission_head(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tmp_path / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    registry = ExecutorRegistry(codex_command="codex")
    project = registry.project_resolver.resolve(tmp_path)
    context = registry._project_prompt_context(project, cwd=tmp_path)
    prompt = registry._build_prompt(
        task="inspect bounded scope",
        goal=None,
        project_context=context,
        files_in_scope=["tracked.txt"],
        context_files=[],
        acceptance_criteria=[],
        verification_commands=[],
        commit_mode="forbidden",
        kind="explore",
    )

    assert f"Git HEAD at submission: {head[:12]}" in context
    assert str(tmp_path.resolve()) in context[0]
    assert all("tracked.txt" not in item for item in context)
    assert "Project context:" in prompt
    assert f"Git HEAD at submission: {head[:12]}" in prompt
    assert "Files in scope:\n- tracked.txt" in prompt

class _RoutingQuotaGate:
    def __init__(self, rows: dict[str, tuple[bool, float]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str | None, bool]] = []

    def decision(self, *, harness: str, model: str | None, fresh: bool = False) -> dict[str, object]:
        self.calls.append((harness, model, fresh))
        allowed, remaining = self.rows.get(harness, (True, 100.0))
        return {
            "allowed": allowed,
            "harness": harness,
            "model": model,
            "provider_status": "ok",
            "reason": None if allowed else "quota_threshold_reached",
            "buckets": [{
                "key": f"{harness}_5h",
                "window": "5h",
                "threshold_percent": 10.0,
                "remaining_percent": remaining,
                "resets_at": None,
                "window_expired": False,
                "blocked": not allowed,
            }],
        }

    def note_terminal(self, _snapshot: dict[str, object]) -> None:
        return


def _automatic_routing_registry(monkeypatch, *, gate: _RoutingQuotaGate | None = None) -> ExecutorRegistry:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    harnesses = [
        GenericCliHarness(name="antigravity", command="agent1", explore_command="agent1-ro"),
        GenericCliHarness(name="antigravity2", command="agent2", explore_command="agent2-ro"),
        GenericCliHarness(name="codex", command="codex-agent", explore_command="codex-agent-ro"),
    ]
    return ExecutorRegistry(
        codex_command=None,
        harnesses=harnesses,
        default_harness="antigravity",
        automatic_routing=True,
        primary_harness="antigravity",
        fallback_harnesses=("antigravity2", "codex"),
        quota_admission_gate=gate,
    )


def test_automatic_routing_prefers_primary_when_admissible(monkeypatch) -> None:
    gate = _RoutingQuotaGate({"antigravity": (True, 30), "antigravity2": (True, 90), "codex": (True, 90)})
    registry = _automatic_routing_registry(monkeypatch, gate=gate)

    name, _adapter, _quota, route = registry._select_harness(
        harness=None, kind="code", model=None, fresh=False, record_selection=True,
    )

    assert name == "antigravity"
    assert route["reason"] == "primary_available"
    assert [call[0] for call in gate.calls] == ["antigravity"]


def test_automatic_routing_balances_fallbacks_by_headroom_and_load(monkeypatch) -> None:
    gate = _RoutingQuotaGate({"antigravity": (False, 0), "antigravity2": (True, 90), "codex": (True, 50)})
    registry = _automatic_routing_registry(monkeypatch, gate=gate)

    selected = [
        registry._select_harness(
            harness=None, kind="code", model=None, fresh=False, record_selection=True,
        )[0]
        for _ in range(3)
    ]

    assert selected == ["antigravity2", "codex", "antigravity2"]
    assert registry._routing_selection_counts == {"antigravity2": 2, "codex": 1}


def test_automatic_routing_preserves_explicit_override(monkeypatch) -> None:
    gate = _RoutingQuotaGate({"antigravity": (True, 100), "codex": (False, 0)})
    registry = _automatic_routing_registry(monkeypatch, gate=gate)
    registry._routing_unavailable_until["codex"] = {"until_epoch": 10**12, "reason": "test"}

    name, _adapter, routed_quota, route = registry._select_harness(
        harness="codex", kind="code", model=None, fresh=True, record_selection=True,
    )

    assert name == "codex"
    assert routed_quota is None
    assert route is None
    assert gate.calls == []


def test_automatic_routing_pins_resume_to_source_antigravity_account(monkeypatch) -> None:
    registry = _automatic_routing_registry(monkeypatch)
    monkeypatch.setattr(
        registry,
        "_resume_source_snapshot",
        lambda _delegate_id: {"harness": "antigravity2", "completed": True},
    )

    name, _adapter, _quota, route = registry._select_harness(
        harness=None, kind="explore", model=None,
        resume_from_delegate_id="abc123abc123", fresh=False, record_selection=True,
    )

    assert name == "antigravity2"
    assert route["reason"] == "resume_account_pinned"


def test_routing_provenance_distinguishes_explicit_automatic_and_resume(
    monkeypatch,
) -> None:
    registry = _automatic_routing_registry(monkeypatch)

    assert registry._routing_provenance(
        requested_harness="codex",
        route=None,
    ) == ("explicit", "explicit_harness")
    assert registry._routing_provenance(
        requested_harness=None,
        route={"reason": "primary_available"},
    ) == ("automatic", "primary_available")
    assert registry._routing_provenance(
        requested_harness=None,
        route={"reason": "resume_account_pinned"},
    ) == ("resume", "resume_account_pinned")


def test_runtime_eligibility_failure_temporarily_removes_primary_from_automatic_routing(monkeypatch) -> None:
    registry = _automatic_routing_registry(monkeypatch)
    registry._note_routing_terminal({
        "harness": "antigravity",
        "error": {
            "code": "antigravity_result_error",
            "message": "Eligibility check failed: account is not currently available in your location.",
        },
    })

    name, _adapter, _quota, route = registry._select_harness(
        harness=None, kind="code", model=None, fresh=False, record_selection=True,
    )

    assert name == "antigravity2"
    primary = route["candidates"][0]
    assert primary["reason"] == "runtime_unavailable_cooldown"
    assert registry.routing_guidance()["runtime_blocks"]["antigravity"]["reason"] == "runtime_provider_unavailable"
    assert registry.routing_guidance()["eligibility_watchdog"]["last_status"] == "disabled"


def test_automatic_routing_disabled_keeps_default_harness(monkeypatch) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[GenericCliHarness(name="antigravity", command="agent")],
        default_harness="antigravity",
        automatic_routing=False,
        primary_harness="missing-primary",
        fallback_harnesses=("missing-fallback",),
    )

    name, _adapter, _quota, route = registry._select_harness(
        harness=None, kind="code", model=None, fresh=False, record_selection=True,
    )

    assert name == "antigravity"
    assert route is None


def _executable_automatic_registry(*, gate: _RoutingQuotaGate) -> ExecutorRegistry:
    command = "python3 -c \"print('done')\""
    harnesses = [
        GenericCliHarness(name="antigravity", command=command, explore_command=command),
        GenericCliHarness(name="antigravity2", command=command, explore_command=command),
        GenericCliHarness(name="codex", command=command, explore_command=command),
    ]
    return ExecutorRegistry(
        codex_command=None,
        harnesses=harnesses,
        default_harness="antigravity",
        automatic_routing=True,
        primary_harness="antigravity",
        fallback_harnesses=("antigravity2", "codex"),
        quota_admission_gate=gate,
    )


def test_run_delegate_uses_automatic_fallback_when_primary_quota_blocked(tmp_path: Path) -> None:
    gate = _RoutingQuotaGate({"antigravity": (False, 0), "antigravity2": (True, 90), "codex": (True, 50)})
    registry = _executable_automatic_registry(gate=gate)

    result = registry.run_delegate(task="bounded code", cwd=tmp_path, harness=None, wait_seconds=3)

    assert result["success"] is True
    assert result["status"] == "succeeded"
    assert result["harness"] == "antigravity2"


def test_delegate_batch_routes_once_for_entire_group(tmp_path: Path) -> None:
    gate = _RoutingQuotaGate({"antigravity": (False, 0), "antigravity2": (True, 90), "codex": (True, 50)})
    registry = _executable_automatic_registry(gate=gate)

    result = registry.run_delegate_batch(
        tasks=[{"task": "inspect A"}, {"task": "inspect B"}],
        cwd=tmp_path,
        harness=None,
        wait_seconds=3,
    )

    assert result["success"] is True
    assert result["harness"] == "antigravity2"
    group = registry.scheduler.get_group(result["group_id"])
    assert group is not None
    tasks = [registry.scheduler.get_task(delegate_id) for delegate_id in group.child_ids]
    assert {task.harness for task in tasks if task is not None} == {"antigravity2"}
    assert registry._routing_selection_counts == {"antigravity2": 1}


def test_automatic_routing_guidance_reports_primary_fallback_policy(monkeypatch) -> None:
    gate = _RoutingQuotaGate({"antigravity": (False, 0), "antigravity2": (True, 90), "codex": (True, 50)})
    registry = _automatic_routing_registry(monkeypatch, gate=gate)

    guidance = registry.routing_guidance()

    assert guidance["automatic_routing"] is True
    assert guidance["policy"]["primary_harness"] == "antigravity"
    assert guidance["policy"]["fallback_harnesses"] == ["antigravity2", "codex"]
    assert guidance["policy"]["fallback_strategy"] == "quota_headroom_weighted_fair"
    assert guidance["profiles"]["implementation"]["preferred_harness"] == "antigravity2"
    assert guidance["current_routes"]["code"]["reason"] == "primary_unavailable_balanced_fallback"


def test_antigravity_eligibility_watchdog_reports_missing_script(tmp_path: Path) -> None:
    script = tmp_path / "missing-recovery.sh"
    registry = ExecutorRegistry(
        antigravity_eligibility_watchdog_script=script,
        delegate_state_root=tmp_path / "delegates",
    )

    registry._note_routing_terminal({
        "harness": "antigravity",
        "error": {
            "code": "antigravity_result_error",
            "message": "Eligibility check failed: not currently available in your location.",
        },
    })

    status = registry._eligibility_watchdog_status
    assert status["configured"] is True
    assert status["script"] == str(script.resolve())
    assert status["last_status"] == "script_missing"


def test_antigravity_eligibility_watchdog_launches_once_per_cooldown(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "recovery.sh"
    marker = tmp_path / "marker.txt"
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s|%s|%s|%s\\n' \"$CHATGPT_MCP_WATCHDOG_EVENT\" \"$CHATGPT_MCP_WATCHDOG_HARNESS\" \"$CHATGPT_MCP_WATCHDOG_ERROR_CODE\" \"${CHATGPT_MCP_AUTH_TOKEN-unset}\" >> \"$WATCHDOG_MARKER\"\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("WATCHDOG_MARKER", str(marker))
    monkeypatch.setenv("CHATGPT_MCP_AUTH_TOKEN", "must-not-leak")
    registry = ExecutorRegistry(
        antigravity_eligibility_watchdog_script=script,
        delegate_state_root=tmp_path / "delegates",
        routing_unavailable_cooldown_seconds=60,
    )
    failure = {
        "harness": "antigravity",
        "error": {
            "code": "antigravity_result_error",
            "message": "Eligibility check failed: not currently available in your location.",
        },
    }

    registry._note_routing_terminal(failure)
    for _ in range(100):
        if registry._eligibility_watchdog_status.get("last_status") == "completed_success":
            break
        time.sleep(0.01)

    lines = marker.read_text(encoding="utf-8").splitlines()
    status = registry._eligibility_watchdog_status
    assert lines == ["antigravity_eligibility_failure|antigravity|antigravity_result_error|unset"]
    assert status["last_status"] == "completed_success"
    assert status["exit_code"] == 0
    assert status["routing_state_refreshed"] is True
    assert status["runtime_block_cleared"] is True
    assert status["next_route_action"] == "reprobe_antigravity_on_next_automatic_delegate"
    assert "antigravity" not in registry._routing_unavailable_until
    assert (tmp_path / "delegates" / "watchdogs" / "antigravity-eligibility.log").is_file()

    # A repeated eligibility failure inside the original episode may create a
    # fresh runtime block, but must not launch the recovery script again.
    registry._note_routing_terminal(failure)
    time.sleep(0.05)
    assert marker.read_text(encoding="utf-8").splitlines() == lines
    assert registry._eligibility_watchdog_status["last_status"] == "suppressed_active_cooldown"


def test_antigravity2_eligibility_failure_does_not_launch_primary_watchdog(tmp_path: Path) -> None:
    script = tmp_path / "recovery.sh"
    marker = tmp_path / "marker.txt"
    script.write_text(f"#!/bin/sh\necho ran >> {marker}\n", encoding="utf-8")
    script.chmod(0o755)
    registry = ExecutorRegistry(
        antigravity_eligibility_watchdog_script=script,
        delegate_state_root=tmp_path / "delegates",
    )

    registry._note_routing_terminal({
        "harness": "antigravity2",
        "error": {
            "code": "antigravity_result_error",
            "message": "Eligibility check failed: not currently available in your location.",
        },
    })
    time.sleep(0.05)

    assert marker.exists() is False
    assert registry._eligibility_watchdog_status["last_status"] == "not_triggered"


def test_antigravity_eligibility_watchdog_failed_script_keeps_runtime_block(tmp_path: Path) -> None:
    script = tmp_path / "recovery.sh"
    script.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    script.chmod(0o755)
    registry = ExecutorRegistry(
        antigravity_eligibility_watchdog_script=script,
        delegate_state_root=tmp_path / "delegates",
        routing_unavailable_cooldown_seconds=60,
    )

    registry._note_routing_terminal({
        "harness": "antigravity",
        "error": {
            "code": "antigravity_result_error",
            "message": "Eligibility check failed: not currently available in your location.",
        },
    })
    for _ in range(100):
        if registry._eligibility_watchdog_status.get("last_status") == "completed_failed":
            break
        time.sleep(0.01)

    status = registry._eligibility_watchdog_status
    assert status["last_status"] == "completed_failed"
    assert status["exit_code"] == 7
    assert status["routing_state_refreshed"] is True
    assert status["runtime_block_cleared"] is False
    assert status["next_route_action"] == "keep_runtime_block_until_cooldown"
    assert registry._routing_block("antigravity") is not None


def test_antigravity_eligibility_watchdog_timeout_terminates_group_and_keeps_block(
    tmp_path: Path,
) -> None:
    script = tmp_path / "recovery.sh"
    script.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    script.chmod(0o755)
    registry = ExecutorRegistry(
        antigravity_eligibility_watchdog_script=script,
        antigravity_eligibility_watchdog_timeout_seconds=0.1,
        delegate_state_root=tmp_path / "delegates",
        routing_unavailable_cooldown_seconds=60,
    )

    registry._note_routing_terminal({
        "harness": "antigravity",
        "error": {
            "code": "antigravity_result_error",
            "message": "Eligibility check failed: not currently available in your location.",
        },
    })
    for _ in range(300):
        if registry._eligibility_watchdog_status.get("last_status") == "completed_failed":
            break
        time.sleep(0.01)

    status = registry._eligibility_watchdog_status
    assert status["last_status"] == "completed_failed"
    assert status["exit_code"] == executors.TIMEOUT_EXIT_CODE
    assert status["timed_out"] is True
    assert status["termination_verified"] is True
    assert status["runtime_block_cleared"] is False
    assert status["next_route_action"] == "keep_runtime_block_until_cooldown"
    assert registry._routing_block("antigravity") is not None


def test_antigravity_eligibility_watchdog_does_not_clear_newer_runtime_block(tmp_path: Path) -> None:
    release = tmp_path / "release"
    script = tmp_path / "recovery.sh"
    script.write_text(
        f"#!/bin/sh\nwhile [ ! -e {release} ]; do sleep 0.01; done\nexit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    registry = ExecutorRegistry(
        antigravity_eligibility_watchdog_script=script,
        delegate_state_root=tmp_path / "delegates",
        routing_unavailable_cooldown_seconds=60,
    )

    registry._note_routing_terminal({
        "harness": "antigravity",
        "error": {
            "code": "antigravity_result_error",
            "message": "Eligibility check failed: not currently available in your location.",
        },
    })
    first_until = float(registry._routing_unavailable_until["antigravity"]["until_epoch"])
    with registry._lock:
        registry._routing_unavailable_until["antigravity"] = {
            "reason": "runtime_provider_unavailable",
            "observed_at_epoch": time.time(),
            "until_epoch": first_until + 30,
            "error_code": "newer_failure",
        }
    release.touch()
    for _ in range(100):
        if registry._eligibility_watchdog_status.get("last_status") == "completed_success":
            break
        time.sleep(0.01)

    status = registry._eligibility_watchdog_status
    assert status["last_status"] == "completed_success"
    assert status["runtime_block_cleared"] is False
    assert status["next_route_action"] == "preserve_newer_runtime_block"
    assert registry._routing_unavailable_until["antigravity"]["error_code"] == "newer_failure"
