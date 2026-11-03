"""Linux ACP isolation: auth runtime and native tool helper have different views.

Only trusted operator settings may select binaries/profiles. The model-facing
native helper has no OAuth files, host home, Opss state or host /proc.
Read-only is enforced by the helper's mount namespace, not ACP annotations.
The helper's private network exposes only two task-owned Unix channels.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .acp_helper_broker import AcpHelperBroker
from .acp_sdk_transport import AcpSdkTransport
from .acp_seccomp import helper_filter_fd
from .acp_session_store import AcpStateError
from .state_io import atomic_write_bytes


def _canonical(path: Path, *, directory: bool) -> Path:
    path = path.resolve(strict=True)
    if not path.is_absolute() or (path.is_dir() if directory else path.is_file()) is not True:
        raise AcpStateError('acp_sandbox_invalid_path')
    return path


def _system_root() -> list[str]:
    args = ['--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/lib', '/lib']
    if Path('/usr/lib64').exists():
        args += ['--symlink', 'usr/lib64', '/lib64']
    # Google's packaged runtime/tcmalloc requires the read-only CPU topology.
    if Path('/sys/devices/system/cpu').is_dir():
        args += ['--ro-bind', '/sys/devices/system/cpu', '/sys/devices/system/cpu']
    args += ['--dev', '/dev', '--proc', '/proc', '--tmpfs', '/tmp',
             '--dir', '/home', '--dir', '/home/agent']
    return args


@dataclass(frozen=True)
class AcpSandbox:
    server: Path
    helper: Path
    profile: Path
    workspace: Path
    control: Path
    conversation_dir: Path | None = None

    def __post_init__(self) -> None:
        for name in ('server', 'helper'):
            p = _canonical(getattr(self, name), directory=False)
            if not os.access(p, os.X_OK):
                raise AcpStateError('acp_runtime_not_executable')
            object.__setattr__(self, name, p)
        for name in ('profile', 'workspace', 'control'):
            object.__setattr__(self, name, _canonical(getattr(self, name), directory=True))
        if self.workspace == Path('/') or self.workspace == self.profile or self.workspace in self.profile.parents or self.profile in self.workspace.parents:
            raise AcpStateError('acp_profile_workspace_overlap')
        if self.workspace in self.control.parents or self.control in self.workspace.parents or self.workspace == self.control:
            raise AcpStateError('acp_control_workspace_overlap')
        for p in (self.profile, self.control):
            s = p.stat()
            if s.st_uid != os.getuid() or s.st_mode & 0o077:
                raise AcpStateError('acp_private_directory_required')
        conversations = self.conversation_dir or (self.control / 'conversations')
        if conversations.is_symlink():
            raise AcpStateError('acp_conversation_store_symlink')
        conversations.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = conversations.resolve()
        if resolved == self.workspace or self.workspace in resolved.parents or resolved in self.workspace.parents:
            raise AcpStateError('acp_conversation_workspace_overlap')
        if resolved == self.profile or self.profile in resolved.parents or resolved in self.profile.parents:
            raise AcpStateError('acp_conversation_profile_overlap')
        if conversations.stat().st_mode & 0o077 or conversations.stat().st_uid != os.getuid():
            raise AcpStateError('acp_private_directory_required')
        object.__setattr__(self, 'conversation_dir', conversations.resolve())
        if not Path('/usr/bin/bwrap').is_file():
            raise AcpStateError('acp_sandbox_unavailable')

    def helper_command(self) -> list[str]:
        policy_fd = helper_filter_fd(block_unix=True)
        try:
            native_policy = os.read(policy_fd, 65536)
        finally:
            os.close(policy_fd)
        atomic_write_bytes(self.control / 'native-policy.bpf', native_policy,
                           mode=0o600, sync_directory=True)
        return [
            '/usr/bin/bwrap', '--die-with-parent', '--unshare-user', '--unshare-pid',
            '--unshare-ipc', '--unshare-uts', '--unshare-net', '--new-session',
            *_system_root(),
            '--ro-bind', str(self.helper), '/opt/acp/localharness_external',
            '--ro-bind', str(Path(__file__).with_name('acp_network.py')), '/opt/acp/acp_network.py',
            '--ro-bind', str(self.control / 'native-policy.bpf'), '/opt/acp/native-policy.bpf',
            '--ro-bind', str(self.control / 'network' / 'native-ws.sock'), '/control/native-ws.sock',
            '--ro-bind', str(self.control / 'network' / 'inference.sock'), '/control/inference.sock',
            '--bind', str(self.conversation_dir), '/profile/antigravity-acp/conversations',
            '--ro-bind', str(self.workspace), str(self.workspace),
            '--chdir', str(self.workspace), '--clearenv',
            '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'HOME', '/home/agent',
            '--setenv', 'TMPDIR', '/tmp', '--setenv', 'LANG', 'C.UTF-8',
            '--', '/usr/bin/python3', '-I', '-B', '/opt/acp/acp_network.py', '--helper',
        ]

    def runtime_transport(self, socket_path: Path) -> AcpSdkTransport:
        auth_dir = self.profile / 'antigravity-acp'
        if auth_dir.is_symlink() or not auth_dir.is_dir():
            raise AcpStateError('acp_auth_directory_invalid')
        wrapper = self.control / 'native-helper.sh'
        script = '#!/bin/sh\nexec /usr/bin/python3 -I -B /opt/acp/acp_helper_proxy.py "$@"\n'
        atomic_write_bytes(wrapper, script.encode(), mode=0o700, sync_directory=True)
        args = [
            '/usr/bin/bwrap', '--die-with-parent', '--unshare-user', '--unshare-pid',
            '--unshare-ipc', '--unshare-uts', '--new-session', *_system_root(),
            '--ro-bind', str(self.server), '/opt/acp/agy_acp_server.par',
            '--ro-bind', str(Path(__file__).with_name('acp_helper_proxy.py')), '/opt/acp/acp_helper_proxy.py',
            '--ro-bind', str(wrapper), '/opt/acp/native-helper.sh',
            '--ro-bind', str(socket_path), '/control/helper.sock',
            # Expose only ACP auth/state, not cross-surface hooks, skills or MCP
            # configuration in profile/config or profile/antigravity-cli.
            # Keep the directory writable for atomic OAuth refresh, without
            # copying or rebinding credentials between accounts.
            '--bind', str(auth_dir), '/profile/antigravity-acp',
            '--bind', str(self.conversation_dir), '/profile/antigravity-acp/conversations',
            # The authenticated runtime must not execute repository hooks or
            # discover project configuration. Its cwd is empty. Only the native
            # helper sees the real workspace under the same absolute path.
            '--tmpfs', str(self.workspace),
            '--chdir', str(self.workspace),
        ]
        for path in ('/etc/ssl', '/etc/resolv.conf', '/etc/hosts', '/etc/nsswitch.conf', '/etc/passwd', '/etc/group', '/etc/localtime'):
            if Path(path).exists():
                args += ['--ro-bind', path, path]
        env = {
            'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/home/agent',
            'GEMINI_HOME': '/profile', 'TMPDIR': '/tmp',
            'XDG_CONFIG_HOME': '/home/agent/.config', 'XDG_CACHE_HOME': '/home/agent/.cache',
            'XDG_DATA_HOME': '/home/agent/.local/share',
            'ANTIGRAVITY_HARNESS_PATH': '/opt/acp/native-helper.sh',
            'AGY_ACP_FORCE_FILE_STORAGE': '1',
        }
        args += ['--clearenv']
        for k, v in env.items():
            args += ['--setenv', k, v]
        args += ['--', '/opt/acp/agy_acp_server.par', '--uid=']
        return AcpSdkTransport(tuple(args), self.workspace, env)

    def transport(self):
        @asynccontextmanager
        async def isolated(client):
            broker = AcpHelperBroker(self.control / 'helper.sock', tuple(self.helper_command()))
            async with broker.running(), self.runtime_transport(broker.path)(client) as connection:
                yield connection
            if broker.error_code:
                raise AcpStateError(broker.error_code)
        return isolated
