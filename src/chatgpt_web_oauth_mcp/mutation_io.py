from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Iterator

from .state_io import (
    atomic_write_bytes as _state_atomic_write_bytes,
    ensure_private_directory,
    interprocess_file_lock,
)


def revision_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def mutation_lock_path(path: Path) -> Path:
    # Keep the historical replace-locks namespace so rolling reloads remain
    # mutually exclusive with the previous replace implementation.
    namespace_root = Path(tempfile.gettempdir()) / "chatgpt-web-oauth-mcp"
    ensure_private_directory(namespace_root)
    root = namespace_root / "replace-locks"
    ensure_private_directory(root)
    name = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()
    return root / f"{name}.lock"


@contextmanager
def exclusive_mutation_lock(path: Path) -> Iterator[None]:
    lock_path = mutation_lock_path(path)
    with interprocess_file_lock(lock_path):
        yield


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
