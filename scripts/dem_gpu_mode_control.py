#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib import error as urlerror
from urllib import request as urlrequest

STATE_DIR = Path.home() / ".local/state/chatgpt-web-oauth-mcp"
REQUEST_PATH = STATE_DIR / "gpu-mode.request"
STATE_PATH = STATE_DIR / "gpu-mode.state.json"
LOCK_PATH = STATE_DIR / "gpu-mode.lock"
MODEL_PATH = Path("/mnt/rag-reference/Models/Qwen3.8-27B/Qwen3.8-27B-Q4_K_M.gguf")
MODEL_SIZE = 17_442_399_968
REVIEWER_UNIT = "llama-local-reviewer.service"
RAG_HEALTH = "http://127.0.0.1:8091/health"
RAG_READY = "http://127.0.0.1:8091/ready"
RAG_SUSPEND = "http://127.0.0.1:8091/admin/runtime/suspend"
RAG_RESUME = "http://127.0.0.1:8091/admin/runtime/resume"
REVIEWER_HEALTH = "http://127.0.0.1:8083/health"
SOURCE_RUN_DB = Path(
    os.environ.get(
        "RAG_SOURCE_SYNC_RUN_DB_PATH",
        "/opt/rag-safe-wrapper-r2/storage/db/rag_clean_core_v1.source-sync-runs.sqlite3",
    )
)
SOURCE_RUN_INFLIGHT_STATES = ("queued", "waiting_for_slot", "running")
SPEAKR_JOB_DB = Path(
    os.environ.get(
        "DEM_GPU_SPEAKR_JOB_DB",
        "/home/vooxerg/speakr-data/instance/transcriptions.db",
    )
)
SPEAKR_INFLIGHT_STATES = ("queued", "processing")
SPEAKR_GPU_LEASE_PATH = Path(
    os.environ.get(
        "DEM_GPU_SPEAKR_LEASE_PATH",
        "/home/vooxerg/.local/state/chatgpt-web-oauth-mcp/speakr-gpu-lease.lock",
    )
)
REVIEWER_SLOTS_URL = "http://127.0.0.1:8083/slots"
SPEAKR_REVIEWER_IDLE_WAIT_SECONDS = 240.0
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
    reviewer = _user_unit_active(REVIEWER_UNIT)
    rag_suspended = rag_state in {"suspending", "suspended"}
    rag_live = rag_state is not None and not rag_suspended
    if reviewer and rag_suspended:
        return "reviewer"
    if reviewer and rag_live:
        return "conflict"
    if not reviewer and rag_live:
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


def _inflight_speakr_jobs() -> list[dict[str, object]]:
    """Read Speakr's canonical queue before starting the 20 GiB reviewer.

    Missing Speakr installations do not prohibit local development. An
    existing but unreadable ledger fails closed rather than interrupting ASR.
    """
    if not SPEAKR_JOB_DB.exists():
        return []
    try:
        with sqlite3.connect(
            SPEAKR_JOB_DB.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=3.0,
        ) as con:
            con.execute("PRAGMA query_only=ON")
            rows = con.execute(
                "SELECT id,recording_id,status FROM processing_job "
                "WHERE status IN (?,?) ORDER BY id LIMIT 20",
                SPEAKR_INFLIGHT_STATES,
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        raise RuntimeError("speakr_job_ledger_unavailable") from exc
    return [
        {"job_id": int(row[0]), "recording_id": int(row[1]), "status": str(row[2])}
        for row in rows
    ]


def _reviewer_idle() -> bool:
    """Fail closed unless the reviewer reports every slot idle."""
    req = urlrequest.Request(REVIEWER_SLOTS_URL, method="GET")
    try:
        with urlrequest.urlopen(req, timeout=3) as response:
            if response.status != 200:
                return False
            data = json.loads(response.read(128 * 1024).decode("utf-8"))
    except (urlerror.URLError, TimeoutError, OSError, ValueError):
        return False
    if not isinstance(data, list) or not data:
        return False
    for slot in data:
        if not isinstance(slot, dict):
            return False
        if slot.get("is_processing") is True:
            return False
        state = slot.get("state")
        if state not in (None, 0, "idle"):
            return False
        if "is_processing" not in slot and state is None:
            return False
    return True


@contextmanager
def _hold_reviewer_admission(timeout_seconds: float):
    """Drain and lock ALL existing local2 slots until reviewer shutdown.

    The canonical local2 harness keeps its slot-{id}.lock flock for the full
    request. Taking every one of these exclusive locks means all admitted
    local reviewer requests have finished AND no new one can be admitted
    while we stop the reviewer and resume RAG. No new process or scheduler.
    """
    if not _user_unit_active(REVIEWER_UNIT):
        yield True
        return
    props = _read_json("http://127.0.0.1:8083/props")
    raw = props.get("total_slots") if props else None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 64:
        yield False
        return
    key = hashlib.sha256(b"http://127.0.0.1:8083").hexdigest()[:16]
    folder = (
        Path(tempfile.gettempdir())
        / "chatgpt-web-oauth-mcp-local-slots"
        / key
    )
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink() or folder.stat().st_uid != os.geteuid():
        yield False
        return
    acquired: dict[int, int] = {}
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline:
            if not _user_unit_active(REVIEWER_UNIT):
                yield True
                return
            for slot_id in range(raw):
                if slot_id in acquired:
                    continue
                fd = os.open(
                    folder / f"slot-{slot_id}.lock",
                    os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                    0o600,
                )
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    continue
                acquired[slot_id] = fd
            if len(acquired) == raw and _reviewer_idle():
                yield True
                return
            time.sleep(1)
        yield False
    finally:
        for fd in acquired.values():
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _ensure_asr_mode() -> int:
    """Use the canonical mode switch to free VRAM for a requested ASR job."""
    if _mode() == "rag" and _wait_http(RAG_READY, 3):
        return 0
    with _hold_reviewer_admission(SPEAKR_REVIEWER_IDLE_WAIT_SECONDS) as admitted:
        if not admitted:
            _write_state(target="rag", status="rejected", error="reviewer_busy_for_speakr")
            return 2
        # The reviewer slot lease stays held across the entire transition.
        result = _switch_to_rag()
    if result == 0:
        # Keep the path-watched desired state consistent with the transition.
        tmp = REQUEST_PATH.with_suffix(".asr.tmp")
        tmp.write_text("rag\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, REQUEST_PATH)
    return result


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
        "reviewer_active": _user_unit_active(REVIEWER_UNIT),
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
    _run_user_systemctl("stop", REVIEWER_UNIT)
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


def _switch_to_reviewer() -> int:
    """Reserve admission atomically before changing the GPU's owner.

    The Speakr proxy holds exactly the same file lock from the beginning of
    each forced-GPU ASR request until the worker exits. Without this lease, a
    job can arrive *after* a ledger check while the reviewer is loading.
    """
    try:
        SPEAKR_GPU_LEASE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            SPEAKR_GPU_LEASE_PATH,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError:
        _write_state(target="reviewer", status="rejected", error="speakr_gpu_lease_unavailable")
        return 2
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid() or st.st_mode & 0o077:
            _write_state(target="reviewer", status="rejected", error="speakr_gpu_lease_unsafe")
            return 2
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _write_state(target="reviewer", status="rejected", error="speakr_gpu_lease_busy")
            return 2
        return _switch_to_reviewer_locked()
    finally:
        os.close(fd)


def _switch_to_reviewer_locked() -> int:
    target = "reviewer"
    if not _model_ready():
        _write_state(target=target, status="rejected", error="reviewer_model_missing_or_wrong_size")
        return 2
    if _mode() == "reviewer" and _wait_http(REVIEWER_HEALTH, 3):
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
        speakr_jobs = _inflight_speakr_jobs()
    except RuntimeError as exc:
        _write_state(target=target, status="rejected", error=str(exc))
        return 2
    if speakr_jobs:
        _write_state(
            target=target, status="rejected", error="speakr_job_inflight",
            details={"speakr_jobs": speakr_jobs[:12]},
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
    try:
        raced_speakr_jobs = _inflight_speakr_jobs()
    except RuntimeError as exc:
        return _rollback_to_rag(target, str(exc))
    if raced_speakr_jobs:
        return _rollback_to_rag(target, "speakr_job_raced_suspend")
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

    started = _run_user_systemctl("start", REVIEWER_UNIT)
    if started.returncode != 0:
        return _rollback_to_rag(target, "reviewer_start_failed: " + started.stderr.strip()[:300])
    if not _wait_user_unit(REVIEWER_UNIT, True, 60):
        return _rollback_to_rag(target, "reviewer_unit_not_active")
    if not _wait_http(REVIEWER_HEALTH, 300):
        return _rollback_to_rag(target, "reviewer_health_timeout")

    try:
        raced_speakr_jobs = _inflight_speakr_jobs()
    except RuntimeError as exc:
        return _rollback_to_rag(target, str(exc))
    if raced_speakr_jobs:
        return _rollback_to_rag(target, "speakr_job_raced_reviewer_start")

    _write_state(target=target, status="ready")
    return 0


def _switch_to_rag() -> int:
    target = "rag"
    previous = _mode()
    if previous == "rag" and _wait_http(RAG_READY, 5):
        _write_state(target=target, status="ready")
        return 0

    _write_state(target=target, status="switching")
    stopped = _run_user_systemctl("stop", REVIEWER_UNIT)
    if stopped.returncode != 0 or not _wait_user_unit(REVIEWER_UNIT, False, 60):
        _write_state(target=target, status="failed", error="reviewer_did_not_stop")
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

    if previous == "reviewer" and _model_ready():
        reviewer_rollback_error: str | None = None
        try:
            _post_json(RAG_SUSPEND, timeout=90)
        except RuntimeError as exc:
            reviewer_rollback_error = f"rag_suspend_failed:{exc}"
        else:
            if not _wait_rag_state({"suspended"}, 15):
                reviewer_rollback_error = "rag_suspend_state_timeout"

        if reviewer_rollback_error is None:
            restarted = _run_user_systemctl("start", REVIEWER_UNIT)
            if restarted.returncode != 0:
                reviewer_rollback_error = (
                    "reviewer_start_failed:" + restarted.stderr.strip()[:300]
                )
            elif not _wait_user_unit(REVIEWER_UNIT, True, 60):
                reviewer_rollback_error = "reviewer_unit_not_active"
            elif not _wait_http(REVIEWER_HEALTH, 300):
                reviewer_rollback_error = "reviewer_health_timeout"
            else:
                _write_state(target=target, status="rolled_back", error=error)
                return 1

            stopped = _run_user_systemctl("stop", REVIEWER_UNIT)
            if stopped.returncode != 0 or not _wait_user_unit(REVIEWER_UNIT, False, 60):
                _write_state(
                    target=target,
                    status="failed",
                    error=(
                        f"{error}; reviewer_rollback_failed:{reviewer_rollback_error}; "
                        "reviewer_cleanup_failed"
                    ),
                )
                return 1

        recovery_error: str | None = None
        try:
            _post_json(RAG_RESUME, timeout=300)
        except RuntimeError as exc:
            recovery_error = f"rag_final_recovery_failed:{exc}"
        else:
            if _wait_http(RAG_READY, 300):
                _write_state(
                    target=target,
                    status="rolled_back",
                    error=(
                        f"{error}; reviewer_rollback_failed:{reviewer_rollback_error}; "
                        "recovered_to_rag"
                    ),
                )
                return 1
            recovery_error = "rag_final_recovery_ready_timeout"
        _write_state(
            target=target,
            status="failed",
            error=(
                f"{error}; reviewer_rollback_failed:{reviewer_rollback_error}; "
                f"{recovery_error}"
            ),
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
    if target == "reviewer":
        return _switch_to_reviewer()
    if target == "rag":
        return _switch_to_rag()
    _write_state(target=target or "empty", status="rejected", error="unsupported_target")
    return 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=("apply", "status", "ensure-asr"),
        nargs="?", default="apply",
    )
    args = parser.parse_args()

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.action == "status":
            print(json.dumps({"mode": _mode(), "model_ready": _model_ready(), "timestamp": _now()}))
            return 0
        if args.action == "ensure-asr":
            return _ensure_asr_mode()
        return apply_request()


if __name__ == "__main__":
    sys.exit(main())
