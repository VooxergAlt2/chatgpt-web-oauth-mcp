import asyncio
import contextlib
import json
import os
import select
import signal
import subprocess
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp.acp_helper_broker import AcpHelperBroker
from chatgpt_web_oauth_mcp.acp_network import AcpNetworkGateway
from chatgpt_web_oauth_mcp.acp_sandbox import AcpSandbox, verification_command
from chatgpt_web_oauth_mcp.acp_seccomp import helper_filter_fd
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
from chatgpt_web_oauth_mcp.acp_workspace import AcpWorkspace


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


@pytest.mark.skipif(not Path('/usr/bin/bwrap').exists(), reason='Linux bwrap required')
def test_cancel_during_helper_spawn_reaps_namespace_init(tmp_path, monkeypatch):
    from chatgpt_web_oauth_mcp.acp_sdk_transport import close_acp_process

    box = make_box(tmp_path, '#!/bin/sh\nexec /usr/bin/sleep 30\n')
    real_spawn = asyncio.create_subprocess_exec

    async def scenario():
        broker = AcpHelperBroker(box.control/'helper.sock', tuple(box.helper_command()))
        spawned = asyncio.Event()
        deliver_handle = asyncio.Event()
        process = writer = init_fd = None

        async def delayed_spawn(*args, **kwargs):
            nonlocal process
            process = await real_spawn(*args, **kwargs)
            spawned.set()
            await deliver_handle.wait()
            return process

        monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_spawn)
        try:
            async with broker.running():
                _, writer = await asyncio.open_unix_connection(str(broker.path))
                try:
                    await spawned.wait()
                    children = Path(f'/proc/{process.pid}/task/{process.pid}/children')
                    while True:
                        assert process.returncode is None
                        for pid in children.read_text().split():
                            try:
                                fd = os.pidfd_open(int(pid))
                            except ProcessLookupError:
                                continue
                            try:
                                ns = Path(f'/proc/{pid}/status').read_text().split('NSpid:')[1].splitlines()[0].split()
                                if ns == [pid, '1']:
                                    init_fd = fd
                                    break
                            except FileNotFoundError:
                                pass
                            finally:
                                if init_fd != fd:
                                    os.close(fd)
                        if init_fd is not None:
                            break
                        await asyncio.sleep(.001)
                    next(iter(broker.connections)).cancel()
                    await asyncio.sleep(0)
                finally:
                    deliver_handle.set()
            poll = select.poll()
            poll.register(init_fd, select.POLLIN)
            assert await asyncio.to_thread(poll.poll, 2000), 'sandbox child survived broker teardown'
            assert process.returncode is not None and not broker.processes
        finally:
            deliver_handle.set()
            if init_fd is not None:
                with contextlib.suppress(ProcessLookupError):
                    signal.pidfd_send_signal(init_fd, signal.SIGKILL)
                os.close(init_fd)
            if process is not None and process.returncode is None:
                await close_acp_process(process)
            if writer is not None:
                writer.close()
                with contextlib.suppress(ConnectionResetError):
                    await writer.wait_closed()

    asyncio.run(asyncio.wait_for(scenario(), 7))


def test_launcher_rejects_overlapping_credentials(tmp_path):
    box = make_box(tmp_path)
    with pytest.raises(AcpStateError, match='profile_workspace_overlap'):
        AcpSandbox(box.server, box.helper, box.workspace, box.workspace, box.control)
    with pytest.raises(AcpStateError, match='profile_workspace_overlap'):
        AcpSandbox(box.server, box.helper, box.profile, tmp_path, box.control)


@pytest.mark.skipif(not Path('/usr/bin/bwrap').exists(), reason='Linux bwrap required')
@pytest.mark.parametrize("proposal", [False, True])
def test_native_helper_cannot_read_profile_host_canary_or_write_workspace(tmp_path, proposal):
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
    if proposal:
        view = tmp_path / 'empty-proposal'
        view.mkdir(mode=0o700)
        box.helper.write_text(text.replace('test "$(cat sentinel.txt)" = ALLOWED',
                                           'test ! -e sentinel.txt'))
        box = AcpSandbox(box.server, box.helper, box.profile, box.workspace, box.control,
                         workspace_view=view)
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


def writer_box(tmp_path, helper_text):
    box = make_box(tmp_path, helper_text)
    main = tmp_path / 'main'
    main.mkdir()
    def git(cwd, *args):
        subprocess.run(['/usr/bin/git', '-C', str(cwd), *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    git(main, 'init', '-q')
    git(main, 'config', 'user.email', 'acp-test@example.invalid')
    git(main, 'config', 'user.name', 'ACP test')
    (main/'source.txt').write_text('before\n')
    git(main, 'add', '.')
    git(main, 'commit', '-qm', 'baseline')
    git(main, 'worktree', 'add', '-qb', 'writer-test', str(box.workspace))
    work = AcpWorkspace.prepare(box.workspace, ['source.txt', 'created.txt'], tmp_path/'writer-state')
    return AcpSandbox(box.server, box.helper, box.profile, box.workspace, box.control,
                      workspace_view=work.root, writable_workspace=True), work


def test_writable_source_mount_is_never_accepted(tmp_path):
    box = make_box(tmp_path)
    with pytest.raises(AcpStateError, match='detached_workspace_required'):
        AcpSandbox(box.server, box.helper, box.profile, box.workspace, box.control,
                   writable_workspace=True)
    with pytest.raises(AcpStateError, match='workspace_overlap'):
        AcpSandbox(box.server, box.helper, box.profile, box.workspace, box.control,
                   workspace_view=box.workspace, writable_workspace=True)


def test_broker_reaps_exited_process_and_closes_paused_stdout(tmp_path, monkeypatch):
    """A reaped PID must not leave its paused pipe transport owned by the loop."""
    import chatgpt_web_oauth_mcp.acp_helper_broker as module
    box = make_box(tmp_path)
    original_spawn = asyncio.create_subprocess_exec
    created = []
    async def spawn(*args, **kwargs):
        # Invalid startup frame, then just enough output to pause StreamReader.
        # Delay gateway cleanup until this process has already exited.
        process = await original_spawn(
            '/usr/bin/python3', '-c',
            'import os; os.write(1, b"\\xff"*4 + b"x"*(2*1048576+32))', **kwargs)
        created.append(process)
        return process
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', spawn)
    async def scenario():
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        class DelayedGateway(AcpNetworkGateway):
            async def close(self):
                cleanup_started.set()
                await release_cleanup.wait()
                await super().close()
        monkeypatch.setattr(module, 'AcpNetworkGateway', DelayedGateway)
        broker = AcpHelperBroker(box.control/'helper.sock', tuple(box.helper_command()))
        process = None
        writer = None
        try:
            async with broker.running():
                _, writer = await asyncio.open_unix_connection(str(broker.path))
                await cleanup_started.wait()
                process = created[0]
                while process.returncode is None or not process.stdout._paused:
                    await asyncio.sleep(.001)
                release_cleanup.set()
            assert process.returncode == 0
            assert process._transport.is_closing()
            assert process.stdin.is_closing()
            for fd in (0, 1):
                pipe = process._transport.get_pipe_transport(fd)
                assert pipe.is_closing()
                assert pipe.get_extra_info('pipe').closed
            assert not broker.connections and not broker.processes
        finally:
            release_cleanup.set()
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
            if process is not None:
                # Test-only cleanup also keeps the intentionally failing old
                # implementation from leaking descriptors after the assertion.
                process._transport.close()
                await asyncio.sleep(.01)
    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.skipif(not Path('/usr/bin/bwrap').exists(), reason='Linux bwrap required')
def test_native_writer_only_changes_detached_copy_and_keeps_existing_seccomp(tmp_path):
    import os
    helper = '''#!/usr/bin/python3
import errno, os, socket, subprocess
from pathlib import Path
assert not Path('.git').exists()
assert not Path('/profile/antigravity-acp/acp_token.json').exists()
assert 'SYNTHETIC_HOST_CREDENTIAL' not in os.environ
try:
    socket.socket(socket.AF_UNIX)
except OSError as exc:
    assert exc.errno == errno.EPERM
else:
    raise AssertionError('native Unix sockets must stay blocked')
Path('source.txt').write_text('native edited\\n')
subprocess.run(['/bin/sh','-c','printf "native created\\\\n" > created.txt'], check=True)
print('DETACHED_NATIVE_WRITER_PASS')
'''
    box, work = writer_box(tmp_path, helper)
    (box.profile/'antigravity-acp'/'acp_token.json').write_text('SYNTHETIC_ONLY')
    async def probe():
        gateway = AcpNetworkGateway(box.control/'network')
        await gateway.start()
        policy_fd = helper_filter_fd()
        try:
            args = box.helper_command()
            args = [args[0], '--seccomp', str(policy_fd), *args[1:args.index('--')],
                    '--', '/usr/bin/python3', '-I', '-B', '/opt/acp/acp_network.py', '--native']
            process = await asyncio.create_subprocess_exec(*args, cwd=box.workspace,
                env={**os.environ, 'SYNTHETIC_HOST_CREDENTIAL':'must-not-enter'},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, pass_fds=(policy_fd,))
            output, error = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0, error.decode(errors='replace')
            assert b'DETACHED_NATIVE_WRITER_PASS' in output
        finally:
            os.close(policy_fd)
            await gateway.close()
    with work:
        asyncio.run(probe())
        assert (box.workspace/'source.txt').read_text() == 'before\n'
        assert not (box.workspace/'created.txt').exists()
        assert work.validate_changes() == ('created.txt', 'source.txt')
        work.promote(expected_digest=work.snapshot_digest())
        assert (box.workspace/'source.txt').read_text() == 'native edited\n'
        assert (box.workspace/'created.txt').read_text() == 'native created\n'


@pytest.mark.skipif(not Path('/usr/bin/bwrap').exists(), reason='Linux bwrap required')
def test_independent_verifier_is_readonly_without_auth_host_network_or_source_sockets(tmp_path):
    import os
    import shlex
    import socket
    box, work = writer_box(tmp_path, '#!/bin/sh\nexit 0\n')
    host_secret = tmp_path/'host-secret'
    host_secret.write_text('SYNTHETIC_ONLY')
    unix_path = tmp_path/'host-service.sock'
    with socket.socket(socket.AF_UNIX) as unix, socket.socket() as tcp, socket.socket(
            socket.AF_INET, socket.SOCK_DGRAM) as udp, work:
        unix.bind(str(unix_path))
        unix.listen()
        tcp.bind(('127.0.0.1', 0))
        tcp.listen()
        udp.bind(('127.0.0.1', 0))
        udp.settimeout(.1)
        baseline = work.snapshot_digest()
        program = f'''
import asyncio, ctypes, errno, os, socket
from pathlib import Path
assert Path.cwd() == Path({str(box.workspace)!r})
assert Path('source.txt').read_text() == 'before\\n'
assert not Path('.git').exists()
assert not Path('/profile').exists()
assert not Path('/control').exists()
assert not Path({str(host_secret)!r}).exists()
assert 'SYNTHETIC_HOST_CREDENTIAL' not in os.environ
try:
    Path('source.txt').write_text('forbidden')
except OSError as exc:
    assert exc.errno == errno.EROFS
else:
    raise AssertionError('verification modified candidate')
for address in [('127.0.0.1', {tcp.getsockname()[1]}), ('203.0.113.1', 443)]:
    with socket.socket() as stream:
        stream.settimeout(.2)
        try:
            stream.connect(address)
        except OSError:
            pass
        else:
            raise AssertionError('host/external TCP reachable')
with socket.socket(socket.AF_UNIX) as stream:
    try:
        stream.connect({str(unix_path)!r})
    except OSError:
        pass
    else:
        raise AssertionError('host Unix service reachable')
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as stream:
    try:
        stream.sendto(b'CANARY', ('127.0.0.1', {udp.getsockname()[1]}))
        stream.sendto(b'CANARY', ('203.0.113.1', 9))
    except OSError:
        pass
with socket.socket(socket.AF_UNIX) as local:
    local.bind('/tmp/verifier.sock')
async def asynchronous_check():
    await asyncio.sleep(.001)
asyncio.run(asynchronous_check())
libc = ctypes.CDLL(None, use_errno=True)
assert libc.ptrace(0,0,0,0) == -1 and ctypes.get_errno() == errno.EPERM
assert libc.unshare(0x40000000) == -1 and ctypes.get_errno() == errno.EPERM
Path('/tmp/result').write_text('temporary outputs work')
print('ISOLATED_VERIFIER_PASS')
'''
        argv = verification_command(work.root, box.workspace, '/usr/bin/python3 -I -c '+shlex.quote(program))
        result = subprocess.run(argv, env={**os.environ, 'SYNTHETIC_HOST_CREDENTIAL':'must-not-enter'},
                                capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        assert b'ISOLATED_VERIFIER_PASS' in result.stdout
        with pytest.raises(socket.timeout):
            udp.recvfrom(1024)
        assert work.snapshot_digest() == baseline
        assert (box.workspace/'source.txt').read_text() == 'before\n'


def test_verifier_rejects_special_files_before_start(tmp_path):
    box, work = writer_box(tmp_path, '#!/bin/sh\nexit 0\n')
    import os
    import socket
    with work, socket.socket(socket.AF_UNIX) as listener:
        root_fd = os.open(work.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            listener.bind(f'/proc/self/fd/{root_fd}/s')
        finally:
            os.close(root_fd)
        with pytest.raises(AcpStateError, match='regular_file_required'):
            verification_command(work.root, box.workspace, '/usr/bin/true')
