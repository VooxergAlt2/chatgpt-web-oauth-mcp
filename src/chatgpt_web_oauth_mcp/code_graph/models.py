from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
from typing import Any, Mapping


class GraphStatus(str, Enum):
    MISSING = "missing"
    BUILDING = "building"
    READY = "ready"
    INVALID = "invalid"


class CodeGraphCacheError(Exception):
    """Base exception for code graph cache operations."""


class GraphAlreadyPublishedError(CodeGraphCacheError):
    """Raised when an attempt is made to commit or republish an already-ready graph."""


class GraphValidationError(CodeGraphCacheError):
    """Raised when manifest or payload integrity validation fails."""


class GraphLockBusyError(CodeGraphCacheError):
    """Raised when a graph build or prune cannot acquire a busy lock non-blockingly."""


@dataclass(frozen=True)
class GraphIdentity:
    """Immutable, content-addressed identity for a code graph."""

    repository_id: str
    git_tree_sha: str
    analyzer_id: str
    schema_version: int
    options: Mapping[str, Any] = field(default_factory=dict)
    graph_id: str = field(default="")

    def __post_init__(self) -> None:
        from .identity import compute_graph_id, freeze_analysis_options

        frozen_options = freeze_analysis_options(self.options)
        object.__setattr__(self, "options", frozen_options)
        if not self.graph_id:
            computed = compute_graph_id(
                repository_id=self.repository_id,
                git_tree_sha=self.git_tree_sha,
                analyzer_id=self.analyzer_id,
                schema_version=self.schema_version,
                options=frozen_options,
            )
            object.__setattr__(self, "graph_id", computed)


@dataclass(frozen=True)
class GraphManifest:
    """Schema-versioned manifest metadata stored alongside the graph payload."""

    manifest_version: int
    graph_id: str
    repository_id: str
    git_tree_sha: str
    analyzer_id: str
    schema_version: int
    options: dict[str, Any]
    payload_filename: str
    payload_size_bytes: int
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "graph_id": self.graph_id,
            "repository_id": self.repository_id,
            "git_tree_sha": self.git_tree_sha,
            "analyzer_id": self.analyzer_id,
            "schema_version": self.schema_version,
            "options": self.options,
            "payload_filename": self.payload_filename,
            "payload_size_bytes": self.payload_size_bytes,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GraphManifest:
        try:
            return cls(
                manifest_version=int(data["manifest_version"]),
                graph_id=str(data["graph_id"]),
                repository_id=str(data["repository_id"]),
                git_tree_sha=str(data["git_tree_sha"]),
                analyzer_id=str(data["analyzer_id"]),
                schema_version=int(data["schema_version"]),
                options=dict(data.get("options") or {}),
                payload_filename=str(data["payload_filename"]),
                payload_size_bytes=int(data["payload_size_bytes"]),
                created_at=str(data["created_at"]),
                metadata=dict(data.get("metadata") or {}),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GraphValidationError(f"Invalid graph manifest structure: {exc}") from exc

    @classmethod
    def from_json_bytes(cls, content: bytes) -> GraphManifest:
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GraphValidationError(f"Unparseable graph manifest JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise GraphValidationError("Graph manifest root must be a JSON object.")
        return cls.from_dict(parsed)


@dataclass(frozen=True)
class GraphEntry:
    """Status and location information for a cached graph entry."""

    graph_id: str
    repository_id: str
    status: GraphStatus
    path: Path
    manifest: GraphManifest | None = None
    payload_path: Path | None = None
    error: str | None = None


@dataclass(frozen=True)
class GCSummary:
    """Summary of deterministic retention and pruning execution."""

    deleted_graph_ids: tuple[str, ...]
    bytes_freed: int
    retained_graph_count: int
    retained_total_bytes: int
    deleted_staging_dirs: tuple[str, ...] = ()
    staging_bytes_freed: int = 0
