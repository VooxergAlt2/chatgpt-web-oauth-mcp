#!/usr/bin/env python3
"""Operator-only parallel code-generation QA; generated code runs offline.

This does not grant repository mutation permission to an ACP session. Each
agent returns one Python source artifact. The trusted harness writes that
artifact into its own disposable directory and tests it in a minimal bwrap
sandbox without network, account profiles, repository or host-home access.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

from chatgpt_web_oauth_mcp.acp_sdk_transport import AcpSdkTransport
from chatgpt_web_oauth_mcp.acp_session import AcpRunLimits, AcpSessionRunner
from chatgpt_web_oauth_mcp.acp_session_store import AcpSessionIdentity, AcpSessionStore

TASKS = {
    "intervals": (
        ("Implement merge_intervals(items) in Python 3.12, standard library only. "
        "items must be a list of 2-item lists or tuples of finite int/float values; "
        "bool is not numeric. Reject bad input, NaN, infinity or a start greater "
        "than end with ValueError. Return a sorted list of tuples merging closed "
        "overlapping or touching intervals. Empty input gives []. Preserve the "
        "input, support negative and floating values. No IO or tools."),
        """
from solution import merge_intervals as f
import copy
x=[[4,6],[1,2],[2,5],[10,10]]; old=copy.deepcopy(x)
assert f(x)==[(1,6),(10,10)] and x==old
assert f([])==[]
assert f([(1,1),(1,1)])==[(1,1)]
assert f([(-5,-2),(-2.5,1),(7,9)])==[(-5,1),(7,9)]
assert f([(0.5,0.7),(0.7,1.2)])==[(0.5,1.2)]
assert f([(1,9),(2,3),(4,7)])==[(1,9)]
for bad in [None, {}, (1,2), [1], [(1,)], [(1,2,3)], [(2,1)], [(True,2)], [(1,False)], [(float('nan'),2)], [(0,float('inf'))], [('1',2)], [(0,None)]]:
    try: f(bad)
    except ValueError: pass
    else: raise AssertionError('invalid input accepted')
print('INDEPENDENT_TESTS_PASS intervals')
""",
    ),
    "dependency_layers": (
        ("Implement topological_layers(graph) in Python 3.12, standard library "
        "only. graph must be a dict mapping nonempty strings to lists of "
        "dependency names. Dependencies must be strings naming existing nodes. "
        "Duplicate dependencies count once. Return a list of sorted node-name "
        "lists, in parallel layers: a layer contains every remaining node "
        "whose dependencies were in earlier layers, not the current layer. "
        "Empty graph gives []. Reject invalid input, self-dependency and "
        "cycles with ValueError. Do not mutate input. No IO or tools."),
        """
from solution import topological_layers as f
import copy
x={'deploy':['test'], 'test':['build'], 'docs':[], 'build':['base','base'], 'base':[]}; old=copy.deepcopy(x)
assert f(x)==[['base','docs'],['build'],['test'],['deploy']] and x==old
assert f({})==[]
assert f({'b':[],'a':[]})==[['a','b']]
assert f({'a':['c'],'b':['c'],'c':[],'d':['a','b']})==[['c'],['a','b'],['d']]
assert f({'a':[],'b':['a'],'c':['b']})==[['a'],['b'],['c']]
for bad in [None, [], {'':[]}, {1:[]}, {'a':()}, {'a':['missing']}, {'a':[1]}, {'a':['a']}, {'a':['b'],'b':['a']}, {'a':[], 'b':['c'],'c':['b']}]:
    try: f(bad)
    except ValueError: pass
    else: raise AssertionError('invalid graph accepted')
print('INDEPENDENT_TESTS_PASS dependency_layers')
""",
    ),
}


def verify(source: str, tests: str, directory: Path) -> dict:
    if not isinstance(source, str) or not 10 < len(source.encode()) < 32768:
        raise ValueError('Invalid generated source artifact')
    (directory / 'solution.py').write_text(source)
    # -I ignores cwd on sys.path. Add ONLY the isolated disposable workspace.
    (directory / 'verify.py').write_text('import sys\nsys.path.insert(0,"/workspace")\n' + tests)
    argv = [
        '/usr/bin/prlimit', '--cpu=8', '--as=536870912', '--fsize=1048576', '--nofile=128', '--',
        '/usr/bin/bwrap', '--die-with-parent', '--unshare-all', '--new-session',
        '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
        '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
        '--dev', '/dev', '--proc', '/proc', '--tmpfs', '/tmp',
        '--ro-bind', str(directory), '/workspace', '--chdir', '/workspace',
        '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin',
        '--', '/usr/bin/python3', '-I', '-B', '/workspace/verify.py',
    ]
    completed = subprocess.run(argv, capture_output=True, timeout=20, check=False)
    return {
        'passed': completed.returncode == 0 and b'INDEPENDENT_TESTS_PASS' in completed.stdout,
        'exit_code': completed.returncode,
        'source_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'stdout': completed.stdout.decode(errors='replace')[-1000:],
        'stderr': completed.stderr.decode(errors='replace')[-1500:],
    }


async def main(spec_path: Path, qa_root: Path) -> None:
    os.umask(0o077)
    qa_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    spec = json.loads(spec_path.read_text())
    transport = AcpSdkTransport(tuple(spec['argv']), Path(spec['cwd']), spec['environment'])
    store = AcpSessionStore(qa_root / 'sessions')
    runner = AcpSessionRunner(store, transport)
    async def exercise(name: str, contract: str, tests: str) -> dict:
        identity = AcpSessionIdentity(
            owner='coding-qa-' + name, account='authorized-acp-qa',
            workspace=str(transport.cwd.resolve()), thinking='medium',
            scope_digest=hashlib.sha256(contract.encode()).hexdigest(),
        )
        started = time.monotonic()
        result = await runner.run(
            reference=secrets.token_hex(16), identity=identity, turn_id='implement',
            prompt=contract + ' Return ONLY a JSON object with keys status="succeeded", '
                   'summary, findings (array), source (the complete Python module as a string).',
            limits=AcpRunLimits(timeout_seconds=240, max_tool_calls=2),
        )
        output = {'case': name, 'status': result['status'], 'error': result.get('error'),
                  'model_seconds': round(time.monotonic() - started, 3)}
        if result.get('success'):
            text = result.get('answer', '').strip()
            if text.startswith('```'):
                text = text.split('\n', 1)[1].rsplit('```', 1)[0]
            artifact = json.loads(text)
            directory = Path(tempfile.mkdtemp(prefix=name + '-', dir=qa_root))
            output['verification'] = await asyncio.to_thread(verify, artifact['source'], tests, directory)
        print('PARALLEL_CODING_RESULT', json.dumps(output), flush=True)
        return output
    started = time.monotonic()
    outputs = await asyncio.gather(*(exercise(name, *values) for name, values in TASKS.items()))
    report = {'parallel_wall_seconds': round(time.monotonic() - started, 3), 'cases': outputs}
    (qa_root / 'report.json').write_text(json.dumps(report, indent=2))
    assert all(x.get('verification', {}).get('passed') for x in outputs), report
    print('PARALLEL_CODING_ACCEPTANCE_PASS', json.dumps({'wall_seconds': report['parallel_wall_seconds']}), flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--launch-spec', type=Path, required=True)
    p.add_argument('--qa-root', type=Path, required=True)
    a = p.parse_args()
    asyncio.run(main(a.launch_spec, a.qa_root))
