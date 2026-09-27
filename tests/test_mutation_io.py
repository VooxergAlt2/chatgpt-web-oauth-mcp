from __future__ import annotations

from pathlib import Path
import stat

import pytest

import chatgpt_web_oauth_mcp.mutation_io as mutation_io


def test_mutation_lock_namespace_and_file_are_private(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mutation_io.tempfile, "gettempdir", lambda: str(tmp_path))
    target = tmp_path / "workspace" / "file.txt"

    with mutation_io.exclusive_mutation_lock(target):
        lock_path = mutation_io.mutation_lock_path(target)
        assert lock_path.is_file()
        assert not lock_path.is_symlink()
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(lock_path.parent.parent.stat().st_mode) == 0o700


def test_mutation_lock_namespace_refuses_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mutation_io.tempfile, "gettempdir", lambda: str(tmp_path))
    outside = tmp_path / "outside"
    outside.mkdir()
    namespace = tmp_path / "chatgpt-web-oauth-mcp"
    namespace.symlink_to(outside, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        mutation_io.mutation_lock_path(tmp_path / "target.txt")

    assert namespace.is_symlink()
    assert list(outside.iterdir()) == []


def test_mutation_lock_refuses_symlink_lock_file_without_touching_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(mutation_io.tempfile, "gettempdir", lambda: str(tmp_path))
    target = tmp_path / "workspace" / "file.txt"
    lock_path = mutation_io.mutation_lock_path(target)
    outside = tmp_path / "outside.lock"
    outside.write_text("outside", encoding="utf-8")
    lock_path.symlink_to(outside)

    with pytest.raises((OSError, ValueError)):
        with mutation_io.exclusive_mutation_lock(target):
            raise AssertionError("symlink mutation lock must not be acquired")

    assert lock_path.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"
