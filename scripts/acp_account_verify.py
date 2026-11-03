#!/usr/bin/env python3
"""Verify one ACP profile against its existing CLI principal; print no secrets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from chatgpt_web_oauth_mcp.acp_identity import ACCOUNTS, verify_binding
from chatgpt_web_oauth_mcp.acp_session_store import AcpStateError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--account", choices=sorted(ACCOUNTS), required=True)
    parser.add_argument("--proof-root", type=Path, required=True)
    parser.add_argument("--cli-home", type=Path)
    args = parser.parse_args()
    try:
        status = verify_binding(
            profile=args.profile, account=args.account,
            proof_root=args.proof_root, cli_home=args.cli_home,
        )
    except AcpStateError as exc:
        print(json.dumps({"success": False, "account": args.account, "error": exc.code}))
        return 1
    except Exception:
        print(json.dumps({"success": False, "account": args.account,
                          "error": "acp_account_identity_verification_failed"}))
        return 1
    print(json.dumps({"success": True, **status}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
