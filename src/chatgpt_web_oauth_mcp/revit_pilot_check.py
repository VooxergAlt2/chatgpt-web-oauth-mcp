"""A bounded native QA consumer of the canonical owner-bound workspace path.

No arbitrary caller code, grant issuance, model writes, GUI actions or retries.
Receipts live in the existing workspace ledger; re-entry only polls the same IDs.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .revit_workspaces import WorkspaceManager


STEPS = (
    ('none', {'code': 'return document.PathName;', 'transactionMode': 'none'}),
    ('auto', {'code': 'return document.PathName;', 'transactionMode': 'auto'}),
    ('error_path', {'code': 'throw new System.InvalidOperationException("QA_EXPECTED_PILOT_FAILURE");',
                    'transactionMode': 'auto'}),
    ('after_error', {'code': 'return document.PathName;', 'transactionMode': 'none'}),
)


async def run_pilot_check(manager: 'WorkspaceManager', workspace_id: str, run_id: str,
                          *, budget_seconds: float = 18.0) -> dict:
    from .revit_workspaces import TERMINAL, document_key

    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', run_id):
        raise ValueError('INVALID_PILOT_RUN_ID')
    owner = manager._owner()
    prefix = 'pilot:' + workspace_id + ':' + run_id + ':'
    ids = {name: prefix + name for name, _ in STEPS}
    if any(len(rid) > 128 for rid in ids.values()):
        raise ValueError('PILOT_REQUEST_ID_TOO_LONG')
    lifecycle_ids = {'bind:' + workspace_id, 'unbind:' + workspace_id}
    allowed_ids = set(ids.values()) | lifecycle_ids
    with manager._state() as state:
        row = manager._owned(state, workspace_id, owner)
        manager._no_ui(state)
        if 'native_authorization_v1' not in row['instance'].get('capabilities', []):
            raise ValueError('PROTECTED_WORKSPACE_REQUIRED')
        if row['state'] not in {'prepared', 'binding', 'ready', 'releasing', 'released', 'unknown', 'fenced'}:
            raise ValueError('PILOT_WORKSPACE_STATE_UNSAFE')
        if row['state'] in {'unknown', 'fenced'} and not any(rid in row['requests'] for rid in allowed_ids):
            raise ValueError('PILOT_WORKSPACE_STATE_UNSAFE')
        if row['state'] == 'released' and not set(ids.values()).issubset(row['requests']):
            raise ValueError('PILOT_RUN_NOT_FOUND')
        if any(item['status'] not in TERMINAL and rid not in allowed_ids
               for rid, item in row['requests'].items()):
            raise ValueError('REQUEST_UNCERTAIN: existing non-pilot work must be reconciled')
        if any(wid != workspace_id and manager._outstanding(other)
               for wid, other in state['workspaces'].items()):
            raise ValueError('DESKTOP_BUSY: another workspace has outstanding native work')
        path = row['document_path']

    def current() -> dict:
        with manager._state() as state:
            return manager._public(manager._owned(state, workspace_id, owner))

    def report(phase: str, *, reason: str | None = None) -> dict:
        row = current()
        return {
            'kind': 'RevitPilotCheckV1', 'workspace_id': workspace_id, 'run_id': run_id,
            'phase': phase, 'workspace_state': row['state'],
            'steps': {name: row['requests'].get(rid, {}).get('status', 'not_submitted')
                      for name, rid in ids.items()},
            'request_ids': ids, 'reason': reason, 'pilot_open': False,
            'model_mutation_tested': False,
            'operator_revoke_required': phase == 'ready_for_revoke',
        }

    async def terminal(rid: str) -> dict:
        while True:
            receipt = current()['requests'].get(rid)
            if not receipt:
                raise ValueError('PILOT_RECEIPT_MISSING')
            if receipt['status'] in TERMINAL:
                return receipt
            await manager.status(workspace_id, rid)
            await asyncio.sleep(0.05)

    try:
        async with asyncio.timeout(min(max(budget_seconds, 0.01), 18.0)):
            row = current()
            if row['state'] == 'prepared':
                await manager.bind_prepared(workspace_id)
            row = current()
            binding_id = 'bind:' + workspace_id
            if binding_id in row['requests'] and row['requests'][binding_id]['status'] not in TERMINAL:
                bind = await terminal('bind:' + workspace_id)
                if bind['status'] != 'succeeded':
                    return report('failed', reason='NATIVE_BIND_FAILED')
            for name, arguments in STEPS:
                rid = ids[name]
                row = current()
                expected_fingerprint = hashlib.sha256(json.dumps(
                    ['send_code_to_revit', arguments], sort_keys=True, allow_nan=False).encode()).hexdigest()
                existing = row['requests'].get(rid)
                if existing and existing.get('fingerprint') != expected_fingerprint:
                    return report('failed', reason='PILOT_REQUEST_CONTENT_MISMATCH')
                if rid not in row['requests']:
                    # submit() reserves once, validates the owner and exact
                    # native instance, and preserves uncertainty across I/O.
                    await manager.submit(workspace_id, 'send_code_to_revit', arguments, rid)
                receipt = await terminal(rid)
                if receipt['status'] == 'rejected':
                    return report('approval_required', reason='REJECTED_WITHOUT_DISPATCH_USE_NEW_RUN_ID_AFTER_APPROVAL')
                if name == 'error_path':
                    # This tests a failure path and worker recovery, NOT an
                    # actually mutated parameter rollback (accepted separately).
                    if (receipt['status'] != 'failed' or not isinstance(receipt.get('error'), str)
                        or 'Failed to execute code: Execution failed:' not in receipt['error']):
                        return report('failed', reason='EXPECTED_FAILURE_NOT_OBSERVED')
                else:
                    outcome = receipt.get('result')
                    if (receipt['status'] != 'succeeded' or not isinstance(outcome, dict)
                        or outcome.get('success') is not True or not isinstance(outcome.get('result'), str)):
                        return report('failed', reason='NATIVE_CODE_RESULT_INVALID')
                    try:
                        returned_path = json.loads(outcome['result'])
                        same_path = document_key(returned_path) == document_key(path)
                    except (ValueError, TypeError):
                        same_path = False
                    if not same_path:
                        return report('failed', reason='NATIVE_DOCUMENT_PATH_MISMATCH')
            row = current()
            if row['state'] == 'ready':
                await manager.release(workspace_id)
            unbind = await terminal('unbind:' + workspace_id)
            if unbind['status'] != 'succeeded' or current()['state'] != 'released':
                return report('failed', reason='NATIVE_UNBIND_NOT_CONFIRMED')
            return report('ready_for_revoke')
    except TimeoutError:
        return report('pending', reason='BOUNDED_WAIT_ENDED_POLL_SAME_RUN_NO_REPLAY')
    except ValueError as exc:
        # Only the manager's exact pre-bind refusal denotes approval needed.
        # Text inside arbitrary backend errors never proves non-dispatch.
        if str(exc) == 'AUTHORIZATION_REQUIRED':
            return report('approval_required', reason='MANUAL_QA_FULL_REQUIRED')
        return report('blocked', reason='INSPECT_RECORDED_REQUESTS_NO_REPLAY')
