"""Optional SDK bridge. The trusted supervisor supplies an isolated launcher.

No SDK import occurs until a connection is actually requested. This factory is
not exposed through MCP arguments and is not a sandbox or account scheduler.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .acp_session_store import AcpStateError
from .acp_network import finish_cleanup


async def close_acp_process(process: asyncio.subprocess.Process) -> None:
    """Reap an owned process AND close its pipe transports before loop teardown.

    Process.wait() may return immediately for a reaped child even when stdout
    is paused or an inherited descriptor keeps the pipe alive. Process has no
    public close operation, so retain its CPython transport once and use the
    public SubprocessTransport/ReadTransport/WriteTransport close methods.
    Call only after stream-reading tasks have stopped.
    """
    transport = process._transport
    for fd in (0, 1, 2):
        pipe = transport.get_pipe_transport(fd)
        if pipe is not None:
            if fd == 0 and pipe.get_write_buffer_size():
                pipe.abort()  # discard queued input; a dead reader cannot drain it
            else:
                pipe.close()
    failure = None
    try:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                failure = AcpStateError("acp_process_reap_timeout")
    finally:
        transport.close()

    async def discard(reader):
        if reader is not None:
            with suppress(OSError):
                while await reader.read(65536):
                    pass
    # EOF and stdin-close callbacks acknowledge descriptor closure; merely
    # scheduling transport.close() is insufficient before asyncio.run exits.
    waits = [discard(process.stdout), discard(process.stderr)]
    if process.stdin is not None:
        waits.append(process.stdin.wait_closed())
    await asyncio.gather(*waits, return_exceptions=True)
    if failure is not None:
        raise failure


@dataclass(frozen=True)
class AcpSdkTransport:
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.argv or not Path(self.argv[0]).is_absolute():
            raise ValueError("ACP launcher must be an absolute trusted executable")
        if not self.cwd.is_absolute() or not self.cwd.is_dir():
            raise ValueError("ACP launch cwd must be an existing absolute directory")
        # API-key billing and inherited application credentials are not part of
        # this subscription-backed adapter. Add new variables only by review.
        allowed = {
            "PATH", "LANG", "LC_ALL", "HOME", "GEMINI_HOME", "TMPDIR",
            "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
            "ANTIGRAVITY_HARNESS_PATH", "AGY_ACP_FORCE_FILE_STORAGE",
            "SSL_CERT_FILE", "SSL_CERT_DIR",
        }
        if set(self.environment) - allowed:
            raise ValueError("Unexpected ACP child environment variable")
        if any(not self.environment.get(k) for k in ("HOME", "GEMINI_HOME", "PATH")):
            raise ValueError("ACP requires explicit isolated home/profile/path")
        # Make a private copy: a mutable caller dict cannot change the launch.
        object.__setattr__(self, "environment", dict(self.environment))

    @asynccontextmanager
    async def __call__(self, client):
        try:
            if version("agent-client-protocol") != "0.12.1":
                raise AcpStateError("acp_sdk_version_mismatch")
        except PackageNotFoundError as exc:
            raise AcpStateError("acp_sdk_not_installed") from exc
        from acp import spawn_agent_process

        async with spawn_agent_process(
            client, self.argv[0], *self.argv[1:],
            cwd=self.cwd, env=self.environment,
            # Do not let provider auth URLs or raw exceptions enter job logs.
            transport_kwargs={"stderr": asyncio.subprocess.DEVNULL, "limit": 1048576, "shutdown_timeout": 2.0},
        ) as (connection, process):
            try:
                yield connection
            finally:
                async def cleanup():
                    try:
                        # SDK close stops its receive/handler tasks. The pinned
                        # SDK's own stdio finalizer then sees an already closed,
                        # reaped process instead of relying on PID state alone.
                        await asyncio.wait_for(connection.close(), 2)
                    finally:
                        await close_acp_process(process)
                await finish_cleanup(asyncio.create_task(cleanup()))
