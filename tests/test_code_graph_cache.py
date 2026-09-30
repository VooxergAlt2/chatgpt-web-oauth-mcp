from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import threading
import time
import pytest

from chatgpt_web_oauth_mcp.code_graph import (
    CodeGraphCache,
    CodeGraphCacheError,
    GraphAlreadyPublishedError,
    GraphIdentity,
    GraphManifest,
    GraphStatus,
    GraphValidationError,
    compute_graph_id,
    create_graph_identity,
    normalize_analysis_options,
)
from chatgpt_web_oauth_mcp.state_io import atomic_write_bytes, interprocess_file_lock


def test_deterministic_identity_and_options_ordering() -> None:
    options_a = {
        "depth": 5,
        "tags": ["frontend", "security"],
        "extra": {"z": 100, "a": 200, "nested": {"k2": True, "k1": False}},
    }
    options_b = {
        "extra": {"nested": {"k1": False, "k2": True}, "a": 200, "z": 100},
        "tags": ["frontend", "security"],
        "depth": 5,
    }

    assert normalize_analysis_options(options_a) == normalize_analysis_options(options_b)

    id_a = compute_graph_id(
        repository_id="repo-alpha",
        git_tree_sha="abcdef1234567890abcdef1234567890abcdef12",
        analyzer_id="joern:v1.1@sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        schema_version=1,
        options=options_a,
    )
    id_b = compute_graph_id(
        repository_id="repo-alpha",
        git_tree_sha="abcdef1234567890abcdef1234567890abcdef12",
        analyzer_id="joern:v1.1@sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        schema_version=1,
        options=options_b,
    )

    assert id_a == id_b
    assert len(id_a) == 64

    # Object creation convenience
    ident_a = create_graph_identity(
        repository_id="repo-alpha",
        git_tree_sha="abcdef1234567890abcdef1234567890abcdef12",
        analyzer_id="joern:v1.1@sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        schema_version=1,
        options=options_a,
    )
    assert ident_a.graph_id == id_a

    with pytest.raises(TypeError):
        GraphIdentity(
            repository_id="repo-alpha",
            git_tree_sha="abcdef1234567890abcdef1234567890abcdef12",
            analyzer_id="joern:v1.1@sha256:test",
            schema_version=1,
            graph_id="0" * 64,  # type: ignore[call-arg]
        )


def test_distinct_analyzer_schema_options_identities() -> None:
    base = {
        "repository_id": "repo-alpha",
        "git_tree_sha": "1111111111111111111111111111111111111111",
        "analyzer_id": "joern:v1.1@sha256:aaaa",
        "schema_version": 1,
        "options": {"opt": "val1"},
    }
    base_id = compute_graph_id(**base)

    # Different analyzer_id (e.g. pinned image digest)
    diff_analyzer = dict(base, analyzer_id="joern:v1.1@sha256:bbbb")
    assert compute_graph_id(**diff_analyzer) != base_id

    # Different schema_version
    diff_schema = dict(base, schema_version=2)
    assert compute_graph_id(**diff_schema) != base_id

    # Different git_tree_sha
    diff_tree = dict(base, git_tree_sha="2222222222222222222222222222222222222222")
    assert compute_graph_id(**diff_tree) != base_id

    # Different repository_id
    diff_repo = dict(base, repository_id="repo-beta")
    assert compute_graph_id(**diff_repo) != base_id

    # Different options
    diff_opts = dict(base, options={"opt": "val2"})
    assert compute_graph_id(**diff_opts) != base_id


def test_identity_rejects_ambiguous_values_and_freezes_options() -> None:
    mutable = {"nested": {"items": [1, 2]}}
    ident = create_graph_identity(
        repository_id="repo-safe",
        git_tree_sha="tree-safe",
        analyzer_id="joern@sha256:abc",
        schema_version=1,
        options=mutable,
    )
    original_id = ident.graph_id
    mutable["nested"]["items"].append(3)
    assert ident.graph_id == original_id
    assert ident.options["nested"]["items"] == (1, 2)
    with pytest.raises(TypeError):
        ident.options["new"] = True  # type: ignore[index]

    for bad in ("", "   "):
        with pytest.raises(ValueError):
            compute_graph_id(
                repository_id="repo",
                git_tree_sha=bad,
                analyzer_id="analyzer",
                schema_version=1,
            )
    with pytest.raises(ValueError):
        compute_graph_id(
            repository_id="repo",
            git_tree_sha="tree",
            analyzer_id="analyzer",
            schema_version=True,
        )
    with pytest.raises(ValueError):
        normalize_analysis_options({"bad": math.inf})
    with pytest.raises(TypeError):
        normalize_analysis_options({1: "bad"})  # type: ignore[dict-item]


def test_staging_and_atomic_publication(tmp_path: Path) -> None:
    cache = CodeGraphCache(tmp_path)
    ident = create_graph_identity(
        repository_id="repo-test",
        git_tree_sha="abc12345",
        analyzer_id="joern:pinned-digest-abc",
        schema_version=1,
        options={"optimize": True},
    )

    # Initial status is MISSING
    entry_before = cache.status(ident.repository_id, ident.graph_id)
    assert entry_before.status == GraphStatus.MISSING
    assert not cache.graph_dir(ident.repository_id, ident.graph_id).exists()

    payload_data = b"STREAMED_GRAPH_PAYLOAD_BYTES" * 1024

    with cache.prepare(ident) as session:
        assert not session.is_ready
        assert session.staging_dir is not None
        assert session.staging_dir.exists()
        # Still not visible as READY in final directory
        assert not cache.graph_dir(ident.repository_id, ident.graph_id).exists()
        # Status during build is BUILDING
        assert cache.status(ident.repository_id, ident.graph_id).status == GraphStatus.BUILDING

        # Write large payload via streaming (never buffered into atomic_write_bytes)
        written = session.write_payload_stream(iter([payload_data]))
        assert written == len(payload_data)

        # Commit validates and atomically publishes
        entry = session.commit(metadata={"generator": "test"})
        assert entry.status == GraphStatus.READY
        assert session.is_committed

    final_dir = cache.graph_dir(ident.repository_id, ident.graph_id)
    assert final_dir.exists()
    status_after = cache.status(ident.repository_id, ident.graph_id)
    assert status_after.status == GraphStatus.READY
    assert status_after.manifest is not None
    assert status_after.manifest.payload_size_bytes == len(payload_data)
    assert status_after.manifest.metadata == {"generator": "test"}
    assert status_after.payload_path is not None
    assert status_after.payload_path.read_bytes() == payload_data


def test_staging_abort_cleans_up_and_leaves_no_corrupt_graph(tmp_path: Path) -> None:
    cache = CodeGraphCache(tmp_path)
    ident = create_graph_identity(
        repository_id="repo-abort",
        git_tree_sha="abc999",
        analyzer_id="joern:pinned",
        schema_version=1,
    )

    staging_path_recorded = None
    with pytest.raises(RuntimeError, match="simulated failure"):
        with cache.prepare(ident) as session:
            staging_path_recorded = session.staging_dir
            assert staging_path_recorded is not None
            session.write_payload_stream(iter([b"partial_payload"]))
            raise RuntimeError("simulated failure")

    assert staging_path_recorded is not None
    assert not staging_path_recorded.exists()
    assert not cache.graph_dir(ident.repository_id, ident.graph_id).exists()
    assert cache.status(ident.repository_id, ident.graph_id).status == GraphStatus.MISSING


def test_payload_filename_and_copy_source_reject_traversal_and_symlink(tmp_path: Path) -> None:
    cache = CodeGraphCache(tmp_path)
    ident = create_graph_identity(
        repository_id="repo-paths",
        git_tree_sha="tree-paths",
        analyzer_id="analyzer-paths",
        schema_version=1,
    )

    for bad_name in ("../escape.bin", "/tmp/escape.bin", "..", "nested/cpg.bin", "nested\\cpg.bin"):
        with pytest.raises(GraphValidationError):
            with cache.prepare(ident, payload_filename=bad_name):
                pass

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    source_link = tmp_path / "source-link.bin"
    source_link.symlink_to(outside)
    with cache.prepare(ident) as session:
        with pytest.raises(ValueError, match="symbolic link"):
            session.copy_payload_from_file(source_link)


def test_invalid_manifest_and_payload_detection(tmp_path: Path) -> None:
    cache = CodeGraphCache(tmp_path)
    ident = create_graph_identity(
        repository_id="repo-invalid",
        git_tree_sha="tree1",
        analyzer_id="analyzer1",
        schema_version=1,
    )

    with cache.prepare(ident) as session:
        session.write_payload_stream(iter([b"valid_payload"]))
        session.commit()

    graph_dir = cache.graph_dir(ident.repository_id, ident.graph_id)
    manifest_path = graph_dir / "manifest.json"
    payload_path = graph_dir / "cpg.bin"

    assert cache.status(ident.repository_id, ident.graph_id).status == GraphStatus.READY

    # 1. Corrupt payload size (append extra bytes)
    with payload_path.open("ab") as f:
        f.write(b"corruption")
    entry = cache.status(ident.repository_id, ident.graph_id)
    assert entry.status == GraphStatus.INVALID
    assert "Payload size mismatch" in (entry.error or "")

    # Restore correct size
    payload_path.write_bytes(b"valid_payload")
    assert cache.status(ident.repository_id, ident.graph_id).status == GraphStatus.READY

    # 2. Corrupted JSON manifest
    manifest_backup = manifest_path.read_bytes()
    manifest_path.write_bytes(b"{not valid json")
    entry = cache.status(ident.repository_id, ident.graph_id)
    assert entry.status == GraphStatus.INVALID
    assert "manifest" in (entry.error or "").lower()

    # 3. Schema version mismatch
    manifest_data = json.loads(manifest_backup.decode("utf-8"))
    manifest_data["manifest_version"] = 999
    atomic_write_bytes(manifest_path, json.dumps(manifest_data).encode("utf-8"))
    entry = cache.status(ident.repository_id, ident.graph_id)
    assert entry.status == GraphStatus.INVALID
    assert "manifest version" in (entry.error or "").lower()

    # 4. Identity mismatch (graph_id tampered)
    manifest_data["manifest_version"] = json.loads(manifest_backup.decode("utf-8"))["manifest_version"]
    manifest_data["graph_id"] = "different_graph_id_0000000000000000000000000000000000000000"
    atomic_write_bytes(manifest_path, json.dumps(manifest_data).encode("utf-8"))
    entry = cache.status(ident.repository_id, ident.graph_id)
    assert entry.status == GraphStatus.INVALID
    assert "mismatch" in (entry.error or "").lower()

    # 5. Same-size payload corruption is detected by content digest.
    atomic_write_bytes(manifest_path, manifest_backup)
    original = payload_path.read_bytes()
    replacement = bytes((byte ^ 0x01) for byte in original)
    assert len(replacement) == len(original)
    payload_path.write_bytes(replacement)
    entry = cache.status(ident.repository_id, ident.graph_id)
    assert entry.status == GraphStatus.INVALID
    assert "sha-256 mismatch" in (entry.error or "").lower()

    # 6. Missing payload
    atomic_write_bytes(manifest_path, manifest_backup)
    payload_path.unlink()
    entry = cache.status(ident.repository_id, ident.graph_id)
    assert entry.status == GraphStatus.INVALID
    assert "Payload file does not exist" in (entry.error or "")

    # 7. Symlink rejection
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"valid_payload")
    payload_path.symlink_to(outside)
    entry = cache.status(ident.repository_id, ident.graph_id)
    assert entry.status == GraphStatus.INVALID
    assert "symbolic link" in (entry.error or "").lower()

    # 8. Manifest payload path traversal is rejected before any external read.
    payload_path.unlink()
    manifest_data = json.loads(manifest_backup.decode("utf-8"))
    manifest_data["payload_filename"] = "../outside.bin"
    atomic_write_bytes(manifest_path, json.dumps(manifest_data).encode("utf-8"))
    entry = cache.status(ident.repository_id, ident.graph_id)
    assert entry.status == GraphStatus.INVALID
    assert "payload_filename" in (entry.error or "")


def test_payload_sha_validation_is_memoized_and_rehashes_after_same_size_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from chatgpt_web_oauth_mcp.code_graph import cache as cache_module

    cache = CodeGraphCache(tmp_path)
    ident = create_graph_identity(
        repository_id="repo-sha-cache",
        git_tree_sha="tree-sha-cache",
        analyzer_id="analyzer-sha-cache",
        schema_version=1,
    )
    with cache.prepare(ident) as session:
        session.write_payload_stream(iter([b"abcdef"]))
        entry = session.commit()

    assert entry.payload_path is not None
    payload = entry.payload_path
    with CodeGraphCache._payload_validation_guard:
        CodeGraphCache._payload_validation_cache.clear()

    original_sha256_file = cache_module.sha256_file
    calls = 0

    def counted_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
        nonlocal calls
        calls += 1
        return original_sha256_file(path, chunk_size=chunk_size)

    monkeypatch.setattr(cache_module, "sha256_file", counted_sha256)

    assert cache.status(ident.repository_id, ident.graph_id).status == GraphStatus.READY
    assert calls == 1
    assert cache.status(ident.repository_id, ident.graph_id).status == GraphStatus.READY
    assert calls == 1

    stat = payload.stat()
    payload.write_bytes(b"abcdeg")
    os.utime(payload, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    invalid = cache.status(ident.repository_id, ident.graph_id)
    assert invalid.status == GraphStatus.INVALID
    assert "sha-256 mismatch" in (invalid.error or "").lower()
    assert calls == 2


def test_concurrency_single_builder_behavior(tmp_path: Path) -> None:
    cache = CodeGraphCache(tmp_path)
    ident = create_graph_identity(
        repository_id="repo-concurrent",
        git_tree_sha="tree-conc",
        analyzer_id="analyzer-conc",
        schema_version=1,
    )

    barrier = threading.Barrier(2)
    builder1_started = threading.Event()
    builder1_can_finish = threading.Event()
    results = []

    def builder_1_worker():
        try:
            with cache.prepare(ident) as session:
                builder1_started.set()
                builder1_can_finish.wait(timeout=5.0)
                session.write_payload_stream(iter([b"PAYLOAD_FROM_BUILDER_1"]))
                entry = session.commit()
                results.append(("builder1", entry.status))
        except Exception as exc:
            results.append(("builder1_err", str(exc)))

    def builder_2_worker():
        try:
            # Wait until builder 1 has acquired lock
            builder1_started.wait(timeout=5.0)
            # Builder 2 attempts to prepare the same graph concurrently
            with cache.prepare(ident) as session:
                # When builder 2 enters, builder 1 has committed and graph is ready
                if session.is_ready:
                    results.append(("builder2_reused", session.existing_entry.status if session.existing_entry else None))
                    # Attempting to commit should fail
                    try:
                        session.commit()
                        results.append(("builder2_commit_unexpected", True))
                    except GraphAlreadyPublishedError:
                        results.append(("builder2_commit_rejected_as_expected", True))
                else:
                    results.append(("builder2_should_not_have_built", True))
        except Exception as exc:
            results.append(("builder2_err", str(exc)))

    t1 = threading.Thread(target=builder_1_worker)
    t2 = threading.Thread(target=builder_2_worker)

    t1.start()
    time.sleep(0.05)
    t2.start()

    time.sleep(0.1)
    # Status while builder 1 is building should be BUILDING
    assert cache.status(ident.repository_id, ident.graph_id).status == GraphStatus.BUILDING

    # Release builder 1 to finish
    builder1_can_finish.set()

    t1.join(timeout=5.0)
    t2.join(timeout=5.0)

    res_dict = dict(results)
    assert res_dict.get("builder1") == GraphStatus.READY
    assert res_dict.get("builder2_reused") == GraphStatus.READY
    assert res_dict.get("builder2_commit_rejected_as_expected") is True
    assert "builder2_should_not_have_built" not in res_dict


def test_bounded_gc_retention(tmp_path: Path) -> None:
    cache = CodeGraphCache(tmp_path)
    repo = "repo-gc"

    # Build 3 graphs with small payloads
    entries = []
    for i in range(3):
        ident = create_graph_identity(
            repository_id=repo,
            git_tree_sha=f"sha-{i}",
            analyzer_id="analyzer-gc",
            schema_version=1,
        )
        with cache.prepare(ident) as session:
            session.write_payload_stream(iter([f"payload-{i}".encode("utf-8") * 100]))
            entries.append(session.commit())
        # Ensure distinct mtimes
        time.sleep(0.02)

    # Verify 3 graphs exist
    for entry in entries:
        assert cache.status(repo, entry.graph_id).status == GraphStatus.READY

    # Prune by max_graph_count=2
    summary = cache.gc(repo, max_graph_count=2)
    assert len(summary.deleted_graph_ids) == 1
    # Oldest graph (sha-0) should have been deleted
    assert summary.deleted_graph_ids[0] == entries[0].graph_id
    assert summary.retained_graph_count == 2
    assert cache.status(repo, entries[0].graph_id).status == GraphStatus.MISSING
    assert cache.status(repo, entries[1].graph_id).status == GraphStatus.READY
    assert cache.status(repo, entries[2].graph_id).status == GraphStatus.READY

    # Prune by max_total_bytes to force eviction down to 1 graph
    remaining_size = sum(
        sum(f.stat().st_size for f in cache.graph_dir(repo, e.graph_id).glob("**/*") if f.is_file())
        for e in entries[1:]
    )
    # Set limit below total size
    summary2 = cache.gc(repo, max_total_bytes=remaining_size - 10)
    assert len(summary2.deleted_graph_ids) == 1
    assert summary2.deleted_graph_ids[0] == entries[1].graph_id
    assert cache.status(repo, entries[1].graph_id).status == GraphStatus.MISSING
    assert cache.status(repo, entries[2].graph_id).status == GraphStatus.READY


def test_gc_never_deletes_active_build(tmp_path: Path) -> None:
    cache = CodeGraphCache(tmp_path)
    repo = "repo-gc-active"

    # 1. An old completed graph
    old_ident = create_graph_identity(
        repository_id=repo,
        git_tree_sha="old-tree",
        analyzer_id="analyzer-gc",
        schema_version=1,
    )
    with cache.prepare(old_ident) as s:
        s.write_payload_stream(iter([b"old_data" * 50]))
        s.commit()

    active_ident = create_graph_identity(
        repository_id=repo,
        git_tree_sha="active-tree",
        analyzer_id="analyzer-gc",
        schema_version=1,
    )

    # 2. An active build holding locks
    with cache.prepare(active_ident) as session:
        session.write_payload_stream(iter([b"active_data" * 50]))

        # Run aggressive GC with max_graph_count=0
        summary = cache.gc(repo, max_graph_count=0)

        # Active build must not be deleted!
        assert active_ident.graph_id not in summary.deleted_graph_ids
        # Active staging directory still exists intact
        assert session.staging_dir is not None
        assert session.staging_dir.exists()

        # The old inactive graph can be pruned
        assert old_ident.graph_id in summary.deleted_graph_ids

        # Active build can now commit successfully
        session.commit()

    assert cache.status(repo, active_ident.graph_id).status == GraphStatus.READY


def test_gc_removes_only_stale_inactive_staging(tmp_path: Path) -> None:
    cache = CodeGraphCache(tmp_path)
    repo = "repo-staging-gc"
    stale_ident = create_graph_identity(
        repository_id=repo,
        git_tree_sha="stale",
        analyzer_id="analyzer",
        schema_version=1,
    )
    stale = cache.staging_root(repo) / f"{stale_ident.graph_id}_orphan"
    stale.mkdir()
    (stale / "cpg.bin").write_bytes(b"orphan")
    old = time.time() - 7200
    os.utime(stale, (old, old))

    active_ident = create_graph_identity(
        repository_id=repo,
        git_tree_sha="active",
        analyzer_id="analyzer",
        schema_version=1,
    )
    with cache.prepare(active_ident) as session:
        assert session.staging_dir is not None
        session.write_payload_stream(iter([b"active"]))
        summary = cache.gc(repo, staging_ttl_seconds=3600)
        assert stale.name in summary.deleted_staging_dirs
        assert summary.staging_bytes_freed == len(b"orphan")
        assert not stale.exists()
        assert session.staging_dir.exists()
