#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from urllib import error as urlerror
from urllib import request as urlrequest

STATE_DIR = Path.home() / ".local/state/chatgpt-web-oauth-mcp"
REQUEST_PATH = STATE_DIR / "gpu-mode.request"
STATE_PATH = STATE_DIR / "gpu-mode.state.json"
LOCK_PATH = STATE_DIR / "gpu-mode.lock"
MODEL_PATH = Path("/mnt/rag-reference/Models/Devstral-Small-2-24B-Instruct-2512/mistralai_Devstral-Small-2-24B-Instruct-2512-Q5_K_M.gguf")
MODEL_SIZE = 16_764_513_152
DEVSTRAL_UNIT = "llama-devstral-gip.service"
RAG_HEALTH = "http://127.0.0.1:8091/health"
RAG_READY = "http://127.0.0.1:8091/ready"
RAG_SUSPEND = "http://127.0.0.1:8091/admin/runtime/suspend"
RAG_RESUME = "http://127.0.0.1:8091/admin/runtime/resume"
DEVSTRAL_HEALTH = "http://127.0.0.1:8083/health"
SOURCE_RUN_DB = Path(
    os.environ.get(
        "RAG_SOURCE_SYNC_RUN_DB_PATH",
        "/opt/rag-safe-wrapper-r2/storage/db/rag_clean_core_v1.source-sync-runs.sqlite3",
    )
)
SOURCE_RUN_INFLIGHT_STATES = ("queued", "waiting_for_slot", "running")
RAG_BACKGROUND_SERVICES = (
    "rag-project-autoindex.service",
    "rag-night-ocr.service",
    "rag-cloud-ocr.service",
    "rag-speakr-periodic-resync.service",
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _run_user_systemctl(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        text=True,
        capture_output=True,
        timeout=45,
        check=check,
    )


def _user_unit_active(unit: str) -> bool:
    return _run_user_systemctl("is-active", "--quiet", unit).returncode == 0


def _system_unit_active(unit: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", unit],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _wait_user_unit(unit: str, active: bool, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _user_unit_active(unit) is active:
            return True
        time.sleep(1)
    return _user_unit_active(unit) is active


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


def _read_json(url: str, *, timeout: float = 3.0) -> dict[str, object] | None:
    req = urlrequest.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                return None
            raw = response.read(128 * 1024)
    except (urlerror.URLError, TimeoutError, OSError):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _post_json(url: str, *, timeout: float = 90.0) -> dict[str, object]:
    req = urlrequest.Request(
        url,
        data=b"",
        headers={"Accept": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=timeout) as response:
            raw = response.read(128 * 1024)
            status = response.status
    except urlerror.HTTPError as exc:
        raw = exc.read(128 * 1024)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        code = ""
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict):
                code = str(detail.get("error") or "")
            code = code or str(payload.get("error") or "")
        raise RuntimeError(f"rag_control_http_{exc.code}:{code or 'unknown'}") from exc
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"rag_control_unreachable:{type(exc).__name__}") from exc
    if status != 200:
        raise RuntimeError(f"rag_control_http_{status}")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("rag_control_invalid_json") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("rag_control_invalid_response")
    return payload


def _rag_health() -> dict[str, object] | None:
    payload = _read_json(RAG_HEALTH)
    if (
        payload is None
        or payload.get("ok") is not True
        or payload.get("component") != "rag-gpu-inference"
    ):
        return None
    return payload


def _rag_state() -> str | None:
    payload = _rag_health()
    state = payload.get("state") if payload else None
    return str(state) if isinstance(state, str) else None


def _maintenance_available() -> bool:
    payload = _rag_health()
    return bool(payload and payload.get("maintenance_control_enabled") is True)


def _wait_rag_state(expected: set[str], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _rag_state() in expected:
            return True
        time.sleep(1)
    return _rag_state() in expected


def _mode() -> str:
    rag_state = _rag_state()
    devstral = _user_unit_active(DEVSTRAL_UNIT)
    rag_suspended = rag_state in {"suspending", "suspended"}
    rag_live = rag_state is not None and not rag_suspended
    if devstral and rag_suspended:
        return "devstral"
    if devstral and rag_live:
        return "conflict"
    if not devstral and rag_live:
        return "rag"
    return "idle"


def _inflight_source_runs() -> list[dict[str, object]]:
    placeholders = ",".join("?" for _ in SOURCE_RUN_INFLIGHT_STATES)
    try:
        con = sqlite3.connect(
            f"file:{SOURCE_RUN_DB}?mode=ro",
            uri=True,
            timeout=3.0,
        )
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "select run_id,scope,source_id,state,active_slot "
                "from source_sync_runs "
                f"where state in ({placeholders}) "
                "order by created_at asc, rowid asc",
                SOURCE_RUN_INFLIGHT_STATES,
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise RuntimeError("source_run_ledger_unavailable") from exc
    return [
        {
            "run_id": str(row["run_id"]),
            "scope": str(row["scope"]),
            "source_id": str(row["source_id"]),
            "state": str(row["state"]),
            "active_slot": (
                int(row["active_slot"]) if row["active_slot"] is not None else None
            ),
        }
        for row in rows
    ]


def _active_rag_background_services() -> list[str]:
    return [unit for unit in RAG_BACKGROUND_SERVICES if _system_unit_active(unit)]


def _write_state(
    *,
    target: str,
    status: str,
    error: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "target": target,
        "status": status,
        "mode": _mode(),
        "rag_state": _rag_state(),
        "devstral_active": _user_unit_active(DEVSTRAL_UNIT),
        "error": error,
        "details": details or {},
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
    _run_user_systemctl("stop", DEVSTRAL_UNIT)
    try:
        _post_json(RAG_RESUME, timeout=300)
    except RuntimeError as exc:
        _write_state(
            target=target,
            status="failed",
            error=f"{error}; rag_rollback_failed:{exc}",
        )
        return 1
    if not _wait_http(RAG_READY, 300):
        _write_state(
            target=target,
            status="failed",
            error=f"{error}; rag_rollback_ready_timeout",
        )
        return 1
    _write_state(
        target=target,
        status="rolled_back",
        error=error,
    )
    return 1


def _switch_to_devstral() -> int:
    target = "devstral"
    if not _model_ready():
        _write_state(target=target, status="rejected", error="devstral_model_missing_or_wrong_size")
        return 2
    if _mode() == "devstral" and _wait_http(DEVSTRAL_HEALTH, 3):
        _write_state(target=target, status="ready")
        return 0
    if not _maintenance_available():
        _write_state(
            target=target,
            status="rejected",
            error="rag_maintenance_control_unavailable",
        )
        return 2

    try:
        inflight_runs = _inflight_source_runs()
    except RuntimeError as exc:
        _write_state(target=target, status="rejected", error=str(exc))
        return 2
    active_services = _active_rag_background_services()
    if inflight_runs or active_services:
        _write_state(
            target=target,
            status="rejected",
            error="rag_background_work_inflight",
            details={
                "source_runs": inflight_runs[:12],
                "active_services": active_services,
            },
        )
        return 2

    _write_state(target=target, status="switching")
    try:
        _post_json(RAG_SUSPEND, timeout=90)
    except RuntimeError as exc:
        _write_state(target=target, status="failed", error=f"rag_suspend_failed:{exc}")
        return 1
    if not _wait_rag_state({"suspended"}, 15):
        return _rollback_to_rag(target, "rag_suspend_state_timeout")

    try:
        raced_runs = _inflight_source_runs()
    except RuntimeError as exc:
        return _rollback_to_rag(target, f"source_run_ledger_unavailable_after_suspend:{exc}")
    raced_services = _active_rag_background_services()
    if raced_runs or raced_services:
        try:
            _post_json(RAG_RESUME, timeout=300)
        except RuntimeError as exc:
            _write_state(
                target=target,
                status="failed",
                error=f"rag_background_work_raced_suspend; rag_resume_failed:{exc}",
                details={
                    "source_runs": raced_runs[:12],
                    "active_services": raced_services,
                },
            )
            return 1
        if not _wait_http(RAG_READY, 300):
            _write_state(
                target=target,
                status="failed",
                error="rag_background_work_raced_suspend; rag_ready_timeout",
                details={
                    "source_runs": raced_runs[:12],
                    "active_services": raced_services,
                },
            )
            return 1
        _write_state(
            target=target,
            status="rejected",
            error="rag_background_work_raced_suspend",
            details={
                "source_runs": raced_runs[:12],
                "active_services": raced_services,
            },
        )
        return 2

    started = _run_user_systemctl("start", DEVSTRAL_UNIT)
    if started.returncode != 0:
        return _rollback_to_rag(target, "devstral_start_failed: " + started.stderr.strip()[:300])
    if not _wait_user_unit(DEVSTRAL_UNIT, True, 60):
        return _rollback_to_rag(target, "devstral_unit_not_active")
    if not _wait_http(DEVSTRAL_HEALTH, 300):
        return _rollback_to_rag(target, "devstral_health_timeout")

    _write_state(target=target, status="ready")
    return 0


def _switch_to_rag() -> int:
    target = "rag"
    previous = _mode()
    if previous == "rag" and _wait_http(RAG_READY, 5):
        _write_state(target=target, status="ready")
        return 0

    _write_state(target=target, status="switching")
    stopped = _run_user_systemctl("stop", DEVSTRAL_UNIT)
    if stopped.returncode != 0 or not _wait_user_unit(DEVSTRAL_UNIT, False, 60):
        _write_state(target=target, status="failed", error="devstral_did_not_stop")
        return 1

    try:
        _post_json(RAG_RESUME, timeout=300)
    except RuntimeError as exc:
        error = f"rag_resume_failed:{exc}"
    else:
        error = "rag_ready_timeout"
        if _wait_http(RAG_READY, 300):
            _write_state(target=target, status="ready")
            return 0

    if previous == "devstral" and _model_ready():
        try:
            _post_json(RAG_SUSPEND, timeout=90)
        except RuntimeError:
            pass
        restarted = _run_user_systemctl("start", DEVSTRAL_UNIT)
        if (
            restarted.returncode == 0
            and _wait_user_unit(DEVSTRAL_UNIT, True, 60)
            and _wait_http(DEVSTRAL_HEALTH, 300)
        ):
            _write_state(target=target, status="rolled_back", error=error)
            return 1
        _write_state(
            target=target,
            status="failed",
            error=f"{error}; devstral_rollback_failed",
        )
        return 1

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
