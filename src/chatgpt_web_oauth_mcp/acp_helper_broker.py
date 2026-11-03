"""Task-owned fixed-command native helper relay, outside the auth sandbox.

This is not a shell API. Every connection launches the SAME trusted executable
with the SAME mount policy. No caller can supply a command or environment.
The socket is private, peer uid is checked, and all byte streams are bounded.
"""
from __future__ import annotations

import asyncio
import os
import socket
import struct
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from .acp_network import AcpNetworkGateway, NetworkBoundaryError, finish_cleanup, frame, read_frame
from .acp_seccomp import helper_filter_fd
from .acp_session_store import AcpStateError
from .acp_sdk_transport import close_acp_process


class AcpHelperBroker:
    def __init__(self, path: Path, argv: tuple[str, ...], *, gateway=None):
        self.path = path
        self.argv = argv
        self.gateway = gateway
        self.connections: set[asyncio.Task] = set()
        self.processes: set = set()
        self.error_code: str | None = None
        self.closing = False

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        peer = writer.get_extra_info('socket')
        creds = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
        _, uid, _ = struct.unpack('3i', creds)
        if uid != os.getuid() or self.connections or self.closing:
            writer.close()
            await writer.wait_closed()
            return
        self.connections.add(task)
        process = None
        pumps = []
        gateway = AcpNetworkGateway(self.path.parent / 'network', gateway=self.gateway)
        try:
            await gateway.start()
            policy_fd = helper_filter_fd()
            try:
                process = await asyncio.create_subprocess_exec(
                    self.argv[0], '--seccomp', str(policy_fd), *self.argv[1:],
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},
                    limit=1048576, pass_fds=(policy_fd,),
                )
            finally:
                os.close(policy_fd)
            self.processes.add(process)
            async def pump(source, target):
                total = 0
                while chunk := await source.read(65536):
                    total += len(chunk)
                    if total > 16777216:
                        raise AcpStateError('acp_helper_byte_limit')
                    target.write(chunk)
                    await target.drain()
                try:
                    target.write_eof()
                except (OSError, AttributeError):
                    pass
            pumps = [asyncio.create_task(pump(reader, process.stdin))]
            startup = await asyncio.wait_for(read_frame(process.stdout), 15)
            writer.write(frame(await gateway.ready(startup)))
            await writer.drain()
            pumps.append(asyncio.create_task(pump(process.stdout, writer)))
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            # Native helper stdin EOF or client disconnect ends this ownership.
            for p in done:
                await p
        except NetworkBoundaryError as exc:
            self.error_code = str(exc)
        except AcpStateError as exc:
            self.error_code = exc.code
        except (TimeoutError, asyncio.IncompleteReadError):
            self.error_code = 'acp_helper_startup_failed'
        except (OSError, ConnectionError):
            self.error_code = 'acp_helper_channel_failed'
        finally:
            async def cleanup():
                gateway.closing = True
                for p in pumps:
                    p.cancel()
                if pumps:
                    await asyncio.gather(*pumps, return_exceptions=True)
                try:
                    await gateway.close()
                finally:
                    self.error_code = self.error_code or gateway.error_code
                    try:
                        if process is not None:
                            await close_acp_process(process)
                    except AcpStateError as exc:
                        self.error_code = exc.code
                    finally:
                        if process is not None and process.returncode is not None:
                            self.processes.discard(process)
                        self.connections.discard(task)
                        writer.close()
                        with suppress(OSError):
                            await writer.wait_closed()
            await finish_cleanup(asyncio.create_task(cleanup()))

    @asynccontextmanager
    async def running(self):
        if self.path.exists() or self.path.is_symlink():
            raise AcpStateError('acp_helper_socket_already_exists')
        server = await asyncio.start_unix_server(self._serve, path=str(self.path), limit=1048576)
        self.path.chmod(0o600)
        try:
            yield self
        finally:
            async def shutdown():
                self.closing = True
                if self.gateway is not None:
                    self.gateway.revoke()
                server.close()
                tasks = list(self.connections)
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                await server.wait_closed()
                self.path.unlink(missing_ok=True)
            await finish_cleanup(asyncio.create_task(shutdown()))
