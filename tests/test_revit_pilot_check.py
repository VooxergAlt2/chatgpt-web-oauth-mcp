"""Bounded fixed-scope pilot acceptance through existing workspace operations."""
import asyncio
from copy import deepcopy
import json

import pytest
from fastmcp import Client, FastMCP

from test_revit_workspaces import setup, create
from test_revit_dispatch_rejection import receipt as rejected_receipt
from chatgpt_web_oauth_mcp.revit_pilot_check import run_pilot_check, STEPS
from chatgpt_web_oauth_mcp.tool_groups import register_tool_groups


async def fixture(tmp_path, *, approved=True):
    manager, backend, owner = setup(tmp_path)
    backend.instance['capabilities'].append('native_authorization_v1')
    row = await create(manager)
    wid = row['workspace_id']
    if approved:
        backend.authorized_workspace_ids.add(wid)
    backend.submit_status = 'succeeded'
    original = backend.dispatch

    async def dispatch(instance, operation, payload):
        result = await original(instance, operation, payload)
        if operation == 'submit':
            name = payload['request_id'].rsplit(':', 1)[-1]
            if name == 'error_path':
                result.update(status='failed', result=None,
                              error='Failed to execute code: Execution failed: invoked method threw')
            else:
                result.update(result={'success': True, 'result': json.dumps(backend.paths[wid])})
            backend.requests[payload['request_id']] = deepcopy(result)
        return result
    backend.dispatch = dispatch
    return manager, backend, owner, wid


def test_one_call_native_smoke_binds_checks_and_unbinds_without_opening_pilot(tmp_path):
    async def check():
        manager, backend, _, wid = await fixture(tmp_path)
        result = await manager.pilot_check(wid, 'run1')
        assert result['phase'] == 'ready_for_revoke'
        assert result['workspace_state'] == 'released'
        assert result['operator_revoke_required'] is True
        assert result['pilot_open'] is False and result['model_mutation_tested'] is False
        assert result['steps'] == {'none': 'succeeded', 'auto': 'succeeded',
                                   'error_path': 'failed', 'after_error': 'succeeded'}
        submits = [p for op, p in backend.calls if op == 'submit']
        assert len(submits) == 4 and all(p['command'] == 'send_code_to_revit' for p in submits)
        assert [p['arguments'] for p in submits] == [args for _, args in STEPS]
        assert [op for op, _ in backend.calls].count('unbind') == 1
        calls = deepcopy(backend.calls)
        assert (await manager.pilot_check(wid, 'run1'))['phase'] == 'ready_for_revoke'
        assert backend.calls == calls, 'same completed run is evidence, not execution'
        with pytest.raises(ValueError, match='RUN_NOT_FOUND'):
            await manager.pilot_check(wid, 'another')
    asyncio.run(check())


def test_no_manual_grant_does_not_create_an_unknown_bind(tmp_path):
    async def check():
        manager, backend, _, wid = await fixture(tmp_path, approved=False)
        result = await manager.pilot_check(wid, 'no-grant')
        assert result['phase'] == 'approval_required'
        assert (await manager.list())['workspaces'][0]['requests'] == {}
        assert not [op for op, _ in backend.calls if op in {'bind', 'submit', 'unbind'}]
    asyncio.run(check())


def test_unrelated_unknown_request_is_never_reclassified_or_ignored(tmp_path):
    async def check():
        manager, backend, _, wid = await fixture(tmp_path)
        await manager.bind_prepared(wid)
        backend.timeout_submit = True
        with pytest.raises(TimeoutError):
            await manager.submit(wid, 'get_phases', {}, 'historical-unknown')
        before = deepcopy(backend.calls)
        with pytest.raises(ValueError, match='REQUEST_UNCERTAIN'):
            await manager.pilot_check(wid, 'new-run')
        assert backend.calls == before
        assert (await manager.list())['workspaces'][0]['requests']['historical-unknown']['status'] == 'unknown'
    asyncio.run(check())


def test_lost_reply_can_be_reconciled_without_second_submit(tmp_path):
    async def check():
        manager, backend, _, wid = await fixture(tmp_path)
        backend.timeout_submit = True
        first = await manager.pilot_check(wid, 'lost')
        assert first['phase'] == 'pending'
        rid = first['request_ids']['none']
        backend.timeout_submit = False
        backend.requests[rid].update(status='succeeded', result={
            'success': True, 'result': json.dumps(backend.paths[wid])})
        second = await manager.pilot_check(wid, 'lost')
        assert second['phase'] == 'ready_for_revoke'
        assert len([p for op, p in backend.calls if op == 'submit' and p['request_id'] == rid]) == 1
    asyncio.run(check())


@pytest.mark.parametrize('bad_result', [
    {'success': False, 'result': '"ok"'},
    {'success': True, 'result': '"C:\\\\Other\\\\model.rvt"'},
    {'success': True, 'result': '{}'},
    {'success': True, 'result': 'not-json'},
])
def test_transport_success_is_not_native_acceptance(tmp_path, bad_result):
    async def check():
        manager, backend, _, wid = await fixture(tmp_path)
        original = backend.dispatch
        async def dispatch(instance, operation, payload):
            result = await original(instance, operation, payload)
            if operation == 'submit':
                result['result'] = bad_result
            return result
        backend.dispatch = dispatch
        result = await manager.pilot_check(wid, 'bad')
        assert result['phase'] == 'failed'
        assert not [op for op, _ in backend.calls if op == 'unbind']
        assert len([op for op, _ in backend.calls if op == 'submit']) == 1
    asyncio.run(check())


def test_foreign_owner_ui_fence_and_unprotected_host_block_before_execution(tmp_path):
    async def check():
        manager, backend, owner, wid = await fixture(tmp_path)
        owner[0] = 'openai:other'
        with pytest.raises(ValueError, match='NOT_OWNED'):
            await manager.pilot_check(wid, 'foreign')
        owner[0] = 'openai:owner-a'
        lease = await manager.acquire_ui(wid)
        with pytest.raises(ValueError, match='DESKTOP_BUSY'):
            await manager.pilot_check(wid, 'ui')
        await manager.release_ui(wid, lease['token'])
        with manager._state() as state:
            state['workspaces'][wid]['instance']['capabilities'].remove('native_authorization_v1')
        with pytest.raises(ValueError, match='PROTECTED_WORKSPACE_REQUIRED'):
            await manager.pilot_check(wid, 'legacy')
    asyncio.run(check())


def test_small_time_budget_stops_polling_and_same_id_resumes(tmp_path):
    async def check():
        manager, backend, _, wid = await fixture(tmp_path)
        original = backend.dispatch
        async def dispatch(instance, operation, payload):
            result = await original(instance, operation, payload)
            if operation == 'submit':
                result['status'] = 'queued'
                backend.requests[payload['request_id']] = deepcopy(result)
            return result
        backend.dispatch = dispatch
        result = await run_pilot_check(manager, wid, 'bounded', budget_seconds=0.02)
        assert result['phase'] == 'pending'
        rid = result['request_ids']['none']
        assert result['steps']['none'] in {'queued', 'running', 'unknown'}
        assert not [op for op, _ in backend.calls if op == 'unbind']
        backend.dispatch = original
        backend.requests[rid]['status'] = 'succeeded'
        assert (await manager.pilot_check(wid, 'bounded'))['phase'] == 'ready_for_revoke'
    asyncio.run(check())


def test_expired_grant_rejection_is_not_retried_by_the_smoke_runner(tmp_path):
    async def check():
        manager, backend, _, wid = await fixture(tmp_path)
        original = backend.dispatch
        denials = []
        async def dispatch(instance, operation, payload):
            if operation == 'submit':
                denials.append(payload['request_id'])
                return rejected_receipt(backend, wid, payload['request_id'])
            return await original(instance, operation, payload)
        backend.dispatch = dispatch
        result = await manager.pilot_check(wid, 'expired')
        assert result['phase'] == 'approval_required'
        assert result['steps']['none'] == 'rejected'
        assert not [op for op, _ in backend.calls if op == 'unbind']
        backend.dispatch = original
        assert (await manager.pilot_check(wid, 'expired'))['phase'] == 'approval_required'
        assert len(denials) == 1
        assert (await manager.pilot_check(wid, 'explicit-new-attempt'))['phase'] == 'ready_for_revoke'
    asyncio.run(check())


def test_preexisting_id_with_other_code_cannot_be_accepted_as_qa_result(tmp_path):
    async def check():
        manager, backend, _, wid = await fixture(tmp_path)
        await manager.bind_prepared(wid)
        rid = 'pilot:' + wid + ':collision:none'
        await manager.submit(wid, 'send_code_to_revit', {'code': 'return 123;', 'transactionMode': 'none'}, rid)
        before = deepcopy(backend.calls)
        result = await manager.pilot_check(wid, 'collision')
        assert result['phase'] == 'failed' and result['reason'] == 'PILOT_REQUEST_CONTENT_MISMATCH'
        assert backend.calls == before
    asyncio.run(check())


def test_authorization_failure_cannot_pass_as_expected_code_exception(tmp_path):
    async def check():
        manager, backend, _, wid = await fixture(tmp_path)
        original = backend.dispatch
        async def dispatch(instance, operation, payload):
            result = await original(instance, operation, payload)
            if operation == 'submit' and payload['request_id'].endswith(':error_path'):
                result.update(error='Authorization revoked before execution')
            return result
        backend.dispatch = dispatch
        result = await manager.pilot_check(wid, 'wrong-error')
        assert result['phase'] == 'failed'
        assert result['steps']['after_error'] == 'not_submitted'
        assert not [op for op, _ in backend.calls if op == 'unbind']
    asyncio.run(check())


def test_pilot_tool_uses_the_existing_three_router_surface(tmp_path):
    async def check():
        manager, backend, _, wid = await fixture(tmp_path)
        remote, server = FastMCP('fixture remote'), FastMCP('public routers')
        register_tool_groups(server, transport_factory=lambda: remote, workspace_manager=manager)
        async with Client(server) as client:
            assert {t.name for t in await client.list_tools()} == {
                'tool_group_select', 'tool_group_catalog', 'tool_group_call'}
            result = await client.call_tool('tool_group_call', {
                'group': 'revit', 'name': 'revit_workspace_pilot_check',
                'arguments': {'workspace_id': wid, 'run_id': 'wire'},
            })
            assert result.structured_content['phase'] == 'ready_for_revoke'
            assert result.structured_content['pilot_open'] is False
            before = deepcopy(backend.calls)
            with pytest.raises(Exception, match='Additional properties'):
                await client.call_tool('tool_group_call', {
                    'group': 'revit', 'name': 'revit_workspace_pilot_check',
                    'arguments': {'workspace_id': wid, 'run_id': 'wire', 'code': 'unapproved code'},
                })
            assert backend.calls == before
    asyncio.run(check())
