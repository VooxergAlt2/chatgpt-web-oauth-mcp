#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib import request as urlrequest

REQUEST_PATH = Path("/home/vooxerg/.local/state/chatgpt-web-oauth-mcp/gpu-mode.request")
STATE_PATH = Path("/run/dem-gpu-mode/state.json")
LOCK_PATH = Path("/run/lock/dem-gpu-mode.lock")
MODEL_PATH = Path("/mnt/rag-reference/Models/Devstral-Small-2-24B-Instruct-2512/mistralai_Devstral-Small-2-24B-Instruct-2512-Q5_K_M.gguf")
MODEL_SIZE = 16_764_513_152
RAG_UNIT = "rag-gpu-inference.service"
DEVSTRAL_UNIT = "llama-devstral-gip.service"
LEGACY_QWEN_UNIT = "llama-qwen35-gip.service"
RAG_HEALTH = "http://127.0.0.1:8091/health"
DEVSTRAL_HEALTH = "http://127.0.0.1:8083/health"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _run_systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        text=True,
        capture_output=True,
        timeout=45,
        check=check,
    )


def _active(unit: str) -> bool:
    return _run_systemctl("is-active", "--quiet", unit).returncode == 0


def _mode() -> str:
    rag = _active(RAG_UNIT)
    devstral = _active(DEVSTRAL_UNIT)
    if devstral and not rag:
        return "devstral"
    if rag and not devstral:
        return "rag"
    if rag and devstral:
        return "conflict"
    return "idle"


def _wait_unit(unit: str, active: bool, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _active(unit) is active:
            return True
        time.sleep(1)
    return _active(unit) is active


def _wait_http(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlrequest.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def _write_state(*, target: str, status: str, error: str | None = None) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "target": target,
        "status": status,
        "mode": _mode(),
        "rag_active": _active(RAG_UNIT),
        "devstral_active": _active(DEVSTRAL_UNIT),
        "error": error,
        "timestamp": _now(),
    }
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, STATE_PATH)


def _model_ready() -> bool:
    try:
        return MODEL_PATH.is_file() and MODEL_PATH.stat().st_size == MODEL_SIZE
    except OSError:
        return False


def _rollback_to_rag(target: str, error: str) -> int:
    _run_systemctl("stop", DEVSTRAL_UNIT)
    _run_systemctl("start", RAG_UNIT)
    _wait_unit(RAG_UNIT, True, 60)
    _wait_http(RAG_HEALTH, 300)
    _write_state(target=target, status="rolled_back", error=error)
    return 1


def _switch_to_devstral() -> int:
    target = "devstral"
    if not _model_ready():
        _write_state(target=target, status="rejected", error="devstral_model_missing_or_wrong_size")
        return 2
    if _mode() == "devstral" and _wait_http(DEVSTRAL_HEALTH, 3):
        _write_state(target=target, status="ready")
        return 0

    _write_state(target=target, status="switching")
    _run_systemctl("stop", LEGACY_QWEN_UNIT)
    _run_systemctl("stop", RAG_UNIT)
    if not _wait_unit(RAG_UNIT, False, 60):
        return _rollback_to_rag(target, "rag_gpu_did_not_stop")

    started = _run_systemctl("start", DEVSTRAL_UNIT)
    if started.returncode != 0:
        return _rollback_to_rag(target, "devstral_start_failed: " + started.stderr.strip()[:300])
    if not _wait_unit(DEVSTRAL_UNIT, True, 60):
        return _rollback_to_rag(target, "devstral_unit_not_active")
    if not _wait_http(DEVSTRAL_HEALTH, 300):
        return _rollback_to_rag(target, "devstral_health_timeout")

    _write_state(target=target, status="ready")
    return 0


def _switch_to_rag() -> int:
    target = "rag"
    previous = _mode()
    if previous == "rag" and _wait_http(RAG_HEALTH, 3):
        _write_state(target=target, status="ready")
        return 0

    _write_state(target=target, status="switching")
    _run_systemctl("stop", DEVSTRAL_UNIT)
    if not _wait_unit(DEVSTRAL_UNIT, False, 60):
        _write_state(target=target, status="failed", error="devstral_did_not_stop")
        return 1

    started = _run_systemctl("start", RAG_UNIT)
    if started.returncode == 0 and _wait_unit(RAG_UNIT, True, 60) and _wait_http(RAG_HEALTH, 300):
        _write_state(target=target, status="ready")
        return 0

    error = "rag_health_timeout_or_start_failed"
    if previous == "devstral" and _model_ready():
        _run_systemctl("stop", RAG_UNIT)
        _run_systemctl("start", DEVSTRAL_UNIT)
        _wait_unit(DEVSTRAL_UNIT, True, 60)
        _wait_http(DEVSTRAL_HEALTH, 300)
        _write_state(target=target, status="rolled_back", error=error)
    else:
        _write_state(target=target, status="failed", error=error)
    return 1


def apply_request() -> int:
    try:
        target = REQUEST_PATH.read_text(encoding="utf-8").strip().lower()
    except OSError as exc:
        _write_state(target="unknown", status="failed", error=f"request_read_failed: {exc}")
        return 2
    if target == "devstral":
        return _switch_to_devstral()
    if target == "rag":
        return _switch_to_rag()
    _write_state(target=target or "empty", status="rejected", error="unsupported_target")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("apply", "status"), nargs="?", default="apply")
    args = parser.parse_args()

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.action == "status":
            print(json.dumps({"mode": _mode(), "model_ready": _model_ready(), "timestamp": _now()}))
            return 0
        return apply_request()


if __name__ == "__main__":
    sys.exit(main())
