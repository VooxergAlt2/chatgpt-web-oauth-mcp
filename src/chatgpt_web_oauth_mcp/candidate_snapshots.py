"""Immutable private draft artifacts, independent of model/provider execution.

A snapshot is evidence and a starting point, never an acceptance receipt. The
filesystem adapter supplies capture/restore; this store supplies identity,
atomic publication, bounded storage and serialization against explicit discard.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from .state_io import atomic_write_bytes
from .supervision import SupervisorBinding, SupervisionError

MAX_SNAPSHOTS = 8
MAX_STORED_BYTES = 64 * 1024 * 1024
MAX_METADATA_BYTES = 262144


def _encoded(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _private_directory(path: Path) -> None:
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise SupervisionError("draft_directory_not_private")


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class CandidateSnapshotStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _private_directory(self.root)

    @staticmethod
    def _id(snapshot_id: str) -> str:
        if not isinstance(snapshot_id, str) or not re.fullmatch(r"[a-f0-9]{32}", snapshot_id):
            raise SupervisionError("draft_snapshot_id_invalid")
        return snapshot_id

    @contextmanager
    def _lease(self, binding: SupervisorBinding):
        fd = os.open(self.root / (binding.key + ".lock"),
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise SupervisionError("draft_lock_not_private")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SupervisionError("draft_store_busy") from None
            directory = self.root / binding.key
            directory.mkdir(mode=0o700, exist_ok=True)
            _private_directory(directory)
            _sync_directory(self.root)
            yield directory
        finally:
            os.close(fd)

    def _load(self, directory: Path, binding: SupervisorBinding, snapshot_id: str) -> dict:
        folder = directory / self._id(snapshot_id)
        try:
            _private_directory(folder)
            fd = os.open(folder / "manifest.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            raise SupervisionError("draft_snapshot_not_found") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise SupervisionError("draft_manifest_not_private")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(MAX_METADATA_BYTES + 1)
            if len(raw) > MAX_METADATA_BYTES:
                raise SupervisionError("draft_manifest_too_large")
            value = json.loads(raw)
        finally:
            os.close(fd)
        if (not isinstance(value, dict) or value.get("version") != 1
                or value.get("snapshot_id") != snapshot_id or value.get("accepted") is not False
                or not isinstance(value.get("payload"), dict)
                or type(value.get("stored_bytes")) is not int
                or not 0 <= value["stored_bytes"] <= MAX_STORED_BYTES):
            raise SupervisionError("draft_manifest_invalid")
        if value.get("binding") != asdict(binding):
            raise SupervisionError("draft_binding_mismatch")
        if value.get("payload_sha256") != hashlib.sha256(_encoded(value["payload"])).hexdigest():
            raise SupervisionError("draft_manifest_changed")
        return value

    def _records(self, directory, binding):
        names = sorted(p.name for p in directory.iterdir() if re.fullmatch(r"[a-f0-9]{32}", p.name))
        if len(names) > MAX_SNAPSHOTS:
            raise SupervisionError("draft_retention_limit")
        return [self._load(directory, binding, name) for name in names]

    @staticmethod
    def _receipt(value):
        payload = value["payload"]
        return {"snapshot_id": value["snapshot_id"], "created_at": value["created_at"],
                "source_execution_id": value["binding"]["execution_id"],
                "baseline_commit": payload["baseline_head"], "candidate_digest": payload["candidate_digest"],
                "changed_file_count": len(payload["changes"]), "stored_bytes": value["stored_bytes"],
                "accepted": False, "retention": "explicit_discard"}

    def list(self, binding: SupervisorBinding) -> list[dict]:
        with self._lease(binding) as directory:
            return sorted((self._receipt(v) for v in self._records(directory, binding)),
                          key=lambda v: v["created_at"])

    def capture(self, binding: SupervisorBinding, *, export: Callable[[Path], dict]) -> dict:
        """Publish only after the adapter has copied, verified and synced bytes."""
        with self._lease(binding) as directory:
            # A crash before publication leaves no usable snapshot. These are
            # private staging directories, never native workspaces or receipts.
            for pending in directory.glob(".pending-*"):
                if re.fullmatch(r"\.pending-[a-f0-9]{32}", pending.name):
                    if pending.is_symlink():
                        raise SupervisionError("draft_staging_invalid")
                    shutil.rmtree(pending)
            records = self._records(directory, binding)
            snapshot_id = secrets.token_hex(16)
            staging = directory / (".pending-" + snapshot_id)
            staging.mkdir(mode=0o700)
            try:
                files = staging / "files"
                files.mkdir(mode=0o700)
                payload = export(files)
                digest = hashlib.sha256(_encoded(payload)).hexdigest()
                for previous in records:
                    if previous["payload_sha256"] == digest:
                        return self._receipt(previous)
                total = sum(item["size"] for item in payload["changes"].values() if item is not None)
                if (len(records) >= MAX_SNAPSHOTS or total > MAX_STORED_BYTES
                        or sum(r["stored_bytes"] for r in records) + total > MAX_STORED_BYTES):
                    raise SupervisionError("draft_retention_limit")
                value = {"version": 1, "snapshot_id": snapshot_id, "binding": asdict(binding),
                         "created_at": time.time(), "accepted": False, "stored_bytes": total,
                         "payload": payload, "payload_sha256": digest}
                raw = _encoded(value)
                if len(raw) > MAX_METADATA_BYTES:
                    raise SupervisionError("draft_manifest_too_large")
                atomic_write_bytes(staging / "manifest.json", raw, mode=0o600, sync_directory=True)
                for folder, _, _ in os.walk(files, topdown=False):
                    _sync_directory(Path(folder))
                _sync_directory(staging)
                os.rename(staging, directory / snapshot_id)
                _sync_directory(directory)
                return self._receipt(value)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)

    def restore(self, binding: SupervisorBinding, snapshot_id: str, *,
                apply: Callable[[Path, dict], None]) -> dict:
        # Keep discard serialized through verification and the private copy.
        with self._lease(binding) as directory:
            value = self._load(directory, binding, snapshot_id)
            apply(directory / snapshot_id / "files", value["payload"])
            return self._receipt(value)

    def discard(self, binding: SupervisorBinding, snapshot_id: str) -> dict:
        with self._lease(binding) as directory:
            try:
                self._load(directory, binding, snapshot_id)
            except SupervisionError as exc:
                if exc.code == "draft_snapshot_not_found":
                    return {"snapshot_id": snapshot_id, "discarded": True, "already_discarded": True}
                raise
            shutil.rmtree(directory / snapshot_id)
            _sync_directory(directory)
            return {"snapshot_id": snapshot_id, "discarded": True, "already_discarded": False}
