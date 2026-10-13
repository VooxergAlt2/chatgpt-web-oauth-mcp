from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

from chatgpt_web_oauth_mcp import executors
import chatgpt_web_oauth_mcp.delegate_harnesses as delegate_harnesses
from chatgpt_web_oauth_mcp.delegate_harnesses import (
    AntigravityHarness,
    CodexHarness,
    GenericCliHarness,
    LocalOpenAIHarness,
)
from chatgpt_web_oauth_mcp.delegate_failures import runtime_provider_failure
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


def test_infer_local_tool_call_budget_from_task_text() -> None:
    assert executors._infer_local_tool_call_budget(
        "Read-only scout, use at most 6 tool calls."
    ) == 6
    assert executors._infer_local_tool_call_budget(
        "Use at most 8 tool calls; max 5 tool calls if possible."
    ) == 5
    assert executors._infer_local_tool_call_budget(
        "Верни не более 6 выводов и не более 4 вызовов инструмента."
    ) == 4
    assert executors._infer_local_tool_call_budget(
        "Return at most 6 bullets."
    ) is None


def test_run_delegate_infers_server_tool_budget_for_explicit_local(
    monkeypatch,
    tmp_path: Path,
) -> None:
    harness = LocalOpenAIHarness(
        endpoint="http://192.0.2.10:8081",
        default_model="qwen-coder-next",
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[harness],
        default_harness="local",
        automatic_routing=False,
        durable_harnesses=(),
        delegate_state_root=tmp_path / "delegate-state",
    )
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)

    result = registry.run_delegate(
        task="Read-only scout, use at most 6 tool calls. Inspect one file.",
        cwd=tmp_path,
        kind="explore",
        harness="local",
        wait_seconds=3,
    )

    assert result["success"] is True
    assert len(captured) == 1
    assert captured[0].max_tool_calls == 6
    assert "Server-enforced local tool-call budget:\n6" in captured[0].prompt


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
    assert result["model"] == "gpt-6.1-sol"
    assert result["reasoning_effort"] == "low"
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
    captured_calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setenv("CHATGPT_MCP_AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("CHATGPT_MCP_HEALTH_TOKEN", "health-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")

    class FakeProcess:
        stdin = None
        stdout = None
        stderr = None
        returncode = 0

        def __init__(self, args, **kwargs) -> None:
            captured_calls.append((args, kwargs))

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

    pi_calls = [
        (args, kwargs)
        for args, kwargs in captured_calls
        if isinstance(args, list) and args and args[0] == "pi"
    ]
    assert len(pi_calls) == 1
    args, pi_kwargs = pi_calls[0]
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
    child_env = pi_kwargs["env"]
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


def test_automatic_code_routing_uses_ordered_fallbacks(monkeypatch) -> None:
    gate = _RoutingQuotaGate({"antigravity": (False, 0), "antigravity2": (True, 90), "codex": (True, 50)})
    registry = _automatic_routing_registry(monkeypatch, gate=gate)

    selected = [
        registry._select_harness(
            harness=None, kind="code", model=None, fresh=False, record_selection=True,
        )[0]
        for _ in range(3)
    ]

    assert selected == ["antigravity2", "antigravity2", "antigravity2"]
    assert registry._routing_selection_counts == {"antigravity2": 3}


def test_complexity_medium_skips_flash(monkeypatch) -> None:
    gate = _RoutingQuotaGate(
        {"antigravity": (True, 100), "antigravity2": (True, 90), "codex": (True, 90)}
    )
    registry = _automatic_routing_registry(monkeypatch, gate=gate)

    name, _adapter, _quota, route = registry._select_harness(
        harness=None,
        kind="code",
        model=None,
        complexity="medium",
        fresh=False,
        record_selection=True,
    )

    assert name == "antigravity2"
    assert route["reason"] == "complexity_medium_selected"
    assert [row["harness"] for row in route["candidates"]] == [
        "antigravity2",
        "codex",
    ]


def test_complexity_medium_falls_back_only_upward(monkeypatch) -> None:
    gate = _RoutingQuotaGate(
        {"antigravity": (True, 100), "antigravity2": (False, 0), "codex": (True, 90)}
    )
    registry = _automatic_routing_registry(monkeypatch, gate=gate)

    name, _adapter, _quota, route = registry._select_harness(
        harness=None,
        kind="code",
        model=None,
        complexity="medium",
        fresh=False,
        record_selection=True,
    )

    assert name == "codex"
    assert route["reason"] == "complexity_medium_selected"
    assert all(row["harness"] != "antigravity" for row in route["candidates"])


def test_complexity_high_goes_directly_to_codex(monkeypatch) -> None:
    gate = _RoutingQuotaGate(
        {"antigravity": (True, 100), "antigravity2": (True, 100), "codex": (True, 90)}
    )
    registry = _automatic_routing_registry(monkeypatch, gate=gate)

    name, _adapter, _quota, route = registry._select_harness(
        harness=None,
        kind="code",
        model=None,
        complexity="high",
        fresh=False,
        record_selection=True,
    )

    assert name == "codex"
    assert route["reason"] == "complexity_high_selected"
    assert [row["harness"] for row in route["candidates"]] == ["codex"]


def test_automatic_explore_routing_balances_fallbacks_by_headroom_and_load(monkeypatch) -> None:
    monkeypatch.setattr(delegate_harnesses, "command_available", lambda _command: True)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    gate = _RoutingQuotaGate(
        {"antigravity": (False, 0), "antigravity2": (True, 90), "reviewer": (True, 50)}
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[
            GenericCliHarness(name="antigravity", command="agent1", explore_command="agent1-ro"),
            GenericCliHarness(name="antigravity2", command="agent2", explore_command="agent2-ro"),
            GenericCliHarness(name="reviewer", command="reviewer", explore_command="reviewer-ro"),
        ],
        default_harness="antigravity",
        automatic_routing=True,
        primary_harness="antigravity",
        fallback_harnesses=("antigravity2", "reviewer"),
        quota_admission_gate=gate,
    )

    selected = [
        registry._select_harness(
            harness=None, kind="explore", model=None, fresh=False, record_selection=True,
        )[0]
        for _ in range(3)
    ]

    assert selected == ["antigravity2", "reviewer", "antigravity2"]
    assert registry._routing_selection_counts == {"antigravity2": 2, "reviewer": 1}


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
    assert registry._routing_provenance(
        requested_harness=None,
        route={"reason": "complexity_high_selected"},
    ) == ("complexity", "complexity_high_selected")


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


def test_generic_harness_unavailable_enters_runtime_cooldown(monkeypatch) -> None:
    registry = _automatic_routing_registry(monkeypatch)

    registry._note_routing_terminal(
        {
            "harness": "codex",
            "error": {
                "code": "delegate_harness_unavailable",
                "message": "codex command unavailable",
            },
        }
    )

    block = registry.routing_guidance()["runtime_blocks"]["codex"]
    assert block["reason"] == "runtime_provider_unavailable"
    assert block["failure_class"] == "harness_unavailable"


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


def test_runtime_geo_failure_falls_through_to_next_code_harness(
    monkeypatch,
    tmp_path: Path,
) -> None:
    gate = _RoutingQuotaGate(
        {"antigravity": (True, 100), "antigravity2": (True, 90), "codex": (True, 80)}
    )
    registry = _executable_automatic_registry(gate=gate)
    calls: list[str] = []

    def fake_start(*, delegate_task):
        calls.append(delegate_task.harness)
        if delegate_task.harness == "antigravity":
            return {
                "success": False,
                "status": "failed",
                "harness": "antigravity",
                "model": delegate_task.model,
                "error": {
                    "code": "antigravity_result_error",
                    "message": (
                        "FAILED_PRECONDITION (code 400): User location is not supported "
                        "for the API use."
                    ),
                },
            }
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)

    result = registry.run_delegate(
        task="bounded code",
        cwd=tmp_path,
        harness=None,
        wait_seconds=3,
    )

    assert calls == ["antigravity", "antigravity2"]
    assert result["success"] is True
    assert result["harness"] == "antigravity2"
    assert result["routing_fallback"]["trigger"] == "provider_runtime_failure"
    assert registry.routing_guidance()["runtime_blocks"]["antigravity"]["failure_class"] == (
        "eligibility_or_location"
    )


def test_runtime_quota_failure_uses_replan_switch_provider(
    monkeypatch,
    tmp_path: Path,
) -> None:
    gate = _RoutingQuotaGate(
        {"antigravity": (True, 100), "antigravity2": (True, 90), "codex": (True, 80)}
    )
    registry = _executable_automatic_registry(gate=gate)
    calls: list[str] = []

    def fake_start(*, delegate_task):
        calls.append(delegate_task.harness)
        if delegate_task.harness == "antigravity":
            return {
                "success": False,
                "status": "failed",
                "harness": "antigravity",
                "model": delegate_task.model,
                "harness_metadata": {"usage": {"total_tokens": 100}},
                "error": {
                    "code": "antigravity_quota_exhausted",
                    "message": "Individual quota reached",
                },
            }
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
            "harness_metadata": {"usage": {"total_tokens": 200}},
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="bounded code",
        cwd=tmp_path,
        harness=None,
        wait_seconds=3,
    )

    assert calls == ["antigravity", "antigravity2"]
    fallback = result["routing_fallback"]
    assert fallback["trigger"] == "rate_limit_replan"
    attempt = fallback["attempts"][0]
    assert attempt["failure_analysis"]["failure_type"] == "rate_limit"
    assert attempt["replan"]["action"] == "switch_provider"
    assert attempt["replan"]["automatic"] is True
    route_attempts = result["route_attempts"]
    assert len(route_attempts) == 2
    assert route_attempts[0]["harness"] == "antigravity"
    assert route_attempts[0]["failure_type"] == "rate_limit"
    assert route_attempts[0]["accepted"] is False
    assert route_attempts[0]["total_tokens"] == 100
    assert route_attempts[1]["harness"] == "antigravity2"
    assert route_attempts[1]["failure_type"] == "none"
    assert route_attempts[1]["accepted"] is True
    assert route_attempts[1]["total_tokens"] == 200
    route_chain = result["route_chain"]
    assert route_chain["attempt_count"] == 2
    assert route_chain["fallback_count"] == 1
    assert route_chain["total_tokens"] == 300
    assert route_chain["initial_route"].startswith("antigravity:")
    assert route_chain["final_route"].startswith("antigravity2:")



def _planner_test_registry(
    *,
    planner_mode: str,
    primary_harness: str,
    split_enabled: bool = False,
    split_max_files: int = 2,
    include_reviewer: bool = False,
    reviewer_split_max_files: int = 12,
    reviewer_split_max_bytes: int = 140_000,
) -> ExecutorRegistry:
    local = LocalOpenAIHarness(
        endpoint="http://192.0.2.10:8081",
        default_model="qwen-coder-next",
    )
    local2 = LocalOpenAIHarness(
        name="local2",
        display_name="Reviewer",
        role="reviewer",
        endpoint="http://192.0.2.11:8083",
        default_model="qwen38-27b",
    )
    return ExecutorRegistry(
        codex_command="true",
        harnesses=[local, *([local2] if include_reviewer else [])],
        allow_unsafe_explore_command=True,
        default_harness=primary_harness,
        automatic_routing=True,
        primary_harness=primary_harness,
        fallback_harnesses=(
            ("local", "local2", "codex")
            if include_reviewer
            else ("local", "codex")
        ),
        planner_mode=planner_mode,
        planner_split_enabled=split_enabled,
        planner_split_max_files_per_shard=split_max_files,
        planner_split_max_bytes_per_shard=100_000,
        planner_split_max_shards=6,
        planner_reviewer_max_files_per_shard=reviewer_split_max_files,
        planner_reviewer_max_bytes_per_shard=reviewer_split_max_bytes,
        planner_reviewer_max_concurrency=2,
        durable_harnesses=(),
    )


def _planner_available_candidate(*, harness, kind, model, fresh):
    del kind, model, fresh
    return {
        "harness": harness,
        "eligible": True,
        "reason": "available",
        "model": (
            "qwen-coder-next"
            if harness == "local"
            else "qwen38-27b"
            if harness == "local2"
            else "gpt-6.1-sol"
        ),
        "quota": None,
        "quota_headroom_percent": 50.0,
        "active_load": 0,
        "automatic_selections": 0,
        "balance_score": 0.0,
    }


def test_shadow_planner_observes_but_preserves_legacy_route(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = _planner_test_registry(
        planner_mode="shadow",
        primary_harness="local",
    )
    monkeypatch.setattr(registry, "_routing_candidate", _planner_available_candidate)
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="Review this small change and verify concrete regressions.",
        cwd=tmp_path,
        kind="explore",
        wait_seconds=3,
    )

    assert result["success"] is True
    assert captured[0].harness == "local"
    route_decision = captured[0].route_decision
    assert isinstance(route_decision, dict)
    planner = route_decision["planner"]
    assert planner["mode"] == "shadow"
    assert planner["authority"] == "legacy"
    assert route_decision["shadow_route"].startswith("codex:")


def test_active_planner_can_replace_legacy_route_with_scout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    registry = _planner_test_registry(
        planner_mode="active",
        primary_harness="codex",
    )
    monkeypatch.setattr(registry, "_routing_candidate", _planner_available_candidate)
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    target = tmp_path / "target.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    result = registry.run_delegate(
        task="Locate the definition in this small repository.",
        cwd=tmp_path,
        kind="explore",
        files_in_scope=[target.name],
        wait_seconds=3,
    )

    assert result["success"] is True
    assert captured[0].harness == "local"
    assert captured[0].routing_mode == "planner"
    route_decision = captured[0].route_decision
    assert isinstance(route_decision, dict)
    assert route_decision["planner"]["authority"] == "planner"
    assert route_decision["initial_route"].startswith("local:")


def test_active_planner_review_fits_reviewer_without_scout_split(
    monkeypatch,
    tmp_path: Path,
) -> None:
    files = []
    for index in range(7):
        path = tmp_path / f"review_{index}.py"
        path.write_text(f"VALUE = {index}\n", encoding="utf-8")
        files.append(path.name)
    registry = _planner_test_registry(
        planner_mode="active",
        primary_harness="codex",
        split_enabled=True,
        split_max_files=2,
        include_reviewer=True,
    )
    monkeypatch.setattr(registry, "_routing_candidate", _planner_available_candidate)
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="Review these files and verify concrete regressions.",
        cwd=tmp_path,
        kind="explore",
        complexity="medium",
        files_in_scope=files,
        wait_seconds=3,
    )

    assert result["success"] is True
    assert len(captured) == 1
    assert captured[0].harness == "local2"
    planner = captured[0].route_decision["planner"]
    assert planner["plan_kind"] == "direct"
    assert planner["split_plan"]["executable"] is False
    assert planner["split_plan"]["reason"] == "reviewer_direct_scope_fits"


def test_active_planner_large_review_splits_to_reviewer(
    monkeypatch,
    tmp_path: Path,
) -> None:
    files = []
    for index in range(13):
        path = tmp_path / f"review_{index}.py"
        path.write_text(f"VALUE = {index}\n", encoding="utf-8")
        files.append(path.name)
    registry = _planner_test_registry(
        planner_mode="active",
        primary_harness="codex",
        split_enabled=True,
        include_reviewer=True,
        reviewer_split_max_files=12,
    )
    monkeypatch.setattr(registry, "_routing_candidate", _planner_available_candidate)
    captured = []
    reserved = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
            "structured_output": {
                "status": "succeeded",
                "summary": "reviewed",
                "files_changed": [],
                "commands_run": [],
                "verification": "read-only",
                "findings": [f"reviewed:{delegate_task.task_id}"],
                "blockers": [],
                "recommended_next_action": "none",
            },
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="Review these files and verify concrete regressions.",
        cwd=tmp_path,
        kind="explore",
        complexity="medium",
        files_in_scope=files,
        wait_seconds=3,
        before_group_submit=lambda slots: reserved.append(slots) or None,
    )

    assert result["success"] is True
    assert result["completed"] is True
    assert reserved == [2]
    assert len(captured) == 2
    assert all(task.harness == "local2" for task in captured)
    assert sorted([
        len(shard["files"])
        for shard in result["planner_plan"]["split_plan"]["shards"]
    ]) == [6, 7]
    assert result["planner_plan"]["split_plan"]["target_harness"] == "local2"
    assert len(result["planner_synthesis"]["findings"]) == 2
    assert result["planner_synthesis"]["review_level"] == "reviewer"
    assert result["planner_synthesis"]["requires_authoritative_review"] is False


def test_active_planner_high_risk_review_stays_direct(
    monkeypatch,
    tmp_path: Path,
) -> None:
    files = []
    for index in range(13):
        path = tmp_path / f"runtime_{index}.py"
        path.write_text(f"VALUE = {index}\n", encoding="utf-8")
        files.append(path.name)
    registry = _planner_test_registry(
        planner_mode="active",
        primary_harness="codex",
        split_enabled=True,
        include_reviewer=True,
    )
    monkeypatch.setattr(registry, "_routing_candidate", _planner_available_candidate)
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="Review this production runtime change and verify concrete regressions.",
        cwd=tmp_path,
        kind="explore",
        complexity="high",
        files_in_scope=files,
        wait_seconds=3,
    )

    assert result["success"] is True
    assert len(captured) == 1
    planner = captured[0].route_decision["planner"]
    assert planner["plan_kind"] == "direct"
    assert planner["split_plan"]["executable"] is False
    assert (
        planner["split_plan"]["reason"]
        == "high_risk_review_prefers_direct_authoritative_route"
    )
    assert captured[0].harness == "codex"




def test_explicit_antigravity_claude_route_uses_no_effort(
    monkeypatch,
    tmp_path: Path,
) -> None:
    antigravity = AntigravityHarness(
        command="agy",
        configured_models=("claude-opus-4-6-thinking",),
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[antigravity],
        default_harness="antigravity",
        automatic_routing=False,
        durable_harnesses=(),
    )
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="Read-only review.",
        cwd=tmp_path,
        harness="antigravity",
        model="claude-opus-4-6-thinking",
        reasoning_effort="high",
        kind="explore",
        wait_seconds=3,
    )

    assert result["success"] is True
    assert captured[0].model == "claude-opus-4-6-thinking"
    assert captured[0].reasoning_effort == "none"
    assert captured[0].route_decision["initial_route"].endswith(
        "claude-opus-4-6-thinking:none"
    )


def test_antigravity_gemini_flash_uses_supported_effort(
    monkeypatch,
    tmp_path: Path,
) -> None:
    antigravity = AntigravityHarness(
        command="agy",
        default_model="gemini-3.8-flash",
        default_reasoning_effort="medium",
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[antigravity],
        default_harness="antigravity",
        automatic_routing=False,
        durable_harnesses=(),
    )
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="Review the repository without editing.",
        cwd=tmp_path,
        harness="antigravity",
        model="gemini-3.8-flash",
        reasoning_effort="medium",
        kind="explore",
        wait_seconds=3,
    )

    assert result["success"] is True
    assert captured[0].model == "gemini-3.8-flash"
    assert captured[0].reasoning_effort == "medium"
    invocation_args = antigravity.build_invocation(captured[0]).args
    assert invocation_args[invocation_args.index("--effort") + 1] == "medium"
    assert captured[0].route_decision["initial_route"].endswith(
        "gemini-3.8-flash:medium"
    )


def test_active_planner_keeps_antigravity_opus_for_explicit_analysis_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    antigravity = AntigravityHarness(
        command="agy",
        configured_models=("claude-opus-4-6-thinking",),
    )
    local = LocalOpenAIHarness(
        endpoint="http://192.0.2.10:8081",
        default_model="qwen-coder-next",
    )
    registry = ExecutorRegistry(
        codex_command="true",
        harnesses=[local, antigravity],
        default_harness="codex",
        automatic_routing=True,
        allow_unsafe_explore_command=True,
        primary_harness="codex",
        fallback_harnesses=("local", "antigravity"),
        planner_mode="active",
        durable_harnesses=(),
    )
    monkeypatch.setattr(registry, "_routing_candidate", _planner_available_candidate)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    files = []
    for index in range(13):
        path = tmp_path / f"prod_{index}.py"
        path.write_text(f"VALUE = {index}\n", encoding="utf-8")
        files.append(path.name)
    result = registry.run_delegate(
        task="Review this production runtime change and verify concrete regressions.",
        cwd=tmp_path,
        kind="explore",
        complexity="high",
        files_in_scope=files,
        wait_seconds=3,
    )

    assert result["success"] is True
    assert captured[0].model != "claude-opus-4-6-thinking"
    planner = captured[0].route_decision["planner"]
    assert all(
        "claude-opus-" not in row["route"]
        for row in planner["candidates"]
    )


def test_opus_candidate_is_explicit_only_in_both_antigravity_accounts() -> None:
    first = AntigravityHarness(
        command="agy",
        configured_models=("claude-opus-4-6-thinking",),
    )
    second = AntigravityHarness(
        name="antigravity2",
        command="agy",
        configured_models=("claude-opus-4-6-thinking",),
    )
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[first, second],
        default_harness="antigravity",
        automatic_routing=True,
        planner_mode="active",
        durable_harnesses=(),
    )
    for harness in ("antigravity", "antigravity2"):
        for kind in ("explore", "code"):
            automatic = registry._planner_candidate_models(
                harness=harness, kind=kind, requested_model=None
            )
            assert automatic
            assert all(
                "claude-opus-" not in str(item or "").lower()
                for item in automatic
            )
            assert registry._planner_candidate_models(
                harness=harness,
                kind=kind,
                requested_model="claude-opus-4-6-thinking",
            ) == ("claude-opus-4-6-thinking",)



def test_active_planner_rechecks_selected_route_with_fresh_admission(
    monkeypatch,
    tmp_path: Path,
) -> None:
    local = LocalOpenAIHarness(
        endpoint="http://192.0.2.10:8081",
        default_model="qwen-coder-next",
    )
    antigravity = AntigravityHarness(command="agy")
    registry = ExecutorRegistry(
        codex_command=None,
        harnesses=[local, antigravity],
        default_harness="antigravity",
        automatic_routing=True,
        primary_harness="antigravity",
        fallback_harnesses=("local",),
        planner_mode="active",
        durable_harnesses=(),
    )
    captured = []

    def candidate(*, harness, kind, model, fresh):
        del kind, model
        if harness == "local" and fresh:
            return {
                "harness": harness,
                "eligible": False,
                "reason": "quota_threshold_reached",
                "quota": {"allowed": False, "reason": "quota_threshold_reached"},
                "quota_headroom_percent": 0.0,
                "active_load": 0,
            }
        return {
            "harness": harness,
            "eligible": True,
            "reason": "available",
            "quota": None,
            "quota_headroom_percent": 50.0,
            "active_load": 0,
            "automatic_selections": 0,
            "balance_score": 0.0,
        }

    monkeypatch.setattr(registry, "_routing_candidate", candidate)
    monkeypatch.setattr(executors, "_command_available", lambda _command: True)

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    target = tmp_path / "target.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    result = registry.run_delegate(
        task="Locate this definition in the repository.",
        cwd=tmp_path,
        kind="explore",
        files_in_scope=[target.name],
        wait_seconds=3,
    )

    assert result["success"] is True
    assert captured[0].harness == "antigravity"
    planner = captured[0].route_decision["planner"]
    assert planner["fresh_admission"][0]["route"].startswith("local:")
    assert planner["fresh_admission"][0]["eligible"] is False
    assert planner["selected_route"].startswith("antigravity:")
    assert planner["reason"] == "active_direct_route_after_fresh_admission_fallback"


def test_active_planner_split_fails_closed_when_shared_context_is_present(
    monkeypatch,
    tmp_path: Path,
) -> None:
    files = []
    for index in range(5):
        path = tmp_path / f"f{index}.py"
        path.write_text(f"VALUE = {index}\n", encoding="utf-8")
        files.append(path.name)
    context = tmp_path / "shared.md"
    context.write_text("shared context\n", encoding="utf-8")
    registry = _planner_test_registry(
        planner_mode="active",
        primary_harness="codex",
        split_enabled=True,
        split_max_files=2,
    )
    monkeypatch.setattr(registry, "_routing_candidate", _planner_available_candidate)
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="Locate definitions across these files and report matches.",
        cwd=tmp_path,
        kind="explore",
        files_in_scope=files,
        context_files=[context.name],
        wait_seconds=3,
    )

    assert result["success"] is True
    assert len(captured) == 1
    planner = captured[0].route_decision["planner"]
    assert planner["plan_kind"] == "direct"
    assert planner["split_plan"]["executable"] is False
    assert (
        planner["split_plan"]["reason"]
        == "shared_context_requires_future_context_node"
    )


def test_active_planner_split_fans_out_to_scout_and_synthesizes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    files = []
    for index in range(5):
        path = tmp_path / f"scope_{index}.py"
        path.write_text(f"value_{index} = {index}\n", encoding="utf-8")
        files.append(path.name)
    registry = _planner_test_registry(
        planner_mode="active",
        primary_harness="codex",
        split_enabled=True,
        split_max_files=2,
    )
    monkeypatch.setattr(registry, "_routing_candidate", _planner_available_candidate)
    captured = []

    def fake_start(*, delegate_task):
        captured.append(delegate_task)
        first = str(delegate_task.task_id or delegate_task.delegate_id)
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
            "structured_output": {
                "status": "succeeded",
                "summary": f"checked {first}",
                "files_changed": [],
                "commands_run": [],
                "verification": "read-only",
                "findings": [f"finding:{first}"],
                "blockers": [],
                "recommended_next_action": "none",
            },
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    reserved = []
    direct_reservations = []
    result = registry.run_delegate(
        task="Locate definitions across these files and report concrete matches.",
        cwd=tmp_path,
        kind="explore",
        files_in_scope=files,
        wait_seconds=3,
        before_submit=lambda: direct_reservations.append(1) or None,
        before_group_submit=lambda slots: reserved.append(slots) or None,
    )

    assert result["success"] is True
    assert result["completed"] is True
    assert result["group_id"].startswith("grp-")
    assert reserved == [3]
    assert direct_reservations == []
    assert len(captured) == 3
    assert all(task.harness == "local" for task in captured)
    shard_files = [
        shard["files"] for shard in result["planner_plan"]["split_plan"]["shards"]
    ]
    assert [len(items) for items in shard_files] == [2, 2, 1]
    assert result["planner_plan"]["plan_kind"] == "split"
    assert result["planner_plan"]["plan_id"].startswith("plan-")
    synthesis = result["planner_synthesis"]
    assert synthesis["contract"] == "deterministic_findings_union_v1"
    assert len(synthesis["findings"]) == 3
    assert synthesis["requires_authoritative_review"] is True


def test_context_overflow_returns_replan_directive_without_blind_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    gate = _RoutingQuotaGate(
        {"antigravity": (True, 100), "antigravity2": (True, 90), "codex": (True, 80)}
    )
    registry = _executable_automatic_registry(gate=gate)
    calls: list[str] = []

    def fake_start(*, delegate_task):
        calls.append(delegate_task.harness)
        return {
            "success": False,
            "status": "failed",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
            "error": {
                "code": "antigravity_result_error",
                "message": "maximum context window exceeded",
            },
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="inspect bounded files",
        cwd=tmp_path,
        harness=None,
        wait_seconds=3,
    )

    assert calls == ["antigravity"]
    assert result["failure_analysis"]["failure_type"] == "context_overflow"
    assert result["replan"]["action"] == "escalate_context"
    assert result["replan"]["automatic"] is False


def test_low_confidence_analysis_is_persisted_in_terminal_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    gate = _RoutingQuotaGate(
        {"antigravity": (True, 100), "antigravity2": (True, 90), "codex": (True, 80)}
    )
    registry = _executable_automatic_registry(gate=gate)

    def fake_start(*, delegate_task):
        return {
            "success": True,
            "status": "succeeded",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
            "structured_output": {"confidence": 0.3},
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="bounded review",
        cwd=tmp_path,
        harness=None,
        wait_seconds=3,
    )

    assert result["failure_analysis"]["failure_type"] == "low_confidence"
    assert result["replan"]["action"] == "add_reviewer"
    task = next(iter(registry.scheduler.tasks.values()))
    persisted = json.loads(task.log_paths.metadata.read_text(encoding="utf-8"))
    assert "structured_output" not in persisted
    assert persisted["failure_analysis"]["failure_type"] == "low_confidence"
    assert persisted["replan"]["action"] == "add_reviewer"


def test_automatic_delegate_reports_invalid_model_without_silent_provider_switch(
    monkeypatch,
    tmp_path: Path,
) -> None:
    gate = _RoutingQuotaGate(
        {"antigravity": (True, 100), "antigravity2": (True, 90), "codex": (True, 80)}
    )
    registry = _executable_automatic_registry(gate=gate)
    attempted: list[str] = []

    def fake_start(*, delegate_task):
        attempted.append(delegate_task.harness)
        return {
            "success": False,
            "status": "failed",
            "harness": delegate_task.harness,
            "model": delegate_task.model,
            "error": {
                "code": "antigravity_result_error",
                "message": (
                    'invalid model selection (--model "gemini-3.1-pro" --effort ""): '
                    "model gemini-3.1-pro is not recognized as a known model "
                    "or custom model in settings"
                ),
            },
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", fake_start)
    result = registry.run_delegate(
        task="bounded model compatibility review",
        cwd=tmp_path,
        harness=None,
        wait_seconds=3,
    )
    assert attempted == ["antigravity"]
    assert result["failure_analysis"]["failure_type"] == "configuration_error"
    assert result["replan"]["action"] == "fix_configuration"
    assert result["replan"]["automatic"] is False
    assert result["route_chain"]["fallback_count"] == 0


def test_provider_failure_classifier_retries_transient_but_not_configuration_errors() -> None:
    transient = runtime_provider_failure(
        harness="antigravity2",
        error={
            "code": "antigravity_result_error",
            "message": "API error (attempt 1): UNAVAILABLE (code 503): The service is currently unavailable.",
        },
    )
    configuration = runtime_provider_failure(
        harness="antigravity2",
        error={
            "code": "antigravity_result_error",
            "message": (
                'invalid model selection (--model "gpt-5.6-luna" --effort "medium"): '
                '--effort is not supported for model "gpt-5.6-luna"'
            ),
        },
    )

    assert transient == (True, False, "transient_provider_failure")
    assert configuration == (False, False, None)


def test_run_delegate_accepts_medium_complexity_for_explore(tmp_path: Path) -> None:
    gate = _RoutingQuotaGate({"antigravity2": (True, 100), "codex": (True, 100)})
    registry = _executable_automatic_registry(gate=gate)

    result = registry.run_delegate(
        task="inspect",
        cwd=tmp_path,
        kind="explore",
        complexity="medium",
        wait_seconds=3,
    )

    assert result["success"] is True
    assert result["status"] == "succeeded"
    assert result["harness"] == "antigravity2"
    assert result["routing_mode"] == "complexity"
    assert result["routing_reason"] == "complexity_medium_selected"
    assert result["complexity"] == "medium"
    route_decision = result["route_decision"]
    assert route_decision["legacy_complexity"] == "medium"
    assert len(route_decision["task_shape_hash"]) == 16
    assert route_decision["shadow_route"] is None
    assert route_decision["initial_route"].startswith("antigravity2:")
    assert route_decision["task_shape"]["purpose"] == "discovery"
    assert route_decision["task_shape"]["depth"] == "medium"


def test_automatic_codex_explore_uses_sol_but_explicit_codex_keeps_explore_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    command = "python3 -c \"print('done')\""
    monkeypatch.setattr(
        delegate_harnesses,
        "codex_read_only_sandbox_available",
        lambda _command: True,
    )
    pro = GenericCliHarness(
        name="antigravity2",
        command=command,
        explore_command=command,
    )
    gate = _RoutingQuotaGate({"antigravity2": (False, 0), "codex": (True, 100)})
    registry = ExecutorRegistry(
        codex_command=command,
        harnesses=[pro],
        allow_unsafe_explore_command=True,
        automatic_routing=True,
        primary_harness="antigravity2",
        fallback_harnesses=("codex",),
        quota_admission_gate=gate,
    )

    routed = registry.run_delegate(
        task="independent review",
        cwd=tmp_path,
        kind="explore",
        complexity="high",
        wait_seconds=3,
    )
    explicit = registry.run_delegate(
        task="explicit codex explore",
        cwd=tmp_path,
        kind="explore",
        harness="codex",
        wait_seconds=3,
    )
    routed_alias = registry.run_delegate(
        task="alias codex explore",
        cwd=tmp_path,
        kind="explore",
        harness="",
        model="default",
        complexity="high",
        wait_seconds=3,
    )

    assert routed["success"] is True
    assert routed["harness"] == "codex"
    assert routed["model"] == "gpt-6.1-sol"
    assert routed["routing_mode"] == "complexity"
    assert any(
        harness == "codex" and model == "gpt-6.1-sol"
        for harness, model, _fresh in gate.calls
    )
    assert explicit["success"] is True
    assert explicit["harness"] == "codex"
    assert explicit["model"] == "gpt-5.6-terra"
    assert routed_alias["success"] is True
    assert routed_alias["harness"] == "codex"
    assert routed_alias["model"] == "gpt-6.1-sol"
    assert routed_alias["routing_mode"] == "complexity"


def test_implicit_codex_explore_keeps_terra_when_automatic_routing_is_disabled(
    monkeypatch,
    tmp_path: Path,
) -> None:
    command = "python3 -c \"print('done')\""
    monkeypatch.setattr(
        delegate_harnesses,
        "codex_read_only_sandbox_available",
        lambda _command: True,
    )
    registry = ExecutorRegistry(
        codex_command=command,
        default_harness="codex",
        allow_unsafe_explore_command=True,
        automatic_routing=False,
    )

    result = registry.run_delegate(
        task="default codex explore",
        cwd=tmp_path,
        kind="explore",
        wait_seconds=3,
    )
    batch = registry.run_delegate_batch(
        tasks=[{"task": "default batch explore", "model": "default"}],
        cwd=tmp_path,
        wait_seconds=3,
    )

    assert result["success"] is True
    assert result["model"] == "gpt-5.6-terra"
    assert result["routing_mode"] == "default"
    assert batch["success"] is True
    group = registry.scheduler.get_group(batch["group_id"])
    assert group is not None
    child = registry.scheduler.get_task(group.child_ids[0])
    assert child is not None
    assert child.model == "gpt-5.6-terra"


def test_run_delegate_rejects_medium_complexity_with_explicit_harness(tmp_path: Path) -> None:
    gate = _RoutingQuotaGate({"antigravity2": (True, 100), "codex": (True, 100)})
    registry = _executable_automatic_registry(gate=gate)

    result = registry.run_delegate(
        task="bounded code",
        cwd=tmp_path,
        harness="antigravity2",
        complexity="medium",
        wait_seconds=0,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "complexity_conflicts_with_harness"


def test_run_delegate_rejects_high_complexity_with_model_override(tmp_path: Path) -> None:
    gate = _RoutingQuotaGate({"antigravity2": (True, 100), "codex": (True, 100)})
    registry = _executable_automatic_registry(gate=gate)

    result = registry.run_delegate(
        task="bounded code",
        cwd=tmp_path,
        complexity="high",
        model="gpt-6.1-sol",
        wait_seconds=0,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "complexity_conflicts_with_model"


def test_run_delegate_rejects_unknown_complexity(tmp_path: Path) -> None:
    gate = _RoutingQuotaGate({"antigravity": (True, 100)})
    registry = _executable_automatic_registry(gate=gate)

    result = registry.run_delegate(
        task="bounded code",
        cwd=tmp_path,
        complexity="extreme",
        wait_seconds=0,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "unsupported_complexity"
    assert result["error"]["allowed_complexity_levels"] == ["low", "medium", "high"]


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


def test_automatic_codex_batch_default_alias_uses_sol(
    monkeypatch,
    tmp_path: Path,
) -> None:
    command = "python3 -c \"print('done')\""
    monkeypatch.setattr(
        delegate_harnesses,
        "codex_read_only_sandbox_available",
        lambda _command: True,
    )
    pro = GenericCliHarness(
        name="antigravity2",
        command=command,
        explore_command=command,
    )
    gate = _RoutingQuotaGate({"antigravity2": (False, 0), "codex": (True, 100)})
    registry = ExecutorRegistry(
        codex_command=command,
        harnesses=[pro],
        allow_unsafe_explore_command=True,
        automatic_routing=True,
        primary_harness="antigravity2",
        fallback_harnesses=("codex",),
        quota_admission_gate=gate,
    )

    result = registry.run_delegate_batch(
        tasks=[{"task": "inspect via alias", "model": "default"}],
        cwd=tmp_path,
        wait_seconds=3,
    )

    assert result["success"] is True
    assert result["harness"] == "codex"
    group = registry.scheduler.get_group(result["group_id"])
    assert group is not None
    child = registry.scheduler.get_task(group.child_ids[0])
    assert child is not None
    assert child.model == "gpt-6.1-sol"
    assert any(
        harness == "codex" and model == "gpt-6.1-sol"
        for harness, model, _fresh in gate.calls
    )


def test_automatic_routing_guidance_reports_primary_fallback_policy(monkeypatch) -> None:
    gate = _RoutingQuotaGate({"antigravity": (False, 0), "antigravity2": (True, 90), "codex": (True, 50)})
    registry = _automatic_routing_registry(monkeypatch, gate=gate)

    guidance = registry.routing_guidance()

    assert guidance["automatic_routing"] is True
    assert guidance["policy"]["primary_harness"] == "antigravity"
    assert guidance["policy"]["fallback_harnesses"] == ["antigravity2", "codex"]
    assert guidance["policy"]["fallback_strategy"] == {
        "explore": "quota_headroom_weighted_fair",
        "code": "ordered_escalation",
    }
    assert guidance["planner"]["mode"] == "off"
    assert guidance["planner"]["future_graph_schema"]["durable_dag_execution"] is False
    assert guidance["profiles"]["implementation"]["preferred_harness"] == "antigravity2"
    assert guidance["current_routes"]["code"]["reason"] == "primary_unavailable_ordered_fallback"


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


def test_antigravity_eligibility_watchdog_clears_newer_eligibility_block_after_verified_recovery(
    tmp_path: Path,
) -> None:
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
    failure = {
        "harness": "antigravity",
        "error": {
            "code": "antigravity_result_error",
            "message": "Eligibility check failed: not currently available in your location.",
        },
    }

    registry._note_routing_terminal(failure)
    first_until = float(registry._routing_unavailable_until["antigravity"]["until_epoch"])
    time.sleep(0.01)
    registry._note_routing_terminal(failure)
    second_block = dict(registry._routing_unavailable_until["antigravity"])
    assert float(second_block["until_epoch"]) > first_until
    assert second_block["eligibility_failure"] is True
    assert registry._eligibility_watchdog_status["last_status"] == "suppressed_active_cooldown"

    release.touch()
    for _ in range(200):
        if registry._eligibility_watchdog_status.get("last_status") == "completed_success":
            break
        time.sleep(0.01)

    status = registry._eligibility_watchdog_status
    assert status["last_status"] == "completed_success"
    assert status["runtime_block_cleared"] is True
    assert status["next_route_action"] == "reprobe_antigravity_after_verified_recovery"
    assert "antigravity" not in registry._routing_unavailable_until


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
