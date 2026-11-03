"""Bounded, secret-free implementation identity for native ACP acceptance reports."""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_GIT_ENV = {
    "PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
    "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
}
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_GIT_TIMEOUT_SECONDS = 5


def _git(repo: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *arguments],
        env=dict(_GIT_ENV), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, timeout=_GIT_TIMEOUT_SECONDS, check=False,
    )
    if result.returncode != 0:
        raise ValueError("git command failed")
    return result.stdout


def capture_implementation(repo_dir: str | Path) -> dict[str, object]:
    """Capture once before setup; never return paths, filenames or Git diagnostics.

    This identifies the committed tree and whether the checkout differs from it.
    A dirty checkout is reported honestly; its modified contents are not attested.
    Missing or incomplete Git evidence makes every identity field unavailable.
    """
    snapshot = {
        "schema_version": 1,
        "captured_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "unavailable", "head": None, "tree": None, "dirty": None,
        "error_code": "qa_implementation_capture_failed",
    }
    try:
        repo = Path(repo_dir).resolve(strict=True)
        head = _git(repo, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
        if not _OID.fullmatch(head):
            raise ValueError("invalid commit identity")
        tree = _git(repo, "rev-parse", "--verify", head + "^{tree}").decode("ascii").strip()
        if not _OID.fullmatch(tree) or len(head) != len(tree):
            raise ValueError("invalid object identity")
        dirty = bool(_git(repo, "status", "--porcelain=v1", "-z",
                          "--untracked-files=all", "--ignore-submodules=none"))
        if _git(repo, "rev-parse", "--verify", "HEAD").decode("ascii").strip() != head:
            raise ValueError("commit changed during capture")
        snapshot.update(status="captured", head=head, tree=tree, dirty=dirty, error_code=None)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    return snapshot
