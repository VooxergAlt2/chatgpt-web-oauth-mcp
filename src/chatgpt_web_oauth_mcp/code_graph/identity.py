from __future__ import annotations

import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping

from .models import GraphIdentity


def normalize_analysis_options(options: Mapping[str, Any] | None) -> dict[str, Any]:
    """Recursively normalize analysis options for deterministic hashing and serialization."""
    if not options:
        return {}

    def _normalize_value(val: Any) -> Any:
        if isinstance(val, Mapping):
            if not all(isinstance(key, str) for key in val):
                raise TypeError("Analysis option mapping keys must be strings.")
            return {k: _normalize_value(val[k]) for k in sorted(val.keys())}
        if isinstance(val, (list, tuple)):
            return [_normalize_value(elem) for elem in val]
        if isinstance(val, float):
            if not math.isfinite(val):
                raise ValueError("Analysis option floats must be finite.")
            return val
        if val is None or isinstance(val, (str, int, bool)):
            return val
        raise TypeError(f"Unsupported option value type: {type(val).__name__} ({val!r})")

    if not all(isinstance(key, str) for key in options):
        raise TypeError("Analysis option mapping keys must be strings.")
    return {k: _normalize_value(options[k]) for k in sorted(options.keys())}


def freeze_analysis_options(options: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Return an immutable recursive snapshot of normalized analysis options."""
    normalized = normalize_analysis_options(options)

    def _freeze(value: Any) -> Any:
        if isinstance(value, dict):
            return MappingProxyType({key: _freeze(child) for key, child in value.items()})
        if isinstance(value, list):
            return tuple(_freeze(child) for child in value)
        return value

    return _freeze(normalized)


def compute_graph_id(
    *,
    repository_id: str,
    git_tree_sha: str,
    analyzer_id: str,
    schema_version: int,
    options: Mapping[str, Any] | None = None,
) -> str:
    """Deterministically compute SHA-256 graph ID from immutable identity components."""
    if not isinstance(repository_id, str) or not repository_id.strip():
        raise ValueError("repository_id must be a non-empty string.")
    repository_id = repository_id.strip()
    if "/" in repository_id or "\\" in repository_id or ".." in repository_id or "\0" in repository_id:
        raise ValueError(f"repository_id contains invalid path traversal characters: {repository_id!r}")
    if not isinstance(git_tree_sha, str) or not git_tree_sha.strip():
        raise ValueError("git_tree_sha must be a non-empty string.")
    git_tree_sha = git_tree_sha.strip()
    if not isinstance(analyzer_id, str) or not analyzer_id.strip():
        raise ValueError("analyzer_id must be a non-empty string.")
    analyzer_id = analyzer_id.strip()
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise ValueError("schema_version must be a positive integer.")

    normalized_options = normalize_analysis_options(options)

    canonical_payload = {
        "analyzer_id": analyzer_id,
        "git_tree_sha": git_tree_sha,
        "options": normalized_options,
        "repository_id": repository_id,
        "schema_version": schema_version,
    }

    canonical_bytes = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    return hashlib.sha256(canonical_bytes).hexdigest()


def create_graph_identity(
    *,
    repository_id: str,
    git_tree_sha: str,
    analyzer_id: str,
    schema_version: int = 1,
    options: Mapping[str, Any] | None = None,
) -> GraphIdentity:
    """Create a validated, immutable GraphIdentity instance."""
    normalized_options = normalize_analysis_options(options)
    graph_id = compute_graph_id(
        repository_id=repository_id,
        git_tree_sha=git_tree_sha,
        analyzer_id=analyzer_id,
        schema_version=schema_version,
        options=normalized_options,
    )
    return GraphIdentity(
        repository_id=repository_id.strip(),
        git_tree_sha=git_tree_sha.strip(),
        analyzer_id=analyzer_id.strip(),
        schema_version=schema_version,
        options=freeze_analysis_options(normalized_options),
        graph_id=graph_id,
    )
