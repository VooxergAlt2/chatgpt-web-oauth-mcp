"""Pinned, credential-free regular-file runtimes for ACP verification only."""
from __future__ import annotations

import os
import re
import hashlib
import json
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .acp_session_store import AcpStateError
from .acp_workspace import _copy, _scan

MAX_RUNTIME_FILES = 10_000
MAX_RUNTIME_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


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


def _private_directory(root: Path) -> None:
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise AcpStateError("acp_verification_runtime_invalid")
    info = root.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise AcpStateError("acp_verification_runtime_not_private")


def _file_bytes(root: Path, name: str, maximum: int) -> bytes:
    directory = os.open(root, _DIRECTORY_FLAGS)
    fd = -1
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=directory)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o444
                or not 0 < before.st_size <= maximum):
            raise AcpStateError("acp_verification_runtime_manifest_invalid")
        data = bytearray()
        while chunk := os.read(fd, min(65536, maximum + 1 - len(data))):
            data.extend(chunk)
            if len(data) > maximum:
                raise AcpStateError("acp_verification_runtime_limit")
        after = os.fstat(fd)
        if (len(data) != before.st_size or any(getattr(before, key) != getattr(after, key)
                for key in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"))):
            raise AcpStateError("acp_verification_runtime_changed")
        return bytes(data)
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(directory)


def _manifest(data: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise AcpStateError("acp_verification_runtime_manifest_invalid")
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=unique)
    except (ValueError, RecursionError):
        raise AcpStateError("acp_verification_runtime_manifest_invalid") from None
    if (not isinstance(value, dict) or set(value) != {"version", "files"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["files"], dict)
            or not 2 <= len(value["files"]) <= MAX_RUNTIME_FILES):
        raise AcpStateError("acp_verification_runtime_manifest_invalid")
    total = 0
    for path, record in value["files"].items():
        if (not isinstance(path, str) or not path
                or not (path == "pyvenv.cfg" or path.startswith(("bin/", "lib/", "include/")))
                or Path(path).as_posix() != path or path.startswith("/")
                or any(part in {".", ".."} or part.startswith(".") for part in Path(path).parts)
                or "\\" in path or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or not isinstance(record, dict) or set(record) != {"sha256", "size", "mode"}
                or not isinstance(record["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])
                or type(record["size"]) is not int or not 0 <= record["size"] <= 16 * 1024 * 1024
                or type(record["mode"]) is not int or record["mode"] not in {0o444, 0o555}):
            raise AcpStateError("acp_verification_runtime_manifest_invalid")
        total += record["size"]
    if (total > MAX_RUNTIME_BYTES or value["files"].get("pyvenv.cfg", {}).get("mode") != 0o444
            or value["files"].get("bin/python", {}).get("mode") != 0o555):
        raise AcpStateError("acp_verification_runtime_manifest_invalid")
    return value["files"]


@dataclass(frozen=True)
class VerificationRuntime:
    name: str
    root: Path
    manifest_sha256: str
    python: str = "bin/python"

    @classmethod
    def load(cls, raw: object, *, forbidden_roots=()):
        identity = validate_runtime_identity(raw)
        root = Path(identity["root"])
        _private_directory(root)
        if root == Path.home() or any(root.is_relative_to(Path(path)) for path in
                ("/usr", "/etc", "/proc", "/dev", "/sys", "/run")):
            raise AcpStateError("acp_verification_runtime_invalid")
        for path in forbidden_roots:
            other = Path(path).resolve(strict=False)
            if root.is_relative_to(other) or other.is_relative_to(root):
                raise AcpStateError("acp_verification_runtime_overlap")
        runtime = cls(identity["id"], root, identity["manifest_sha256"])
        runtime.validate()
        return runtime

    def identity(self) -> dict:
        return {"id": self.name, "root": str(self.root),
                "manifest_sha256": self.manifest_sha256, "python": self.python}

    def _snapshot(self):
        try:
            _private_directory(self.root)
            raw = _file_bytes(self.root, "manifest.json", MAX_MANIFEST_BYTES)
            if hashlib.sha256(raw).hexdigest() != self.manifest_sha256:
                raise AcpStateError("acp_verification_runtime_changed")
            expected = _manifest(raw)
            records = _scan(self.root)
            actual = {path: record.public() for path, record in records.items() if path != "manifest.json"}
            if actual != expected or records["manifest.json"].sha256 != self.manifest_sha256:
                raise AcpStateError("acp_verification_runtime_changed")
            config_raw = _file_bytes(self.root, "pyvenv.cfg", 65536)
            if hashlib.sha256(config_raw).hexdigest() != records["pyvenv.cfg"].sha256:
                raise AcpStateError("acp_verification_runtime_changed")
            config = config_raw.decode("utf-8")
            values = {}
            for line in config.splitlines():
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key in values:
                    raise AcpStateError("acp_verification_runtime_venv_invalid")
                values[key] = value.strip()
            if (values.get("include-system-site-packages", "").lower() != "false"
                    or values.get("home") not in {"/usr/bin", "/usr/local/bin"}):
                raise AcpStateError("acp_verification_runtime_venv_invalid")
            return records
        except AcpStateError as exc:
            if exc.code.startswith("acp_verification_runtime_"):
                raise
            raise AcpStateError("acp_verification_runtime_changed") from exc
        except (OSError, ValueError, UnicodeError):
            raise AcpStateError("acp_verification_runtime_changed") from None

    def validate(self) -> None:
        self._snapshot()

    def stage(self, destination: Path):
        destination = Path(destination)
        _private_directory(destination.parent)
        if (not destination.is_absolute() or destination.parent / destination.name != destination
                or destination.exists() or destination.is_symlink()
                or destination.is_relative_to(self.root) or self.root.is_relative_to(destination)):
            raise AcpStateError("acp_verification_runtime_destination_invalid")
        records = self._snapshot()
        destination.mkdir(mode=0o700)
        try:
            for path, record in records.items():
                _copy(self.root, path, destination / path, record)
            if self._snapshot() != records:
                raise AcpStateError("acp_verification_runtime_changed")
            staged = StagedVerificationRuntime(destination, self, tuple(sorted(records.items())))
            staged.validate()
            return staged
        except BaseException:
            shutil.rmtree(destination)
            raise


@dataclass(frozen=True)
class StagedVerificationRuntime:
    root: Path
    source: VerificationRuntime
    _records: tuple

    def identity(self) -> dict:
        return self.source.identity()

    def validate(self) -> None:
        try:
            _private_directory(self.root)
            if _scan(self.root) != dict(self._records):
                raise AcpStateError("acp_verification_runtime_changed")
        except (AcpStateError, OSError, ValueError) as exc:
            raise AcpStateError("acp_verification_runtime_changed") from exc
