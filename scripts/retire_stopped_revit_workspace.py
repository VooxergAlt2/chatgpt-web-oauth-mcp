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
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.observation_file.is_symlink() or args.observation_file.stat().st_size > 16384:
        raise ValueError('INVALID_OPERATOR_OBSERVATION_FILE')
    observation = json.loads(args.observation_file.read_text(encoding='utf-8'))
    manager = WorkspaceManager(args.state_file, None,
                               owner_provider=lambda: 'openai:local-operator')
    result = manager.operator_retire_stopped_workspace(
        args.workspace_id, observation=observation, reason=args.reason, apply=args.apply,
        confirm_process_stopped=args.confirm_process_stopped,
        confirm_unknown_outcome=args.confirm_unknown_outcome,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
