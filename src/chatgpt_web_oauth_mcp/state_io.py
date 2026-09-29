from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO, Iterator

try:  # pragma: no cover - Windows fallback is exercised only on Windows.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def ensure_private_directory(path: Path) -> None:
    """Create or validate one private state directory without following its final symlink."""

    if path.is_symlink():
        raise ValueError("State directory must not be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ValueError("State directory must be a directory.")
        if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():  # pragma: no cover
            raise ValueError("State directory must not be a symbolic link.")
        try:
            os.fchmod(descriptor, 0o700)
        except OSError:
            pass
    finally:
        os.close(descriptor)


def _open_private_regular_file(path: Path, *, flags: int) -> int:
    if path.is_symlink():
        raise ValueError("Private state file must not be a symbolic link.")
    effective_flags = flags
    if hasattr(os, "O_NOFOLLOW"):
        effective_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        effective_flags |= os.O_NONBLOCK
    descriptor = os.open(path, effective_flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("Private state file must be a regular file.")
        if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():  # pragma: no cover
            raise ValueError("Private state file must not be a symbolic link.")
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def ensure_private_file(path: Path) -> None:
    descriptor = _open_private_regular_file(
        path,
        flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND,
    )
    os.close(descriptor)


def open_private_append_binary(path: Path, *, buffering: int = -1) -> BinaryIO:
    descriptor = _open_private_regular_file(
        path,
        flags=os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0),
    )
    try:
        return os.fdopen(descriptor, "ab", buffering=buffering)
    except Exception:
        os.close(descriptor)
        raise


def open_private_write_binary(
    path: Path,
    *,
    buffering: int = -1,
    exclusive: bool = False,
) -> BinaryIO:
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if exclusive:
        flags |= os.O_EXCL
    else:
        flags |= os.O_TRUNC
    descriptor = _open_private_regular_file(path, flags=flags)
    try:
        return os.fdopen(descriptor, "wb", buffering=buffering)
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def interprocess_file_lock(
    path: Path,
    *,
    exclusive: bool = True,
    blocking: bool = True,
) -> Iterator[None]:
    """Hold a process-safe shared or exclusive lock for the context lifetime."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("State lock file must not be a symbolic link.")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("State lock file must be a regular file.")
        if not hasattr(os, "O_NOFOLLOW") and path.is_symlink():  # pragma: no cover - platform fallback.
            raise ValueError("State lock file must not be a symbolic link.")
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            lock_flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if not blocking:
                lock_flags |= fcntl.LOCK_NB
            fcntl.flock(descriptor, lock_flags)
        else:  # pragma: no cover
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(descriptor, mode, 1)
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        else:  # pragma: no cover
            import msvcrt

            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(descriptor)


def is_file_locked(path: Path, *, exclusive: bool = True) -> bool:
    """Check if a file lock is currently held without blocking."""
    if not path.exists():
        return False
    try:
        with interprocess_file_lock(path, exclusive=exclusive, blocking=False):
            return False
    except (BlockingIOError, OSError):
        return True


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    mode: int = 0o600,
    sync_directory: bool = False,
) -> None:
    """Atomically replace a file using a unique same-directory temp file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
        try:
            path.chmod(mode)
        except OSError:
            pass
        if sync_directory and os.name == "posix":
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
