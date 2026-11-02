#!/usr/bin/env python3
"""LOCAL ONLY: audited break-glass for an abandoned, unscoped Windows UI fence.

No MCP tool is registered. Never clears a live, young, or scoped operation.
Does not terminate Windows or Revit processes. Inspect Windows independently
before passing the two explicit operator acknowledgments.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from chatgpt_web_oauth_mcp.revit_workspaces import WorkspaceManager


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--fence-id", type=str, required=True)
    parser.add_argument("--reason", type=str, required=True)
    parser.add_argument("--evidence", type=str, required=True)
    parser.add_argument("--apply", action="store_true",
                        help="Actually clear the exact eligible orphan; default is read-only.")
    parser.add_argument("--confirm-remote-idle", action="store_true",
                        help="I verified Windows has no outstanding operation from this controller.")
    parser.add_argument("--confirm-cross-owner-abort", action="store_true",
                        help="Operator explicitly authorizes retiring an orphan from another chat.")
    args = parser.parse_args()
    # A local-only maintenance path; it cannot mint a chat owner and is not
    # included in the MCP router or the Windows bridge's remote tool catalog.
    manager = WorkspaceManager(args.state_file, None,
                               owner_provider=lambda: "openai:local-operator")
    report = asyncio.run(manager.ui_status())
    if report.get("fence_id") != args.fence_id:
        raise ValueError("UI_FENCE_CHANGED")
    if not report.get("operator_recovery_eligible"):
        raise ValueError("ORPHAN_RECOVERY_NOT_ELIGIBLE")
    if not args.apply:
        print(json.dumps({"dry_run": True, "eligible": True,
                          "fence_id": args.fence_id, "state": report["state"]},
                         sort_keys=True))
        return 0
    if not (args.confirm_remote_idle and args.confirm_cross_owner_abort):
        raise ValueError("EXPLICIT_LOCAL_OPERATOR_APPROVAL_REQUIRED")
    result = manager.operator_abort_orphaned_ui(
        args.fence_id, confirmed_remote_idle=True,
        evidence=args.evidence, reason=args.reason)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
