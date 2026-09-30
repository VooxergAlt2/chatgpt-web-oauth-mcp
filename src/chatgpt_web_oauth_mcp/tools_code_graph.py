from __future__ import annotations

import shlex
import sys
import threading
from typing import Annotated, Any

from pydantic import Field

from .code_graph.backend import JoernBackendConfig, JoernDockerBackend
from .code_graph.cache import CodeGraphCache
from .code_graph.identity import create_graph_identity
from .code_graph.models import GraphEntry, GraphStatus
from .code_graph.snapshot import GitSnapshotError, resolve_git_snapshot
from .code_graph.worker import ANALYSIS_OPTIONS, GRAPH_SCHEMA_VERSION
from .owned_jobs import start_owned_job
from .pathing import resolve_cwd
from .tool_context import LOCAL_STATE_TOOL, ToolContext


_PREPARE_GATE = threading.Lock()
_ACTIVE_JOB_SCAN_LIMIT = 200


def _error(code: str, message: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "success": False,
        "error": {"code": code, "message": message},
    }
    payload.update(extra)
    return payload


def _backend_config(ctx: ToolContext) -> JoernBackendConfig:
    return JoernBackendConfig(
        enabled=bool(ctx.global_value("CODE_GRAPH_ENABLED", True)),
        docker_binary=str(ctx.global_value("JOERN_DOCKER_BINARY", "docker")),
        image=str(ctx.global_value("JOERN_IMAGE", "")),
        version=str(ctx.global_value("JOERN_VERSION", "")),
        memory_mb=int(ctx.global_value("JOERN_MEMORY_MB", 8192)),
        cpus=int(ctx.global_value("JOERN_CPUS", 4)),
        pids_limit=int(ctx.global_value("JOERN_PIDS_LIMIT", 512)),
        tmpfs_mb=int(ctx.global_value("JOERN_TMPFS_MB", 2048)),
        build_timeout_seconds=int(ctx.global_value("JOERN_BUILD_TIMEOUT_SECONDS", 1800)),
    )


def _resolve_graph(ctx: ToolContext, *, cwd: str | None, ref: str):
    resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
    snapshot = resolve_git_snapshot(resolved_cwd, ref)
    backend_config = _backend_config(ctx)
    identity = create_graph_identity(
        repository_id=snapshot.repository_id,
        git_tree_sha=snapshot.tree_sha,
        analyzer_id=backend_config.analyzer_id,
        schema_version=GRAPH_SCHEMA_VERSION,
        options=ANALYSIS_OPTIONS,
    )
    return resolved_cwd, snapshot, backend_config, identity


def _safe_entry(entry: GraphEntry) -> dict[str, object]:
    payload: dict[str, object] = {
        "cache_status": entry.status.value,
        "graph_id": entry.graph_id,
        "repository_id": entry.repository_id,
    }
    if entry.manifest is not None:
        payload.update(
            {
                "payload_size_bytes": entry.manifest.payload_size_bytes,
                "created_at": entry.manifest.created_at,
                "metadata": dict(entry.manifest.metadata),
            }
        )
    if entry.error:
        payload["cache_error"] = entry.error
    return payload


def _active_build_job(ctx: ToolContext, job_name: str) -> dict[str, object] | None:
    result = ctx.job_registry.list_jobs(
        state_dir=ctx.state_dir,
        status="running",
        offset=0,
        limit=_ACTIVE_JOB_SCAN_LIMIT,
        max_tokens=ctx.tool_output_token_budget,
    )
    jobs = result.get("jobs")
    if not isinstance(jobs, list):
        return None
    for job in jobs:
        if isinstance(job, dict) and str(job.get("name") or "") == job_name:
            return job
    return None


def _worker_command(
    ctx: ToolContext,
    *,
    snapshot,
    backend_config: JoernBackendConfig,
) -> str:
    argv = [
        sys.executable,
        "-m",
        "chatgpt_web_oauth_mcp.code_graph.worker",
        "--repository-root",
        str(snapshot.repository_root),
        "--repository-id",
        snapshot.repository_id,
        "--tree-sha",
        snapshot.tree_sha,
        "--state-dir",
        str(ctx.state_dir),
        "--docker-binary",
        backend_config.docker_binary,
        "--joern-image",
        backend_config.image,
        "--joern-version",
        backend_config.version,
        "--memory-mb",
        str(backend_config.memory_mb),
        "--cpus",
        str(backend_config.cpus),
        "--pids-limit",
        str(backend_config.pids_limit),
        "--tmpfs-mb",
        str(backend_config.tmpfs_mb),
        "--build-timeout-seconds",
        str(backend_config.build_timeout_seconds),
        "--cache-max-bytes",
        str(int(ctx.global_value("CODE_GRAPH_CACHE_MAX_BYTES", 5 * 1024 * 1024 * 1024))),
        "--cache-max-graphs",
        str(int(ctx.global_value("CODE_GRAPH_CACHE_MAX_GRAPHS", 8))),
        "--staging-ttl-seconds",
        str(int(ctx.global_value("CODE_GRAPH_STAGING_TTL_SECONDS", 24 * 60 * 60))),
    ]
    return shlex.join(argv)


def register_code_graph_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    @mcp.tool(
        name="code_graph_status",
        title="Code Graph Status",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Resolve an exact committed Git ref to its immutable tree-based Code Graph identity "
            "and report Joern backend/cache status. The current dirty working tree is never "
            "silently analyzed under a committed tree identity."
        ),
    )
    def code_graph_status(
        cwd: Annotated[
            str | None,
            Field(description="Git repository directory. Defaults to the current session cwd."),
        ] = None,
        ref: Annotated[
            str,
            Field(description="Committed Git ref to inspect, such as HEAD or a commit SHA."),
        ] = "HEAD",
    ) -> dict[str, object]:
        try:
            _resolved_cwd, snapshot, backend_config, identity = _resolve_graph(
                ctx, cwd=cwd, ref=ref
            )
            backend_status = JoernDockerBackend(backend_config).status()
            cache = CodeGraphCache(ctx.state_dir)
            entry = cache.status(
                identity.repository_id,
                identity.graph_id,
                expected_identity=identity,
            )
        except (GitSnapshotError, OSError, TypeError, ValueError) as exc:
            return _error("code_graph_resolution_failed", f"{type(exc).__name__}: {exc}")
        return {
            "success": True,
            "ref": snapshot.requested_ref,
            "tree_sha": snapshot.tree_sha,
            "graph_id": identity.graph_id,
            "repository_id": identity.repository_id,
            "identity_kind": "committed_git_tree",
            "working_tree_included": False,
            "analyzer_id": identity.analyzer_id,
            "backend": {
                "enabled": backend_status.enabled,
                "available": backend_status.available,
                "image": backend_status.image,
                "version": backend_status.version,
                "error_code": backend_status.error_code,
                "error_message": backend_status.error_message,
            },
            **_safe_entry(entry),
        }

    @mcp.tool(
        name="code_graph_prepare",
        title="Code Graph Prepare",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Ensure an immutable Joern CPG exists for an exact committed Git ref. Returns "
            "immediately on cache hit; otherwise starts one session-owned durable build job. "
            "The source snapshot is exported from Git and mounted read-only into an offline "
            "hardened Joern container."
        ),
    )
    def code_graph_prepare(
        cwd: Annotated[
            str | None,
            Field(description="Git repository directory. Defaults to the current session cwd."),
        ] = None,
        ref: Annotated[
            str,
            Field(description="Committed Git ref to prepare, such as HEAD or a commit SHA."),
        ] = "HEAD",
    ) -> dict[str, object]:
        try:
            resolved_cwd, snapshot, backend_config, identity = _resolve_graph(
                ctx, cwd=cwd, ref=ref
            )
            backend_status = JoernDockerBackend(backend_config).status()
        except (GitSnapshotError, OSError, TypeError, ValueError) as exc:
            return _error("code_graph_resolution_failed", f"{type(exc).__name__}: {exc}")
        if not backend_status.available:
            return _error(
                backend_status.error_code or "code_graph_backend_unavailable",
                backend_status.error_message or "Code Graph backend is unavailable.",
                graph_id=identity.graph_id,
                tree_sha=snapshot.tree_sha,
            )

        cache = CodeGraphCache(ctx.state_dir)
        job_name = f"code-graph-build:{identity.graph_id}"
        with _PREPARE_GATE:
            entry = cache.status(
                identity.repository_id,
                identity.graph_id,
                expected_identity=identity,
            )
            if entry.status == GraphStatus.READY:
                return {
                    "success": True,
                    "status": "ready",
                    "cache_hit": True,
                    "ref": snapshot.requested_ref,
                    "tree_sha": snapshot.tree_sha,
                    "graph_id": identity.graph_id,
                    "repository_id": identity.repository_id,
                    **_safe_entry(entry),
                }
            active_job = _active_build_job(ctx, job_name)
            if entry.status == GraphStatus.BUILDING or active_job is not None:
                return {
                    "success": True,
                    "status": "building",
                    "cache_hit": False,
                    "ref": snapshot.requested_ref,
                    "tree_sha": snapshot.tree_sha,
                    "graph_id": identity.graph_id,
                    "repository_id": identity.repository_id,
                    "job_id": (
                        str(active_job.get("job_id"))
                        if isinstance(active_job, dict) and active_job.get("job_id")
                        else None
                    ),
                    "next_action": "Wait for the existing build to finish, then call code_graph_status.",
                }

            job_result = start_owned_job(
                ctx,
                tool_name="code_graph_prepare",
                command=_worker_command(
                    ctx,
                    snapshot=snapshot,
                    backend_config=backend_config,
                ),
                cwd=resolved_cwd,
                name=job_name,
                timeout_seconds=min(
                    float(backend_config.build_timeout_seconds + 120),
                    float(ctx.global_value("JOB_MAX_TIMEOUT_SECONDS", 24 * 60 * 60)),
                ),
            )
        if job_result.get("success") is False:
            return {
                **job_result,
                "graph_id": identity.graph_id,
                "repository_id": identity.repository_id,
                "tree_sha": snapshot.tree_sha,
            }
        return {
            "success": True,
            "status": "building",
            "cache_hit": False,
            "ref": snapshot.requested_ref,
            "tree_sha": snapshot.tree_sha,
            "graph_id": identity.graph_id,
            "repository_id": identity.repository_id,
            "job_id": job_result.get("job_id"),
            "next_action": (
                "Use await_job for this owned build; after it is terminal and verified, "
                "call code_graph_status."
            ),
        }

    return {
        "code_graph_status": code_graph_status,
        "code_graph_prepare": code_graph_prepare,
    }
