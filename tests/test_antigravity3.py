"""Third AGY account: isolated authentication, quotas, capacity, and watchdog."""
from __future__ import annotations

import time
import os
import subprocess
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.delegate_harnesses import AntigravityHarness
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.quota_admission import quota_bucket_keys


FAILURE = {
    "harness": "antigravity3",
    "error": {
        "code": "antigravity_result_error",
        "message": "Eligibility check failed: not currently available in your location.",
    },
}


def test_agy3_interactive_wrapper_isolates_home_and_xdg(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_agy = fake_bin / "agy"
    fake_agy.write_text(
        '#!/bin/sh\nprintf "%s|%s|%s|%s|%s\\n" "$HOME" "$XDG_CONFIG_HOME" '
        '"$XDG_CACHE_HOME" "$XDG_DATA_HOME" "$*"\n',
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)
    home = tmp_path / "third-account"
    script = Path(__file__).resolve().parents[1] / "scripts" / "agy3-account.sh"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "CHATGPT_MCP_ANTIGRAVITY3_HOME": str(home),
    }
    result = subprocess.run(
        ["bash", str(script), "--version"],
        env=env, capture_output=True, text=True, check=False, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        f"{home}|{home}/.config|{home}/.cache|{home}/.local/share|--version"
    )
    assert home.stat().st_mode & 0o777 == 0o700


def test_third_account_credentials_are_not_inherited(tmp_path: Path):
    home = tmp_path / "antigravity3-home"
    home.mkdir(mode=0o700)
    third = AntigravityHarness(
        command="agy", name="antigravity3", home_dir=home,
    )
    info = third.info()
    assert info["account_authenticated"] is False
    assert info["available"] is False
    assert info["availability_reason"] == "account_authentication_required"
    assert third._env_overrides()["HOME"] == str(home)
    credential = home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("test-only-credential", encoding="utf-8")
    assert third.info()["account_authenticated"] is True
    assert third.info()["available"] is True


def test_third_account_quota_buckets_are_disjoint():
    first = quota_bucket_keys("antigravity", "gemini-3.8-flash-low")
    second = quota_bucket_keys("antigravity2", "gemini-3.8-flash-low")
    third = quota_bucket_keys("antigravity3", "gemini-3.8-flash-low")
    assert not (set(first) & set(second))
    assert not (set(first) & set(third))
    assert not (set(second) & set(third))
    assert third == ("antigravity3_gemini_5h", "antigravity3_gemini_weekly")
    assert quota_bucket_keys("antigravity3", "claude-opus-4-6-thinking") == (
        "antigravity3_claude_gpt_5h", "antigravity3_claude_gpt_weekly",
    )


def test_agy2_watchdog_isolated_from_primary_and_agy3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    second_home = tmp_path / "second"
    second_home.mkdir(mode=0o700)
    primary_script = tmp_path / "primary.sh"
    marker = tmp_path / "markers.txt"
    primary_script.write_text(
        "#!/bin/sh\nprintf '%s\\n' primary >> '$AGY_WATCHDOG_MARKER'\n",
        encoding="utf-8",
    )
    second_script = tmp_path / "second.sh"
    second_script.write_text(
        '#!/bin/sh\nprintf "%s|%s\\n" "$CHATGPT_MCP_WATCHDOG_HARNESS" '
        '"$CHATGPT_MCP_AGY_RECOVERY_PROBE_HOME" >> "$AGY_WATCHDOG_MARKER"\n',
        encoding="utf-8",
    )
    primary_script.chmod(0o755)
    second_script.chmod(0o755)
    monkeypatch.setenv("AGY_WATCHDOG_MARKER", str(marker))
    registry = ExecutorRegistry(
        antigravity_eligibility_watchdog_script=primary_script,
        antigravity2_eligibility_watchdog_script=second_script,
        antigravity2_home=second_home,
        delegate_state_root=tmp_path / "delegates",
        routing_unavailable_cooldown_seconds=30,
    )
    failure = {**FAILURE, "harness": "antigravity2"}
    registry._note_routing_terminal(failure)
    for _ in range(150):
        if registry._eligibility_watchdog_status_by_harness["antigravity2"].get(
            "last_status"
        ) == "completed_success":
            break
        time.sleep(0.01)
    assert registry._eligibility_watchdog_status_by_harness[
        "antigravity2"
    ]["last_status"] == "completed_success"
    assert registry._eligibility_watchdog_status["last_status"] == "not_triggered"
    assert registry._eligibility_watchdog_status_by_harness[
        "antigravity3"
    ]["last_status"] == "not_triggered"
    assert marker.read_text(encoding="utf-8").splitlines() == [
        f"antigravity2|{second_home}"
    ]
    assert (
        tmp_path / "delegates" / "watchdogs" / "antigravity2-eligibility.log"
    ).is_file()
    registry._note_routing_terminal(failure)
    assert registry._eligibility_watchdog_status_by_harness[
        "antigravity2"
    ]["last_status"] == "suppressed_active_cooldown"


def test_agy3_watchdog_isolated_and_suppressed_per_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "third"
    home.mkdir(mode=0o700)
    script = tmp_path / "watchdog.sh"
    marker = tmp_path / "watchdog-markers.txt"
    script.write_text(
        "#!/bin/sh\n"
        'printf "%s|%s|%s|%s\\n" "$CHATGPT_MCP_WATCHDOG_HARNESS" '
        '"$CHATGPT_MCP_AGY_RECOVERY_PROBE_HOME" '
        '"$CHATGPT_MCP_WATCHDOG_ERROR_CODE" '
        '"${CHATGPT_MCP_AUTH_TOKEN-unset}" >> "$AGY_TEST_MARKER"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("AGY_TEST_MARKER", str(marker))
    monkeypatch.setenv("CHATGPT_MCP_AUTH_TOKEN", "never-leak")
    registry = ExecutorRegistry(
        antigravity3_eligibility_watchdog_script=script,
        antigravity3_home=home,
        delegate_state_root=tmp_path / "delegates",
        routing_unavailable_cooldown_seconds=30,
    )
    registry._note_routing_terminal(FAILURE)
    for _ in range(150):
        if registry._eligibility_watchdog_status_by_harness["antigravity3"].get(
            "last_status"
        ) == "completed_success":
            break
        time.sleep(0.01)
    agy3 = registry._eligibility_watchdog_status_by_harness["antigravity3"]
    assert agy3["last_status"] == "completed_success"
    assert agy3["exit_code"] == 0
    assert agy3["runtime_block_cleared"] is True
    assert agy3["next_route_action"] == "reprobe_antigravity3_on_next_automatic_delegate"
    assert registry._eligibility_watchdog_status["last_status"] == "not_triggered"
    assert "antigravity3" not in registry._routing_unavailable_until
    assert marker.read_text(encoding="utf-8").splitlines() == [
        f"antigravity3|{home}|antigravity_result_error|unset"
    ]
    log = tmp_path / "delegates" / "watchdogs" / "antigravity3-eligibility.log"
    assert log.is_file()
    registry._note_routing_terminal(FAILURE)
    assert registry._eligibility_watchdog_status_by_harness[
        "antigravity3"
    ]["last_status"] == "suppressed_active_cooldown"
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1


def test_agy3_failed_watchdog_keeps_only_third_runtime_block(tmp_path: Path):
    script = tmp_path / "failure.sh"
    script.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    script.chmod(0o755)
    home = tmp_path / "third"
    home.mkdir(mode=0o700)
    registry = ExecutorRegistry(
        antigravity3_eligibility_watchdog_script=script,
        antigravity3_home=home,
        delegate_state_root=tmp_path / "delegates",
    )
    registry._note_routing_terminal(FAILURE)
    for _ in range(150):
        if registry._eligibility_watchdog_status_by_harness["antigravity3"].get(
            "last_status"
        ) == "completed_failed":
            break
        time.sleep(0.01)
    assert registry._eligibility_watchdog_status_by_harness[
        "antigravity3"
    ]["last_status"] == "completed_failed"
    assert "antigravity3" in registry._routing_unavailable_until
    assert registry._eligibility_watchdog_status["last_status"] == "not_triggered"


def test_agy3_missing_watchdog_does_not_trigger_primary(tmp_path: Path):
    registry = ExecutorRegistry(
        antigravity3_eligibility_watchdog_script=tmp_path / "missing.sh",
        antigravity3_home=tmp_path,
        delegate_state_root=tmp_path / "delegates",
    )
    registry._note_routing_terminal(FAILURE)
    assert registry._eligibility_watchdog_status_by_harness["antigravity3"][
        "last_status"
    ] == "script_missing"
    assert registry._eligibility_watchdog_status["last_status"] == "not_triggered"
