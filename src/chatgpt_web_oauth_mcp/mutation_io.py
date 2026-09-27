from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Iterator

from .state_io import atomic_write_bytes as _state_atomic_write_bytes

try:  # pragma: no cover - Windows fallback is exercised on Windows CI.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def revision_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def mutation_lock_path(path: Path) -> Path:
    # Keep the historical replace-locks namespace so rolling reloads remain
    # mutually exclusive with the previous replace implementation.
    root = Path(tempfile.gettempdir()) / "chatgpt-web-oauth-mcp" / "replace-locks"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    name = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()
    return root / f"{name}.lock"


@contextmanager
def exclusive_mutation_lock(path: Path) -> Iterator[None]:
    lock_path = mutation_lock_path(path)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        else:  # pragma: no cover
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
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


def durable_unlink(path: Path) -> None:
    path.unlink()
    if os.name != "posix":  # pragma: no cover - directory fsync is POSIX-specific.
        return
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_bytes(path: Path, raw: bytes, *, mode: int) -> None:
    _state_atomic_write_bytes(
        path,
        raw,
        mode=mode,
        sync_directory=True,
    )
