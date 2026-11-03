"""Private, task-scoped ACP state. No provider SDK, secrets or scheduler here."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from .state_io import atomic_write_bytes


class AcpStateError(RuntimeError):
    """Typed fail-closed state error; its code is safe to expose."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AcpSessionIdentity:
    owner: str
    account: str
    workspace: str
    scope_digest: str
    model: str = "gemini-3.8-flash"
    thinking: str = "medium"

    def __post_init__(self) -> None:
        for value in (self.owner, self.account):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
                raise ValueError("Invalid ACP owner/account identifier")
        if self.model != "gemini-3.8-flash" or self.thinking not in {"low", "medium", "high"}:
            raise ValueError("Unsupported ACP model/thinking contract")
        if not re.fullmatch(r"[a-f0-9]{64}", self.scope_digest):
            raise ValueError("ACP scope requires a SHA-256 digest")
        if not Path(self.workspace).is_absolute() or str(Path(self.workspace).resolve()) != self.workspace:
            raise ValueError("ACP workspace must be an absolute canonical path")

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


class AcpSessionStore:
    """One OS lease per logical session, with atomic 0600 checkpoints.

    The controller supplies an opaque reference. The caller cannot change the
    binding by selecting the same reference with another account or scope.
    Raw provider session IDs are never used as paths.
    """

    def __init__(self, root: Path):
        self.root = root
        if root.is_symlink():
            raise AcpStateError("acp_state_symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise AcpStateError("acp_state_not_owned")
        if info.st_mode & 0o077:
            raise AcpStateError("acp_state_not_private")

    @staticmethod
    def validate_reference(reference: str) -> None:
        if not isinstance(reference, str) or not re.fullmatch(r"[a-f0-9]{32}", reference):
            raise AcpStateError("acp_invalid_session_reference")

    def _path(self, reference: str, suffix: str) -> Path:
        self.validate_reference(reference)
        return self.root / (reference + suffix)

    @contextmanager
    def lease(self, reference: str) -> Iterator[None]:
        path = self._path(reference, ".lock")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AcpStateError("acp_lock_not_private")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AcpStateError("acp_session_busy") from exc
            yield
        finally:
            os.close(fd)

    def load(self, reference: str, identity: AcpSessionIdentity) -> dict:
        path = self._path(reference, ".json")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return {"version": 1, "identity": asdict(identity), "phase": "new", "turn": None}
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 262144:
                raise AcpStateError("acp_checkpoint_invalid")
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                state = json.load(stream)
        except (ValueError, UnicodeError) as exc:
            raise AcpStateError("acp_checkpoint_corrupt") from exc
        finally:
            os.close(fd)
        if not isinstance(state, dict) or state.get("version") != 1:
            raise AcpStateError("acp_checkpoint_version")
        if state.get("identity") != asdict(identity):
            raise AcpStateError("acp_session_binding_mismatch")
        if state.get("phase") not in {"new", "ready", "running", "completed", "cancelled", "blocked", "failed", "abandoned"}:
            raise AcpStateError("acp_checkpoint_invalid")
        return state

    def abandon(self, reference: str, identity: AcpSessionIdentity, *, turn_id: str, reason: str) -> dict:
        """Explicitly freeze an uncertain session without claiming an outcome.

        The OS lease rejects a still-running owner. The prior result and native
        history remain intact for review; future work needs a new reference.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 2048:
            raise AcpStateError("acp_abandon_reason_required")
        if not isinstance(turn_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", turn_id):
            raise AcpStateError("acp_control_invalid_identifier")
        with self.lease(reference):
            state = self.load(reference, identity)
            if state.get("turn") != turn_id:
                raise AcpStateError("acp_control_turn_mismatch")
            if state.get("phase") != "running":
                raise AcpStateError("acp_abandon_requires_uncertain_turn")
            result = state.get("result")
            if result is not None and (not isinstance(result, dict) or result.get("outcome_uncertain") is not True):
                raise AcpStateError("acp_abandon_requires_uncertain_turn")
            state["phase"] = "abandoned"
            state["abandonment"] = {"turn_id": turn_id, "reason": reason, "at": time.time(),
                                    "outcome_uncertain": True, "resume_allowed": False}
            self.save(reference, state)
            return {"status": "abandoned", "session_ref": reference, "turn_id": turn_id,
                    "outcome_uncertain": True, "resume_allowed": False, "requires_new_session": True}

    def save(self, reference: str, state: dict) -> None:
        path = self._path(reference, ".json")
        if path.is_symlink():
            raise AcpStateError("acp_state_symlink")
        raw = json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(raw) > 262144:
            raise AcpStateError("acp_checkpoint_too_large")
        atomic_write_bytes(path, raw + b"\n", mode=0o600, sync_directory=True)
