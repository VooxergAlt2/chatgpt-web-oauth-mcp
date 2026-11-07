"""Opt-in ACP transport for existing logical AGY accounts, not new quota pools."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .acp_result import parse_acp_worker_output
from .acp_identity import validate_binding
from .acp_session_store import AcpStateError
from .acp_verification_runtime import VerificationRuntime
from .delegate_harnesses import AntigravityHarness, HarnessTaskDefaults
from .delegate_process import Invocation

ACCOUNTS = frozenset({'antigravity', 'antigravity2', 'antigravity3'})
MODELS = frozenset({'gemini-3.8-flash', 'gemini-3.8-flash-low', 'gemini-3.8-flash-medium', 'gemini-3.8-flash-high'})


@dataclass(frozen=True)
class AcpBackend:
    config_path: Path
    python: Path
    server: Path
    helper: Path
    state_root: Path
    accounts: dict
    verification_runtime: VerificationRuntime | None = None

    @classmethod
    def load_optional(cls, value: str):
        if not value:
            return None
        try:
            return cls.load(Path(value))
        except (AcpStateError, OSError, ValueError, TypeError, KeyError):
            # A broken optional account must not take down the existing MCP.
            logging.getLogger(__name__).warning('ACP backend disabled: invalid operator configuration')
            return None

    @classmethod
    def load(cls, path: Path):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_mode & 0o077 or s.st_size > 32768:
                raise AcpStateError('acp_config_not_private')
            with os.fdopen(fd, 'r', closefd=False) as f:
                raw = json.load(f)
        finally:
            os.close(fd)
        required = {'version','python','server','helper','state_root','accounts'}
        if (not isinstance(raw, dict) or raw.get('version') != 1
                or set(raw) - {'verification_runtime'} != required):
            raise AcpStateError('acp_config_invalid')
        paths = {}
        for key in ('python','server','helper','state_root'):
            value = raw[key]
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise AcpStateError('acp_config_invalid_path')
            p = Path(value).resolve(strict=True)
            if key == 'state_root':
                if not p.is_dir() or p.stat().st_mode & 0o077 or p.stat().st_uid != os.getuid():
                    raise AcpStateError('acp_state_not_private')
            elif not p.is_file() or not os.access(p, os.X_OK):
                raise AcpStateError('acp_runtime_not_executable')
            # Preserve a venv executable path: resolving its python symlink loses the venv.
            paths[key] = Path(value) if key == 'python' else p
        accounts = raw['accounts']
        if not isinstance(accounts, dict) or not accounts or set(accounts) - ACCOUNTS:
            raise AcpStateError('acp_config_accounts_invalid')
        profiles = set()
        normalized = {}
        for name, value in accounts.items():
            if not isinstance(value, dict) or set(value) != {'profile','cli_home','workspace_roots'}:
                raise AcpStateError('acp_account_config_invalid')
            if not isinstance(value['profile'], str) or not Path(value['profile']).is_absolute():
                raise AcpStateError('acp_account_config_invalid')
            profile = Path(value['profile']).resolve(strict=True)
            if not profile.is_dir() or profile.stat().st_uid != os.getuid() or profile.stat().st_mode & 0o077:
                raise AcpStateError('acp_profile_not_private')
            if profile in profiles:
                raise AcpStateError('acp_profile_duplicate_quota_identity')
            profiles.add(profile)
            settings = profile/'antigravity-acp/settings.json'
            if settings.is_symlink() or not settings.is_file() or settings.stat().st_uid != os.getuid():
                raise AcpStateError('acp_settings_unsafe')
            if settings.stat().st_size > 65536:
                raise AcpStateError('acp_settings_unsafe')
            settings_value = json.loads(settings.read_text())
            auth = settings_value.get('auth') if isinstance(settings_value, dict) else None
            if not isinstance(auth, dict) or auth.get('type') != 'oauth-personal':
                raise AcpStateError('acp_personal_oauth_required')
            token = profile/'antigravity-acp/acp_token.json'
            if token.is_symlink() or not token.is_file() or token.stat().st_uid != os.getuid() or token.stat().st_mode & 0o077:
                raise AcpStateError('acp_authorization_required')
            roots = value['workspace_roots']
            if not isinstance(roots, list) or not roots or len(roots)>16:
                raise AcpStateError('acp_workspace_roots_required')
            allowed = tuple(Path(r).resolve(strict=True) for r in roots)
            if any(not r.is_dir() or r == Path('/') or profile == r or r in profile.parents for r in allowed):
                raise AcpStateError('acp_unsafe_workspace_root')
            if not isinstance(value['cli_home'], str) or not Path(value['cli_home']).is_absolute():
                raise AcpStateError('acp_account_binding_cli_home_invalid')
            cli_home = Path(value['cli_home']).resolve(strict=True)
            if not cli_home.is_dir():
                raise AcpStateError('acp_account_binding_cli_home_invalid')
            validate_binding(profile=profile, account=name, cli_home=cli_home,
                             proof_root=paths['state_root'] / 'account-bindings')
            normalized[name] = {'profile':profile,'cli_home':cli_home,'workspace_roots':allowed}
        runtime = None
        if raw.get('verification_runtime') is not None:
            forbidden = [paths['state_root']]
            for account in normalized.values():
                forbidden.extend((account['profile'], account['cli_home'] / '.gemini'))
                forbidden.extend(account['workspace_roots'])
            runtime = VerificationRuntime.load(raw['verification_runtime'], forbidden_roots=forbidden)
        return cls(path.resolve(), **paths, accounts=normalized, verification_runtime=runtime)

    def validate(self, *, account: str, workspace: Path, kind: str, model: str | None, timeout: int | None) -> None:
        if account not in self.accounts:
            raise AcpStateError('acp_account_not_configured')
        validate_binding(profile=self.accounts[account]['profile'], account=account,
                         cli_home=self.accounts[account]['cli_home'],
                         proof_root=self.state_root / 'account-bindings')
        if kind not in {'explore', 'code'}:
            raise AcpStateError('acp_task_kind_invalid')
        if model is not None and model not in MODELS:
            raise AcpStateError('acp_flash_only')
        if timeout is not None and not 15 <= timeout <= 900:
            raise AcpStateError('acp_timeout_must_be_15_to_900')
        target = workspace.resolve(strict=True)
        if not self.workspace_allowed(account, target):
            raise AcpStateError('acp_workspace_not_allowed')
        if self.state_root == target or target in self.state_root.parents:
            raise AcpStateError('acp_workspace_contains_state')

    def adapter(self, account: str):
        return AcpHarness(self, account)

    def workspace_allowed(self, account: str, target: Path) -> bool:
        roots = self.accounts[account]['workspace_roots']
        if any(target.is_relative_to(root) for root in roots):
            return True
        # A linked worktree belongs to an approved repository even when its
        # directory is a sibling. Never approve unrelated repositories by name.
        if not (target / '.git').is_file() or (target / '.git').is_symlink():
            return False
        def common(path: Path):
            completed = subprocess.run(
                ['git', '-C', str(path), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5,
                env={'PATH':'/usr/bin:/bin', 'HOME':'/nonexistent',
                     'GIT_CONFIG_NOSYSTEM':'1', 'GIT_CONFIG_GLOBAL':'/dev/null'},
            )
            return Path(completed.stdout.strip()).resolve() if completed.returncode == 0 else None
        try:
            repository = common(target)
            return repository is not None and any(common(root) == repository for root in roots)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False

    def validate_harness(self, account: str, harness) -> None:
        """The existing scheduler adapter, not operator labels, owns quota identity."""
        if not isinstance(harness, AntigravityHarness) or harness.name != account:
            raise AcpStateError('acp_requires_registered_antigravity_account')
        expected_home = (harness.home_dir or Path.home()).resolve()
        if account not in self.accounts or self.accounts[account]['cli_home'] != expected_home:
            raise AcpStateError('acp_account_harness_binding_mismatch')

    def cancel_path(self, delegate_id: str) -> Path:
        if not re.fullmatch(r'[a-f0-9]{12}', delegate_id):
            raise AcpStateError('acp_invalid_delegate_id')
        return self.state_root / (delegate_id + '.cancel')


@dataclass(frozen=True)
class AcpHarness:
    backend: AcpBackend
    name: str
    display_name: str = 'Antigravity ACP'

    @property
    def command(self):
        return shlex.quote(str(self.backend.python))

    def command_for(self, kind):
        return self.command if kind in {'explore', 'code'} else None

    def task_defaults(self, kind):
        return HarnessTaskDefaults('gemini-3.8-flash-medium','medium',
            'acp-native-isolated-writer' if kind == 'code' else 'acp-native-read-only')

    def supports_read_only(self):
        return Path('/usr/bin/bwrap').is_file()

    def info(self):
        binding = None
        reason = None
        try:
            binding = validate_binding(profile=self.backend.accounts[self.name]['profile'], account=self.name,
                                       cli_home=self.backend.accounts[self.name]['cli_home'],
                                       proof_root=self.backend.state_root / 'account-bindings')
        except AcpStateError as exc:
            reason = exc.code
        available = binding is not None and self.supports_read_only()
        return {'enabled': True, 'available':available,'explore_available':available,
                'read_only_supported':True,'backend':'acp','quota_pool':self.name,
                'configured_models':sorted(MODELS),'code_available':available,
                'continuation': True, 'automatic_routing': True,
                'routing_policy': 'existing_quota_scheduler_then_account_pinned_acp',
                'scope_boundary': 'workspace', 'custom_schema': False,
                'writer_boundary':'private-copy-verified-promotion',
                'live_questions':True, 'steering':'queued_next_turn',
                'reconciliation':'explicit_abandon', 'scoped_gateway':True,
                'draft_snapshots': {'version': 1, 'capture': 'supervisor_checkpoint',
                                    'controller_tool': 'delegate_draft', 'acceptance': False,
                                    'restore': 'explicit_snapshot_id', 'retention': 'explicit_discard'},
                'supervision': {'version': 1, 'checkpoint': 'cooperative_tool_checkpoint',
                                'controller_tool': 'delegate_supervisor',
                                'free_text_steer': 'same_turn_at_checkpoint',
                                'account_pinned': True, 'automatic_senior_calls': False},
                'account_binding': binding, 'availability_reason': reason}

    @staticmethod
    def reasoning_effort_for_model(model: str | None, reasoning_effort: str | None) -> str:
        for level in ('low', 'medium', 'high'):
            if str(model or '').endswith('-' + level):
                return level
        return reasoning_effort or 'medium'

    @staticmethod
    def context(task):
        resume = task.acp_resume_context or {}
        owner = resume.get('owner_digest') or hashlib.sha256((task.logical_session_id or task.delegate_id).encode()).hexdigest()
        scope = resume.get('scope_digest') or hashlib.sha256((str(task.cwd.resolve())+'\n'+task.prompt).encode()).hexdigest()
        reference = resume.get('session_ref') or hashlib.sha256((task.harness+':'+task.delegate_id).encode()).hexdigest()[:32]
        return {'owner_digest':owner, 'scope_digest':scope, 'session_ref':reference}

    def build_invocation(self, task):
        self.backend.validate(account=task.harness, workspace=task.cwd, kind=task.kind,
                              model=task.model, timeout=task.execution_timeout_seconds)
        context = self.context(task)
        request = {
            'delegate_id':task.delegate_id, 'account':task.harness, 'owner':context['owner_digest'],
            'workspace':str(task.cwd.resolve()), 'thinking':task.reasoning_effort,
            'scope_digest':context['scope_digest'], 'session_ref':context['session_ref'], 'turn_id':task.delegate_id,
            'prompt':task.prompt, 'timeout':task.execution_timeout_seconds,
            'max_tool_calls':task.max_tool_calls or 24,
            'kind':task.kind, 'scope':task.acp_scope or {'files_in_scope':[], 'out_of_scope':[]},
            'code_contract':task.code_contract,
            'code_baseline':task.code_baseline,
            'resume_from_turn':task.resume_from_delegate_id,
        }
        if getattr(task, 'work_role', None) == 'proposal':
            if task.resume_from_delegate_id is not None:
                raise AcpStateError('acp_proposal_requires_new_session')
            request.pop('resume_from_turn')
            request['proposal'] = True
        return Invocation(
            args=[str(self.backend.python),'-m','chatgpt_web_oauth_mcp.acp_worker',
                  '--config',str(self.backend.config_path)], use_shell=False,
            stdin=json.dumps(request).encode(), read_only_enforced=task.kind == 'explore',
            output_parser=parse_acp_worker_output,
            env_overrides={'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1'},
        )
