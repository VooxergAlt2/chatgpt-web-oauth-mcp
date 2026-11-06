#!/usr/bin/env python3
"""LOCAL ONLY: retire a stopped disposable QA process, preserving unknown outcomes.

Requires operator-reviewed fresh Windows process-list and native-discovery
observations. Never registered as an MCP tool. Does not close Revit, save/delete
models, mint grants, replay commands or turn unknown results into successes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-file', type=Path, required=True)
    parser.add_argument('--observation-file', type=Path, required=True)
    parser.add_argument('--workspace-id', required=True)
    parser.add_argument('--reason', required=True)
    parser.add_argument('--confirm-process-stopped', action='store_true')
    parser.add_argument('--confirm-unknown-outcome', action='store_true')
    parser.add_argument('--confirm-stopped-scoped-ui', action='store_true',
                        help='Retire a same-owner verifying UI fence only with exact stopped-process proof')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--orphaned-ui-observation-file', type=Path,
                        help='Fresh ControlStatus/screenshot attestation for an abandoned scoped Windows call')
    parser.add_argument('--confirm-remote-idle', action='store_true',
                        help='Operator reviewed the desktop and confirms the old Windows operation ended')
    args = parser.parse_args()
    if args.observation_file.is_symlink() or args.observation_file.stat().st_size > 16384:
        raise ValueError('INVALID_OPERATOR_OBSERVATION_FILE')
    observation = json.loads(args.observation_file.read_text(encoding='utf-8'))
    ui_observation = None
    if args.orphaned_ui_observation_file is not None:
        path = args.orphaned_ui_observation_file
        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError('INVALID_OPERATOR_OBSERVATION_FILE')
        ui_observation = json.loads(path.read_text(encoding='utf-8'))
    manager = WorkspaceManager(args.state_file, None,
                               owner_provider=lambda: 'openai:local-operator')
    result = manager.operator_retire_stopped_workspace(
        args.workspace_id, observation=observation, reason=args.reason, apply=args.apply,
        confirm_process_stopped=args.confirm_process_stopped,
        confirm_unknown_outcome=args.confirm_unknown_outcome,
        confirm_stopped_scoped_ui=args.confirm_stopped_scoped_ui,
        orphaned_ui_observation=ui_observation,
        confirm_remote_idle=args.confirm_remote_idle,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
