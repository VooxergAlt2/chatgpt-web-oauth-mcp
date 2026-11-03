"""ACP and CLI are transports of one account, never independent quota lanes."""
import hashlib
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from chatgpt_web_oauth_mcp.acp_backend import AcpBackend
from chatgpt_web_oauth_mcp.acp_identity import verify_binding
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError
from chatgpt_web_oauth_mcp.delegate_harnesses import AntigravityHarness
from chatgpt_web_oauth_mcp.executors import ExecutorRegistry
from chatgpt_web_oauth_mcp.shell import JobRegistry


def configuration(tmp_path):
    state = tmp_path/'state'; state.mkdir(mode=0o700)
    profile = tmp_path/'profile'; profile.mkdir(mode=0o700)
    nested = profile/'antigravity-acp'; nested.mkdir(mode=0o700)
    (nested/'settings.json').write_text(json.dumps({'auth':{'type':'oauth-personal'}}))
    token = nested/'acp_token.json'
    token.write_text(json.dumps({'client_id':'fixture-client','client_secret':'fixture-secret','refresh_token':'fixture-acp'}))
    token.chmod(0o600)
    cli_home = tmp_path/'cli-home'
    native = cli_home/'.gemini/antigravity-cli/antigravity-oauth-token'
    native.parent.mkdir(parents=True)
    native.write_text(json.dumps({'id_token_email':'qa@example.invalid','token':{'refresh_token':'fixture-cli'}}))
    native.chmod(0o600)
    with patch('chatgpt_web_oauth_mcp.acp_identity._google_identity', return_value='qa@example.invalid'):
        verify_binding(profile=profile, account='antigravity', cli_home=cli_home,
                       proof_root=state/'account-bindings')
    workspace = tmp_path/'workspace';workspace.mkdir()
    (workspace/'sample.py').write_text('def sample():\n    return 2\n')
    path = tmp_path/'config.json'
    data = {'version':1,'python':sys.executable,'server':'/bin/true','helper':'/bin/true',
        'state_root':str(state),'accounts':{'antigravity':{'profile':str(profile),'cli_home':str(cli_home),'workspace_roots':[str(workspace)]}}}
    path.write_text(json.dumps(data));path.chmod(0o600)
    return path, workspace, data


class FixtureAgyHarness(AntigravityHarness):
    def supports_read_only(self):
        return True

    def info(self):
        return {'available': True, 'explore_available': True, 'account_authenticated': True}

    def model_admission(self, model):
        return True, None


def registry(tmp_path):
    path, workspace, _data = configuration(tmp_path)
    r = ExecutorRegistry(codex_command=None,default_harness='antigravity',
        harnesses=[FixtureAgyHarness(name='antigravity',command='true',home_dir=tmp_path/'cli-home')],
        acp_backend=AcpBackend.load(path),automatic_routing=False,
        max_explore_per_project=3,max_explore_global=3,
        max_active_per_harness={'antigravity':1},
        durable_job_registry=JobRegistry(),durable_state_dir=tmp_path/'jobs',
        delegate_state_root=tmp_path/'delegates')
    r.quota_admission_gate=None
    return r,workspace


def test_private_optional_configuration_and_flash_account_contract(tmp_path):
    path, workspace, _data = configuration(tmp_path)
    backend = AcpBackend.load(path)
    assert set(backend.accounts)=={'antigravity'}
    assert backend.adapter('antigravity').info()['quota_pool']=='antigravity'
    backend.validate(account='antigravity',workspace=workspace,kind='explore',model='gemini-3.8-flash-medium',timeout=90)
    for kwargs, code in [({'kind':'code'},'code_execution'),({'account':'antigravity2'},'not_configured'),
                         ({'workspace':tmp_path},'not_allowed'),({'model':'claude'},'flash_only'),
                         ({'timeout':1200},'timeout')]:
        values={'account':'antigravity','workspace':workspace,'kind':'explore','model':None,'timeout':90,**kwargs}
        with pytest.raises(AcpStateError,match=code):backend.validate(**values)
    assert AcpBackend.load_optional('') is None
    path.chmod(0o644)
    assert AcpBackend.load_optional(str(path)) is None
    with pytest.raises(AcpStateError,match='not_private'):AcpBackend.load(path)


def test_acp_admission_fails_without_enabling_an_independent_harness(tmp_path):
    r,workspace=registry(tmp_path)
    for kwargs,code in [({'harness':None},'explicit_account'),({'kind':'code'},'code_execution'),
                        ({'resume_from_delegate_id':'a'*12},'acp_resume_source_unavailable'),
                        ({'output_schema':{}},'custom_schema')]:
        params={'task':'Inspect sample','cwd':workspace,'kind':'explore','harness':'antigravity',
                'backend':'acp','wait_seconds':0,**kwargs}
        result=r.run_delegate(**params)
        assert not result['success'] and code in result['error']['code']
    r.acp_backend=None
    denied=r.run_delegate(task='Inspect',cwd=workspace,kind='explore',harness='antigravity',backend='acp',wait_seconds=0)
    assert denied['error']['code']=='acp_backend_disabled'
    assert not r.scheduler.tasks


def test_cli_and_acp_share_account_capacity_and_deduplication_is_transport_aware(tmp_path,monkeypatch):
    r,workspace=registry(tmp_path)
    started=threading.Event();release=threading.Event();seen=[]
    def execute(*,delegate_task):
        seen.append((delegate_task.execution_backend,delegate_task.harness))
        started.set()
        assert release.wait(8)
        return {'status':'succeeded','success':True,'structured_output':{'status':'succeeded','summary':'ok','findings':[]},
                'execution_backend':delegate_task.execution_backend}
    monkeypatch.setattr(r,'_start_codex_delegate_impl',execute)
    first=r.run_delegate(task='Inspect sample',cwd=workspace,harness='antigravity',kind='explore',backend='cli',wait_seconds=0)
    assert started.wait(3)
    try:
        second=r.run_delegate(task='Inspect sample',cwd=workspace,harness='antigravity',kind='explore',backend='acp',wait_seconds=0)
        assert second.get('delegate_id'),second
        assert first['delegate_id']!=second['delegate_id']
        pending=r.scheduler.get_task(second['delegate_id'])
        assert pending.state=='queued'
        assert pending.execution_backend=='acp' and pending.harness=='antigravity'
        assert 'isolated read-only ACP research subagent' in pending.prompt
        assert 'status MUST be exactly "succeeded", "partial" or "blocked".' in pending.prompt
        assert '"next_execution_prompt":null' in pending.prompt
        assert 'native JSON Schema' not in pending.prompt
        assert 'Progress logging contract:' not in pending.prompt
        assert 'Antigravity shell contract:' not in pending.prompt
        assert 'Finish is exempt' not in pending.prompt
        assert seen==[('cli','antigravity')]
        persisted=json.loads(pending.log_paths.metadata.read_text())
        assert persisted['execution_backend']=='acp'
        invocation=r._build_invocation_for_task(pending)
        request=json.loads(invocation.stdin)
        assert request['account']=='antigravity'
        assert 'acp_worker' in invocation.args[2]
        assert invocation.read_only_enforced
        assert r._runtime_retry_reason(task=pending,result={'status':'failed','error':{'code':'acp_transport_error'}}) is None
        assert r._run_same_account_recovery(task=pending,failed_result={'status':'failed'}) is None
        assert r._agy_tool_budget_meter(pending) is None
    finally:
        release.set()
    assert r.scheduler.get_task(first['delegate_id']).completed_event.wait(5)
    assert pending.completed_event.wait(5)
    assert seen==[('cli','antigravity'),('acp','antigravity')]


def test_cli_cannot_resume_acp_conversation(tmp_path,monkeypatch):
    r,workspace=registry(tmp_path)
    monkeypatch.setattr(r,'_resume_source_snapshot',lambda _: {'completed':True,'execution_backend':'acp','harness':'antigravity'})
    conversation,error=r._resolve_resume_conversation(resume_from_delegate_id='a'*12,harness='antigravity',
        project=r.project_resolver.resolve(workspace),cwd=workspace,timeout=60)
    assert conversation is None
    assert error['error']['code']=='delegate_resume_backend_mismatch'


def test_acp_continuation_preserves_owner_account_and_source_scope(tmp_path, monkeypatch):
    r, workspace = registry(tmp_path)
    owner = "qa-mcp-session-stable"
    project = r.project_resolver.resolve(workspace)
    source = {
        "execution_backend": "acp",
        "completed": True,
        "harness": "antigravity",
        "kind": "explore",
        "cwd": str(workspace),
        "project": project.as_payload(),
        "model": "gemini-3.8-flash-medium",
        "reasoning_effort": "medium",
        "harness_metadata": {
            "session_ref": "a" * 32,
            "owner_digest": hashlib.sha256(owner.encode()).hexdigest(),
            "scope_digest": "b" * 64,
        },
        "acp_scope": {
            "files_in_scope": ["sample.py"],
            "out_of_scope": ["secret.py"],
        },
    }
    monkeypatch.setattr(r, "_resume_source_snapshot", lambda _: source)
    values = {
        "source_id": "c" * 12,
        "harness": "antigravity",
        "project": project,
        "cwd": workspace,
        "logical_session_id": owner,
        "files_in_scope": None,
        "out_of_scope": None,
        "model": "gemini-3.8-flash-medium",
        "reasoning_effort": "medium",
        "timeout": 90,
    }
    context, narrowed, error = r._resolve_acp_resume_context(**values)
    assert error is None
    assert context["session_ref"] == "a" * 32
    assert context["owner_digest"] == hashlib.sha256(owner.encode()).hexdigest()
    assert narrowed == {"files_in_scope": ["sample.py"], "out_of_scope": ["secret.py"]}
    for changes, expected in (
        ({"logical_session_id": "another-session"}, "acp_resume_owner_mismatch"),
        ({"files_in_scope": ["other.py"]}, "acp_resume_file_scope_widened"),
        ({"out_of_scope": []}, "acp_resume_exclusion_weakened"),
        ({"model": "gemini-3.8-flash-high"}, "acp_resume_model_mismatch"),
        ({"harness": "antigravity2"}, "acp_resume_scope_mismatch"),
    ):
        _, _, denied = r._resolve_acp_resume_context(**{**values, **changes})
        assert denied["error"]["code"] == expected


def test_acp_worker_invocation_uses_pinned_resume_reference(tmp_path):
    r, workspace = registry(tmp_path)
    project = r.project_resolver.resolve(workspace)
    context = {
        "session_ref": "c" * 32,
        "owner_digest": "d" * 64,
        "scope_digest": "e" * 64,
    }
    task = r._make_task(
        harness="antigravity", project=project, cwd=workspace, kind="explore",
        task="Continue inspection", goal=None, task_id=None, group_id=None,
        model="gemini-3.8-flash-medium", reasoning_effort="medium",
        sandbox_mode="acp-native-read-only", commit_mode="forbidden",
        execution_timeout_seconds=90, depends_on_group_ids=(),
        files_in_scope=["sample.py"], out_of_scope=["secret.py"],
        context_files=[], acceptance_criteria=[], done_means=[],
        verification_commands=[], output_schema=None, parse_structured_output=True,
        request_fingerprint="test-resume", acp_resume_context=context,
        execution_backend="acp",
    )
    request = json.loads(r._build_invocation_for_task(task).stdin)
    assert request["session_ref"] == context["session_ref"]
    assert request["owner"] == context["owner_digest"]
    assert request["scope_digest"] == context["scope_digest"]
    assert request["turn_id"] == task.delegate_id


def test_cancel_requests_protocol_before_process_signal(tmp_path):
    r,_workspace=registry(tmp_path)
    calls=[]
    class Jobs:
        def job_status(self,**kwargs):
            calls.append('status')
            assert r.acp_backend.cancel_path('a'*12).is_file()
            return {'status':'succeeded'}
        def kill_job(self,**kwargs):
            raise AssertionError('Protocol cancellation finished; no TERM allowed')
    r.durable_job_registry=Jobs()
    task=SimpleNamespace(durable_job_id='job-test',execution_backend='acp',delegate_id='a'*12,
        log_paths=SimpleNamespace(metadata=tmp_path/'absent.json'),cancel_reason='explicit_cancel',
        cancel_source='test',cancel_rationale='test')
    r._terminate_task(task)
    assert calls==['status']


def stored_acp_task(r, workspace):
    context = {'session_ref': 'c' * 32, 'owner_digest': 'd' * 64, 'scope_digest': 'e' * 64}
    task = r._make_task(
        harness='antigravity', project=r.project_resolver.resolve(workspace), cwd=workspace,
        kind='explore', task='Continue inspection', goal=None, task_id=None, group_id=None,
        model='gemini-3.8-flash-medium', reasoning_effort='medium', sandbox_mode='acp-native-read-only',
        commit_mode='forbidden', execution_timeout_seconds=90, depends_on_group_ids=(),
        files_in_scope=['sample.py'], out_of_scope=['secret.py'], context_files=[],
        acceptance_criteria=[], done_means=[], verification_commands=[], output_schema=None,
        parse_structured_output=True, request_fingerprint='persisted-acp',
        logical_session_id='stable-owner', execution_backend='acp', acp_resume_context=context,
    )
    task.durable_job_id = 'fixture-durable-job'
    r._write_durable_delegate_metadata(task, invocation=r._build_invocation_for_task(task),
                                       before_status=None, job_status={'status':'running'})
    return task


def test_acp_recovery_preserves_scope_and_identity_when_admission_disabled(tmp_path):
    r, workspace = registry(tmp_path)
    task = stored_acp_task(r, workspace)
    payload = json.loads(task.log_paths.metadata.read_text())
    # A rolling rollback may disable future ACP launches while a durable child
    # still owns the provider turn. Reconcile it without configuration or a spawn.
    r.acp_backend = None
    restored = r._task_from_persisted_durable(payload, task.log_paths.metadata)
    assert restored.acp_scope == task.acp_scope
    assert restored.acp_resume_context == task.acp_resume_context
    assert restored.logical_session_id == task.logical_session_id
    invocation = r._build_invocation_for_task(restored, for_recovery=True)
    assert invocation.args == [] and invocation.read_only_enforced
    parsed = invocation.output_parser(json.dumps({'event': 'acp_result', 'result': {
        'success': True, 'status': 'succeeded',
        'answer': json.dumps({'status': 'succeeded', 'summary': 'recovered', 'findings': []}),
    }}), '')
    assert parsed.error is None and parsed.metadata['transport'] == 'acp'
    with pytest.raises(OSError, match='disabled'):
        r._build_invocation_for_task(restored)


def test_acp_recovery_drops_malformed_scope_instead_of_reauthorizing(tmp_path):
    r, workspace = registry(tmp_path)
    task = stored_acp_task(r, workspace)
    payload = json.loads(task.log_paths.metadata.read_text())
    payload['acp_scope'] = {'files_in_scope': 'all', 'out_of_scope': []}
    payload['acp_resume_context']['session_ref'] = '../other-session'
    restored = r._task_from_persisted_durable(payload, task.log_paths.metadata)
    assert restored.acp_scope is None and restored.acp_resume_context is None


@pytest.mark.parametrize('level', ['low', 'medium', 'high'])
def test_acp_model_alias_preserves_actual_thinking_level(tmp_path, level):
    r, _workspace = registry(tmp_path)
    model, effort, commit, _ = r._task_defaults(
        adapter=r.acp_backend.adapter('antigravity'), kind='explore',
        model='gemini-3.8-flash-' + level, reasoning_effort=None, commit_mode='forbidden',
    )
    assert model.endswith('-' + level) and effort == level and commit == 'forbidden'


def test_acp_capabilities_discoverable_without_extra_account_pool(tmp_path):
    r, _workspace = registry(tmp_path)
    info = r.harness_info()
    assert 'acp' not in info and 'antigravity_acp' not in info
    acp = info['antigravity']['execution_backends']['acp']
    assert acp['enabled'] and acp['continuation']
    assert acp['quota_pool'] == 'antigravity' and not acp['code_available']
    r.acp_backend = None
    assert not r.harness_info()['antigravity']['execution_backends']['acp']['enabled']


def test_acp_evidence_uses_typed_scope_after_durable_recovery(tmp_path):
    r, workspace = registry(tmp_path)
    task = stored_acp_task(r, workspace)
    payload = json.loads(task.log_paths.metadata.read_text())
    restored = r._task_from_persisted_durable(payload, task.log_paths.metadata)
    restored.prompt = 'A continuation with no original headings'
    result = {'status': 'succeeded', 'structured_output': {
        'status': 'succeeded', 'summary': 'Inspected source',
        'findings': ['sample.py:1: defines sample'], 'blockers': [],
    }}
    r._assess_explore_result(restored, result)
    assert result['research_evidence']['status'] == 'all_findings_anchored'
    assert result['research_evidence']['anchored_finding_count'] == 1


def test_acp_changed_account_binding_denies_admission_without_provider_request(tmp_path):
    path, workspace, data = configuration(tmp_path)
    backend = AcpBackend.load(path)
    from pathlib import Path
    token = Path(data['accounts']['antigravity']['profile'])/'antigravity-acp/acp_token.json'
    values = json.loads(token.read_text())
    values['refresh_token'] = 'another-principal'
    token.write_text(json.dumps(values))
    with pytest.raises(AcpStateError, match='binding_changed'):
        backend.validate(account='antigravity', workspace=workspace, kind='explore', model=None, timeout=90)
    assert backend.adapter('antigravity').info()['availability_reason'] == 'acp_account_binding_changed'


@pytest.mark.parametrize('settings', [[], {'auth':'invalid'}, {'auth':None}, 'invalid'])
def test_malformed_optional_settings_disable_acp_without_crashing_service(tmp_path, settings):
    path, _workspace, data = configuration(tmp_path)
    from pathlib import Path
    config = Path(data['accounts']['antigravity']['profile'])/'antigravity-acp/settings.json'
    config.write_text(json.dumps(settings))
    assert AcpBackend.load_optional(str(path)) is None


def test_verified_profile_cannot_be_relabelled_to_another_scheduler_account(tmp_path):
    from pathlib import Path
    r, workspace = registry(tmp_path)
    configured = r.acp_backend.accounts['antigravity']
    other_home = tmp_path/'other-cli-home'
    token = other_home/'.gemini/antigravity-cli/antigravity-oauth-token'
    token.parent.mkdir(parents=True)
    token.write_text(json.dumps({'id_token_email':'qa@example.invalid','token':{'refresh_token':'other-cli'}}))
    token.chmod(0o600)
    with patch('chatgpt_web_oauth_mcp.acp_identity._google_identity', return_value='qa@example.invalid'):
        verify_binding(profile=configured['profile'], account='antigravity', cli_home=other_home,
                       proof_root=r.acp_backend.state_root/'account-bindings')
    data = json.loads(r.acp_backend.config_path.read_text())
    data['accounts']['antigravity']['cli_home'] = str(other_home)
    r.acp_backend.config_path.write_text(json.dumps(data))
    r.acp_backend = AcpBackend.load(r.acp_backend.config_path)
    result = r.run_delegate(task='Inspect sample', cwd=workspace, kind='explore',
                            harness='antigravity', backend='acp', wait_seconds=0)
    assert result['error']['code'] == 'acp_account_harness_binding_mismatch'
    assert not r.scheduler.tasks
    info = r.harness_info()['antigravity']['execution_backends']['acp']
    assert not info['available'] and info['availability_reason'] == 'acp_account_harness_binding_mismatch'


@pytest.mark.parametrize("source_model", ["gemini-3.8-flash-high", "gemini-3.8-flash"])
def test_acp_resume_inherits_high_model_and_effort_when_omitted(tmp_path, monkeypatch, source_model):
    r, workspace = registry(tmp_path)
    owner = "stable-high-session"
    source = {
        "execution_backend": "acp", "completed": True,
        "harness": "antigravity", "kind": "explore", "cwd": str(workspace),
        "project": r.project_resolver.resolve(workspace).as_payload(),
        "model": source_model, "reasoning_effort": "high",
        "harness_metadata": {
            "session_ref": "a" * 32, "scope_digest": "b" * 64,
            "owner_digest": hashlib.sha256(owner.encode()).hexdigest(),
        },
        "acp_scope": {"files_in_scope": ["sample.py"], "out_of_scope": ["secret.py"]},
    }
    monkeypatch.setattr(r, "_resume_source_snapshot", lambda _identifier: source)
    observed = []

    def execute(*, delegate_task):
        observed.append(json.loads(r._build_invocation_for_task(delegate_task).stdin))
        return {"status": "succeeded", "success": True, "completed": True,
                "structured_output": {"status": "succeeded", "summary": "fixture", "findings": []}}

    monkeypatch.setattr(r, "_start_codex_delegate_impl", execute)
    arguments = {
        "task": "Continue the high reasoning review", "cwd": workspace,
        "harness": "antigravity", "backend": "acp", "kind": "explore",
        "resume_from_delegate_id": "c" * 12, "logical_session_id": owner,
        "execution_timeout_seconds": 90, "wait_seconds": 0,
    }
    # The public default complexity is low. Omitted settings must not reset an
    # existing high session to the default medium model/effort.
    response = r.run_delegate(**arguments)
    assert response.get("delegate_id"), response
    task = r.scheduler.get_task(response["delegate_id"])
    assert task.completed_event.wait(5)
    assert task.model == source["model"] and task.reasoning_effort == "high"
    assert observed[0]["thinking"] == "high" and observed[0]["session_ref"] == "a" * 32
    assert task.acp_scope == source["acp_scope"]
    # An explicitly different model stays explicit and is rejected instead of
    # being silently replaced by the inherited value.
    denied = r.run_delegate(**{**arguments, "model": "gemini-3.8-flash-medium"})
    assert denied["error"]["code"] == "acp_resume_model_mismatch"
    assert len(observed) == 1
