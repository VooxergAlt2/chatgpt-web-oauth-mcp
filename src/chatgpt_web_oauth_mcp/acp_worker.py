"""Durable-job child for an operator-configured ACP backend. No MCP server loop."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import resource
import signal
import sys
import tempfile
from pathlib import Path

from .acp_backend import AcpBackend
from .acp_sandbox import AcpSandbox
from .acp_session import AcpRunLimits, AcpSessionRunner
from .acp_session_store import AcpSessionIdentity, AcpSessionStore, AcpStateError


def emit(event: str, value: dict):
    print(json.dumps({'event':event, **value},ensure_ascii=False),flush=True)


async def execute(config_path: Path, request: dict) -> dict:
    backend = AcpBackend.load(config_path)
    if not isinstance(request, dict) or set(request) != {'delegate_id','account','owner','workspace','thinking','scope_digest','session_ref','turn_id','prompt','timeout','max_tool_calls'}:
        raise AcpStateError('acp_worker_request_invalid')
    delegate_id = request['delegate_id']
    cancel_path = backend.cancel_path(delegate_id)
    # A continuation has a new delegate/turn ID but owns the same native history.
    # Validate before constructing any filesystem paths or sandbox directories.
    reference = request['session_ref']
    AcpSessionStore.validate_reference(reference)
    workspace = Path(request['workspace']).resolve(strict=True)
    backend.validate(account=request['account'],workspace=workspace,kind='explore',model=None,timeout=request['timeout'])
    identity = AcpSessionIdentity(owner=request['owner'],account=request['account'],workspace=str(workspace),
        scope_digest=request['scope_digest'],thinking=request['thinking'])
    cancel = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig,cancel.set)
    async def watch_cancel():
        while not cancel.is_set():
            if cancel_path.exists():
                cancel.set()
                return
            await asyncio.sleep(0.1)
    watcher = asyncio.create_task(watch_cancel())
    try:
        with tempfile.TemporaryDirectory(prefix='opss-acp-helper-') as temporary:
            control = Path(temporary); control.chmod(0o700)
            box = AcpSandbox(backend.server,backend.helper,backend.accounts[request['account']]['profile'],workspace,control,
                             backend.state_root/'conversations'/reference)
            runner = AcpSessionRunner(AcpSessionStore(backend.state_root/'sessions'),box.transport())
            result = await runner.run(
                reference=reference,
                identity=identity,turn_id=request['turn_id'],prompt=request['prompt'],
                limits=AcpRunLimits(timeout_seconds=request['timeout']-3,cancel_grace_seconds=5,max_tool_calls=request['max_tool_calls']),
                cancel=cancel,progress=lambda data:emit('acp_progress',{'progress':data}),
            )
            return result
    finally:
        watcher.cancel()
        await asyncio.gather(watcher,return_exceptions=True)
        cancel_path.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(196609)
        if len(raw)>196608:
            raise AcpStateError('acp_request_too_large')
        result=asyncio.run(execute(args.config,json.loads(raw)))
    except AcpStateError as exc:
        result={'success':False,'status':'failed','error':{'code':exc.code}}
    except Exception:  # noqa: BLE001
        result={'success':False,'status':'failed','error':{'code':'acp_worker_failed'}}
    emit('acp_result',{'result':result})


if __name__=='__main__':
    main()
