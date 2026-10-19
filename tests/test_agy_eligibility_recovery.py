"""Regression coverage for the deployed Antigravity Unlocker v1.5 watchdog."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agy-eligibility-recovery.sh"


@pytest.fixture
def harness(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agy = bin_dir / "agy"
    agy.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time
if "--version" in sys.argv:
    print("1.4.2")
    raise SystemExit(0)
sys.stdin.read()
mode = os.environ.get("FAKE_AGY_MODE", "success")
if mode == "timeout":
    time.sleep(3)
if mode == "mutate":
    p = Path(__file__)
    p.write_text(p.read_text() + "\\n# changed while probing\\n")
if mode == "exit_failure":
    print("eligibility check failed")
    raise SystemExit(3)
if mode == "missing":
    print('{"event":"diagnostic","status":"SUCCESS"}')
    raise SystemExit(0)
result = {"status": "SUCCESS", "response": "OK\\n"}
if mode == "provider_error":
    result = {"status": "ERROR", "error": "Eligibility check failed"}
if mode == "false_success":
    result = {"status": "SUCCESS", "error": "Eligibility check failed", "response": "OK"}
if mode == "wrong_answer":
    result["response"] = "Not OK"
print(json.dumps({"event": "result", "result": result}))
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    agy.chmod(0o755)
    unlocker = bin_dir / "antigravity-unlock"
    unlocker.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path
import sys
args = sys.argv[1:]
with Path(os.environ["FAKE_UNLOCKER_LOG"]).open("a") as f:
    f.write(" ".join(args) + "\\n")
mode = os.environ.get("FAKE_UNLOCKER_MODE", "success")
if args == ["patch"] and mode == "patch_failure":
    print("patch failed")
    raise SystemExit(9)
if args == ["patch", "--dry-run"]:
    if mode == "verify_failure":
        raise SystemExit(4)
    if mode == "foreign":
        print("State: foreign (unsupported build)")
        raise SystemExit(0)
    print("State:  patched (fixture)")
else:
    print("Result: Binary is already patched")
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    unlocker.chmod(0o755)
    git = bin_dir / "git"
    git.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path
import sys
args = sys.argv[1:]
with Path(os.environ["FAKE_GIT_LOG"]).open("a") as f:
    f.write(" ".join(args) + "\\n")
if "rev-parse" in args:
    mark = Path(os.environ["FAKE_GIT_MARK"]).exists()
    print("b" * 40 if mark else "a" * 40)
    raise SystemExit(0)
if "pull" in args:
    if os.environ.get("FAKE_GIT_FAIL") == "1":
        print("simulated Git outage", file=sys.stderr)
        raise SystemExit(5)
    if os.environ.get("FAKE_GIT_UPDATED") == "1":
        Path(os.environ["FAKE_GIT_MARK"]).touch()
    print("Already up to date.")
raise SystemExit(0)
""",
        encoding="utf-8",
    )
    git.chmod(0o755)
    pip = bin_dir / "pip"
    pip.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path
import sys
Path(os.environ["FAKE_PIP_LOG"]).write_text(" ".join(sys.argv[1:]))
if os.environ.get("FAKE_PIP_FAIL") == "1":
    print("simulated pip failure", file=sys.stderr)
    raise SystemExit(4)
""",
        encoding="utf-8",
    )
    pip.chmod(0o755)
    repo = tmp_path / "unlocker-repo"
    (repo / ".git").mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "CHATGPT_MCP_AGY_UNLOCKER_BIN": str(unlocker),
            "CHATGPT_MCP_AGY_UNLOCKER_REPO": str(repo),
            "CHATGPT_MCP_AGY_UNLOCKER_PIP": str(pip),
            "CHATGPT_MCP_AGY_BIN": str(agy),
            "CHATGPT_MCP_AGY_RECOVERY_PYTHON": sys.executable,
            "CHATGPT_MCP_AGY_RECOVERY_STATE_DIR": str(tmp_path / "recovery"),
            "CHATGPT_MCP_AGY_RECOVERY_MODEL": "gemini-3.8-flash-low",
            "CHATGPT_MCP_AGY_RECOVERY_PATCH_TIMEOUT_SECONDS": "3",
            "CHATGPT_MCP_AGY_RECOVERY_PROBE_TIMEOUT_SECONDS": "3",
            "CHATGPT_MCP_AGY_RECOVERY_PROBE_WALL_SECONDS": "3",
            "CHATGPT_MCP_AGY_RECOVERY_LOCK_TIMEOUT_SECONDS": "1",
            "CHATGPT_MCP_AGY_RECOVERY_GIT_TIMEOUT_SECONDS": "3",
            "CHATGPT_MCP_AGY_RECOVERY_PIP_TIMEOUT_SECONDS": "3",
            "CHATGPT_MCP_AGY_RECOVERY_UPDATE_ON_RUN": "0",
            "FAKE_UNLOCKER_LOG": str(tmp_path / "unlocker.log"),
            "FAKE_GIT_LOG": str(tmp_path / "git.log"),
            "FAKE_GIT_MARK": str(tmp_path / "git-updated"),
            "FAKE_PIP_LOG": str(tmp_path / "pip.log"),
        }
    )
    return env, tmp_path


def run(harness: tuple[dict[str, str], Path], **values: str) -> subprocess.CompletedProcess[str]:
    env, path = harness
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=path,
        env={**env, **values},
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )


def test_success_requires_patch_and_real_final_response(harness):
    env, path = harness
    result = run(harness)
    assert result.returncode == 0, result.stderr
    assert "AGY_PATCH_VERIFIED" in result.stdout
    assert "AGY_RECOVERY_VERIFIED" in result.stdout
    assert (path / "unlocker.log").read_text().splitlines() == [
        "patch", "patch --dry-run",
    ]
    assert not (path / "git.log").exists()
    assert not (path / "pip.log").exists()
    assert (path / "recovery" / "watchdog.lock").stat().st_mode & 0o777 == 0o600
    assert list((path / "recovery").glob("run.*")) == []


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("exit_failure", "live AGY eligibility probe failed"),
        ("provider_error", "live AGY result verification failed"),
        ("false_success", "live AGY result verification failed"),
        ("missing", "live AGY result verification failed"),
        ("wrong_answer", "live AGY result verification failed"),
        ("mutate", "AGY binary changed during probe"),
        ("timeout", "live AGY eligibility probe failed"),
    ],
)
def test_bad_model_results_never_clear_block(harness, mode, message):
    result = run(harness, FAKE_AGY_MODE=mode, CHATGPT_MCP_AGY_RECOVERY_PROBE_WALL_SECONDS="1")
    assert result.returncode != 0
    assert message in result.stderr
    assert "AGY_RECOVERY_VERIFIED" not in result.stdout


@pytest.mark.parametrize("mode", ["patch_failure", "verify_failure", "foreign"])
def test_invalid_patch_state_never_clears_block(harness, mode):
    result = run(harness, FAKE_UNLOCKER_MODE=mode)
    assert result.returncode != 0
    assert "AGY_RECOVERY_VERIFIED" not in result.stdout
    assert not (harness[1] / "git.log").exists()


def test_missing_unlocker_is_failure(harness):
    env, path = harness
    Path(env["CHATGPT_MCP_AGY_UNLOCKER_BIN"]).unlink()
    result = run(harness)
    assert result.returncode != 0
    assert "missing or not executable" in result.stderr


def test_wrong_agy_path_is_failure(harness):
    result = run(harness, CHATGPT_MCP_AGY_BIN=sys.executable)
    assert result.returncode != 0
    assert "differs from agy on PATH" in result.stderr


def test_invalid_timeout_is_failure(harness):
    result = run(harness, CHATGPT_MCP_AGY_RECOVERY_PATCH_TIMEOUT_SECONDS="xyz")
    assert result.returncode != 0
    assert "invalid positive timeout" in result.stderr


def test_lock_contention_fails_without_patching(harness):
    env, path = harness
    lock_path = path / "recovery" / "watchdog.lock"
    lock_path.parent.mkdir()
    guard = subprocess.Popen(
        ["flock", "-x", str(lock_path), "sleep", "3"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Synchronize with the flock holder rather than relying on startup timing.
        import time
        time.sleep(0.2)
        result = run(harness)
        assert result.returncode != 0
        assert "holds the lock" in result.stderr
        assert not (path / "unlocker.log").exists()
    finally:
        guard.terminate()
        guard.wait(timeout=5)


def test_opt_in_git_failure_fails_closed(harness):
    result = run(harness, CHATGPT_MCP_AGY_RECOVERY_UPDATE_ON_RUN="1", FAKE_GIT_FAIL="1")
    assert result.returncode != 0
    assert "unlocker Git update failed" in result.stderr
    assert not (harness[1] / "unlocker.log").exists()


def test_opt_in_updated_repo_install_failure_fails_closed(harness):
    result = run(
        harness,
        CHATGPT_MCP_AGY_RECOVERY_UPDATE_ON_RUN="1",
        FAKE_GIT_UPDATED="1",
        FAKE_PIP_FAIL="1",
    )
    assert result.returncode != 0
    assert "unlocker installation failed" in result.stderr
    assert not (harness[1] / "unlocker.log").exists()


@pytest.mark.parametrize("changed", ["0", "1"])
def test_opt_in_update_only_installs_changed_source(harness, changed):
    result = run(
        harness,
        CHATGPT_MCP_AGY_RECOVERY_UPDATE_ON_RUN="1",
        FAKE_GIT_UPDATED=changed,
    )
    assert result.returncode == 0, result.stderr
    assert "AGY_RECOVERY_VERIFIED" in result.stdout
    assert (harness[1] / "pip.log").exists() is (changed == "1")
    assert "pull --ff-only origin main" in (harness[1] / "git.log").read_text()
