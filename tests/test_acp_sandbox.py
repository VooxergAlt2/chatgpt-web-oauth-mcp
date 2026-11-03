import asyncio
import contextlib
import json
import subprocess
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.acp_helper_broker import AcpHelperBroker
from chatgpt_web_oauth_mcp.acp_network import AcpNetworkGateway
from chatgpt_web_oauth_mcp.acp_sandbox import AcpSandbox
from chatgpt_web_oauth_mcp.acp_seccomp import helper_filter_fd
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError


def make_box(tmp_path, helper_text='#!/bin/sh\nexit 0\n'):
    profile = tmp_path / 'profile'; profile.mkdir(mode=0o700)
    (profile / 'antigravity-acp').mkdir(mode=0o700)
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    control = tmp_path / 'control'; control.mkdir(mode=0o700)
    helper = tmp_path / 'helper'; helper.write_text(helper_text); helper.chmod(0o700)
    return AcpSandbox(Path('/bin/true'), helper, profile, workspace, control)


def test_launcher_separates_profile_from_native_helper(tmp_path):
    box = make_box(tmp_path)
    transport = box.runtime_transport(box.control / 'helper.sock')
    helper = ' '.join(box.helper_command())
    assert str(box.profile) not in helper
    assert '/profile/antigravity-acp/conversations' in helper
    assert 'acp_token.json' not in helper and 'settings.json' not in helper
    assert '--unshare-pid' in helper and '--clearenv' in helper and '--unshare-net' in helper
    assert '--ro-bind' in helper and str(box.workspace) in helper
    assert transport.environment['GEMINI_HOME'] == '/profile'
    assert '--ro-bind' in transport.argv and '--clearenv' in transport.argv
    assert '--bind' in transport.argv
    assert '/control/helper.sock' in transport.argv


@pytest.mark.skipif(not Path('/usr/bin/bwrap').exists(), reason='Linux bwrap required')
def test_broker_shutdown_cleans_live_process_with_client_still_open(tmp_path):
    box = make_box(tmp_path, '#!/bin/sh\nexec /usr/bin/sleep 30\n')
    async def scenario():
        broker = AcpHelperBroker(box.control/'helper.sock', tuple(box.helper_command()))
        async with broker.running():
            _, writer = await asyncio.open_unix_connection(str(broker.path))
            while not broker.processes:
                await asyncio.sleep(.001)
            # Do not close the client before __aexit__; the broker owns teardown.
        assert not broker.processes and not broker.connections
        assert broker.error_code is None
        assert not broker.path.exists()
        assert not list((box.control/'network').glob('*.sock'))
        writer.close()
        with contextlib.suppress(ConnectionResetError):
            await writer.wait_closed()
    asyncio.run(asyncio.wait_for(scenario(), 4))


@pytest.mark.skipif(not Path('/usr/bin/bwrap').exists(), reason='Linux bwrap required')
def test_broker_shutdown_cannot_interrupt_cleanup_already_in_progress(tmp_path, monkeypatch):
    import chatgpt_web_oauth_mcp.acp_helper_broker as module
    box = make_box(tmp_path)
    async def scenario():
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        interrupted = []
        class DelayedGateway(AcpNetworkGateway):
            async def close(self):
                cleanup_started.set()
                try:
                    await release_cleanup.wait()
                except asyncio.CancelledError:
                    interrupted.append(True)
                    raise
                await super().close()
        monkeypatch.setattr(module, 'AcpNetworkGateway', DelayedGateway)
        broker = AcpHelperBroker(box.control/'helper.sock', tuple(box.helper_command()))
        async def release():
            await asyncio.sleep(.03)
            release_cleanup.set()
        async with broker.running():
            _, writer = await asyncio.open_unix_connection(str(broker.path))
            # Native exits without a handshake; _serve is already in finally.
            await cleanup_started.wait()
            release_task = asyncio.create_task(release())
        await release_task
        assert not interrupted
        assert not broker.processes and not broker.connections
        assert broker.error_code == 'acp_helper_startup_failed'
        assert not broker.path.exists()
        assert not list((box.control/'network').glob('*.sock'))
        writer.close()
        with contextlib.suppress(ConnectionResetError):
            await writer.wait_closed()
    asyncio.run(asyncio.wait_for(scenario(), 4))


def test_launcher_rejects_overlapping_credentials(tmp_path):
    box = make_box(tmp_path)
    with pytest.raises(AcpStateError, match='profile_workspace_overlap'):
        AcpSandbox(box.server, box.helper, box.workspace, box.workspace, box.control)
    with pytest.raises(AcpStateError, match='profile_workspace_overlap'):
        AcpSandbox(box.server, box.helper, box.profile, tmp_path, box.control)


@pytest.mark.skipif(not Path('/usr/bin/bwrap').exists(), reason='Linux bwrap required')
def test_native_helper_cannot_read_profile_host_canary_or_write_workspace(tmp_path):
    secret = tmp_path / 'host-secret'; secret.write_text('SYNTHETIC_ONLY')
    # The substitute tool helper executes inside exactly the production inner namespace.
    text = f'''#!/bin/sh
set -eu
test ! -e /profile/antigravity-acp/credential-canary
test ! -e {secret}
test ! -e /home/vooxerg/.ssh
test ! -e /opt/acp/agy_acp_server.par
test "$(cat sentinel.txt)" = ALLOWED
if (echo wrong > sentinel.txt) 2>/dev/null; then exit 42; fi
/usr/bin/python3 -I -c 'import ctypes,errno; c=ctypes.CDLL(None,use_errno=True); assert c.ptrace(0,0,0,0)==-1 and ctypes.get_errno()==errno.EPERM'
echo HELPER_ISOLATION_PASS
'''
    box = make_box(tmp_path, text)
    (box.workspace / 'sentinel.txt').write_text('ALLOWED')
    (box.profile / 'antigravity-acp' / 'credential-canary').write_text('SYNTHETIC_AUTH')
    async def probe():
        gateway = AcpNetworkGateway(box.control / 'network')
        await gateway.start()
        policy_fd = helper_filter_fd()
        try:
            # Same production mounts, network namespace and seccomp policy;
            # execute the synthetic native helper directly, without its relay.
            args = box.helper_command()
            args = [args[0], '--seccomp', str(policy_fd), *args[1:args.index('--')],
                    '--', '/usr/bin/python3', '-I', '-B', '/opt/acp/acp_network.py', '--native']
            p = await asyncio.create_subprocess_exec(*args, cwd=box.workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, pass_fds=(policy_fd,))
            out, err = await asyncio.wait_for(p.communicate(), 10)
            return subprocess.CompletedProcess(args, p.returncode, out, err)
        finally:
            import os
            os.close(policy_fd)
            await gateway.close()
    r = asyncio.run(probe())
    if r.returncode and b'No permissions to create new namespace' in r.stderr:
        pytest.skip('User namespaces unavailable on this host')
    assert r.returncode == 0, r.stderr.decode(errors='replace')
    assert b'HELPER_ISOLATION_PASS' in r.stdout
    assert (box.workspace / 'sentinel.txt').read_text() == 'ALLOWED'


@pytest.mark.skipif(not Path('/usr/bin/bwrap').exists(), reason='Linux bwrap required')
def test_auth_runtime_sees_empty_workspace_and_no_global_hooks_but_refreshes_own_token(tmp_path):
    box = make_box(tmp_path)
    auth = box.profile / 'antigravity-acp'
    (auth / 'settings.json').write_text('{"auth":{"type":"oauth-personal"}}')
    (auth / 'acp_token.json').write_text('SYNTHETIC_OLD_TOKEN')
    malicious = {'command': "/usr/bin/touch /profile/antigravity-acp/hook-ran"}
    for name in ('.agents', '.agent', '_agents', '_agent'):
        folder = box.workspace / name
        folder.mkdir()
        (folder / 'hooks.json').write_text(json.dumps(malicious))
    global_config = box.profile / 'config'
    global_config.mkdir()
    (global_config / 'hooks.json').write_text(json.dumps(malicious))
    (global_config / 'mcp_config.json').write_text('{"servers":{"malicious":{}}}')
    (box.workspace / 'source.py').write_text('REAL_WORKSPACE_CONTENT')
    probe = tmp_path / 'auth-probe'
    probe.write_text('''#!/usr/bin/python3
import json, os, subprocess
from pathlib import Path
cwd = Path.cwd()
# An auth-side hook loader cannot discover any of the host's hook files.
for path in [cwd/name/'hooks.json' for name in ('.agents','.agent','_agents','_agent')] + [Path('/profile/config/hooks.json')]:
    if path.exists():
        subprocess.run(json.loads(path.read_text())['command'].split(), check=True)
assert list(cwd.iterdir()) == []
assert not Path('/profile/config/mcp_config.json').exists()
auth = Path('/profile/antigravity-acp')
assert not (auth/'hook-ran').exists()
assert json.loads((auth/'settings.json').read_text())['auth']['type'] == 'oauth-personal'
assert (auth/'acp_token.json').read_text() == 'SYNTHETIC_OLD_TOKEN'
(auth/'new-token').write_text('SYNTHETIC_REFRESHED_TOKEN')
os.replace(auth/'new-token', auth/'acp_token.json')
print('AUTH_CONFIGURATION_ISOLATION_PASS')
''')
    probe.chmod(0o700)
    box = AcpSandbox(probe, box.helper, box.profile, box.workspace, box.control)
    async def scenario():
        broker = AcpHelperBroker(box.control/'helper.sock', tuple(box.helper_command()))
        async with broker.running():
            transport = box.runtime_transport(broker.path)
            child = await asyncio.create_subprocess_exec(*transport.argv, env=transport.environment,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(child.communicate(), 8)
            assert child.returncode == 0, err.decode()
            assert b'AUTH_CONFIGURATION_ISOLATION_PASS' in out
    asyncio.run(scenario())
    assert not (auth/'hook-ran').exists()
    assert (auth/'acp_token.json').read_text() == 'SYNTHETIC_REFRESHED_TOKEN'
    assert (box.workspace/'source.py').read_text() == 'REAL_WORKSPACE_CONTENT'
