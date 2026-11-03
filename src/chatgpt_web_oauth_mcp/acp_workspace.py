"""Detached ACP writer copies and optimistic, scope-checked promotion.

Native tools never receive the source worktree or its Git metadata writable.
Only regular tracked files from a clean dedicated linked worktree enter the
copy. Scope is enforced at promotion: the copy itself is writable so editors
can use atomic rename. Callers must stop native processes before inspection.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

from .acp_session_store import AcpStateError

MAX_FILES = 50_000
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_SCOPE_FILES = 512
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


@dataclass(frozen=True)
class _File:
    sha256: str
    size: int
    mode: int
    git_oid: str

    def public(self) -> dict[str, object]:
        return {"sha256": self.sha256, "size": self.size, "mode": self.mode}


def _path(value: str) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(c in value for c in "*?[]{}") or len(value.encode("utf-8")) > 4096):
        raise AcpStateError("acp_writer_invalid_path")
    parsed = PurePosixPath(value)
    if (parsed.is_absolute() or parsed.as_posix() != value
            or any(part in (".", "..") or part.casefold() == ".git"
                   or len(part.encode("utf-8")) > 255 for part in parsed.parts)
            or not parsed.parts):
        raise AcpStateError("acp_writer_invalid_path")
    return value


def validate_permitted_paths(paths: Sequence[str]) -> tuple[str, ...]:
    if isinstance(paths, (str, bytes)) or not paths or len(paths) > MAX_SCOPE_FILES:
        raise AcpStateError("acp_writer_explicit_paths_required")
    result = tuple(sorted({_path(item) for item in paths}))
    for index, path in enumerate(result):
        if any(other.startswith(path + "/") for other in result[index + 1:]):
            raise AcpStateError("acp_writer_scope_is_directory")
    return result


def _directory(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=True):
        raise AcpStateError("acp_writer_directory_invalid")
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AcpStateError("acp_writer_directory_invalid")
    return path


def _read_fd(fd: int, *, oid_length: int = 40, destination: int | None = None) -> _File:
    before = os.fstat(fd)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_uid != os.getuid()):
        raise AcpStateError("acp_writer_regular_file_required")
    mode = stat.S_IMODE(before.st_mode)
    if mode & 0o7000 or not mode & 0o400:
        raise AcpStateError("acp_writer_file_mode_invalid")
    if before.st_size > MAX_FILE_BYTES:
        raise AcpStateError("acp_writer_snapshot_limit")
    digest = hashlib.sha256()
    git_digest = hashlib.sha1() if oid_length == 40 else hashlib.sha256()
    git_digest.update(b"blob " + str(before.st_size).encode() + b"\0")
    size = 0
    while chunk := os.read(fd, 128 * 1024):
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise AcpStateError("acp_writer_snapshot_limit")
        digest.update(chunk)
        git_digest.update(chunk)
        if destination is not None:
            view = memoryview(chunk)
            while view:
                view = view[os.write(destination, view):]
    after = os.fstat(fd)
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
    if size != before.st_size or any(getattr(before, k) != getattr(after, k) for k in fields):
        raise AcpStateError("acp_writer_snapshot_changed")
    return _File(digest.hexdigest(), size, mode, git_digest.hexdigest())


def _scan(root: Path, *, source: bool = False, oid_length: int = 40) -> dict[str, _File]:
    result: dict[str, _File] = {}
    total = 0
    entries = 0

    def visit(fd: int, prefix: str) -> None:
        nonlocal total, entries
        with os.scandir(fd) as iterator:
            names = sorted(entry.name for entry in iterator)
        for name in names:
            if source and not prefix and name == ".git":
                continue
            entries += 1
            if entries > MAX_FILES * 2:
                raise AcpStateError("acp_writer_snapshot_limit")
            relative = _path(prefix + name)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
                try:
                    visit(child, relative + "/")
                finally:
                    os.close(child)
                continue
            file_fd = os.open(name, _FILE_FLAGS, dir_fd=fd)
            try:
                record = _read_fd(file_fd, oid_length=oid_length)
            finally:
                os.close(file_fd)
            result[relative] = record
            total += record.size
            if len(result) > MAX_FILES or total > MAX_TOTAL_BYTES:
                raise AcpStateError("acp_writer_snapshot_limit")

    fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        visit(fd, "")
    except OSError as exc:
        raise AcpStateError("acp_writer_regular_file_required") from exc
    finally:
        os.close(fd)
    return result


def inspect_regular_workspace(root: Path) -> dict[str, dict[str, object]]:
    """Validate a Git-free copy before exposing Unix IPC to a verifier."""
    return {path: record.public() for path, record in _scan(_directory(root)).items()}


def _parent_fd(root_fd: int, relative: str, *, create: bool = False,
               created: list[str] | None = None) -> tuple[int, str]:
    parts = PurePosixPath(relative).parts
    fd = os.dup(root_fd)
    walked: list[str] = []
    try:
        for part in parts[:-1]:
            walked.append(part)
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                    if created is not None:
                        created.append("/".join(walked))
                except FileExistsError:
                    pass
            next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _copy(root: Path, relative: str, destination: Path, expected: _File) -> None:
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    parent_fd = -1
    source_fd = -1
    target_fd = -1
    try:
        parent_fd, name = _parent_fd(root_fd, relative)
        source_fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        actual = _read_fd(source_fd, oid_length=len(expected.git_oid), destination=target_fd)
        if actual != expected:
            raise AcpStateError("acp_writer_snapshot_changed")
        os.fchmod(target_fd, expected.mode)
        os.fsync(target_fd)
    finally:
        for fd in (target_fd, source_fd, parent_fd, root_fd):
            if fd >= 0:
                os.close(fd)


def _git(source: Path, *arguments: str) -> bytes:
    # These calls only inspect object/index metadata; never status/diff/checkout
    # (which may invoke repository filters or external diff programs).
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1")
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.run(
                ["/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                 "-C", str(source), *arguments], env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.DEVNULL, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AcpStateError("acp_writer_git_unavailable") from exc
        if process.returncode or output.tell() > 32 * 1024 * 1024:
            raise AcpStateError("acp_writer_git_unavailable")
        output.seek(0)
        return output.read()


def _git_state(source: Path) -> tuple[str, bytes, dict[str, tuple[str, str]]]:
    dotgit = source / ".git"
    info = dotgit.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
        raise AcpStateError("acp_writer_linked_worktree_required")
    git_link = dotgit.read_bytes()
    if not git_link.startswith(b"gitdir: "):
        raise AcpStateError("acp_writer_linked_worktree_required")
    gitdir = Path(_git(source, "rev-parse", "--absolute-git-dir").decode().strip())
    common = Path(_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
    if (gitdir == common or gitdir.parent != common / "worktrees"
            or gitdir != gitdir.resolve(strict=True) or common != common.resolve(strict=True)
            or (gitdir / "gitdir").read_text().strip() != str(dotgit)
            or _git(source, "rev-parse", "--show-toplevel").decode().strip() != str(source)):
        raise AcpStateError("acp_writer_linked_worktree_required")
    head = _git(source, "rev-parse", "--verify", "HEAD").decode().strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head):
        raise AcpStateError("acp_writer_git_unavailable")
    tree: dict[str, tuple[str, str]] = {}
    for row in _git(source, "ls-tree", "-r", "-z", "--full-tree", head).split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        mode, kind, oid = meta.decode("ascii").split()
        path = _path(name.decode("utf-8"))
        if kind != "blob" or mode not in ("100644", "100755"):
            raise AcpStateError("acp_writer_regular_file_required")
        tree[path] = (mode, oid)
        if len(tree) > MAX_FILES:
            raise AcpStateError("acp_writer_snapshot_limit")
    index: dict[str, tuple[str, str]] = {}
    for row in _git(source, "ls-files", "--stage", "-z").split(b"\0"):
        if not row:
            continue
        meta, name = row.split(b"\t", 1)
        mode, oid, stage = meta.decode("ascii").split()
        if stage != "0":
            raise AcpStateError("acp_writer_source_dirty")
        index[_path(name.decode("utf-8"))] = (mode, oid)
    if index != tree:
        raise AcpStateError("acp_writer_source_dirty")
    return head, git_link, tree


def _digest(manifest: dict[str, _File]) -> str:
    payload = {path: record.public() for path, record in sorted(manifest.items())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def capture_source_snapshot(source: Path, *, require_clean: bool = True) -> dict[str, object]:
    """Read the dedicated worktree without running Git filters, hooks or diff.

    Even after promotion, the registered linked-worktree identity and unchanged
    HEAD index remain mandatory. Only the working files may differ from HEAD.
    """
    try:
        source = _directory(source)
        state = _git_state(source)
        head, _, tree = state
        manifest = _scan(source, source=True, oid_length=len(head))
        if require_clean and (set(manifest) != set(tree) or any(
                record.git_oid != tree[path][1]
                or bool(record.mode & 0o111) != (tree[path][0] == "100755")
                for path, record in manifest.items())):
            raise AcpStateError("acp_writer_source_dirty")
        if (_git_state(source) != state
                or _scan(source, source=True, oid_length=len(head)) != manifest):
            raise AcpStateError("acp_writer_source_changed")
        return {"head": head,
                "manifest": {path: record.public() for path, record in manifest.items()},
                "digest": _digest(manifest)}
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, AcpStateError):
            raise
        raise AcpStateError("acp_writer_snapshot_failed") from exc


class AcpWorkspace:
    """One task-owned detached writable copy. No scheduling or provider calls."""

    def __init__(self, *, source: Path, root: Path, baseline_root: Path,
                 baseline_head: str, permitted_paths: tuple[str, ...],
                 baseline: dict[str, _File], git_link: bytes,
                 tree: dict[str, tuple[str, str]], source_identity: tuple[int, int]):
        self.source = source
        self.root = root
        self.baseline_root = baseline_root
        self.baseline_head = baseline_head
        self.permitted_paths = permitted_paths
        self._baseline = baseline
        self._git_link = git_link
        self._tree = tree
        self._source_identity = source_identity
        self._closed = False
        self._promoted = False

    @classmethod
    def prepare(cls, source: Path, permitted_paths: Sequence[str], state_dir: Path) -> "AcpWorkspace":
        source = _directory(source)
        permitted = validate_permitted_paths(permitted_paths)
        state_dir = Path(state_dir)
        if state_dir == source or source in state_dir.parents or state_dir in source.parents:
            raise AcpStateError("acp_writer_state_overlap")
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_dir = _directory(state_dir)
        if state_dir.stat().st_mode & 0o077:
            raise AcpStateError("acp_private_directory_required")
        try:
            head, git_link, tree = _git_state(source)
            common = Path(_git(source, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
            if state_dir == common or common in state_dir.parents or state_dir in common.parents:
                raise AcpStateError("acp_writer_state_overlap")
            baseline = _scan(source, source=True, oid_length=len(head))
            if set(baseline) != set(tree) or any(
                    record.git_oid != tree[path][1]
                    or bool(record.mode & 0o111) != (tree[path][0] == "100755")
                    for path, record in baseline.items()):
                raise AcpStateError("acp_writer_source_dirty")
            for path in permitted:
                if (source / path).is_dir():
                    raise AcpStateError("acp_writer_scope_is_directory")
            identity = source.stat()
            container = Path(tempfile.mkdtemp(prefix="writer-", dir=state_dir))
            root = container / "tree"
            immutable = container / "baseline"
            root.mkdir(mode=0o700)
            immutable.mkdir(mode=0o700)
            try:
                for path, record in baseline.items():
                    _copy(source, path, root / path, record)
                    _copy(source, path, immutable / path, record)
                if _scan(source, source=True, oid_length=len(head)) != baseline or _git_state(source) != (head, git_link, tree):
                    raise AcpStateError("acp_writer_snapshot_changed")
                for folder, directories, files in os.walk(immutable, topdown=False):
                    for name in files:
                        p = Path(folder) / name
                        p.chmod(p.stat().st_mode & 0o555)
                    Path(folder).chmod(0o500)
                return cls(source=source, root=root, baseline_root=immutable, baseline_head=head,
                           permitted_paths=permitted, baseline=baseline, git_link=git_link,
                           tree=tree, source_identity=(identity.st_dev, identity.st_ino))
            except BaseException:
                _remove_container(container)
                raise
        except (OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, AcpStateError):
                raise
            raise AcpStateError("acp_writer_prepare_failed") from exc

    def _result(self) -> tuple[dict[str, _File], tuple[str, ...]]:
        if self._closed:
            raise AcpStateError("acp_writer_closed")
        manifest = _scan(self.root, oid_length=len(self.baseline_head))
        changed = tuple(sorted(path for path in manifest.keys() | self._baseline.keys()
                               if manifest.get(path) != self._baseline.get(path)))
        if set(changed) - set(self.permitted_paths):
            raise AcpStateError("acp_writer_scope_violation")
        return manifest, changed

    def validate_changes(self) -> tuple[str, ...]:
        return self._result()[1]

    def inspect_changes_unchecked(self, *, max_entries: int = 32) -> dict[str, object]:
        """Bounded rejection evidence, never an authorization to export.

        Only scope enforcement is omitted here. The same no-follow, regular
        file, ownership and size checks still apply. No file contents, absolute
        paths, Git metadata or symlink targets enter the diagnostics.
        """
        if type(max_entries) is not int or not 1 <= max_entries <= 32:
            raise ValueError("Invalid ACP change diagnostic limit")
        if self._closed:
            return {"available": False, "error_code": "acp_writer_closed"}
        try:
            manifest = _scan(self.root, oid_length=len(self.baseline_head))
        except (AcpStateError, OSError):
            return {"available": False, "error_code": "acp_writer_diagnostics_unavailable"}
        changed = [path for path in manifest.keys() | self._baseline.keys()
                   if manifest.get(path) != self._baseline.get(path)]
        allowed = set(self.permitted_paths)
        changed.sort(key=lambda path: (path in allowed, path))
        rows = []
        for path in changed[:max_entries]:
            before, after = self._baseline.get(path), manifest.get(path)
            kind = ("added" if before is None else "deleted" if after is None
                    else "mode_changed" if before.sha256 == after.sha256 and before.size == after.size
                    else "modified")
            rows.append({
                "path": path if len(path.encode("utf-8")) <= 256 else "<long-relative-path>",
                "path_sha256": hashlib.sha256(path.encode("utf-8")).hexdigest(),
                "kind": kind, "in_scope": path in allowed,
                "before": before.public() if before is not None else None,
                "after": after.public() if after is not None else None,
            })
        return {"available": True, "changed_file_count": len(changed),
                "outside_scope_count": len(set(changed) - allowed),
                "changes": rows, "truncated": len(changed) > len(rows)}

    def result_manifest(self) -> dict[str, dict[str, object]]:
        return {path: record.public() for path, record in self._result()[0].items()}

    def baseline_manifest(self) -> dict[str, dict[str, object]]:
        return {path: record.public() for path, record in self._baseline.items()}

    def snapshot_digest(self) -> str:
        return _digest(self._result()[0])

    def _source_unchanged(self) -> None:
        info = self.source.stat(follow_symlinks=False)
        if ((info.st_dev, info.st_ino) != self._source_identity
                or _git_state(self.source) != (self.baseline_head, self._git_link, self._tree)
                or _scan(self.source, source=True, oid_length=len(self.baseline_head)) != self._baseline):
            raise AcpStateError("acp_writer_source_changed")

    def promote(self, *, expected_digest: str | None = None) -> tuple[str, ...]:
        """CAS promotion after independent verification and native teardown.

        Individual file replacements are atomic. A failed multi-file export
        rolls back files still matching our own output; it never overwrites a
        concurrent external edit during rollback.
        """
        if self._promoted:
            raise AcpStateError("acp_writer_already_promoted")
        manifest, changed = self._result()
        if expected_digest is not None and _digest(manifest) != expected_digest:
            raise AcpStateError("acp_writer_result_changed")
        self._source_unchanged()
        staged = self.root.parent / "promotion"
        staged.mkdir(mode=0o700)
        for path in changed:
            if path in manifest:
                _copy(self.root, path, staged / path, manifest[path])
        self._source_unchanged()
        source_fd = os.open(self.source, _DIRECTORY_FLAGS)
        identity = os.fstat(source_fd)
        if (identity.st_dev, identity.st_ino) != self._source_identity:
            os.close(source_fd)
            raise AcpStateError("acp_writer_source_changed")
        applied: list[str] = []
        created: list[str] = []
        try:
            for path in changed:
                _replace_checked(source_fd, path, self._baseline.get(path), manifest.get(path),
                                 staged, created=created, applied=applied)
            expected = dict(self._baseline)
            for path in changed:
                if path in manifest:
                    expected[path] = manifest[path]
                else:
                    expected.pop(path, None)
            if (_scan(self.source, source=True, oid_length=len(self.baseline_head)) != expected
                    or _git_state(self.source) != (self.baseline_head, self._git_link, self._tree)):
                raise AcpStateError("acp_writer_source_changed")
            self._promoted = True
            return changed
        except BaseException:
            rollback_failed = False
            for path in reversed(applied):
                try:
                    _replace_checked(source_fd, path, manifest.get(path), self._baseline.get(path),
                                     self.baseline_root)
                except BaseException:
                    rollback_failed = True
            for relative in reversed(created):
                try:
                    parent_fd, name = _parent_fd(source_fd, relative)
                    try:
                        os.rmdir(name, dir_fd=parent_fd)
                    finally:
                        os.close(parent_fd)
                except OSError:
                    pass
            if rollback_failed:
                raise AcpStateError("acp_writer_rollback_failed")
            raise
        finally:
            os.close(source_fd)

    def close(self) -> None:
        if not self._closed:
            _remove_container(self.root.parent)
            self._closed = True

    def __enter__(self) -> "AcpWorkspace":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _current(parent_fd: int, name: str, expected: _File | None) -> _File | None:
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    try:
        return _read_fd(fd, oid_length=len(expected.git_oid) if expected else 40)
    finally:
        os.close(fd)


def _replace_checked(source_fd: int, relative: str, expected: _File | None,
                     result: _File | None, payload_root: Path, *,
                     created: list[str] | None = None,
                     applied: list[str] | None = None) -> None:
    parent_fd, name = _parent_fd(source_fd, relative, create=result is not None, created=created)
    temporary = ".acp-" + os.urandom(12).hex()
    try:
        if _current(parent_fd, name, expected) != expected:
            raise AcpStateError("acp_writer_source_changed")
        if result is None:
            os.unlink(name, dir_fd=parent_fd)
        else:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent_fd)
            try:
                # Payload is a private prepared snapshot, never a model-selected
                # host pathname. The immutable rollback copy has read-only modes.
                payload = payload_root / relative
                with payload.open("rb") as stream:
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := stream.read(128 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(fd, view):]
                if digest.hexdigest() != result.sha256 or size != result.size:
                    raise AcpStateError("acp_writer_result_changed")
                os.fchmod(fd, result.mode)
                os.fsync(fd)
            finally:
                os.close(fd)
            if _current(parent_fd, name, expected) != expected:
                raise AcpStateError("acp_writer_source_changed")
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        if applied is not None:
            applied.append(relative)
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _remove_container(container: Path) -> None:
    if not container.exists():
        return
    # Baseline directories are read-only; allow trusted cleanup without changing
    # permissions on anything the native helper could point outside this tree.
    for folder, directories, files in os.walk(container, followlinks=False):
        if not Path(folder).is_symlink():
            Path(folder).chmod(0o700)
        for name in directories:
            child = Path(folder) / name
            if not child.is_symlink():
                child.chmod(0o700)
    shutil.rmtree(container)
