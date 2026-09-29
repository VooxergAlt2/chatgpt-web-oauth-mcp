from __future__ import annotations

import importlib

import pytest

from chatgpt_web_oauth_mcp import config


def _restore_config_after_env_test() -> None:
    importlib.reload(config)


def test_command_timeout_defaults_distinguish_local_and_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.delenv("CHATGPT_MCP_COMMAND_TIMEOUT", raising=False)
        patch.delenv("CHATGPT_MCP_OPENAI_FOREGROUND_TIMEOUT", raising=False)
        importlib.reload(config)
        assert config.COMMAND_TIMEOUT == 300
        assert config.OPENAI_FOREGROUND_TIMEOUT == 30

        patch.setenv("CHATGPT_MCP_COMMAND_TIMEOUT", "480")
        patch.setenv("CHATGPT_MCP_OPENAI_FOREGROUND_TIMEOUT", "90")
        importlib.reload(config)
        assert config.COMMAND_TIMEOUT == 480
        assert config.OPENAI_FOREGROUND_TIMEOUT == 90

    _restore_config_after_env_test()


def test_delegate_state_defaults_under_state_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        patch.delenv("CHATGPT_MCP_DELEGATE_STATE_DIR", raising=False)
        patch.setenv("CHATGPT_MCP_STATE_DIR", str(tmp_path / "state"))
        importlib.reload(config)

        assert config.DELEGATE_STATE_DIR == (tmp_path / "state" / "delegates").resolve()
        assert len(config.LEGACY_DELEGATE_STATE_DIRS) == 1

    _restore_config_after_env_test()


def test_response_token_budgets_default_and_read_inherits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.delenv("CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_READ_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_RUN_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES", raising=False)
        importlib.reload(config)

        assert config.TOOL_OUTPUT_TOKEN_BUDGET == 8500
        assert config.READ_TOKEN_BUDGET == 8500
        assert config.RUN_TOKEN_BUDGET == 8500
        assert config.JOB_OUTPUT_TOKEN_BUDGET == 8500
        assert config.RUN_CAPTURE_MAX_BYTES == 1024 * 1024

    _restore_config_after_env_test()


def test_durable_job_safety_defaults_and_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        for name in (
            "CHATGPT_MCP_JOB_DEFAULT_TIMEOUT_SECONDS",
            "CHATGPT_MCP_JOB_MAX_TIMEOUT_SECONDS",
            "CHATGPT_MCP_JOB_LOG_MAX_BYTES",
        ):
            patch.delenv(name, raising=False)
        importlib.reload(config)
        assert config.JOB_DEFAULT_TIMEOUT_SECONDS == 8 * 60 * 60
        assert config.JOB_MAX_TIMEOUT_SECONDS == 24 * 60 * 60
        assert config.JOB_LOG_MAX_BYTES == 64 * 1024 * 1024

        patch.setenv("CHATGPT_MCP_JOB_DEFAULT_TIMEOUT_SECONDS", "120")
        patch.setenv("CHATGPT_MCP_JOB_MAX_TIMEOUT_SECONDS", "600")
        patch.setenv("CHATGPT_MCP_JOB_LOG_MAX_BYTES", "4096")
        importlib.reload(config)
        assert config.JOB_DEFAULT_TIMEOUT_SECONDS == 120
        assert config.JOB_MAX_TIMEOUT_SECONDS == 600
        assert config.JOB_LOG_MAX_BYTES == 4096

        patch.setenv("CHATGPT_MCP_JOB_DEFAULT_TIMEOUT_SECONDS", "601")
        with pytest.raises(ValueError, match="JOB_DEFAULT_TIMEOUT_SECONDS"):
            importlib.reload(config)

    _restore_config_after_env_test()


def test_tool_token_budgets_can_inherit_or_override_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.setenv("CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET", "1200")
        patch.delenv("CHATGPT_MCP_READ_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_RUN_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", raising=False)
        importlib.reload(config)
        assert config.READ_TOKEN_BUDGET == 1200
        assert config.RUN_TOKEN_BUDGET == 1200
        assert config.JOB_OUTPUT_TOKEN_BUDGET == 1200

        patch.setenv("CHATGPT_MCP_READ_TOKEN_BUDGET", "321")
        patch.setenv("CHATGPT_MCP_RUN_TOKEN_BUDGET", "654")
        patch.setenv("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", "777")
        importlib.reload(config)
        assert config.TOOL_OUTPUT_TOKEN_BUDGET == 1200
        assert config.READ_TOKEN_BUDGET == 321
        assert config.RUN_TOKEN_BUDGET == 654
        assert config.JOB_OUTPUT_TOKEN_BUDGET == 777

    _restore_config_after_env_test()


def test_delegate_scheduler_defaults_and_wait_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        for name in [
            "CHATGPT_MCP_ANTIGRAVITY_DEFAULT_MODEL",
            "CHATGPT_MCP_ANTIGRAVITY_DEFAULT_REASONING_EFFORT",
            "CHATGPT_MCP_ANTIGRAVITY2_ENABLED",
            "CHATGPT_MCP_ANTIGRAVITY2_COMMAND",
            "CHATGPT_MCP_ANTIGRAVITY2_HOME",
            "CHATGPT_MCP_QUOTA_ADMISSION_POLICY_PATH",
            "CHATGPT_MCP_DELEGATE_TIMEOUT",
            "CHATGPT_MCP_DELEGATE_WAIT_TIMEOUT",
            "CHATGPT_MCP_DELEGATE_EXPLORE_EXECUTION_TIMEOUT",
            "CHATGPT_MCP_DELEGATE_CODE_EXECUTION_TIMEOUT",
            "CHATGPT_MCP_DELEGATE_EXPLORE_MAX_PER_PROJECT",
            "CHATGPT_MCP_DELEGATE_EXPLORE_MAX_GLOBAL",
            "CHATGPT_MCP_DELEGATE_CODE_MAX_PER_PROJECT",
            "CHATGPT_MCP_DELEGATE_CODE_MAX_GLOBAL",
            "CHATGPT_MCP_DELEGATE_QUEUE_LIMIT_PER_PROJECT",
            "CHATGPT_MCP_DELEGATE_QUEUE_LIMIT_GLOBAL",
            "CHATGPT_MCP_DELEGATE_AUTOMATIC_ROUTING",
            "CHATGPT_MCP_DELEGATE_PRIMARY_HARNESS",
            "CHATGPT_MCP_DELEGATE_FALLBACK_HARNESSES",
            "CHATGPT_MCP_DELEGATE_ROUTING_UNAVAILABLE_COOLDOWN_SECONDS",
        ]:
            patch.delenv(name, raising=False)
        importlib.reload(config)

        assert config.DELEGATE_DEFAULT_HARNESS == "codex"
        assert config.DELEGATE_AUTOMATIC_ROUTING is False
        assert config.DELEGATE_PRIMARY_HARNESS == "codex"
        assert config.DELEGATE_FALLBACK_HARNESSES == ()
        assert config.DELEGATE_ROUTING_UNAVAILABLE_COOLDOWN_SECONDS == 900
        assert config.ANTIGRAVITY_DEFAULT_MODEL == "gemini-3.8-flash"
        assert config.ANTIGRAVITY_DEFAULT_REASONING_EFFORT == "high"
        assert config.ANTIGRAVITY2_ENABLED is False
        assert config.ANTIGRAVITY2_COMMAND == config.ANTIGRAVITY_COMMAND
        assert config.ANTIGRAVITY2_HOME == config.STATE_DIR / "antigravity2-home"
        assert config.QUOTA_ADMISSION_POLICY_PATH == (
            config.STATE_DIR / "delegate-quota-policy.json"
        )
        assert config.CODEX_COMMAND == "codex"
        assert config.PI_COMMAND == "pi"
        assert config.DELEGATE_WAIT_TIMEOUT == 300
        assert config.DELEGATE_EXPLORE_EXECUTION_TIMEOUT == 900
        assert config.DELEGATE_CODE_EXECUTION_TIMEOUT == 3600
        assert config.DELEGATE_EXPLORE_MAX_PER_PROJECT == 4
        assert config.DELEGATE_EXPLORE_MAX_GLOBAL == 8
        assert config.DELEGATE_CODE_MAX_PER_PROJECT == 1
        assert config.DELEGATE_CODE_MAX_GLOBAL == 4
        assert config.DELEGATE_QUEUE_LIMIT_PER_PROJECT == 32
        assert config.DELEGATE_QUEUE_LIMIT_GLOBAL == 128

        patch.setenv("CHATGPT_MCP_DELEGATE_TIMEOUT", "45")
        patch.delenv("CHATGPT_MCP_DELEGATE_WAIT_TIMEOUT", raising=False)
        importlib.reload(config)
        assert config.DELEGATE_WAIT_TIMEOUT == 45

    _restore_config_after_env_test()


def test_antigravity_defaults_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setenv("CHATGPT_MCP_ANTIGRAVITY_DEFAULT_MODEL", "custom-flash")
        patch.setenv("CHATGPT_MCP_ANTIGRAVITY_DEFAULT_REASONING_EFFORT", "medium")
        importlib.reload(config)

        assert config.ANTIGRAVITY_DEFAULT_MODEL == "custom-flash"
        assert config.ANTIGRAVITY_DEFAULT_REASONING_EFFORT == "medium"

    _restore_config_after_env_test()


def test_antigravity_default_effort_rejects_unknown_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.setenv("CHATGPT_MCP_ANTIGRAVITY_DEFAULT_REASONING_EFFORT", "xhigh")
        with pytest.raises(ValueError, match="must be low, medium, or high"):
            importlib.reload(config)

    _restore_config_after_env_test()


def test_codex_cua_approval_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.delenv("CHATGPT_MCP_CODEX_RUNTIME_CUA_APPROVAL_MODE", raising=False)
        patch.delenv("CHATGPT_MCP_CODEX_RUNTIME_CUA_ALLOWED_APPS", raising=False)
        importlib.reload(config)
        assert config.CODEX_RUNTIME_CUA_APPROVAL_MODE == "interactive"
        assert config.CODEX_RUNTIME_CUA_ALLOWED_APPS == ()

        patch.setenv("CHATGPT_MCP_CODEX_RUNTIME_CUA_APPROVAL_MODE", " PROTOTYPE ")
        patch.setenv(
            "CHATGPT_MCP_CODEX_RUNTIME_CUA_ALLOWED_APPS",
            " com.tencent.xinWeChat,com.google.Chrome,com.google.Chrome, ",
        )
        importlib.reload(config)
        assert config.CODEX_RUNTIME_CUA_APPROVAL_MODE == "prototype"
        assert config.CODEX_RUNTIME_CUA_ALLOWED_APPS == (
            "com.tencent.xinWeChat",
            "com.google.Chrome",
        )

        patch.setenv("CHATGPT_MCP_CODEX_RUNTIME_CUA_APPROVAL_MODE", "allow-all")
        with pytest.raises(ValueError, match="Computer Use approval mode must be one of"):
            importlib.reload(config)

    _restore_config_after_env_test()


def test_delegate_harness_commands_and_default_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.setenv("CHATGPT_MCP_CODEX_COMMAND", "/opt/agents/codex")
        patch.setenv("CHATGPT_MCP_PI_COMMAND", "/opt/agents/pi")
        patch.setenv("CHATGPT_MCP_DELEGATE_DEFAULT_HARNESS", " PI ")
        importlib.reload(config)

        assert config.CODEX_COMMAND == "/opt/agents/codex"
        assert config.PI_COMMAND == "/opt/agents/pi"
        assert config.DELEGATE_DEFAULT_HARNESS == "pi"

    _restore_config_after_env_test()


@pytest.mark.parametrize(
    "variable",
    [
        "CHATGPT_MCP_DELEGATE_TIMEOUT",
        "CHATGPT_MCP_DELEGATE_WAIT_TIMEOUT",
        "CHATGPT_MCP_DELEGATE_EXPLORE_EXECUTION_TIMEOUT",
        "CHATGPT_MCP_DELEGATE_CODE_EXECUTION_TIMEOUT",
        "CHATGPT_MCP_DELEGATE_EXPLORE_MAX_PER_PROJECT",
        "CHATGPT_MCP_DELEGATE_EXPLORE_MAX_GLOBAL",
        "CHATGPT_MCP_DELEGATE_CODE_MAX_PER_PROJECT",
        "CHATGPT_MCP_DELEGATE_CODE_MAX_GLOBAL",
        "CHATGPT_MCP_DELEGATE_QUEUE_LIMIT_PER_PROJECT",
        "CHATGPT_MCP_DELEGATE_QUEUE_LIMIT_GLOBAL",
    ],
)
def test_delegate_positive_integer_settings_reject_zero(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    with monkeypatch.context() as patch:
        patch.setenv(variable, "0")
        with pytest.raises(ValueError, match=rf"{variable} must be a positive integer"):
            importlib.reload(config)

    _restore_config_after_env_test()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET", "0"),
        ("CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET", "invalid"),
        ("CHATGPT_MCP_READ_TOKEN_BUDGET", "-1"),
        ("CHATGPT_MCP_READ_TOKEN_BUDGET", "1.5"),
        ("CHATGPT_MCP_RUN_TOKEN_BUDGET", "0"),
        ("CHATGPT_MCP_RUN_TOKEN_BUDGET", "invalid"),
        ("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", "0"),
        ("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", "invalid"),
        ("CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES", "-1"),
        ("CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES", "1.5"),
    ],
)
def test_response_token_budgets_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    with monkeypatch.context() as patch:
        patch.setenv("CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET", "8500")
        patch.delenv("CHATGPT_MCP_READ_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_RUN_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET", raising=False)
        patch.delenv("CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES", raising=False)
        patch.setenv(variable, value)

        with pytest.raises(ValueError, match=rf"{variable} must be a positive integer"):
            importlib.reload(config)

    _restore_config_after_env_test()


def test_ripgrep_binary_defaults_and_can_be_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as patch:
        patch.delenv("CHATGPT_MCP_RIPGREP_BINARY", raising=False)
        importlib.reload(config)
        assert config.RIPGREP_BINARY == "rg"

        patch.setenv("CHATGPT_MCP_RIPGREP_BINARY", "/custom/bin/rg")
        importlib.reload(config)
        assert config.RIPGREP_BINARY == "/custom/bin/rg"

        patch.setenv("CHATGPT_MCP_RIPGREP_BINARY", "   ")
        importlib.reload(config)
        assert config.RIPGREP_BINARY == "rg"

    _restore_config_after_env_test()


def test_ensure_runtime_directories_requires_existing_workspace_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "missing-workspace"
    state_dir = tmp_path / "state"
    delegate_state_dir = state_dir / "delegates"

    monkeypatch.setattr(config, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "DELEGATE_STATE_DIR", delegate_state_dir)

    with pytest.raises(FileNotFoundError):
        config.ensure_runtime_directories()

    assert workspace_root.exists() is False
    assert state_dir.exists() is False
    assert delegate_state_dir.exists() is False


def test_ensure_runtime_directories_creates_state_dir_for_valid_workspace(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_root = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    delegate_state_dir = state_dir / "delegates"
    workspace_root.mkdir()

    monkeypatch.setattr(config, "WORKSPACE_ROOT", workspace_root)
    monkeypatch.setattr(config, "STATE_DIR", state_dir)
    monkeypatch.setattr(config, "DELEGATE_STATE_DIR", delegate_state_dir)

    config.ensure_runtime_directories()

    assert workspace_root.is_dir() is True
    assert state_dir.is_dir() is True
    assert delegate_state_dir.is_dir() is True


def test_delegate_automatic_routing_config(monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setenv("CHATGPT_MCP_DELEGATE_DEFAULT_HARNESS", "codex")
        patch.setenv("CHATGPT_MCP_DELEGATE_AUTOMATIC_ROUTING", "true")
        patch.setenv("CHATGPT_MCP_DELEGATE_PRIMARY_HARNESS", " ANTIGRAVITY ")
        patch.setenv("CHATGPT_MCP_DELEGATE_FALLBACK_HARNESSES", " antigravity2,codex,antigravity2, ")
        patch.setenv("CHATGPT_MCP_DELEGATE_ROUTING_UNAVAILABLE_COOLDOWN_SECONDS", "600")
        importlib.reload(config)

        assert config.DELEGATE_AUTOMATIC_ROUTING is True
        assert config.DELEGATE_PRIMARY_HARNESS == "antigravity"
        assert config.DELEGATE_FALLBACK_HARNESSES == ("antigravity2", "codex")
        assert config.DELEGATE_ROUTING_UNAVAILABLE_COOLDOWN_SECONDS == 600

    _restore_config_after_env_test()
