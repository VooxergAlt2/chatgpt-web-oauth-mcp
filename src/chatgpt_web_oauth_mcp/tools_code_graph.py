from __future__ import annotations

import shlex
import sys
import threading
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from .code_graph.backend import CodeGraphBackendError, JoernBackendConfig, JoernDockerBackend
from .code_graph.cache import CodeGraphCache
from .code_graph.change_impact import (
    CodeGraphChangeError,
    collect_python_change_set,
    is_test_path,
    resolve_comparison_base,
)
from .code_graph.identity import create_graph_identity
from .code_graph.models import GraphEntry, GraphStatus
from .code_graph.queries import CodeGraphQueryError, JoernStructuralQueryEngine
from .code_graph.server_runtime import (
    JoernQueryRuntimeNotReady,
    JoernQueryServerConfig,
    JoernQueryServerRuntime,
)
from .code_graph.snapshot import GitSnapshotError, resolve_git_snapshot
from .code_graph.worker import ANALYSIS_OPTIONS, GRAPH_SCHEMA_VERSION
from .owned_jobs import start_owned_job
from .pathing import resolve_cwd
from .response_budget import ResponseBudget, with_budget_metadata
from .tool_context import LOCAL_STATE_TOOL, READ_ONLY_TOOL, ToolContext


_PREPARE_GATE = threading.Lock()
_ACTIVE_JOB_SCAN_LIMIT = 200
_DUNDER_UNRESOLVED_EVIDENCE_LIMIT = 5


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
            ctx.global_value("JOERN_QUERY_SERVER_START_TIMEOUT_SECONDS", 120)
        ),
        max_containers=int(ctx.global_value("JOERN_QUERY_SERVER_MAX_CONTAINERS", 1)),
        lifecycle_lock_path=(
            Path(ctx.state_dir).expanduser().resolve()
            / "code-graph"
            / ".query-runtime.lock"
        ),
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
        "--query-timeout-seconds",
        str(backend_config.query_timeout_seconds),
        "--query-server-start-timeout-seconds",
        str(int(ctx.global_value("JOERN_QUERY_SERVER_START_TIMEOUT_SECONDS", 120))),
        "--query-server-max-containers",
        str(int(ctx.global_value("JOERN_QUERY_SERVER_MAX_CONTAINERS", 1))),
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
    for field in ("results", "matches", "target_matches", "unresolved_call_sites"):
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
    rendered["returned_unresolved_call_sites"] = (
        len(rendered.get("unresolved_call_sites", []))
        if isinstance(rendered.get("unresolved_call_sites"), list)
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


def _fit_diff_impact_payload(
    ctx: ToolContext,
    payload: dict[str, object],
) -> dict[str, object]:
    budget = ResponseBudget(max_tokens=ctx.tool_output_token_budget)
    rendered = dict(payload)
    rendered, measurement = with_budget_metadata(
        rendered,
        budget=budget,
        truncated=bool(rendered.get("partial")),
        stop_reason=str(rendered.get("stop_reason") or "end_of_results"),
    )
    impacts = rendered.get("symbol_impacts")
    if isinstance(impacts, list):
        for index in range(len(impacts) - 1, -1, -1):
            item = impacts[index]
            if not isinstance(item, dict):
                continue
            for field in ("impact_methods", "unresolved_call_sites"):
                values = item.get(field)
                while not measurement.fits and isinstance(values, list) and values:
                    values.pop()
                    rendered["partial"] = True
                    rendered["stop_reason"] = "token_budget"
                    rendered, measurement = with_budget_metadata(
                        rendered,
                        budget=budget,
                        truncated=True,
                        stop_reason="token_budget",
                    )
                    impacts = rendered.get("symbol_impacts")
                    if not isinstance(impacts, list) or index >= len(impacts):
                        break
                    item = impacts[index]
                    values = item.get(field) if isinstance(item, dict) else None
    impacts = rendered.get("symbol_impacts")
    while not measurement.fits and isinstance(impacts, list) and impacts:
        impacts.pop()
        rendered["partial"] = True
        rendered["stop_reason"] = "token_budget"
        rendered, measurement = with_budget_metadata(
            rendered,
            budget=budget,
            truncated=True,
            stop_reason="token_budget",
        )
        impacts = rendered.get("symbol_impacts")
    for field in (
        "changed_files",
        "changed_test_files",
        "unsupported_changed_files",
        "module_scope_changes",
        "exact_affected_files",
        "exact_affected_tests",
        "candidate_affected_files",
        "candidate_affected_tests",
    ):
        values = rendered.get(field)
        while not measurement.fits and isinstance(values, list) and values:
            values.pop()
            rendered["partial"] = True
            rendered["stop_reason"] = "token_budget"
            rendered, measurement = with_budget_metadata(
                rendered,
                budget=budget,
                truncated=True,
                stop_reason="token_budget",
            )
            values = rendered.get(field)
    rendered["returned_symbol_impacts"] = (
        len(rendered.get("symbol_impacts", []))
        if isinstance(rendered.get("symbol_impacts"), list)
        else 0
    )
    requested_page_count = int(rendered.get("page_symbol_count") or 0)
    if rendered["returned_symbol_impacts"] < requested_page_count:
        rendered["next_offset"] = int(rendered.get("offset") or 0) + int(
            rendered["returned_symbol_impacts"]
        )
        rendered["partial"] = True
        rendered["stop_reason"] = "token_budget"
    rendered, _measurement = with_budget_metadata(
        rendered,
        budget=budget,
        truncated=bool(rendered.get("partial")),
        stop_reason=str(rendered.get("stop_reason") or "end_of_results"),
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
    except (CodeGraphBackendError, GitSnapshotError, OSError, TypeError, ValueError) as exc:
        return _error("code_graph_resolution_failed", f"{type(exc).__name__}: {exc}")

    if not backend_config.enabled:
        return _error(
            "code_graph_disabled",
            "Code Graph is disabled by configuration.",
            ref=snapshot.requested_ref,
            tree_sha=snapshot.tree_sha,
            graph_id=identity.graph_id,
            repository_id=identity.repository_id,
        )

    if (
        entry.status != GraphStatus.READY
        or entry.payload_path is None
        or entry.manifest is None
    ):
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
            cpg_sha256=entry.manifest.payload_sha256,
            mode=mode,
            symbol=symbol,
            target=target,
            max_depth=max_depth,
            limit=limit,
            include_external=include_external,
        )
    except JoernQueryRuntimeNotReady as exc:
        return _error(
            "code_graph_runtime_not_ready",
            str(exc),
            ref=snapshot.requested_ref,
            tree_sha=snapshot.tree_sha,
            graph_id=identity.graph_id,
            repository_id=identity.repository_id,
            next_action=(
                "Call code_graph_prepare for this ref and await the returned durable "
                "prepare job before retrying the semantic query."
            ),
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


def _execute_diff_impact(
    ctx: ToolContext,
    *,
    cwd: str | None,
    base_ref: str,
    head_ref: str,
    use_merge_base: bool,
    max_depth: int,
    impact_limit: int,
    offset: int,
    symbol_limit: int,
    include_external: bool,
) -> dict[str, object]:
    try:
        resolved_cwd, head_snapshot, backend_config, identity = _resolve_graph(
            ctx, cwd=cwd, ref=head_ref
        )
        base_snapshot = resolve_comparison_base(
            resolved_cwd,
            base_ref=base_ref,
            head_ref=head_ref,
            use_merge_base=use_merge_base,
        )
        change_set = collect_python_change_set(base_snapshot, head_snapshot)
    except (
        CodeGraphBackendError,
        CodeGraphChangeError,
        GitSnapshotError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        return _error("code_graph_diff_resolution_failed", f"{type(exc).__name__}: {exc}")

    changed_symbols = change_set.get("changed_symbols")
    if not isinstance(changed_symbols, list):
        changed_symbols = []
    start = max(0, offset)
    page = changed_symbols[start : start + symbol_limit]
    next_offset = start + len(page) if start + len(page) < len(changed_symbols) else None
    needs_head_graph = any(
        isinstance(symbol, dict)
        and symbol.get("status") != "deleted"
        and not bool(symbol.get("test_symbol"))
        for symbol in page
    )

    entry = None
    engine = None
    if needs_head_graph:
        if not backend_config.enabled:
            return _error(
                "code_graph_disabled",
                "Code Graph is disabled by configuration.",
                head_ref=head_snapshot.requested_ref,
                head_tree_sha=head_snapshot.tree_sha,
            )
        cache = CodeGraphCache(ctx.state_dir)
        entry = cache.status(
            identity.repository_id,
            identity.graph_id,
            expected_identity=identity,
        )
        if (
            entry.status != GraphStatus.READY
            or entry.payload_path is None
            or entry.manifest is None
        ):
            return _error(
                "code_graph_not_ready",
                (
                    f"Code Graph for {head_snapshot.requested_ref} is {entry.status.value}; "
                    "diff impact requires a READY head CPG."
                ),
                base_ref=base_ref,
                resolved_base_ref=base_snapshot.requested_ref,
                base_tree_sha=base_snapshot.tree_sha,
                head_ref=head_snapshot.requested_ref,
                head_tree_sha=head_snapshot.tree_sha,
                graph_id=identity.graph_id,
                repository_id=identity.repository_id,
                total_changed_symbols=len(changed_symbols),
                changed_files=change_set.get("changed_files", []),
                next_action=(
                    "Call code_graph_prepare for head_ref, await the durable prepare job, "
                    "then retry code_graph_diff_impact."
                ),
            )
        engine = JoernStructuralQueryEngine(
            JoernQueryServerRuntime(_query_server_config(ctx, backend_config))
        )

    exact_files = {
        path
        for path in change_set.get("changed_files", [])
        if isinstance(path, str)
    }
    exact_tests = {
        path
        for path in change_set.get("changed_test_files", [])
        if isinstance(path, str)
    }
    candidate_files: set[str] = set()
    candidate_tests: set[str] = set()
    symbol_impacts: list[dict[str, object]] = []
    page_semantic_complete = True

    for raw_symbol in page:
        if not isinstance(raw_symbol, dict):
            continue
        symbol = dict(raw_symbol)
        status = str(symbol.get("status") or "")
        head_full_name = symbol.get("head_full_name")
        impact_payload: dict[str, object] = {
            **symbol,
            "impact_status": "pending",
            "impact_methods": [],
            "unresolved_call_sites": [],
        }
        if status == "deleted":
            impact_payload["impact_status"] = "base_graph_required"
            impact_payload["next_action"] = (
                "Prepare/query the comparison base graph for deleted-symbol reverse impact."
            )
            page_semantic_complete = False
            symbol_impacts.append(impact_payload)
            continue
        if bool(symbol.get("test_symbol")):
            impact_payload["impact_status"] = "direct_test_change"
            symbol_impacts.append(impact_payload)
            continue
        if not isinstance(head_full_name, str) or not head_full_name:
            impact_payload["impact_status"] = "head_symbol_missing"
            page_semantic_complete = False
            symbol_impacts.append(impact_payload)
            continue
        if engine is None or entry is None or entry.payload_path is None or entry.manifest is None:
            return _error(
                "code_graph_internal_error",
                "Head graph query engine was not initialized for a queryable changed symbol.",
            )
        try:
            query = engine.run(
                graph_id=identity.graph_id,
                cpg_path=entry.payload_path,
                cpg_sha256=entry.manifest.payload_sha256,
                mode="impact",
                symbol=head_full_name,
                max_depth=max_depth,
                limit=impact_limit,
                include_external=include_external,
            )
        except JoernQueryRuntimeNotReady as exc:
            return _error(
                "code_graph_runtime_not_ready",
                str(exc),
                head_ref=head_snapshot.requested_ref,
                head_tree_sha=head_snapshot.tree_sha,
                graph_id=identity.graph_id,
                repository_id=identity.repository_id,
                next_action=(
                    "Call code_graph_prepare for head_ref and await its durable prewarm "
                    "before retrying code_graph_diff_impact."
                ),
            )
        except (CodeGraphBackendError, CodeGraphQueryError, OSError, TypeError, ValueError) as exc:
            return _error(
                "code_graph_query_failed",
                f"{type(exc).__name__}: {exc}",
                head_ref=head_snapshot.requested_ref,
                head_tree_sha=head_snapshot.tree_sha,
                graph_id=identity.graph_id,
                repository_id=identity.repository_id,
            )

        methods = query.get("results")
        if not isinstance(methods, list):
            methods = []
        unresolved = query.get("unresolved_call_sites")
        if not isinstance(unresolved, list):
            unresolved = []
        symbol_name = str(symbol.get("name") or "")
        aggregate_unresolved_scope = not (
            symbol_name.startswith("__") and symbol_name.endswith("__")
        )
        unresolved_scope_reason = (
            "specific_symbol_name"
            if aggregate_unresolved_scope
            else "dunder_symbol_noise"
        )
        unresolved_evidence = (
            unresolved
            if aggregate_unresolved_scope
            else unresolved[:_DUNDER_UNRESOLVED_EVIDENCE_LIMIT]
        )
        unresolved_evidence_omitted = max(
            0,
            int(query.get("total_unresolved_call_sites") or 0)
            - len(unresolved_evidence),
        )
        impact_status = "ok"
        if bool(query.get("ambiguous")):
            impact_status = "ambiguous"
            page_semantic_complete = False
        elif bool(query.get("not_found")):
            impact_status = "symbol_not_found_in_head_cpg"
            page_semantic_complete = False
        if query.get("call_resolution_complete") is False:
            page_semantic_complete = False

        for method in methods:
            if not isinstance(method, dict):
                continue
            path = method.get("file")
            if isinstance(path, str) and path:
                exact_files.add(path)
                if is_test_path(path):
                    exact_tests.add(path)
        if aggregate_unresolved_scope:
            for call_site in unresolved:
                if not isinstance(call_site, dict):
                    continue
                path = call_site.get("caller_file")
                if isinstance(path, str) and path:
                    candidate_files.add(path)
                    if is_test_path(path):
                        candidate_tests.add(path)

        impact_payload.update(
            {
                "impact_status": impact_status,
                "impact_methods": methods,
                "unresolved_call_sites": unresolved_evidence,
                "total_impact_methods": int(query.get("total_results") or 0),
                "total_unresolved_call_sites": int(
                    query.get("total_unresolved_call_sites") or 0
                ),
                "returned_unresolved_call_sites": len(unresolved_evidence),
                "omitted_unresolved_call_sites": unresolved_evidence_omitted,
                "unresolved_scope_aggregated": aggregate_unresolved_scope,
                "unresolved_scope_reason": unresolved_scope_reason,
                "call_resolution_complete": query.get("call_resolution_complete"),
                "query_truncated": bool(query.get("query_truncated")),
                "query_duration_seconds": query.get("query_duration_seconds"),
            }
        )
        if bool(query.get("query_truncated")):
            page_semantic_complete = False
        symbol_impacts.append(impact_payload)

    deleted_total = sum(
        1
        for item in changed_symbols
        if isinstance(item, dict) and item.get("status") == "deleted"
    )
    unsupported_files = change_set.get("unsupported_changed_files", [])
    module_scope_changes = change_set.get("module_scope_changes", [])
    analysis_errors = change_set.get("analysis_errors", [])
    pagination_complete = next_offset is None
    semantic_scope_complete = (
        page_semantic_complete
        and deleted_total == 0
        and not unsupported_files
        and not module_scope_changes
        and not analysis_errors
    )
    impact_complete = pagination_complete and semantic_scope_complete
    payload: dict[str, object] = {
        "success": True,
        "base_ref": base_ref,
        "resolved_base_ref": base_snapshot.requested_ref,
        "base_tree_sha": base_snapshot.tree_sha,
        "head_ref": head_snapshot.requested_ref,
        "head_tree_sha": head_snapshot.tree_sha,
        "merge_base_used": use_merge_base,
        "graph_id": identity.graph_id,
        "repository_id": identity.repository_id,
        "identity_kind": "committed_git_tree_diff",
        "working_tree_included": False,
        "changed_files": change_set.get("changed_files", []),
        "changed_test_files": change_set.get("changed_test_files", []),
        "unsupported_changed_files": unsupported_files,
        "module_scope_changes": module_scope_changes,
        "analysis_errors": analysis_errors,
        "total_changed_symbols": len(changed_symbols),
        "total_deleted_symbols": deleted_total,
        "offset": start,
        "symbol_limit": symbol_limit,
        "page_symbol_count": len(page),
        "next_offset": next_offset,
        "pagination_complete": pagination_complete,
        "semantic_scope_complete": semantic_scope_complete,
        "impact_complete": impact_complete,
        "base_graph_required": deleted_total > 0,
        "module_scope_requires_import_analysis": bool(module_scope_changes),
        "symbol_impacts": symbol_impacts,
        "exact_affected_files": sorted(exact_files),
        "exact_affected_tests": sorted(exact_tests),
        "candidate_affected_files": sorted(candidate_files),
        "candidate_affected_tests": sorted(candidate_tests),
        "partial": not pagination_complete,
        "stop_reason": "symbol_page" if not pagination_complete else "end_of_results",
    }
    return _fit_diff_impact_payload(ctx, payload)


def register_code_graph_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    @mcp.tool(
        name="code_graph_status",
        title="Code Graph Status",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Always call this first for Code Graph work. Resolve an exact committed Git ref to "
            "its immutable tree-based Code Graph identity, report backend/cache/runtime state, "
            "and return query_ready plus the exact next lifecycle action. The current dirty "
            "working tree is never silently analyzed under a committed tree identity."
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
            runtime_ready = False
            if (
                backend_config.enabled
                and entry.status == GraphStatus.READY
                and entry.payload_path is not None
                and entry.manifest is not None
            ):
                runtime_ready = JoernQueryServerRuntime(
                    _query_server_config(ctx, backend_config)
                ).is_ready(
                    graph_id=identity.graph_id,
                    cpg_path=entry.payload_path,
                    cpg_sha256=entry.manifest.payload_sha256,
                )
        except (CodeGraphBackendError, GitSnapshotError, OSError, TypeError, ValueError) as exc:
            return _error("code_graph_resolution_failed", f"{type(exc).__name__}: {exc}")
        query_ready = (
            backend_status.available
            and entry.status == GraphStatus.READY
            and runtime_ready
        )
        if query_ready:
            lifecycle_state = "ready"
            next_action = (
                "Run the required code_graph_callers/callees/impact/diff_impact/path query "
                "for this same committed ref."
            )
        elif not backend_status.available:
            lifecycle_state = "backend_unavailable"
            next_action = (
                "Fix the reported Code Graph backend availability error before preparing "
                "or querying this graph."
            )
        elif entry.status == GraphStatus.READY:
            lifecycle_state = "runtime_cold"
            next_action = (
                "Call code_graph_prepare for this same ref. If it returns a job_id, call "
                "await_job(job_id), then call code_graph_status again until query_ready=true."
            )
        elif entry.status == GraphStatus.BUILDING:
            lifecycle_state = "building"
            next_action = (
                "Call code_graph_prepare for this same ref to reuse/discover the owned prepare "
                "job, await_job(job_id) when returned, then call code_graph_status again."
            )
        else:
            lifecycle_state = entry.status.value
            next_action = (
                "Call code_graph_prepare for this same ref. If it returns a job_id, call "
                "await_job(job_id), then call code_graph_status again until query_ready=true."
            )
        return {
            "success": True,
            "ref": snapshot.requested_ref,
            "tree_sha": snapshot.tree_sha,
            "graph_id": identity.graph_id,
            "repository_id": identity.repository_id,
            "identity_kind": "committed_git_tree",
            "working_tree_included": False,
            "analyzer_id": identity.analyzer_id,
            "query_runtime": "persistent-rest",
            "query_runtime_ready": runtime_ready,
            "query_ready": query_ready,
            "lifecycle_state": lifecycle_state,
            "next_action": next_action,
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
            "Ensure an immutable Joern CPG and its persistent query runtime are ready for an "
            "exact committed Git ref. Returns immediately only when both are ready; otherwise "
            "starts or reuses one session-owned durable prepare job. The source snapshot is "
            "exported from Git and mounted read-only into an offline hardened Joern container."
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
        except (CodeGraphBackendError, GitSnapshotError, OSError, TypeError, ValueError) as exc:
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
            cache_ready = entry.status == GraphStatus.READY
            if (
                cache_ready
                and entry.payload_path is not None
                and entry.manifest is not None
            ):
                runtime = JoernQueryServerRuntime(_query_server_config(ctx, backend_config))
                if runtime.is_ready(
                    graph_id=identity.graph_id,
                    cpg_path=entry.payload_path,
                    cpg_sha256=entry.manifest.payload_sha256,
                ):
                    return {
                        "success": True,
                        "status": "ready",
                        "cache_hit": True,
                        "runtime_ready": True,
                        "query_runtime": "persistent-rest",
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
                    "status": "warming" if cache_ready else "building",
                    "cache_hit": cache_ready,
                    "runtime_ready": False,
                    "ref": snapshot.requested_ref,
                    "tree_sha": snapshot.tree_sha,
                    "graph_id": identity.graph_id,
                    "repository_id": identity.repository_id,
                    "job_id": (
                        str(active_job.get("job_id"))
                        if isinstance(active_job, dict) and active_job.get("job_id")
                        else None
                    ),
                    "next_action": (
                        "Wait for the existing prepare job to finish, then call "
                        "code_graph_status."
                    ),
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
            "status": "warming" if cache_ready else "building",
            "cache_hit": cache_ready,
            "runtime_ready": False,
            "ref": snapshot.requested_ref,
            "tree_sha": snapshot.tree_sha,
            "graph_id": identity.graph_id,
            "repository_id": identity.repository_id,
            "job_id": job_result.get("job_id"),
            "next_action": (
                "Use await_job for this owned prepare job; after it is terminal and "
                "verified, call code_graph_status."
            ),
        }

    @mcp.tool(
        name="code_graph_callers",
        title="Code Graph Callers",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return exact semantic callers of one Joern method name or full_name from a READY "
            "immutable CPG. For callers/impact, same-name Python calls whose methodFullName is "
            "unresolved are reported separately as candidate unresolved_call_sites; inspect "
            "call_resolution_complete before treating exact callers as exhaustive."
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
            "callers with depth. The seed method is depth 0. Same-name unresolved Python call "
            "sites are reported separately; call_resolution_complete=false means the exact "
            "reverse-call graph is not exhaustive."
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
        name="code_graph_diff_impact",
        title="Code Graph Diff Impact",
        annotations=READ_ONLY_TOOL,
        description=(
            "Map committed Python changes between base_ref and head_ref to changed functions/"
            "methods, then run bounded reverse semantic impact on the READY head Code Graph. "
            "Exact affected files/tests and unresolved Python candidate call sites stay separate. "
            "Deleted symbols are reported as requiring base-graph evidence rather than guessed."
        ),
    )
    def code_graph_diff_impact(
        cwd: Annotated[
            str | None,
            Field(description="Git repository directory. Defaults to the current session cwd."),
        ] = None,
        base_ref: Annotated[
            str,
            Field(description="Committed comparison base ref, such as main or HEAD~1."),
        ] = "HEAD~1",
        head_ref: Annotated[
            str,
            Field(description="Committed head ref whose READY Code Graph is queried."),
        ] = "HEAD",
        use_merge_base: Annotated[
            bool,
            Field(
                description=(
                    "Resolve the base/head merge-base before computing the diff. "
                    "Recommended for branch or PR review."
                )
            ),
        ] = True,
        max_depth: Annotated[
            int,
            Field(description="Maximum reverse-call depth per changed symbol.", ge=1, le=20),
        ] = 4,
        impact_limit: Annotated[
            int,
            Field(description="Maximum impact methods returned per changed symbol.", ge=1, le=100),
        ] = 40,
        offset: Annotated[
            int,
            Field(description="Changed-symbol offset for lossless pagination.", ge=0),
        ] = 0,
        symbol_limit: Annotated[
            int,
            Field(description="Maximum changed symbols analyzed in this call.", ge=1, le=25),
        ] = 10,
        include_external: Annotated[
            bool,
            Field(description="Include Joern external/operator methods when true."),
        ] = False,
    ) -> dict[str, object]:
        return _execute_diff_impact(
            ctx,
            cwd=cwd,
            base_ref=base_ref,
            head_ref=head_ref,
            use_merge_base=use_merge_base,
            max_depth=max_depth,
            impact_limit=impact_limit,
            offset=offset,
            symbol_limit=symbol_limit,
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
        "code_graph_diff_impact": code_graph_diff_impact,
        "code_graph_path": code_graph_path,
    }
