"""Pinned, credential-free regular-file runtimes for ACP verification only."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .acp_session_store import AcpStateError


def validate_runtime_identity(value: object) -> dict:
    """Validate an identity without consulting current or historical files."""
    invalid = "acp_verification_runtime_invalid"
    if not isinstance(value, dict) or set(value) != {"id", "root", "manifest_sha256", "python"}:
        raise AcpStateError(invalid)
    name, root, digest = value["id"], value["root"], value["manifest_sha256"]
    if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", name)
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or value["python"] != "bin/python" or not isinstance(root, str)):
        raise AcpStateError(invalid)
    try:
        size = len(root.encode("utf-8"))
    except UnicodeError:
        raise AcpStateError(invalid) from None
    if (not root or size > 4096 or "\\" in root or root.startswith("//")
            or any(ord(c) < 32 or ord(c) == 127 for c in root)
            or not Path(root).is_absolute() or os.path.normpath(root) != root
            or root in {"/", "/home", "/usr", "/etc", "/proc", "/dev", "/sys", "/run", "/var", "/opt"}):
        raise AcpStateError(invalid)
    return {"id": name, "root": root, "manifest_sha256": digest, "python": "bin/python"}
