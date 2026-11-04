"""Operator cleanup after proven process exit must not invent request outcomes."""
import asyncio
from copy import deepcopy
import json

import pytest

from test_revit_workspaces import setup, create
from test_revit_dispatch_rejection import receipt
from chatgpt_web_oauth_mcp.revit_workspace_backend import verified_identity
from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager, workspace_tools


async def stopped_fixture(tmp_path):
    manager, backend, owner = setup(tmp_path)
    manager.clock = lambda: 1000.0
    row = await create(manager)
    wid = row['workspace_id']
    backend.timeout_submit = True
    with pytest.raises(TimeoutError):
        await manager.submit(wid, 'get_phases', {}, 'historic-unknown')
    observed = {
        'kind': 'RevitStoppedProcessObservationV1',
        'observed_at': 1000.0,
        'workspace_id': wid,
        'native_identity': verified_identity(backend.instance),
        'windows_process_tool': 'windows_Process:list:all',
        'process_inventory_complete': True,
        'revit_processes': [],
        'native_discovery_tool': 'revit_workspace_instances',
        'native_instances': [],
    }
    return manager, backend, owner, wid, observed


def retire(manager, wid, observation, **kwargs):
    return manager.operator_retire_stopped_workspace(
        wid, observation=observation,
        confirm_process_stopped=True, confirm_unknown_outcome=True,
        reason='Operator ended only the disposable QA session without saving.',
        **kwargs,
    )


def test_retirement_is_local_audited_and_retains_every_request(tmp_path):
    async def check():
        manager, backend, owner, wid, observed = await stopped_fixture(tmp_path)
        before = json.loads(manager.path.read_text())
        calls = deepcopy(backend.calls)
        assert retire(manager, wid, observed)['dry_run'] is True
        assert json.loads(manager.path.read_text()) == before
        result = retire(manager, wid, observed, apply=True)
        assert result['state'] == 'retired'
        assert result['unknown_request_ids'] == ['historic-unknown']
        assert result['native_completion_confirmed'] is False
        after = json.loads(manager.path.read_text())
        assert after['workspaces'][wid]['requests'] == before['workspaces'][wid]['requests']
        assert len(after['workspace_retirement_audit']) == 1
        assert backend.calls == calls, 'retirement must not send to any native endpoint'
        restarted = WorkspaceManager(manager.path, backend, owner_provider=lambda: owner[0])
        row = await restarted.status(wid, 'historic-unknown')
        assert row['requests']['historic-unknown']['status'] == 'unknown'
        assert row['retirement']['native_completion_confirmed'] is False
        assert 'owner_proof' not in json.dumps(row)
        assert not manager._outstanding(row)
        manager.require_unscoped_allowed()
        assert (await restarted.release(wid))['state'] == 'retired'
        late = {**backend.requests['historic-unknown'], 'status': 'succeeded'}
        with pytest.raises(ValueError, match='RETIRED'):
            restarted._record(wid, owner[0], late, request_id='historic-unknown')
        with pytest.raises(ValueError, match='RETIRED'):
            restarted._record_not_sent(wid, owner[0], receipt(backend, wid, 'historic-unknown'), 'historic-unknown')
        with pytest.raises(ValueError, match='RETIRED'):
            await restarted.acquire_ui(wid)
        with pytest.raises(ValueError, match='RETIRED'):
            await restarted.cancel(wid, 'historic-unknown')
        with pytest.raises(ValueError, match='WORKSPACE_NOT_READY'):
            await restarted.submit(wid, 'get_phases', {}, 'new-request')
        assert backend.calls == calls
        owner[0] = 'openai:foreign'
        with pytest.raises(ValueError, match='NOT_OWNED'):
            await restarted.status(wid)
        assert not any('retire' in tool.name for tool in workspace_tools())
    asyncio.run(check())


@pytest.mark.parametrize('change', [
    {'revit_processes': [{'pid': 51}]}, {'native_instances': [{'pid': 51}]},
    {'observed_at': 900.0}, {'observed_at': 1050.0}, {'observed_at': True},
    {'windows_process_tool': 'invented'}, {'native_discovery_tool': 'raw_tcp'},
    {'process_inventory_complete': False},
    {'workspace_id': 'another-workspace'}, {'extra': 'field'},
    {'native_identity': {'pid': 52}},
])
def test_live_stale_incomplete_or_uncorrelated_proof_cannot_retire(tmp_path, change):
    async def check():
        manager, _, _, wid, observed = await stopped_fixture(tmp_path)
        before = manager.path.read_bytes()
        with pytest.raises(ValueError):
            retire(manager, wid, {**observed, **change}, apply=True)
        assert manager.path.read_bytes() == before
    asyncio.run(check())


def test_retirement_refuses_ui_fence_and_missing_operator_acknowledgment(tmp_path):
    async def check():
        manager, _, _, wid, observed = await stopped_fixture(tmp_path)
        with pytest.raises(ValueError, match='OPERATOR'):
            manager.operator_retire_stopped_workspace(
                wid, observation=observed, apply=True,
                confirm_process_stopped=False, confirm_unknown_outcome=True, reason='No approval',
            )
        with manager._state() as state:
            state['ui'] = {'state': 'draining', 'owner': 'openai:foreign'}
        with pytest.raises(ValueError, match='DESKTOP_BUSY'):
            retire(manager, wid, observed, apply=True)
    asyncio.run(check())


def test_stopped_native_verifying_fence_retires_only_with_exact_attestation(tmp_path):
    async def check():
        manager, backend, owner, wid, observed = await stopped_fixture(tmp_path)
        with manager._state() as state:
            row = state['workspaces'][wid]
            row['requests']['activate:dead-revit'] = {
                'status': 'unknown', 'request_id': 'activate:dead-revit'
            }
            state['ui'] = {
                'state': 'verifying', 'workspace_id': wid, 'owner': row['owner'],
                'token': 'scoped-lease-token', 'activation_request_id': 'activate:dead-revit',
            }
        before = json.loads(manager.path.read_text())
        with pytest.raises(ValueError, match='EXPLICIT_STOPPED_SCOPED_UI'):
            retire(manager, wid, observed, apply=True)
        with pytest.raises(ValueError, match='NATIVE_PROCESS_STOP_NOT_VERIFIED'):
            manager.operator_retire_stopped_workspace(
                wid, observation={**observed, 'revit_processes': [{'pid': backend.instance['pid']}]},
                confirm_process_stopped=True, confirm_unknown_outcome=True,
                confirm_stopped_scoped_ui=True,
                reason='Operator confirmed this stopped disposable QA process',
                apply=True,
            )
        assert json.loads(manager.path.read_text()) == before
        result = manager.operator_retire_stopped_workspace(
            wid, observation=observed, confirm_process_stopped=True,
            confirm_unknown_outcome=True, confirm_stopped_scoped_ui=True,
            reason='Operator reviewed full Windows process inventory and native discovery',
            apply=True,
        )
        assert result['stopped_scoped_ui_retired'] is True
        after = json.loads(manager.path.read_text())
        assert after['ui'] is None
        assert after['workspaces'][wid]['state'] == 'retired'
        assert after['workspaces'][wid]['requests'] == before['workspaces'][wid]['requests']
        receipt = after['workspaces'][wid]['retirement']
        assert receipt['native_ui_completion_confirmed'] is False
        assert receipt['stopped_scoped_ui_state'] == 'verifying'
        manager.require_unscoped_allowed()
        with pytest.raises(ValueError, match='RETIRED'):
            await manager.acquire_ui(wid)
    asyncio.run(check())
