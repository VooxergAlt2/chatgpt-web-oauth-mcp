from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agy-eligibility-recovery.sh"


def _fixture(
    tmp_path: Path,
    *,
    initial_status: str,
    probe_mode: str,
    patch_sleep: str = "0",
) -> dict[str, str]:
    patcher = tmp_path / "patcher"
    patcher.mkdir()
    manager = patcher / "manager.py"
    manager.write_text(
        """
import os
from pathlib import Path
import sys
import time


def state(_target, _overrides):
    marker = Path(os.environ["FAKE_PATCH_MARKER"])
    state = "patched" if marker.exists() else os.environ["FAKE_INITIAL_PATCH_STATUS"]
    return os.environ["CHATGPT_MCP_AGY_BIN"], state


if __name__ == "__main__":
    log = Path(os.environ["FAKE_MANAGER_LOG"])
    with log.open("a", encoding="utf-8") as handle:
        handle.write(" ".join(sys.argv[1:]) + "\\n")

    action = sys.argv[1]
    if action == "patch":
        time.sleep(float(os.environ.get("FAKE_PATCH_SLEEP", "0")))
        Path(os.environ["FAKE_PATCH_MARKER"]).touch()
        print("agy-manager - patch")
        print("  [ok] CLI patched")
        raise SystemExit(0)
    raise SystemExit(9)
""".lstrip(),
        encoding="utf-8",
    )

    agy = tmp_path / "agy"
    agy.write_text(
        """#!/usr/bin/env sh
cat >/dev/null
printf '%s\\n' "$*" >> "$FAKE_AGY_LOG"
case "$FAKE_AGY_MODE" in
  success)
    printf '%s\\n' '{"event":"result","result":{"status":"SUCCESS","response":"OK"}}'
    exit 0
    ;;
  misleading)
    printf '%s\\n' '{"event":"diagnostic","status":"SUCCESS"}'
    printf '%s\\n' '{"event":"result","result":{"status":"ERROR","error":"provider failed"}}'
    exit 0
    ;;
  *)
    printf '%s\\n' '{"event":"result","result":{"status":"ERROR","error":"Eligibility check failed: not currently available in your location."}}'
    exit 1
    ;;
esac
""",
        encoding="utf-8",
    )
    agy.chmod(0o755)

    patch_marker = tmp_path / "patched.marker"
    if initial_status == "patched":
        patch_marker.touch()

    env = os.environ.copy()
    env.update(
        {
            "CHATGPT_MCP_AGY_PATCHER_DIR": str(patcher),
            "CHATGPT_MCP_AGY_BIN": str(agy),
            "CHATGPT_MCP_AGY_PATCHER_PYTHON": sys.executable,
            "CHATGPT_MCP_AGY_RECOVERY_PATCH_TIMEOUT_SECONDS": "1",
            "CHATGPT_MCP_AGY_RECOVERY_PROBE_TIMEOUT_SECONDS": "2",
            "CHATGPT_MCP_AGY_RECOVERY_PROBE_WALL_SECONDS": "5",
            "FAKE_MANAGER_LOG": str(tmp_path / "manager.log"),
            "FAKE_AGY_LOG": str(tmp_path / "agy.log"),
            "FAKE_PATCH_MARKER": str(patch_marker),
            "FAKE_INITIAL_PATCH_STATUS": initial_status,
            "FAKE_AGY_MODE": probe_mode,
            "FAKE_PATCH_SLEEP": patch_sleep,
        }
    )
    return env


def _run(
    tmp_path: Path,
    *,
    initial_status: str,
    probe_mode: str,
    patch_sleep: str = "0",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=_fixture(
            tmp_path,
            initial_status=initial_status,
            probe_mode=probe_mode,
            patch_sleep=patch_sleep,
        ),
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def test_recovery_patches_unpatched_cli_then_requires_final_result_success(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, initial_status="unpatched", probe_mode="success")

    assert result.returncode == 0
    assert "patch_state_before=unpatched" in result.stdout
    assert "patch_state=patched" in result.stdout
    assert "AGY_RECOVERY_VERIFIED" in result.stdout
    manager_calls = (tmp_path / "manager.log").read_text(encoding="utf-8").splitlines()
    assert manager_calls == [f"patch cli --path-cli {tmp_path / 'agy'}"]
    assert "--output-format stream-json" in (tmp_path / "agy.log").read_text(encoding="utf-8")


def test_recovery_skips_binary_write_when_cli_is_already_patched(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, initial_status="patched", probe_mode="success")

    assert result.returncode == 0
    assert "patch_state_before=patched" in result.stdout
    assert "CLI already patched; skipping binary write" in result.stdout
    assert "AGY_RECOVERY_VERIFIED" in result.stdout
    assert (tmp_path / "manager.log").exists() is False
    assert "--output-format stream-json" in (tmp_path / "agy.log").read_text(encoding="utf-8")


def test_recovery_fails_closed_when_patch_status_is_unknown(tmp_path: Path) -> None:
    result = _run(tmp_path, initial_status="unknown", probe_mode="success")

    assert result.returncode == 20
    assert "patch_state_before=unknown" in result.stdout
    assert "unsupported patch state: unknown" in result.stderr
    assert (tmp_path / "manager.log").exists() is False
    assert (tmp_path / "agy.log").exists() is False


def test_recovery_fails_closed_when_patch_command_hangs(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        initial_status="unpatched",
        probe_mode="success",
        patch_sleep="2",
    )

    assert result.returncode == 14
    assert "patch command failed with exit code 124" in result.stderr
    assert (tmp_path / "agy.log").exists() is False


def test_recovery_fails_closed_when_live_probe_still_hits_location_gate(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, initial_status="unpatched", probe_mode="location")

    assert result.returncode == 21
    assert "patch_state=patched" in result.stdout
    assert "live AGY probe failed with exit code 1" in result.stderr
    assert "Eligibility check failed" in result.stdout


def test_recovery_uses_final_result_event_not_incidental_success_text(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, initial_status="unpatched", probe_mode="misleading")

    assert result.returncode == 22
    assert "final AGY result status is 'ERROR', not SUCCESS" in result.stderr
