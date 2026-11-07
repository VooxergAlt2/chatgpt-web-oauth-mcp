"""Runtime profile integration uses real namespaces with a simulated native turn."""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from chatgpt_web_oauth_mcp import acp_worker
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
from chatgpt_web_oauth_mcp.acp_sandbox import verification_command
from chatgpt_web_oauth_mcp.delegate_code_verification import _run_verification_commands
from test_acp_writer_worker import (
    AFTER, BEFORE, _forbid_native_construction, assert_unpromoted, execute, setup_worker,
)

pytestmark = pytest.mark.skipif(not Path('/usr/bin/bwrap').exists(), reason='Linux bwrap required')
MOUNT = '/opt/opss-verification'
CANARY = "VALUE = 'snapshot-only'\n"


def runtime_profile(tmp_path):
    from chatgpt_web_oauth_mcp.acp_verification_runtime import VerificationRuntime
    from test_acp_verification_runtime import make_runtime

    root = tmp_path / 'verification-runtime-source'
    runtime = VerificationRuntime.load(make_runtime(root, real_python=True))
    module = next((root / 'lib').glob('python*/site-packages/opss_runtime_canary.py'))
    return runtime, module


def python_command(program):
    return 'python -I -c ' + shlex.quote(program)


def test_real_runtime_namespace_is_readonly_private_and_uses_pinned_python(tmp_path, monkeypatch):
    runtime, module = runtime_profile(tmp_path)
    fixture = setup_worker(tmp_path, monkeypatch, verification_runtime=runtime)
    writer = acp_worker.AcpWorkspace.prepare(
        fixture.source, ['calc.py'], fixture.state / 'workspaces',
    )
    host_secret = tmp_path / 'host-credential'
    host_secret.write_text('SYNTHETIC_HOST_SECRET')
    monkeypatch.setenv('SYNTHETIC_HOST_CREDENTIAL', 'must-not-enter')
    host_network = str(Path('/proc/self/ns/net').readlink())
    host_mount = str(Path('/proc/self/ns/mnt').readlink())
    with writer:
        staged = runtime.stage(writer.root.parent / 'verification-runtime')
        program = f'''
import errno, os, sys
from pathlib import Path
import opss_runtime_canary
assert sys.executable == {MOUNT + '/bin/python'!r}
assert sys.prefix == {MOUNT!r}
assert opss_runtime_canary.VALUE == 'snapshot-only'
assert Path(opss_runtime_canary.__file__).is_relative_to(Path({MOUNT!r}))
assert os.environ['PATH'].split(':')[0] == {MOUNT + '/bin'!r}
assert os.environ['VIRTUAL_ENV'] == {MOUNT!r}
assert 'SYNTHETIC_HOST_CREDENTIAL' not in os.environ
assert not Path({str(runtime.root)!r}).exists()
assert not Path({str(host_secret)!r}).exists()
assert not Path('/profile').exists()
assert not Path('/control').exists()
assert os.readlink('/proc/self/ns/net') != {host_network!r}
assert os.readlink('/proc/self/ns/mnt') != {host_mount!r}
for path in (Path(opss_runtime_canary.__file__), Path('calc.py')):
    try:
        path.write_text('forbidden')
    except OSError as exc:
        assert exc.errno == errno.EROFS
    else:
        raise AssertionError('verification mount was writable')
Path('/tmp/runtime-result').write_text('temporary output works')
print('PINNED_RUNTIME_NAMESPACE_PASS')
'''
        rows = _run_verification_commands(
            writer.root, [python_command(program)], timeout_seconds=10,
            argv_factory=lambda command: verification_command(
                writer.root, fixture.source, command, runtime=staged,
            ),
        )
        assert len(rows) == 1 and rows[0]['passed'], rows
        assert rows[0]['stdout'].strip() == 'PINNED_RUNTIME_NAMESPACE_PASS'
        assert module.read_text() == CANARY
    assert_unpromoted(fixture)


def test_worker_binds_same_runtime_before_and_after_native_in_receipt(tmp_path, monkeypatch):
    runtime, _ = runtime_profile(tmp_path)
    preflight = python_command("import opss_runtime_canary; assert opss_runtime_canary.VALUE == 'snapshot-only'; print('ENV_READY')")
    final = python_command("import runpy, opss_runtime_canary; assert opss_runtime_canary.VALUE == 'snapshot-only'; assert runpy.run_path('calc.py')['add'](2, 3) == 5; print('FINAL_READY')")
    fixture = setup_worker(
        tmp_path, monkeypatch, commands=[final], preflight_commands=[preflight], verification_runtime=runtime,
    )
    original_mutation = fixture.control.mutation

    def candidate_only_view(view):
        assert not (view / 'verification-runtime').exists()
        assert not list(view.rglob('opss_runtime_canary.py'))
        original_mutation(view)

    fixture.control.mutation = candidate_only_view
    result = execute(fixture)
    assert result['success'] is True, result
    assert fixture.control.check_calls == [
        {'commands': [preflight], 'opened': 0, 'native_constructed': False, 'candidate': BEFORE},
        {'commands': [final], 'opened': 1, 'native_constructed': True, 'candidate': AFTER},
    ]
    receipt = result['harness_metadata']['acp_code_receipt']
    assert receipt['verification_runtime'] == fixture.request['code_contract']['verification_runtime'] == runtime.identity()
    assert receipt['verification_preflight_commands'][0]['stdout'].strip() == 'ENV_READY'
    assert receipt['verification_commands'][0]['stdout'].strip() == 'FINAL_READY'
    assert (fixture.source / 'calc.py').read_text() == AFTER
    assert (fixture.main / 'calc.py').read_text() == BEFORE
    assert not list((fixture.state / 'workspaces').iterdir())


@pytest.mark.parametrize('binding', ['missing', 'changed'])
def test_worker_rejects_configured_runtime_mismatch_before_native(tmp_path, monkeypatch, binding):
    runtime, _ = runtime_profile(tmp_path)
    fixture = setup_worker(tmp_path, monkeypatch, verification_runtime=runtime)
    _forbid_native_construction(monkeypatch)
    if binding == 'missing':
        fixture.backend.verification_runtime = None
    else:
        fixture.request['code_contract']['verification_runtime']['manifest_sha256'] = 'f' * 64
    with pytest.raises(AcpStateError, match='acp_verification_runtime_binding_changed'):
        execute(fixture)
    assert fixture.control.checks == [] and fixture.control.views == []
    assert not (fixture.state / 'workspaces').exists()
    assert (fixture.source / 'calc.py').read_text() == BEFORE


def test_worker_rejects_tampered_source_runtime_before_native(tmp_path, monkeypatch):
    runtime, module = runtime_profile(tmp_path)
    fixture = setup_worker(tmp_path, monkeypatch, verification_runtime=runtime)
    _forbid_native_construction(monkeypatch)
    module.chmod(0o644)
    module.write_text('VALUE = "tampered"\n')
    module.chmod(0o444)
    with pytest.raises(AcpStateError, match='acp_verification_runtime'):
        execute(fixture)
    assert fixture.control.checks == [] and fixture.control.views == []
    assert_unpromoted(fixture)


def test_staged_runtime_tamper_rejects_promotion(tmp_path, monkeypatch):
    runtime, module = runtime_profile(tmp_path)
    command = python_command("import opss_runtime_canary; assert opss_runtime_canary.VALUE == 'snapshot-only'")
    fixture = setup_worker(tmp_path, monkeypatch, commands=[command], verification_runtime=runtime)

    def corrupt_stage():
        staged_module = fixture.control.views[0].parent / 'verification-runtime' / module.relative_to(runtime.root)
        staged_module.chmod(0o644)
        staged_module.write_text('VALUE = "staged-tamper"\n')
        staged_module.chmod(0o444)

    fixture.control.after_native = corrupt_stage
    result = execute(fixture)
    assert result['success'] is False
    assert result['error']['code'].startswith('acp_verification_runtime')
    assert 'acp_code_receipt' not in result['harness_metadata']
    assert fixture.control.opened == 1
    assert_unpromoted(fixture)


def test_changed_host_profile_cannot_change_already_staged_runtime(tmp_path, monkeypatch):
    runtime, module = runtime_profile(tmp_path)
    command = python_command("import runpy, opss_runtime_canary; assert opss_runtime_canary.VALUE == 'snapshot-only'; assert runpy.run_path('calc.py')['add'](2, 3) == 5")
    fixture = setup_worker(tmp_path, monkeypatch, commands=[command], verification_runtime=runtime)
    expected_identity = runtime.identity()

    def change_host_only():
        module.chmod(0o644)
        module.write_text('VALUE = "host-mutated-after-staging"\n')
        module.chmod(0o444)

    fixture.control.after_native = change_host_only
    result = execute(fixture)
    assert result['success'] is True, result
    assert module.read_text() == 'VALUE = "host-mutated-after-staging"\n'
    assert result['harness_metadata']['acp_code_receipt']['verification_runtime'] == expected_identity
    assert (fixture.source / 'calc.py').read_text() == AFTER
    assert not list((fixture.state / 'workspaces').iterdir())
