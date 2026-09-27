from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import time
from typing import Callable, Iterator, Mapping

try:  # pragma: no cover - Windows fallback is exercised only on Windows.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


JOB_METADATA_SCHEMA_VERSION = 1
JOB_METADATA_FILENAME = "metadata.json"
ACTIVE_JOB_INDEX_DIRECTORY_NAME = "active-jobs"
ACTIVE_JOB_INDEX_READY_FILENAME = ".initialized-v1"
DEFAULT_JOB_TIMEOUT_SECONDS = 8 * 60 * 60
MAX_JOB_TIMEOUT_SECONDS = 24 * 60 * 60
DEFAULT_JOB_LOG_MAX_BYTES = 64 * 1024 * 1024
JOB_SAFETY_POLL_SECONDS = 0.05
TERMINAL_JOB_STATUSES = frozenset(
    {"succeeded", "failed", "killed", "interrupted", "timed_out"}
)


class _SupervisorShutdownRequested(RuntimeError):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"received signal {signum} during supervisor startup or execution")


class _DetachedBootstrapShutdownRequested(RuntimeError):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"received signal {signum} during detached bootstrap")


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Private directory must not be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise ValueError("Private directory must be a directory.")
        if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():  # pragma: no cover
            raise ValueError("Private directory must not be a symbolic link.")
        try:
            os.fchmod(fd, 0o700)
        except OSError:
            pass
    finally:
        os.close(fd)


def ensure_private_file(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Private file must not be a symbolic link.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    fd = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("Private file must be a regular file.")
        if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():  # pragma: no cover
            raise ValueError("Private file must not be a symbolic link.")
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
    finally:
        os.close(fd)


def active_job_index_dir(job_dir: Path) -> Path:
    return job_dir.parent.parent / ACTIVE_JOB_INDEX_DIRECTORY_NAME


def active_job_marker_path(job_dir: Path) -> Path:
    return active_job_index_dir(job_dir) / job_dir.name


def mark_job_active(job_dir: Path) -> None:
    active_dir = active_job_index_dir(job_dir)
    if active_dir.is_symlink():
        raise ValueError("The active durable jobs index must not be a symbolic link.")
    ensure_private_directory(active_dir)
    marker = active_job_marker_path(job_dir)
    if marker.is_symlink():
        raise ValueError("The active durable job marker must not be a symbolic link.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(marker, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)


def unmark_job_active(job_dir: Path) -> None:
    marker = active_job_marker_path(job_dir)
    try:
        marker.unlink()
    except FileNotFoundError:
        return


def _combined_log_bytes(stdout_log: Path, stderr_log: Path) -> int:
    return stdout_log.stat().st_size + stderr_log.stat().st_size


@contextmanager
def _metadata_lock(job_dir: Path) -> Iterator[None]:
    lock_path = job_dir / ".metadata.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


def _read_job_metadata_unlocked(job_dir: Path) -> dict[str, object] | None:
    metadata_path = job_dir / JOB_METADATA_FILENAME
    try:
        raw = metadata_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(f"Job metadata must be a JSON object: {metadata_path}")
    return value


def read_job_metadata(job_dir: Path) -> dict[str, object] | None:
    """Read one atomically-written metadata snapshot.

    Atomic replacement means a reader normally sees either the old or new JSON.
    A short retry also tolerates unusual filesystems and initial publication.
    """

    last_error: OSError | ValueError | json.JSONDecodeError | None = None
    for attempt in range(3):
        try:
            return _read_job_metadata_unlocked(job_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.005)
    assert last_error is not None
    raise last_error


def _write_job_metadata_unlocked(job_dir: Path, metadata: Mapping[str, object]) -> None:
    ensure_private_directory(job_dir)
    metadata_path = job_dir / JOB_METADATA_FILENAME
    temporary_path = job_dir / f".{JOB_METADATA_FILENAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    encoded = (json.dumps(dict(metadata), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    fd = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, metadata_path)
        try:
            metadata_path.chmod(0o600)
        except OSError:
            pass
        try:
            directory_fd = os.open(job_dir, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def write_job_metadata(job_dir: Path, metadata: Mapping[str, object]) -> dict[str, object]:
    snapshot = dict(metadata)
    with _metadata_lock(job_dir):
        _write_job_metadata_unlocked(job_dir, snapshot)
    return snapshot


def mutate_job_metadata(
    job_dir: Path,
    mutate: Callable[[dict[str, object]], dict[str, object]],
) -> dict[str, object]:
    with _metadata_lock(job_dir):
        current = _read_job_metadata_unlocked(job_dir)
        if current is None:
            raise FileNotFoundError(job_dir / JOB_METADATA_FILENAME)
        updated = mutate(dict(current))
        _write_job_metadata_unlocked(job_dir, updated)
        return updated


def update_job_metadata(job_dir: Path, **updates: object) -> dict[str, object]:
    return mutate_job_metadata(job_dir, lambda current: {**current, **updates})


def process_exists(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def _linux_process_identity(pid: int) -> str | None:
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        raw = stat_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    closing_parenthesis = raw.rfind(")")
    if closing_parenthesis < 0:
        return None
    fields_after_command = raw[closing_parenthesis + 2 :].split()
    if len(fields_after_command) <= 19:
        return None
    return f"linux-start-ticks:{fields_after_command[19]}"


def _ps_process_identity(pid: int) -> str | None:
    ps_binary = "/bin/ps" if Path("/bin/ps").exists() else "ps"
    try:
        completed = subprocess.run(
            [ps_binary, "-p", str(pid), "-o", "lstart="],
            text=True,
            capture_output=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    started = " ".join(completed.stdout.split())
    if completed.returncode != 0 or not started:
        return None
    return f"ps-lstart:{started}"


def process_identity(pid: int | None) -> str | None:
    if not isinstance(pid, int) or pid <= 0:
        return None
    identity = _linux_process_identity(pid)
    if identity is not None:
        return identity
    if not process_exists(pid):
        return None
    return _ps_process_identity(pid)


def process_identity_matches(pid: int | None, expected: object) -> bool | None:
    """Return True for the recorded process, False when it is gone/reused, None if unverifiable."""

    if not isinstance(pid, int) or pid <= 0:
        return False
    if not process_exists(pid):
        return False
    current = process_identity(pid)
    if current is None or not isinstance(expected, str) or not expected:
        return None
    return current == expected


def process_cpu_seconds(pid: int | None) -> float | None:
    """Return cumulative user+system CPU seconds for one process when observable."""

    if not isinstance(pid, int) or pid <= 0:
        return None

    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        raw = stat_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        raw = ""
    if raw:
        closing_parenthesis = raw.rfind(")")
        if closing_parenthesis >= 0:
            fields_after_command = raw[closing_parenthesis + 2 :].split()
            # fields_after_command[0] is /proc stat field 3 (state), so utime/stime
            # fields 14/15 are indexes 11/12 here.
            if len(fields_after_command) > 12:
                try:
                    ticks = int(fields_after_command[11]) + int(fields_after_command[12])
                    clock_ticks = int(os.sysconf("SC_CLK_TCK"))
                except (ValueError, OSError, TypeError):
                    pass
                else:
                    if clock_ticks > 0:
                        return ticks / clock_ticks

    if not process_exists(pid):
        return None
    ps_binary = "/bin/ps" if Path("/bin/ps").exists() else "ps"
    try:
        completed = subprocess.run(
            [ps_binary, "-p", str(pid), "-o", "time="],
            text=True,
            capture_output=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return _parse_ps_cpu_time(completed.stdout.strip())


def _parse_ps_cpu_time(value: str) -> float | None:
    """Parse portable ps TIME values such as MM:SS, HH:MM:SS, or DD-HH:MM:SS."""

    normalized = value.strip()
    if not normalized:
        return None
    days = 0
    if "-" in normalized:
        day_text, normalized = normalized.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return None
    parts = normalized.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
        elif len(parts) == 2:
            hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
        else:
            return None
    except ValueError:
        return None
    if min(days, hours, minutes, seconds) < 0 or minutes >= 60 or seconds >= 60:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def process_group_exists(process_group_id: int | None) -> bool:
    if os.name != "posix" or not hasattr(os, "killpg"):
        return False
    if not isinstance(process_group_id, int) or process_group_id <= 0:
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def snapshot_process_group(process_group_id: int | None) -> dict[int, str | None]:
    """Capture best-effort identities for every current member of one process group."""

    if os.name != "posix" or not isinstance(process_group_id, int) or process_group_id <= 0:
        return {}
    ps_binary = "/bin/ps" if Path("/bin/ps").exists() else "ps"
    try:
        completed = subprocess.run(
            [ps_binary, "-axo", "pid=,pgid="],
            text=True,
            capture_output=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if completed.returncode != 0:
        return {}
    members: dict[int, str | None] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, pgid = (int(field) for field in fields)
        except ValueError:
            continue
        if pgid != process_group_id:
            continue
        try:
            if os.getpgid(pid) != process_group_id:
                continue
        except OSError:
            continue
        members[pid] = process_identity(pid)
    return members


def process_group_matches_snapshot(
    process_group_id: int,
    expected_members: Mapping[int, str | None],
) -> bool:
    """Verify every surviving group member existed with the same identity before TERM."""

    current_members = snapshot_process_group(process_group_id)
    if not current_members:
        return False
    for pid, current_identity in current_members.items():
        expected_identity = expected_members.get(pid)
        if expected_identity is None or current_identity != expected_identity:
            return False
    return True


def _capture_identity(pid: int) -> str | None:
    deadline = time.monotonic() + 0.5
    identity = process_identity(pid)
    while identity is None and process_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
        identity = process_identity(pid)
    return identity


def _append_supervisor_error(stderr_log: Path, message: str) -> None:
    try:
        ensure_private_file(stderr_log)
        with stderr_log.open("a", encoding="utf-8") as handle:
            handle.write(f"job supervisor: {message}\n")
    except OSError:
        pass


def _terminate_supervised_process(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float = 0.5,
) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix" and hasattr(os, "killpg"):
            process_group_id = os.getpgid(process.pid)
            expected_group = snapshot_process_group(process_group_id)
            os.killpg(process_group_id, signal.SIGTERM)
            deadline = time.monotonic() + max(0.0, grace_seconds)
            while (
                process_group_exists(process_group_id)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            if (
                process_group_exists(process_group_id)
                and process_group_matches_snapshot(process_group_id, expected_group)
            ):
                os.killpg(
                    process_group_id,
                    getattr(signal, "SIGKILL", signal.SIGTERM),
                )
        else:  # pragma: no cover - durable jobs are POSIX-oriented.
            process.kill()
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass


def supervise_job(
    *,
    job_dir: Path,
    command: str,
    cwd: Path,
    timeout_seconds: float | None = None,
    log_max_bytes: int | None = None,
) -> int:
    stdout_log = job_dir / "stdout.log"
    stderr_log = job_dir / "stderr.log"
    ensure_private_directory(job_dir)
    ensure_private_file(stdout_log)
    ensure_private_file(stderr_log)

    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    log_limit_exceeded = False
    shutdown_signals = [signal.SIGTERM]
    if hasattr(signal, "SIGINT"):
        shutdown_signals.append(signal.SIGINT)
    previous_handlers: dict[int, object] = {}

    def request_shutdown(signum: int, _frame: object) -> None:
        signal.signal(signum, signal.SIG_IGN)
        raise _SupervisorShutdownRequested(signum)

    for signum in shutdown_signals:
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_shutdown)

    try:
        supervisor_pid = os.getpid()
        update_job_metadata(
            job_dir,
            supervisor_pid=supervisor_pid,
            supervisor_identity=_capture_identity(supervisor_pid),
            updated_at=time.time(),
        )
        with stdout_log.open("ab", buffering=0) as stdout_handle, stderr_log.open("ab", buffering=0) as stderr_handle:
            popen_kwargs: dict[str, object] = {"close_fds": True}
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            elif os.name == "nt":  # pragma: no cover
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                command,
                cwd=str(cwd),
                shell=True,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                **popen_kwargs,
            )
            process_group_id: int | None = None
            if os.name == "posix":
                try:
                    process_group_id = os.getpgid(process.pid)
                except OSError:
                    process_group_id = None
            mark_job_active(job_dir)
            update_job_metadata(
                job_dir,
                status="running",
                pid=process.pid,
                pgid=process_group_id,
                process_identity=_capture_identity(process.pid),
                updated_at=time.time(),
            )
            deadline = (
                time.monotonic() + max(0.001, timeout_seconds)
                if timeout_seconds is not None
                else None
            )
            while True:
                polled = process.poll()
                if isinstance(polled, int):
                    exit_code = polled
                    break
                if log_max_bytes is not None:
                    log_bytes = _combined_log_bytes(stdout_log, stderr_log)
                    if log_bytes > log_max_bytes:
                        log_limit_exceeded = True
                        _append_supervisor_error(
                            stderr_log,
                            (
                                "durable job output limit exceeded: "
                                f"{log_bytes} > {log_max_bytes} bytes"
                            ),
                        )
                        _terminate_supervised_process(process)
                        polled = process.poll()
                        exit_code = int(polled) if isinstance(polled, int) else 125
                        break
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    _terminate_supervised_process(process)
                    polled = process.poll()
                    exit_code = int(polled) if isinstance(polled, int) else 124
                    break
                time.sleep(JOB_SAFETY_POLL_SECONDS)
    except BaseException as exc:
        _append_supervisor_error(stderr_log, str(exc))
        if process is not None and process.poll() is None:
            _terminate_supervised_process(process)
        exit_code = process.poll() if process is not None else -1
        if exit_code is None:
            exit_code = -1
        if isinstance(exc, _SupervisorShutdownRequested):
            exit_code = -exc.signum

    completed_at = time.time()

    def finalize(current: dict[str, object]) -> dict[str, object]:
        kill_signal = current.get("kill_signal")
        if timed_out:
            status = "timed_out"
            termination_reason = "timed_out"
        elif log_limit_exceeded:
            status = "failed"
            termination_reason = "log_limit_exceeded"
        elif kill_signal is not None:
            status = "killed"
            termination_reason = None
        elif exit_code == 0:
            status = "succeeded"
            termination_reason = None
        else:
            status = "failed"
            termination_reason = None
        current.update(
            {
                "status": status,
                "exit_code": exit_code,
                "termination_reason": termination_reason,
                "log_limit_exceeded": log_limit_exceeded,
                "completed_at": completed_at,
                "updated_at": completed_at,
            }
        )
        return current

    terminal_recorded = False
    try:
        try:
            mutate_job_metadata(job_dir, finalize)
            terminal_recorded = True
        except BaseException as exc:
            _append_supervisor_error(stderr_log, f"failed to record terminal status: {exc}")
            return 1
        return 0
    finally:
        if terminal_recorded:
            try:
                unmark_job_active(job_dir)
            except OSError as exc:
                _append_supervisor_error(stderr_log, f"failed to clear active job marker: {exc}")
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)


def _wait_for_detached_supervisor(child_pid: int, job_dir: Path) -> int:
    while True:
        try:
            waited_pid, wait_status = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            return 1
        if waited_pid == child_pid:
            return os.waitstatus_to_exitcode(wait_status)
        try:
            metadata = read_job_metadata(job_dir)
        except (OSError, ValueError):
            metadata = None
        if metadata is not None and metadata.get("supervisor_pid") == child_pid:
            return 0
        time.sleep(0.01)


def _terminate_and_reap_detached_child(child_pid: int) -> None:
    try:
        os.kill(child_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            waited_pid, _ = os.waitpid(child_pid, os.WNOHANG)
        except ChildProcessError:
            return
        if waited_pid == child_pid:
            return
        time.sleep(0.01)
    try:
        os.kill(child_pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except ProcessLookupError:
        pass
    try:
        os.waitpid(child_pid, 0)
    except ChildProcessError:
        pass


def _detach_and_supervise(
    *,
    job_dir: Path,
    command: str,
    cwd: Path,
    timeout_seconds: float | None = None,
    log_max_bytes: int | None = None,
) -> int:
    child_pid: int | None = None
    handled_signals = [signal.SIGTERM]
    if hasattr(signal, "SIGINT"):
        handled_signals.append(signal.SIGINT)
    previous_handlers: dict[int, object] = {}

    def request_bootstrap_shutdown(signum: int, _frame: object) -> None:
        signal.signal(signum, signal.SIG_IGN)
        raise _DetachedBootstrapShutdownRequested(signum)

    for signum in handled_signals:
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, request_bootstrap_shutdown)

    try:
        child_pid = os.fork()
        if child_pid == 0:
            for signum, previous_handler in previous_handlers.items():
                signal.signal(signum, previous_handler)
            os.setsid()
            return supervise_job(
                job_dir=job_dir,
                command=command,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                log_max_bytes=log_max_bytes,
            )
        return _wait_for_detached_supervisor(child_pid, job_dir)
    except _DetachedBootstrapShutdownRequested as exc:
        if child_pid is not None and child_pid > 0:
            _terminate_and_reap_detached_child(child_pid)
        return 128 + exc.signum
    finally:
        if child_pid != 0:
            for signum, previous_handler in previous_handlers.items():
                signal.signal(signum, previous_handler)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detached durable job supervisor.")
    parser.add_argument("--detach", action="store_true")
    parser.add_argument("--job-dir", required=True)
    parser.add_argument("--command", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--log-max-bytes", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.detach and os.name == "posix":
        return _detach_and_supervise(
            job_dir=Path(args.job_dir).expanduser().resolve(),
            command=args.command,
            cwd=Path(args.cwd).expanduser().resolve(),
            timeout_seconds=args.timeout_seconds,
            log_max_bytes=args.log_max_bytes,
        )
    return supervise_job(
        job_dir=Path(args.job_dir).expanduser().resolve(),
        command=args.command,
        cwd=Path(args.cwd).expanduser().resolve(),
        timeout_seconds=args.timeout_seconds,
        log_max_bytes=args.log_max_bytes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
