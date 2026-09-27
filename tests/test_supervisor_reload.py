from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from chatgpt_web_oauth_mcp import supervisor


pytestmark = pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP required")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])




def _python_for_subprocess(repo_root: Path) -> str:
    venv_python = repo_root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _wait_for_ready(url: str, *, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    with httpx.Client(timeout=1.0) as client:
        while time.time() < deadline:
            try:
                response = client.head(url)
                if response.status_code == 204:
                    return
            except Exception as exc:  # pragma: no cover - diagnostic only
                last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for supervisor-backed server: {last_error!r}")


def test_spawn_server_force_kills_child_that_ignores_failed_readiness_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        pid = 4242
        returncode = None

        def __init__(self) -> None:
            self.terminate_calls = 0
            self.kill_calls = 0
            self.wait_calls = 0

        def poll(self):
            return None

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = -9

        def wait(self, timeout=None):
            self.wait_calls += 1
            if self.kill_calls == 0:
                raise subprocess.TimeoutExpired(cmd="fake-server", timeout=timeout)
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        supervisor,
        "_wait_for_ready_pipe",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("not ready")),
    )

    with pytest.raises(RuntimeError, match="not ready"):
        supervisor._spawn_server(
            listener_fd=1,
            log_file=tmp_path / "server.log",
            ready_timeout=0.1,
            stream=sys.stderr,
        )

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 2


def test_supervisor_reload_keeps_mcp_endpoint_available(tmp_path: Path) -> None:
    port = _find_free_port()
    state_dir = tmp_path / "state"
    log_file = tmp_path / "server.log"
    pid_file = state_dir / "dev-tunnel-supervisor.pid"
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(repo_root / "src") if not existing_pythonpath else f"{repo_root / 'src'}{os.pathsep}{existing_pythonpath}"
    env.update(
        {
            "CHATGPT_MCP_HOST": "127.0.0.1",
            "CHATGPT_MCP_PORT": str(port),
            "CHATGPT_MCP_WORKSPACE_ROOT": str(tmp_path),
            "CHATGPT_MCP_STATE_DIR": str(state_dir),
            "CHATGPT_MCP_AUTH_TOKEN": "",
            "CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS": "5",
        }
    )
    proc = subprocess.Popen(
        [
            _python_for_subprocess(repo_root),
            "-m",
            "chatgpt_web_oauth_mcp.supervisor",
            "--pid-file",
            str(pid_file),
            "--log-file",
            str(log_file),
        ],
        cwd=repo_root,
        env=env,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    failures: list[str] = []
    stop_polling = threading.Event()

    def poll_endpoint() -> None:
        with httpx.Client(timeout=0.5) as client:
            while not stop_polling.is_set():
                try:
                    response = client.head(url)
                    if response.status_code != 204:
                        failures.append(f"unexpected_status={response.status_code}")
                except Exception as exc:  # pragma: no cover - diagnostic only
                    failures.append(type(exc).__name__)
                time.sleep(0.03)

    try:
        _wait_for_ready(url)
        poller = threading.Thread(target=poll_endpoint, daemon=True)
        poller.start()
        time.sleep(0.2)

        os.kill(proc.pid, signal.SIGHUP)
        deadline = time.time() + 5.0
        log_text = ""
        while time.time() < deadline:
            log_text = log_file.read_text(encoding="utf-8")
            if log_text.count("Started server process") >= 2:
                break
            time.sleep(0.05)

        stop_polling.set()
        poller.join(timeout=5)
        assert not poller.is_alive()
        assert failures == []
        assert log_text.count("Started server process") >= 2
    finally:
        stop_polling.set()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
