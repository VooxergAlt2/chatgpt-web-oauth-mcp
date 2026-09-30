from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from .backend import CodeGraphBackendError, JoernBackendConfig, JoernDockerBackend
from .cache import CodeGraphCache
from .identity import create_graph_identity
from .server_runtime import (
    JoernQueryServerConfig,
    JoernQueryServerRuntime,
    cleanup_owned_query_servers,
)
from .snapshot import (
    GitSnapshotError,
    cleanup_exported_snapshot,
    export_git_snapshot,
    resolve_git_snapshot,
)


GRAPH_SCHEMA_VERSION = 1
ANALYSIS_OPTIONS = {"language": "PYTHONSRC"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build one immutable Code Graph CPG.")
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--tree-sha", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--docker-binary", required=True)
    parser.add_argument("--joern-image", required=True)
    parser.add_argument("--joern-version", required=True)
    parser.add_argument("--memory-mb", required=True, type=int)
    parser.add_argument("--cpus", required=True, type=int)
    parser.add_argument("--pids-limit", required=True, type=int)
    parser.add_argument("--tmpfs-mb", required=True, type=int)
    parser.add_argument("--build-timeout-seconds", required=True, type=int)
    parser.add_argument("--query-timeout-seconds", required=True, type=int)
    parser.add_argument("--query-server-start-timeout-seconds", required=True, type=int)
    parser.add_argument("--query-server-max-containers", required=True, type=int)
    parser.add_argument("--cache-max-bytes", required=True, type=int)
    parser.add_argument("--cache-max-graphs", required=True, type=int)
    parser.add_argument("--staging-ttl-seconds", required=True, type=int)
    return parser


def _emit(payload: dict[str, object], *, stream=None) -> None:
    target = stream or sys.stdout
    target.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    target.flush()


def run_worker(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).expanduser().resolve()
    repository_root = Path(args.repository_root).expanduser().resolve()
    backend_config = JoernBackendConfig(
        enabled=True,
        docker_binary=args.docker_binary,
        image=args.joern_image,
        version=args.joern_version,
        memory_mb=args.memory_mb,
        cpus=args.cpus,
        pids_limit=args.pids_limit,
        tmpfs_mb=args.tmpfs_mb,
        build_timeout_seconds=args.build_timeout_seconds,
        query_timeout_seconds=args.query_timeout_seconds,
    )
    backend = JoernDockerBackend(backend_config)
    query_runtime = JoernQueryServerRuntime(
        JoernQueryServerConfig(
            backend=backend_config,
            start_timeout_seconds=args.query_server_start_timeout_seconds,
            max_containers=args.query_server_max_containers,
            lifecycle_lock_path=state_dir / "code-graph" / ".query-runtime.lock",
        )
    )

    try:
        snapshot = resolve_git_snapshot(repository_root, args.tree_sha)
        if snapshot.repository_id != args.repository_id:
            raise GitSnapshotError("Repository identity changed after the build was scheduled.")
        if snapshot.tree_sha != args.tree_sha.lower():
            raise GitSnapshotError("Resolved Git tree differs from the scheduled tree.")

        identity = create_graph_identity(
            repository_id=snapshot.repository_id,
            git_tree_sha=snapshot.tree_sha,
            analyzer_id=backend_config.analyzer_id,
            schema_version=GRAPH_SCHEMA_VERSION,
            options=ANALYSIS_OPTIONS,
        )
        cache = CodeGraphCache(state_dir)
        cleanup_owned_query_servers(
            args.docker_binary,
            state_dir,
            orphaned_only=True,
            strict=True,
        )
        ready_entry = None
        cache_hit = False
        scratch_root = cache.base_dir / "scratch"
        started = time.monotonic()
        build_result = None
        gc_deleted_graph_ids: tuple[str, ...] = ()
        gc_bytes_freed = 0

        with cache.prepare(identity) as build_session:
            if build_session.is_ready:
                ready_entry = build_session.existing_entry
                cache_hit = True
            else:
                source_dir = None
                try:
                    source_dir = export_git_snapshot(
                        snapshot,
                        scratch_root=scratch_root,
                        timeout=min(120.0, float(args.build_timeout_seconds)),
                    )
                    if build_session.staging_dir is None:
                        raise RuntimeError("Graph build staging directory is unavailable.")
                    build_result = backend.build(
                        source_dir=source_dir,
                        output_dir=build_session.staging_dir,
                    )
                    ready_entry = build_session.commit(
                        metadata={
                            "backend": "joern-docker",
                            "joern_image": backend_config.image,
                            "joern_image_digest": backend_config.image_digest,
                            "joern_version": backend_config.version,
                            "analysis_options": ANALYSIS_OPTIONS,
                            "build_duration_seconds": round(build_result.duration_seconds, 6),
                        }
                    )
                finally:
                    if source_dir is not None:
                        cleanup_exported_snapshot(source_dir)

        if not cache_hit:
            gc = cache.gc(
                snapshot.repository_id,
                max_total_bytes=args.cache_max_bytes,
                max_graph_count=args.cache_max_graphs,
                staging_ttl_seconds=args.staging_ttl_seconds,
            )
            gc_deleted_graph_ids = gc.deleted_graph_ids
            gc_bytes_freed = gc.bytes_freed + gc.staging_bytes_freed
            if gc.deleted_graph_ids:
                cleanup_owned_query_servers(
                    args.docker_binary,
                    state_dir,
                    graph_ids=gc.deleted_graph_ids,
                    strict=True,
                )
        if (
            ready_entry is None
            or ready_entry.manifest is None
            or ready_entry.payload_path is None
        ):
            raise RuntimeError("Graph publication completed without a ready manifest.")
        warm_result = query_runtime.warm(
            graph_id=identity.graph_id,
            cpg_path=ready_entry.payload_path,
            cpg_sha256=ready_entry.manifest.payload_sha256,
        )
        total_duration = time.monotonic() - started
        _emit(
            {
                "success": True,
                "status": "ready",
                "cache_hit": cache_hit,
                "runtime_ready": True,
                "query_runtime": "persistent-rest",
                "query_runtime_cold_start": warm_result.cold_start,
                "query_runtime_warm_duration_seconds": round(
                    warm_result.duration_seconds, 6
                ),
                "graph_id": identity.graph_id,
                "repository_id": identity.repository_id,
                "tree_sha": identity.git_tree_sha,
                "payload_size_bytes": ready_entry.manifest.payload_size_bytes,
                "joern_version": backend_config.version,
                "joern_image_digest": backend_config.image_digest,
                "build_duration_seconds": (
                    round(build_result.duration_seconds, 6) if build_result is not None else None
                ),
                "total_duration_seconds": round(total_duration, 6),
                "gc_deleted_graphs": len(gc_deleted_graph_ids),
                "gc_bytes_freed": gc_bytes_freed,
            }
        )
        return 0
    except (CodeGraphBackendError, GitSnapshotError, OSError, RuntimeError, ValueError) as exc:
        _emit(
            {
                "success": False,
                "status": "failed",
                "error": {
                    "code": "code_graph_build_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            },
            stream=sys.stderr,
        )
        return 1


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(run_worker(args))


if __name__ == "__main__":
    main()
