from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import BinaryIO, Iterator, Mapping
import uuid

from .. import config
from ..state_io import (
    atomic_write_bytes,
    ensure_private_directory,
    interprocess_file_lock,
    is_file_locked,
    open_private_write_binary,
)
from .identity import normalize_analysis_options
from .models import (
    CodeGraphCacheError,
    GCSummary,
    GraphAlreadyPublishedError,
    GraphEntry,
    GraphIdentity,
    GraphManifest,
    GraphStatus,
    GraphValidationError,
)

DEFAULT_PAYLOAD_FILENAME = "cpg.bin"
MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 2
DEFAULT_STAGING_TTL_SECONDS = 24 * 60 * 60
_SAFE_REPOSITORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_GRAPH_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_PAYLOAD_VALIDATION_CACHE_MAX_ENTRIES = 512


def _validate_repository_id(repository_id: str) -> str:
    if not isinstance(repository_id, str) or not _SAFE_REPOSITORY_ID_RE.fullmatch(repository_id):
        raise ValueError("repository_id must be one safe path segment (letters, digits, '.', '_' or '-').")
    if repository_id in {".", ".."} or ".." in repository_id:
        raise ValueError("repository_id must not contain path traversal components.")
    return repository_id


def _validate_graph_id(graph_id: str) -> str:
    if not isinstance(graph_id, str) or not _GRAPH_ID_RE.fullmatch(graph_id):
        raise ValueError("graph_id must be a lowercase 64-character SHA-256 hex digest.")
    return graph_id


def _validate_payload_filename(payload_filename: str) -> str:
    if (
        not isinstance(payload_filename, str)
        or not _PAYLOAD_FILENAME_RE.fullmatch(payload_filename)
        or payload_filename in {".", ".."}
        or ".." in payload_filename
        or "/" in payload_filename
        or "\\" in payload_filename
    ):
        raise GraphValidationError("payload_filename must be one safe relative filename.")
    return payload_filename


def _directory_size(path: Path) -> int:
    return sum(
        item.stat().st_size
        for item in path.glob("**/*")
        if item.is_file() and not item.is_symlink()
    )


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class GraphBuildSession:
    """Active staging session for building and publishing a graph."""

    def __init__(
        self,
        cache: CodeGraphCache,
        identity: GraphIdentity,
        staging_dir: Path | None,
        payload_filename: str,
        *,
        is_ready: bool = False,
        existing_entry: GraphEntry | None = None,
    ) -> None:
        self.cache = cache
        self.identity = identity
        self.staging_dir = staging_dir
        self.payload_filename = _validate_payload_filename(payload_filename)
        self.is_ready = is_ready
        self.existing_entry = existing_entry
        self.is_committed = False
        self._payload_size: int | None = None

    @property
    def payload_path(self) -> Path:
        if self.staging_dir is None:
            raise CodeGraphCacheError("Graph is already ready; no staging payload path available.")
        return self.staging_dir / self.payload_filename

    def open_payload_write(self, *, buffering: int = -1) -> BinaryIO:
        """Open payload file in staging for streaming writing without symlink traversal."""
        if self.is_ready:
            raise GraphAlreadyPublishedError("Cannot write payload: graph is already published and ready.")
        if self.staging_dir is None:
            raise CodeGraphCacheError("Staging directory is not available.")
        return open_private_write_binary(self.payload_path, buffering=buffering)

    def write_payload_stream(self, stream: BinaryIO | Iterator[bytes], *, chunk_size: int = 65536) -> int:
        """Stream bytes directly to staging payload file without full in-memory buffering."""
        if self.is_ready:
            raise GraphAlreadyPublishedError("Cannot write payload: graph is already published and ready.")
        total_written = 0
        with self.open_payload_write() as handle:
            if hasattr(stream, "read"):
                while True:
                    chunk = stream.read(chunk_size)  # type: ignore[union-attr]
                    if not chunk:
                        break
                    handle.write(chunk)
                    total_written += len(chunk)
            else:
                for chunk in stream:  # type: ignore[union-attr]
                    if chunk:
                        handle.write(chunk)
                        total_written += len(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        self._payload_size = total_written
        return total_written

    def copy_payload_from_file(self, source_path: Path) -> int:
        """Streamingly copy a payload file into staging without symlink following."""
        source_path = Path(source_path).expanduser()
        if source_path.is_symlink():
            raise ValueError("Payload source file must not be a symbolic link.")
        source_path = source_path.resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Payload source file does not exist: {source_path}")
        with source_path.open("rb") as src_handle:
            return self.write_payload_stream(src_handle)

    def commit(self, *, metadata: Mapping[str, object] | None = None) -> GraphEntry:
        """Validate staging payload + manifest, and atomically publish to final cache path."""
        if self.is_ready:
            raise GraphAlreadyPublishedError(
                f"Graph {self.identity.graph_id} is already built and ready; publication rejected."
            )
        if self.is_committed:
            raise CodeGraphCacheError("Graph build session has already been committed.")
        if self.staging_dir is None:
            raise CodeGraphCacheError("Staging directory is missing.")

        if not self.payload_path.exists():
            raise GraphValidationError(f"Payload file was not created: {self.payload_path}")
        if self.payload_path.is_symlink():
            raise GraphValidationError("Payload file must not be a symbolic link.")

        actual_size = self.payload_path.stat().st_size
        payload_sha256 = sha256_file(self.payload_path)
        manifest = GraphManifest(
            manifest_version=MANIFEST_SCHEMA_VERSION,
            graph_id=self.identity.graph_id,
            repository_id=self.identity.repository_id,
            git_tree_sha=self.identity.git_tree_sha,
            analyzer_id=self.identity.analyzer_id,
            schema_version=self.identity.schema_version,
            options=normalize_analysis_options(self.identity.options),
            payload_filename=self.payload_filename,
            payload_size_bytes=actual_size,
            payload_sha256=payload_sha256,
            created_at=datetime.now(timezone.utc).isoformat(),
            metadata=dict(metadata or {}),
        )

        staging_manifest_path = self.staging_dir / MANIFEST_FILENAME
        atomic_write_bytes(staging_manifest_path, manifest.to_json_bytes())

        # Validate complete staging directory before publication
        self.cache.validate_manifest_payload(
            manifest_path=staging_manifest_path,
            payload_path=self.payload_path,
            expected_graph_id=self.identity.graph_id,
            expected_repo_id=self.identity.repository_id,
        )

        # Atomic publication
        final_dir = self.cache.graph_dir(self.identity.repository_id, self.identity.graph_id)
        self.cache._publish_staging_dir(self.staging_dir, final_dir)
        self.is_committed = True

        return GraphEntry(
            graph_id=self.identity.graph_id,
            repository_id=self.identity.repository_id,
            status=GraphStatus.READY,
            path=final_dir,
            manifest=manifest,
            payload_path=final_dir / self.payload_filename,
        )


class CodeGraphCache:
    """Safe, immutable on-disk cache core for code property graphs."""

    _thread_locks: dict[tuple[str, str], threading.Lock] = {}
    _thread_lock_guard = threading.Lock()
    _payload_validation_cache: OrderedDict[
        str,
        tuple[tuple[int, int, int, int, int], str],
    ] = OrderedDict()
    _payload_validation_guard = threading.Lock()

    def __init__(self, root_dir: Path | str | None = None) -> None:
        if root_dir is None:
            base = config.STATE_DIR / "code-graph"
        else:
            p = Path(root_dir).expanduser().resolve()
            base = p if p.name == "code-graph" else p / "code-graph"
        self.base_dir = base
        ensure_private_directory(self.base_dir)
        self.repositories_dir = self.base_dir / "repositories"
        ensure_private_directory(self.repositories_dir)

    def _get_thread_lock(self, repository_id: str, graph_id: str) -> threading.Lock:
        key = (repository_id, graph_id)
        with self._thread_lock_guard:
            if key not in self._thread_locks:
                self._thread_locks[key] = threading.Lock()
            return self._thread_locks[key]

    @staticmethod
    def _payload_stat_identity(path: Path) -> tuple[int, int, int, int, int]:
        stat = path.stat()
        return (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )

    @classmethod
    def _validate_payload_sha256(cls, path: Path, expected_sha256: str) -> None:
        if not _GRAPH_ID_RE.fullmatch(expected_sha256):
            raise GraphValidationError(
                "payload_sha256 must be a lowercase 64-character SHA-256 hex digest."
            )
        resolved = Path(path).resolve()
        stat_before = cls._payload_stat_identity(resolved)
        cache_key = str(resolved)
        with cls._payload_validation_guard:
            cached = cls._payload_validation_cache.get(cache_key)
            if cached == (stat_before, expected_sha256):
                cls._payload_validation_cache.move_to_end(cache_key)
                return

        actual_sha256 = sha256_file(resolved)
        stat_after = cls._payload_stat_identity(resolved)
        if stat_after != stat_before:
            raise GraphValidationError("Payload changed during SHA-256 validation.")
        if actual_sha256 != expected_sha256:
            raise GraphValidationError(
                f"Payload SHA-256 mismatch: expected {expected_sha256}, "
                f"found {actual_sha256}"
            )

        with cls._payload_validation_guard:
            cls._payload_validation_cache[cache_key] = (stat_after, actual_sha256)
            cls._payload_validation_cache.move_to_end(cache_key)
            while len(cls._payload_validation_cache) > _PAYLOAD_VALIDATION_CACHE_MAX_ENTRIES:
                cls._payload_validation_cache.popitem(last=False)

    def repo_dir(self, repository_id: str) -> Path:
        repository_id = _validate_repository_id(repository_id)
        path = self.repositories_dir / repository_id
        ensure_private_directory(path)
        return path

    def graphs_dir(self, repository_id: str) -> Path:
        path = self.repo_dir(repository_id) / "graphs"
        ensure_private_directory(path)
        return path

    def graph_dir(self, repository_id: str, graph_id: str) -> Path:
        _validate_repository_id(repository_id)
        _validate_graph_id(graph_id)
        return self.graphs_dir(repository_id) / graph_id

    def staging_root(self, repository_id: str) -> Path:
        path = self.repo_dir(repository_id) / "staging"
        ensure_private_directory(path)
        return path

    def locks_root(self, repository_id: str) -> Path:
        path = self.repo_dir(repository_id) / "locks"
        ensure_private_directory(path)
        return path

    def lock_file(self, repository_id: str, graph_id: str) -> Path:
        _validate_repository_id(repository_id)
        _validate_graph_id(graph_id)
        return self.locks_root(repository_id) / f"{graph_id}.lock"

    def _is_active_build(self, repository_id: str, graph_id: str) -> bool:
        """Check if graph is currently actively locked by any thread or process."""
        lock = self._get_thread_lock(repository_id, graph_id)
        if lock.locked():
            return True
        lock_path = self.lock_file(repository_id, graph_id)
        return is_file_locked(lock_path)

    def validate_manifest_payload(
        self,
        *,
        manifest_path: Path,
        payload_path: Path,
        expected_graph_id: str | None = None,
        expected_repo_id: str | None = None,
    ) -> GraphManifest:
        """Validate manifest structure, identity match, and payload existence/size."""
        if manifest_path.is_symlink():
            raise GraphValidationError("Manifest file must not be a symbolic link.")
        if not manifest_path.is_file():
            raise GraphValidationError(f"Manifest file does not exist: {manifest_path}")

        try:
            content = manifest_path.read_bytes()
            manifest = GraphManifest.from_json_bytes(content)
        except Exception as exc:
            raise GraphValidationError(f"Failed to load graph manifest: {exc}") from exc

        if manifest.manifest_version != MANIFEST_SCHEMA_VERSION:
            raise GraphValidationError(
                f"Unsupported manifest version: {manifest.manifest_version} (expected {MANIFEST_SCHEMA_VERSION})"
            )

        if expected_graph_id is not None and manifest.graph_id != expected_graph_id:
            raise GraphValidationError(
                f"Manifest graph_id mismatch: expected {expected_graph_id}, got {manifest.graph_id}"
            )
        if expected_repo_id is not None and manifest.repository_id != expected_repo_id:
            raise GraphValidationError(
                f"Manifest repository_id mismatch: expected {expected_repo_id}, got {manifest.repository_id}"
            )

        payload_filename = _validate_payload_filename(manifest.payload_filename)
        if payload_path.name != payload_filename or payload_path.parent != manifest_path.parent:
            raise GraphValidationError("Payload path does not match the manifest cache directory.")

        if payload_path.is_symlink():
            raise GraphValidationError("Payload file must not be a symbolic link.")
        if not payload_path.is_file():
            raise GraphValidationError(f"Payload file does not exist: {payload_path}")

        actual_size = payload_path.stat().st_size
        if actual_size != manifest.payload_size_bytes:
            raise GraphValidationError(
                f"Payload size mismatch: expected {manifest.payload_size_bytes} bytes, found {actual_size} bytes"
            )

        self._validate_payload_sha256(payload_path, manifest.payload_sha256)

        return manifest

    def status(
        self,
        repository_id: str,
        graph_id: str,
        *,
        expected_identity: GraphIdentity | None = None,
    ) -> GraphEntry:
        """Lookup and distinguish missing / building / ready / invalid states."""
        target_dir = self.graph_dir(repository_id, graph_id)
        is_active = self._is_active_build(repository_id, graph_id)

        if not target_dir.exists():
            if is_active:
                return GraphEntry(
                    graph_id=graph_id,
                    repository_id=repository_id,
                    status=GraphStatus.BUILDING,
                    path=target_dir,
                )
            return GraphEntry(
                graph_id=graph_id,
                repository_id=repository_id,
                status=GraphStatus.MISSING,
                path=target_dir,
            )

        if target_dir.is_symlink():
            return GraphEntry(
                graph_id=graph_id,
                repository_id=repository_id,
                status=GraphStatus.INVALID,
                path=target_dir,
                error="Graph directory must not be a symbolic link.",
            )

        if not target_dir.is_dir():
            return GraphEntry(
                graph_id=graph_id,
                repository_id=repository_id,
                status=GraphStatus.INVALID,
                path=target_dir,
                error="Graph path exists but is not a directory.",
            )

        manifest_path = target_dir / MANIFEST_FILENAME
        if not manifest_path.exists():
            return GraphEntry(
                graph_id=graph_id,
                repository_id=repository_id,
                status=GraphStatus.BUILDING if is_active else GraphStatus.INVALID,
                path=target_dir,
                error="Missing manifest.json",
            )

        try:
            # We first parse manifest to discover payload filename
            content = manifest_path.read_bytes()
            manifest = GraphManifest.from_json_bytes(content)
            payload_filename = _validate_payload_filename(manifest.payload_filename)
            payload_path = target_dir / payload_filename
            self.validate_manifest_payload(
                manifest_path=manifest_path,
                payload_path=payload_path,
                expected_graph_id=graph_id,
                expected_repo_id=repository_id,
            )

            if expected_identity is not None:
                if (
                    manifest.git_tree_sha != expected_identity.git_tree_sha
                    or manifest.analyzer_id != expected_identity.analyzer_id
                    or manifest.schema_version != expected_identity.schema_version
                    or normalize_analysis_options(manifest.options)
                    != normalize_analysis_options(expected_identity.options)
                ):
                    return GraphEntry(
                        graph_id=graph_id,
                        repository_id=repository_id,
                        status=GraphStatus.INVALID,
                        path=target_dir,
                        error="Manifest does not match expected graph identity details.",
                    )

            return GraphEntry(
                graph_id=graph_id,
                repository_id=repository_id,
                status=GraphStatus.READY,
                path=target_dir,
                manifest=manifest,
                payload_path=payload_path,
            )
        except Exception as exc:
            if is_active:
                return GraphEntry(
                    graph_id=graph_id,
                    repository_id=repository_id,
                    status=GraphStatus.BUILDING,
                    path=target_dir,
                )
            return GraphEntry(
                graph_id=graph_id,
                repository_id=repository_id,
                status=GraphStatus.INVALID,
                path=target_dir,
                error=str(exc),
            )

    def lookup(
        self,
        repository_id: str,
        graph_id: str,
        *,
        expected_identity: GraphIdentity | None = None,
    ) -> GraphEntry:
        """Alias for status check."""
        return self.status(repository_id, graph_id, expected_identity=expected_identity)

    def _publish_staging_dir(self, staging_dir: Path, target_dir: Path) -> None:
        """Atomically rename staging directory to final target directory."""
        repo_dir = target_dir.parent.parent
        trash_dir = repo_dir / ".trash"

        if target_dir.exists():
            ensure_private_directory(trash_dir)
            temp_trash = trash_dir / f"{target_dir.name}_{uuid.uuid4().hex}"
            os.replace(target_dir, temp_trash)
            os.replace(staging_dir, target_dir)
            shutil.rmtree(temp_trash, ignore_errors=True)
        else:
            os.replace(staging_dir, target_dir)

        if os.name == "posix":
            parent_fd = os.open(target_dir.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    @contextmanager
    def prepare(
        self,
        identity: GraphIdentity,
        *,
        payload_filename: str = DEFAULT_PAYLOAD_FILENAME,
    ) -> Iterator[GraphBuildSession]:
        """Acquire threading + interprocess file locks and prepare build session with safe staging."""
        _validate_repository_id(identity.repository_id)
        _validate_graph_id(identity.graph_id)
        payload_filename = _validate_payload_filename(payload_filename)
        thread_lock = self._get_thread_lock(identity.repository_id, identity.graph_id)
        lock_file_path = self.lock_file(identity.repository_id, identity.graph_id)

        with thread_lock:
            with interprocess_file_lock(lock_file_path, exclusive=True):
                # Check inside the double lock if already ready
                existing_entry = self.status(identity.repository_id, identity.graph_id, expected_identity=identity)
                if existing_entry.status == GraphStatus.READY:
                    yield GraphBuildSession(
                        cache=self,
                        identity=identity,
                        staging_dir=None,
                        payload_filename=payload_filename,
                        is_ready=True,
                        existing_entry=existing_entry,
                    )
                    return

                staging_dir = self.staging_root(identity.repository_id) / f"{identity.graph_id}_{uuid.uuid4().hex}"
                ensure_private_directory(staging_dir)
                session = GraphBuildSession(
                    cache=self,
                    identity=identity,
                    staging_dir=staging_dir,
                    payload_filename=payload_filename,
                    is_ready=False,
                )
                try:
                    yield session
                finally:
                    if not session.is_committed and staging_dir.exists():
                        shutil.rmtree(staging_dir, ignore_errors=True)

    def gc(
        self,
        repository_id: str | None = None,
        *,
        max_total_bytes: int | None = None,
        max_graph_count: int | None = None,
        staging_ttl_seconds: float = DEFAULT_STAGING_TTL_SECONDS,
    ) -> GCSummary:
        """Deterministic, conservative GC based on max total bytes and/or max graph count."""
        if max_total_bytes is not None and max_total_bytes < 0:
            raise ValueError("max_total_bytes must be non-negative.")
        if max_graph_count is not None and max_graph_count < 0:
            raise ValueError("max_graph_count must be non-negative.")
        if staging_ttl_seconds < 0:
            raise ValueError("staging_ttl_seconds must be non-negative.")

        repos = [repository_id] if repository_id is not None else [
            d.name for d in self.repositories_dir.iterdir() if d.is_dir() and not d.is_symlink()
        ]

        class _Candidate:
            def __init__(self, repo_id: str, g_id: str, path: Path, size: int, mtime: float):
                self.repo_id = repo_id
                self.graph_id = g_id
                self.path = path
                self.size = size
                self.mtime = mtime

        candidates: list[_Candidate] = []
        active_bytes = 0
        active_count = 0
        deleted_staging_dirs: list[str] = []
        staging_bytes_freed = 0

        cutoff = time.time() - staging_ttl_seconds
        for r_id in sorted(repos):
            staging_root = self.staging_root(r_id)
            for staging in sorted(staging_root.iterdir(), key=lambda item: item.name):
                if staging.is_symlink() or not staging.is_dir():
                    continue
                graph_id, separator, _suffix = staging.name.partition("_")
                if not separator or not _GRAPH_ID_RE.fullmatch(graph_id):
                    continue
                try:
                    if staging.stat().st_mtime > cutoff:
                        continue
                except OSError:
                    continue
                thread_lock = self._get_thread_lock(r_id, graph_id)
                if not thread_lock.acquire(blocking=False):
                    continue
                try:
                    lock_path = self.lock_file(r_id, graph_id)
                    try:
                        with interprocess_file_lock(lock_path, exclusive=True, blocking=False):
                            if not staging.exists() or staging.is_symlink() or not staging.is_dir():
                                continue
                            size = _directory_size(staging)
                            trash = (
                                self.repo_dir(r_id)
                                / ".trash"
                                / f"staging_{staging.name}_{uuid.uuid4().hex}"
                            )
                            ensure_private_directory(trash.parent)
                            os.replace(staging, trash)
                            shutil.rmtree(trash, ignore_errors=True)
                            deleted_staging_dirs.append(staging.name)
                            staging_bytes_freed += size
                    except (BlockingIOError, OSError):
                        continue
                finally:
                    thread_lock.release()

        for r_id in sorted(repos):
            g_dir = self.graphs_dir(r_id)
            if not g_dir.exists():
                continue
            for item in g_dir.iterdir():
                if item.is_symlink() or not item.is_dir():
                    continue
                g_id = item.name

                # Check if locked / active build - never delete active staging or active build!
                if self._is_active_build(r_id, g_id):
                    # Sum size of active graph
                    item_size = _directory_size(item)
                    active_bytes += item_size
                    active_count += 1
                    continue

                item_size = _directory_size(item)
                item_mtime = item.stat().st_mtime
                candidates.append(_Candidate(r_id, g_id, item, item_size, item_mtime))

        # Deterministic sort: oldest mtime first; graph_id string as tie-breaker
        candidates.sort(key=lambda c: (c.mtime, c.graph_id))

        deleted_ids: list[str] = []
        freed_bytes = 0

        total_bytes = active_bytes + sum(c.size for c in candidates)
        total_count = active_count + len(candidates)

        for candidate in candidates:
            needs_eviction = False
            if max_graph_count is not None and total_count > max_graph_count:
                needs_eviction = True
            if max_total_bytes is not None and total_bytes > max_total_bytes:
                needs_eviction = True

            if not needs_eviction:
                break

            # Attempt safe eviction with non-blocking lock to guarantee no concurrent builder is using it
            lock_path = self.lock_file(candidate.repo_id, candidate.graph_id)
            thread_lock = self._get_thread_lock(candidate.repo_id, candidate.graph_id)
            if not thread_lock.acquire(blocking=False):
                continue
            try:
                try:
                    with interprocess_file_lock(lock_path, exclusive=True, blocking=False):
                        if not candidate.path.exists():
                            continue
                        trash = (
                            self.repo_dir(candidate.repo_id)
                            / ".trash"
                            / f"{candidate.graph_id}_{uuid.uuid4().hex}"
                        )
                        ensure_private_directory(trash.parent)
                        os.replace(candidate.path, trash)
                        shutil.rmtree(trash, ignore_errors=True)
                        deleted_ids.append(candidate.graph_id)
                        freed_bytes += candidate.size
                        total_bytes -= candidate.size
                        total_count -= 1
                except (BlockingIOError, OSError):
                    # Lock is held; skip this graph conservatively
                    continue
            finally:
                thread_lock.release()

        return GCSummary(
            deleted_graph_ids=tuple(deleted_ids),
            bytes_freed=freed_bytes,
            retained_graph_count=total_count,
            retained_total_bytes=total_bytes,
            deleted_staging_dirs=tuple(deleted_staging_dirs),
            staging_bytes_freed=staging_bytes_freed,
        )
