from __future__ import annotations

import hashlib
import json
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
    seed_last_known_good: bool = False,
    current_version: str = "1.2.14",
    cached_version: str = "1.2.13",
    probe_sleep: str = "0",
    mutate_after_probe: bool = False,
) -> dict[str, str]:
    patcher = tmp_path / "patcher"
    patcher.mkdir()
    manager = patcher / "manager.py"
    manager.write_text(
        """
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import time


class SignatureNotFound(LookupError):
    pass


class SignatureAmbiguous(LookupError):
    pass


def executable_info(path):
    data = Path(path).read_bytes()
    return ((0, len(data)),), "x64"


@contextmanager
def mapped(path):
    yield Path(path).read_bytes()


class _Gate:
    def resolve(self, data, _ranges=None, _arch=None):
        marker = Path(os.environ["FAKE_PATCH_MARKER"])
        if marker.exists() or b"LKG_PATCHED" in data:
            return "patched", 0, self
        state = os.environ["FAKE_INITIAL_PATCH_STATUS"]
        if state == "patched":
            return "patched", 0, self
        if state == "unpatched":
            return "unpatched", 0, self
        if state == "signature_not_found":
            raise SignatureNotFound("gate signature not found (unsupported version?)")
        if state == "signature_ambiguous":
            raise SignatureAmbiguous("multiple architecture gate signatures matched")
        raise ValueError(f"invalid fake state: {state}")


CLI_GATE = _Gate()


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
    agy_template = """#!/usr/bin/env sh
if [ "${1:-}" = "--version" ]; then
  printf '%s\\n' '__VERSION__'
  exit 0
fi
cat >/dev/null
printf '%s\\n' "$*" >> "$FAKE_AGY_LOG"
sleep "$FAKE_AGY_SLEEP"
case "$FAKE_AGY_MODE" in
  success)
    if [ "$FAKE_AGY_MUTATE_AFTER_PROBE" = "1" ]; then
      printf '\\n# mutated-after-probe\\n' >> "$0"
    fi
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
"""
    agy.write_text(
        agy_template.replace("__VERSION__", current_version),
        encoding="utf-8",
    )
    agy.chmod(0o755)

    patch_marker = tmp_path / "patched.marker"
    if initial_status == "patched":
        patch_marker.touch()

    recovery_state = tmp_path / "recovery-state"
    if seed_last_known_good:
        lkg_dir = recovery_state / "last-known-good"
        lkg_dir.mkdir(parents=True)
        lkg = lkg_dir / "agy"
        lkg.write_text(
            agy_template.replace("__VERSION__", cached_version).replace(
                "#!/usr/bin/env sh",
                "#!/usr/bin/env sh\n# LKG_PATCHED",
            ),
            encoding="utf-8",
        )
        lkg.chmod(0o755)
        digest = hashlib.sha256(lkg.read_bytes()).hexdigest()
        (lkg_dir / "metadata.json").write_text(
            json.dumps({"version": cached_version, "sha256": digest}) + "\n",
            encoding="utf-8",
        )

    env = os.environ.copy()
    env.update(
        {
            "CHATGPT_MCP_AGY_PATCHER_DIR": str(patcher),
            "CHATGPT_MCP_AGY_BIN": str(agy),
            "CHATGPT_MCP_AGY_PATCHER_PYTHON": sys.executable,
            "CHATGPT_MCP_AGY_RECOVERY_PATCH_TIMEOUT_SECONDS": "1",
            "CHATGPT_MCP_AGY_RECOVERY_PROBE_TIMEOUT_SECONDS": "2",
            "CHATGPT_MCP_AGY_RECOVERY_PROBE_WALL_SECONDS": "5",
            "CHATGPT_MCP_AGY_RECOVERY_STATE_DIR": str(recovery_state),
            "FAKE_MANAGER_LOG": str(tmp_path / "manager.log"),
            "FAKE_AGY_LOG": str(tmp_path / "agy.log"),
            "FAKE_PATCH_MARKER": str(patch_marker),
            "FAKE_INITIAL_PATCH_STATUS": initial_status,
            "FAKE_AGY_MODE": probe_mode,
            "FAKE_PATCH_SLEEP": patch_sleep,
            "FAKE_AGY_SLEEP": probe_sleep,
            "FAKE_AGY_MUTATE_AFTER_PROBE": "1" if mutate_after_probe else "0",
        }
    )
    return env


def _run(
    tmp_path: Path,
    *,
    initial_status: str,
    probe_mode: str,
    patch_sleep: str = "0",
    seed_last_known_good: bool = False,
    remove_binary: bool = False,
    current_version: str = "1.2.14",
    cached_version: str = "1.2.13",
    probe_sleep: str = "0",
    mutate_after_probe: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = _fixture(
        tmp_path,
        initial_status=initial_status,
        probe_mode=probe_mode,
        patch_sleep=patch_sleep,
        seed_last_known_good=seed_last_known_good,
        current_version=current_version,
        cached_version=cached_version,
        probe_sleep=probe_sleep,
        mutate_after_probe=mutate_after_probe,
    )
    if remove_binary:
        Path(env["CHATGPT_MCP_AGY_BIN"]).unlink()
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
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
    agy_args = (tmp_path / "agy.log").read_text(encoding="utf-8").split()
    assert ["--output-format", "stream-json"] == agy_args[
        agy_args.index("--output-format"):agy_args.index("--output-format") + 2
    ]
    assert "--disable-slash-commands" in agy_args
    assert "--mode" not in agy_args
    assert "--sandbox" not in agy_args
    cache_dir = tmp_path / "recovery-state" / "last-known-good"
    metadata = json.loads((cache_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["version"] == "1.2.14"
    assert metadata["sha256"] == hashlib.sha256((cache_dir / "agy").read_bytes()).hexdigest()


def test_recovery_skips_binary_write_when_cli_is_already_patched(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, initial_status="patched", probe_mode="success")

    assert result.returncode == 0
    assert "patch_state_before=patched" in result.stdout
    assert "CLI already patched; skipping binary write" in result.stdout
    assert "AGY_RECOVERY_VERIFIED" in result.stdout
    assert (tmp_path / "manager.log").exists() is False
    agy_args = (tmp_path / "agy.log").read_text(encoding="utf-8").split()
    assert ["--output-format", "stream-json"] == agy_args[
        agy_args.index("--output-format"):agy_args.index("--output-format") + 2
    ]
    assert "--disable-slash-commands" in agy_args
    assert "--mode" not in agy_args
    assert "--sandbox" not in agy_args


def test_recovery_fails_closed_when_patch_status_is_unknown(tmp_path: Path) -> None:
    result = _run(tmp_path, initial_status="unknown", probe_mode="success")

    assert result.returncode == 20
    assert "patch_state_error:ValueError:invalid fake state: unknown" in result.stderr
    assert "could not read patch state (exit code 44)" in result.stderr
    assert (tmp_path / "manager.log").exists() is False
    assert (tmp_path / "agy.log").exists() is False


def test_recovery_rolls_back_unsupported_cli_to_last_known_good_and_retries(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        initial_status="signature_not_found",
        probe_mode="success",
        seed_last_known_good=True,
        current_version="1.2.14",
        cached_version="1.2.13",
    )

    assert result.returncode == 0
    assert "signature_not_found:" in result.stderr
    assert "attempting last-known-good rollback reason=unsupported_signature" in result.stdout
    assert "AGY_ROLLBACK_APPLIED from=1.2.14 to=1.2.13" in result.stdout
    assert "patch_state_before=patched" in result.stdout
    assert "AGY_RECOVERY_VERIFIED" in result.stdout
    assert "LKG_PATCHED" in (tmp_path / "agy").read_text(encoding="utf-8")
    assert (tmp_path / "manager.log").exists() is False


def test_recovery_restores_missing_cli_from_last_known_good(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        initial_status="signature_not_found",
        probe_mode="success",
        seed_last_known_good=True,
        remove_binary=True,
    )

    assert result.returncode == 0
    assert "AGY binary missing or not executable" in result.stderr
    assert "AGY_ROLLBACK_APPLIED from=missing to=1.2.13" in result.stdout
    assert "AGY_RECOVERY_VERIFIED" in result.stdout


def test_recovery_does_not_rollback_ambiguous_signature(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        initial_status="signature_ambiguous",
        probe_mode="success",
        seed_last_known_good=True,
    )

    assert result.returncode == 20
    assert "signature_ambiguous:" in result.stderr
    assert "could not read patch state (exit code 43)" in result.stderr
    assert "AGY_ROLLBACK_APPLIED" not in result.stdout
    completed = subprocess.run(
        [str(tmp_path / "agy"), "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.stdout.strip() == "1.2.14"


def test_recovery_fails_when_unsupported_cli_has_no_last_known_good(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        initial_status="signature_not_found",
        probe_mode="success",
    )

    assert result.returncode == 23
    assert "last-known-good cache is missing" in result.stderr
    assert "unsupported AGY version and no usable previous version" in result.stderr


def test_recovery_rejects_corrupted_last_known_good(tmp_path: Path) -> None:
    env = _fixture(
        tmp_path,
        initial_status="signature_not_found",
        probe_mode="success",
        seed_last_known_good=True,
    )
    cached = tmp_path / "recovery-state" / "last-known-good" / "agy"
    cached.write_bytes(cached.read_bytes() + b"\n# corrupted\n")

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 23
    assert "last-known-good checksum mismatch" in result.stderr
    assert "AGY_ROLLBACK_APPLIED" not in result.stdout


def test_recovery_fails_if_agy_changes_after_successful_probe(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        initial_status="patched",
        probe_mode="success",
        mutate_after_probe=True,
    )

    assert result.returncode == 26
    assert "AGY binary changed after live probe" in result.stderr
    assert "could not refresh verified last-known-good cache" in result.stderr
    assert "AGY_RECOVERY_VERIFIED" not in result.stdout


def test_recovery_serializes_concurrent_invocations(tmp_path: Path) -> None:
    env = _fixture(
        tmp_path,
        initial_status="patched",
        probe_mode="success",
        probe_sleep="3",
    )
    first = subprocess.Popen(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert first.stdout is not None
        first_line = first.stdout.readline()
        assert "[agy-recovery] patcher=" in first_line
        second = subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        first_stdout_tail, first_stderr = first.communicate(timeout=10)
        first_stdout = first_line + first_stdout_tail
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)

    assert second.returncode == 24
    assert "another recovery invocation holds the state lock" in second.stderr
    assert first.returncode == 0, (first_stdout, first_stderr)
    assert "AGY_RECOVERY_VERIFIED" in first_stdout


def test_recovery_fails_if_verified_cache_cannot_be_persisted(tmp_path: Path) -> None:
    env = _fixture(
        tmp_path,
        initial_status="patched",
        probe_mode="success",
    )
    state_dir = Path(env["CHATGPT_MCP_AGY_RECOVERY_STATE_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "last-known-good").write_text("not-a-directory", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 26
    assert "could not refresh verified last-known-good cache" in result.stderr
    assert "AGY_RECOVERY_VERIFIED" not in result.stdout


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
