from __future__ import annotations

import shlex
import sys
import threading
from typing import Annotated, Any

from pydantic import Field

from .code_graph.backend import CodeGraphBackendError, JoernBackendConfig, JoernDockerBackend
from .code_graph.cache import CodeGraphCache
from .code_graph.identity import create_graph_identity
from .code_graph.models import GraphEntry, GraphStatus
from .code_graph.queries import CodeGraphQueryError, JoernStructuralQueryEngine
from .code_graph.server_runtime import JoernQueryServerConfig, JoernQueryServerRuntime
from .code_graph.snapshot import GitSnapshotError, resolve_git_snapshot
from .code_graph.worker import ANALYSIS_OPTIONS, GRAPH_SCHEMA_VERSION
from .owned_jobs import start_owned_job
from .pathing import resolve_cwd
from .response_budget import ResponseBudget, with_budget_metadata
from .tool_context import LOCAL_STATE_TOOL, READ_ONLY_TOOL, ToolContext


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
        query_timeout_seconds=int(ctx.global_value("JOERN_QUERY_TIMEOUT_SECONDS", 20)),
    )


def _query_server_config(
    ctx: ToolContext,
    backend_config: JoernBackendConfig,
) -> JoernQueryServerConfig:
    return JoernQueryServerConfig(
        backend=backend_config,
        start_timeout_seconds=int(
            ctx.global_value("JOERN_QUERY_SERVER_START_TIMEOUT_SECONDS", 30)
        ),
        max_containers=int(ctx.global_value("JOERN_QUERY_SERVER_MAX_CONTAINERS", 1)),
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


def _fit_query_payload(
    ctx: ToolContext,
    payload: dict[str, object],
) -> dict[str, object]:
    budget = ResponseBudget(max_tokens=ctx.tool_output_token_budget)
    result = dict(payload)
    query_truncated = bool(result.get("query_truncated"))
    rendered, measurement = with_budget_metadata(
        result,
        budget=budget,
        truncated=query_truncated,
        stop_reason="limit" if query_truncated else "end_of_results",
    )
    for field in ("results", "matches", "target_matches"):
        values = rendered.get(field)
        while not measurement.fits and isinstance(values, list) and values:
            values.pop()
            rendered, measurement = with_budget_metadata(
                rendered,
                budget=budget,
                truncated=True,
                stop_reason="token_budget",
            )
            values = rendered.get(field)
    rendered["returned_results"] = (
        len(rendered.get("results", []))
        if isinstance(rendered.get("results"), list)
        else 0
    )
    rendered["returned_matches"] = (
        len(rendered.get("matches", []))
        if isinstance(rendered.get("matches"), list)
        else 0
    )
    rendered["returned_target_matches"] = (
        len(rendered.get("target_matches", []))
        if isinstance(rendered.get("target_matches"), list)
        else 0
    )
    final_truncated = bool(rendered.get("truncated"))
    final_stop_reason = str(rendered.get("stop_reason") or "end_of_results")
    rendered, _measurement = with_budget_metadata(
        rendered,
        budget=budget,
        truncated=final_truncated,
        stop_reason=final_stop_reason,
    )
    return rendered


def _execute_structural_query(
    ctx: ToolContext,
    *,
    cwd: str | None,
    ref: str,
    mode: str,
    symbol: str,
    target: str = "",
    max_depth: int = 6,
    limit: int = 100,
    include_external: bool = False,
) -> dict[str, object]:
    try:
        _resolved_cwd, snapshot, backend_config, identity = _resolve_graph(
            ctx,
            cwd=cwd,
            ref=ref,
        )
        cache = CodeGraphCache(ctx.state_dir)
        entry = cache.status(
            identity.repository_id,
            identity.graph_id,
            expected_identity=identity,
        )
    except (GitSnapshotError, OSError, TypeError, ValueError) as exc:
        return _error("code_graph_resolution_failed", f"{type(exc).__name__}: {exc}")

    if entry.status != GraphStatus.READY or entry.payload_path is None:
        return _error(
            "code_graph_not_ready",
            (
                f"Code Graph for {snapshot.requested_ref} is {entry.status.value}; "
                "semantic queries require a READY immutable CPG."
            ),
            ref=snapshot.requested_ref,
            tree_sha=snapshot.tree_sha,
            graph_id=identity.graph_id,
            repository_id=identity.repository_id,
            cache_status=entry.status.value,
            cache_error=entry.error,
            next_action="Call code_graph_prepare for this ref, await the build, then retry the query.",
        )

    try:
        runtime = JoernQueryServerRuntime(_query_server_config(ctx, backend_config))
        query = JoernStructuralQueryEngine(runtime).run(
            graph_id=identity.graph_id,
            cpg_path=entry.payload_path,
            mode=mode,
            symbol=symbol,
            target=target,
            max_depth=max_depth,
            limit=limit,
            include_external=include_external,
        )
    except (CodeGraphBackendError, CodeGraphQueryError, OSError, TypeError, ValueError) as exc:
        return _error(
            "code_graph_query_failed",
            f"{type(exc).__name__}: {exc}",
            ref=snapshot.requested_ref,
            tree_sha=snapshot.tree_sha,
            graph_id=identity.graph_id,
            repository_id=identity.repository_id,
        )

    payload: dict[str, object] = {
        "success": True,
        "ref": snapshot.requested_ref,
        "tree_sha": snapshot.tree_sha,
        "graph_id": identity.graph_id,
        "repository_id": identity.repository_id,
        "identity_kind": "committed_git_tree",
        "working_tree_included": False,
        **query,
    }
    return _fit_query_payload(ctx, payload)


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

    @mcp.tool(
        name="code_graph_callers",
        title="Code Graph Callers",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return semantic callers of one exact Joern method name or full_name from a READY "
            "immutable CPG. Ambiguous symbols are reported instead of guessed."
        ),
    )
    def code_graph_callers(
        symbol: Annotated[str, Field(description="Exact method name or exact Joern full_name.")],
        cwd: Annotated[
            str | None,
            Field(description="Git repository directory. Defaults to the current session cwd."),
        ] = None,
        ref: Annotated[
            str,
            Field(description="Committed Git ref whose READY Code Graph should be queried."),
        ] = "HEAD",
        limit: Annotated[int, Field(description="Maximum returned methods.", ge=1, le=200)] = 100,
        include_external: Annotated[
            bool,
            Field(description="Include Joern external/operator methods when true."),
        ] = False,
    ) -> dict[str, object]:
        return _execute_structural_query(
            ctx,
            cwd=cwd,
            ref=ref,
            mode="callers",
            symbol=symbol,
            max_depth=1,
            limit=limit,
            include_external=include_external,
        )

    @mcp.tool(
        name="code_graph_callees",
        title="Code Graph Callees",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return semantic callees of one exact Joern method name or full_name from a READY "
            "immutable CPG. Ambiguous symbols are reported instead of guessed."
        ),
    )
    def code_graph_callees(
        symbol: Annotated[str, Field(description="Exact method name or exact Joern full_name.")],
        cwd: Annotated[
            str | None,
            Field(description="Git repository directory. Defaults to the current session cwd."),
        ] = None,
        ref: Annotated[
            str,
            Field(description="Committed Git ref whose READY Code Graph should be queried."),
        ] = "HEAD",
        limit: Annotated[int, Field(description="Maximum returned methods.", ge=1, le=200)] = 100,
        include_external: Annotated[
            bool,
            Field(description="Include Joern external/operator methods when true."),
        ] = False,
    ) -> dict[str, object]:
        return _execute_structural_query(
            ctx,
            cwd=cwd,
            ref=ref,
            mode="callees",
            symbol=symbol,
            max_depth=1,
            limit=limit,
            include_external=include_external,
        )

    @mcp.tool(
        name="code_graph_impact",
        title="Code Graph Impact",
        annotations=READ_ONLY_TOOL,
        description=(
            "Walk reverse semantic call edges from one exact method and return bounded transitive "
            "callers with depth. The seed method is depth 0."
        ),
    )
    def code_graph_impact(
        symbol: Annotated[str, Field(description="Exact method name or exact Joern full_name.")],
        cwd: Annotated[
            str | None,
            Field(description="Git repository directory. Defaults to the current session cwd."),
        ] = None,
        ref: Annotated[
            str,
            Field(description="Committed Git ref whose READY Code Graph should be queried."),
        ] = "HEAD",
        max_depth: Annotated[
            int,
            Field(description="Maximum reverse-call depth.", ge=1, le=20),
        ] = 6,
        limit: Annotated[int, Field(description="Maximum returned methods.", ge=1, le=200)] = 100,
        include_external: Annotated[
            bool,
            Field(description="Include Joern external/operator methods when true."),
        ] = False,
    ) -> dict[str, object]:
        return _execute_structural_query(
            ctx,
            cwd=cwd,
            ref=ref,
            mode="impact",
            symbol=symbol,
            max_depth=max_depth,
            limit=limit,
            include_external=include_external,
        )

    @mcp.tool(
        name="code_graph_path",
        title="Code Graph Path",
        annotations=READ_ONLY_TOOL,
        description=(
            "Find one deterministic bounded semantic call path between exact source and target "
            "method names/full_names in a READY immutable CPG."
        ),
    )
    def code_graph_path(
        source: Annotated[
            str,
            Field(description="Exact source method name or exact Joern full_name."),
        ],
        target: Annotated[
            str,
            Field(description="Exact target method name or exact Joern full_name."),
        ],
        cwd: Annotated[
            str | None,
            Field(description="Git repository directory. Defaults to the current session cwd."),
        ] = None,
        ref: Annotated[
            str,
            Field(description="Committed Git ref whose READY Code Graph should be queried."),
        ] = "HEAD",
        max_depth: Annotated[
            int,
            Field(description="Maximum forward-call depth.", ge=1, le=20),
        ] = 6,
        limit: Annotated[int, Field(description="Maximum returned path nodes.", ge=1, le=200)] = 100,
        include_external: Annotated[
            bool,
            Field(description="Allow external methods in the traversed path when true."),
        ] = False,
    ) -> dict[str, object]:
        return _execute_structural_query(
            ctx,
            cwd=cwd,
            ref=ref,
            mode="path",
            symbol=source,
            target=target,
            max_depth=max_depth,
            limit=limit,
            include_external=include_external,
        )

    return {
        "code_graph_status": code_graph_status,
        "code_graph_prepare": code_graph_prepare,
        "code_graph_callers": code_graph_callers,
        "code_graph_callees": code_graph_callees,
        "code_graph_impact": code_graph_impact,
        "code_graph_path": code_graph_path,
    }
