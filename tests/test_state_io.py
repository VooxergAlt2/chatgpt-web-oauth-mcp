from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest

from chatgpt_web_oauth_mcp.state_io import interprocess_file_lock


def test_interprocess_lock_refuses_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("outside", encoding="utf-8")
    target.chmod(0o644)
    lock_path = tmp_path / "state.lock"
    lock_path.symlink_to(target)

    with pytest.raises((OSError, ValueError)):
        with interprocess_file_lock(lock_path):
            raise AssertionError("symlink lock must not be acquired")

    assert lock_path.is_symlink()
    assert target.read_text(encoding="utf-8") == "outside"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_interprocess_lock_creates_private_regular_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"

    with interprocess_file_lock(lock_path):
        assert lock_path.is_file()
        assert not lock_path.is_symlink()
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "posix", reason="FIFO test is POSIX-specific.")
def test_interprocess_lock_refuses_non_regular_file(tmp_path: Path) -> None:
    lock_path = tmp_path / "state.lock"
    os.mkfifo(lock_path, 0o600)

    with pytest.raises(ValueError, match="regular file"):
        with interprocess_file_lock(lock_path):
            raise AssertionError("FIFO lock must not be acquired")
