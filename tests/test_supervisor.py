from __future__ import annotations

import os
from pathlib import Path
import stat

from chatgpt_web_oauth_mcp.supervisor import _remove_pid_file, _write_pid_file


def test_write_pid_file_replaces_symlink_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("do-not-touch", encoding="utf-8")
    pid_file = tmp_path / "supervisor.pid"
    pid_file.symlink_to(target)

    _write_pid_file(pid_file)

    assert not pid_file.is_symlink()
    assert pid_file.read_text(encoding="utf-8") == str(os.getpid())
    assert target.read_text(encoding="utf-8") == "do-not-touch"
    assert stat.S_IMODE(pid_file.stat().st_mode) == 0o600


def test_remove_pid_file_does_not_follow_symlink(tmp_path: Path) -> None:
    target = tmp_path / "outside.txt"
    target.write_text(str(os.getpid()), encoding="utf-8")
    pid_file = tmp_path / "supervisor.pid"
    pid_file.symlink_to(target)

    _remove_pid_file(pid_file)

    assert pid_file.is_symlink()
    assert target.read_text(encoding="utf-8") == str(os.getpid())


def test_remove_pid_file_only_removes_current_process_record(tmp_path: Path) -> None:
    pid_file = tmp_path / "supervisor.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    _remove_pid_file(pid_file)
    assert not pid_file.exists()

    pid_file.write_text(str(os.getpid() + 1), encoding="utf-8")
    _remove_pid_file(pid_file)
    assert pid_file.exists()
