from __future__ import annotations

import io
import os
from pathlib import Path
import stat

import pytest

from chatgpt_web_oauth_mcp import supervisor as supervisor_module
from chatgpt_web_oauth_mcp.supervisor import _remove_pid_file, _spawn_server, _write_pid_file


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


def test_spawn_server_refuses_symlink_log_and_closes_ready_pipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"outside")
    log_file = tmp_path / "server.log"
    log_file.symlink_to(outside)
    real_pipe = os.pipe
    opened_fds: list[int] = []

    def tracked_pipe() -> tuple[int, int]:
        read_fd, write_fd = real_pipe()
        opened_fds.extend([read_fd, write_fd])
        return read_fd, write_fd

    monkeypatch.setattr(supervisor_module.os, "pipe", tracked_pipe)

    with pytest.raises(ValueError, match="symbolic link"):
        _spawn_server(
            listener_fd=-1,
            log_file=log_file,
            ready_timeout=0.1,
            stream=io.StringIO(),
        )

    assert outside.read_bytes() == b"outside"
    assert log_file.is_symlink()
    assert len(opened_fds) == 2
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_spawn_server_closes_ready_pipe_when_popen_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_pipe = os.pipe
    opened_fds: list[int] = []

    def tracked_pipe() -> tuple[int, int]:
        read_fd, write_fd = real_pipe()
        opened_fds.extend([read_fd, write_fd])
        return read_fd, write_fd

    def fail_popen(*_args, **_kwargs):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(supervisor_module.os, "pipe", tracked_pipe)
    monkeypatch.setattr(supervisor_module.subprocess, "Popen", fail_popen)
    log_file = tmp_path / "server.log"

    with pytest.raises(RuntimeError, match="spawn failed"):
        _spawn_server(
            listener_fd=-1,
            log_file=log_file,
            ready_timeout=0.1,
            stream=io.StringIO(),
        )

    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
    assert len(opened_fds) == 2
    for fd in opened_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
