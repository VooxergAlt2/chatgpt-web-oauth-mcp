from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection
import hashlib
import json
from typing import Annotated, Any, cast

from fastmcp import Context
from mcp import types as mcp_types
from mcp.server.session import MODERN_PROTOCOL_VERSIONS
from pydantic import Field

from .codex_runtime.errors import CodexRuntimeError
from .codex_runtime.models import SandboxMode
from .pathing import resolve_cwd
from .response_budget import ResponseBudget, with_budget_metadata
from .tool_context import OPEN_WORLD_WRITE_TOOL, READ_ONLY_TOOL, LOCAL_STATE_TOOL, ToolContext


_MAX_TEXT_BYTES = 256 * 1024
_ELICITATION_TIMEOUT_SECONDS = 285.0
_MODERN_ELICITATION_STATE_VERSION = 1
_MODERN_ELICITATION_KEY_PREFIX = "codex_elicitation:"


def _uses_modern_input_required(session: Any) -> bool:
    return session.protocol_version in MODERN_PROTOCOL_VERSIONS


class _ModernInteractionRequired(Exception):
    def __init__(self, interaction: dict[str, Any], interaction_index: int) -> None:
        super().__init__("Modern MCP client input is required.")
        self.interaction = interaction
        self.interaction_index = interaction_index


def _form_elicitation_fields(
    interaction: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    params = interaction.get("params")
    if not isinstance(params, dict):
        return None
    mode = params.get("mode", "form")
    message = params.get("message")
    requested_schema = params.get("requestedSchema")
    if (
        mode not in {"form", "openai/form", "openaiForm"}
        or not isinstance(message, str)
        or not isinstance(requested_schema, dict)
    ):
        return None
    return message, requested_schema


def _interaction_digest(interaction: dict[str, Any]) -> str:
    rendered = json.dumps(
        {
            "method": interaction.get("method"),
            "params": interaction.get("params"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _invocation_digest(
    *,
    runtime_id: str,
    server: str,
    tool: str,
    arguments: dict[str, Any] | None,
    meta: dict[str, Any] | None,
) -> str:
    rendered = json.dumps(
        {
            "runtime_id": runtime_id,
            "server": server,
            "tool": tool,
            "arguments": arguments,
            "_meta": meta,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _decode_modern_elicitation_state(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {"version": _MODERN_ELICITATION_STATE_VERSION, "responses": []}
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid modern elicitation requestState.") from exc
    if (
        not isinstance(state, dict)
        or state.get("version") != _MODERN_ELICITATION_STATE_VERSION
        or not isinstance(state.get("invocation_digest"), str)
        or not isinstance(state.get("responses"), list)
    ):
        raise ValueError("Unsupported modern elicitation requestState.")
    responses = state["responses"]
    for item in responses:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("digest"), str)
            or not isinstance(item.get("response"), dict)
        ):
            raise ValueError("Malformed modern elicitation replay entry.")
    pending = state.get("pending")
    if pending is not None and (
        not isinstance(pending, dict)
        or not isinstance(pending.get("key"), str)
        or not isinstance(pending.get("digest"), str)
    ):
        raise ValueError("Malformed modern elicitation pending state.")
    return state


def _elicitation_result_payload(result: Any) -> dict[str, Any]:
    action = getattr(result, "action", None)
    if action not in {"accept", "decline", "cancel"}:
        raise ValueError("Modern MCP client returned an unsupported elicitation action.")
    return {
        "action": action,
        "content": getattr(result, "content", None) if action == "accept" else None,
        "_meta": getattr(result, "meta", None),
    }


def _prepare_modern_elicitation_replay(
    context: Context,
    invocation_digest: str,
) -> list[dict[str, Any]]:
    state = _decode_modern_elicitation_state(context.request_state)
    if context.request_state and state["invocation_digest"] != invocation_digest:
        raise ValueError("Modern elicitation replay invocation mismatch.")
    responses = list(state["responses"])
    incoming = context.input_responses
    pending = state.get("pending")
    if incoming is None:
        return responses
    if pending is None:
        raise ValueError("Modern elicitation responses arrived without pending request state.")
    key = pending["key"]
    if set(incoming) != {key}:
        raise ValueError("Modern elicitation response keys do not match pending request state.")
    responses.append(
        {
            "digest": pending["digest"],
            "response": _elicitation_result_payload(incoming[key]),
        }
    )
    return responses


def _modern_input_required_result(
    exc: _ModernInteractionRequired,
    replay_responses: list[dict[str, Any]],
    invocation_digest: str,
) -> mcp_types.InputRequiredResult:
    fields = _form_elicitation_fields(exc.interaction)
    if fields is None:
        raise ValueError("Unsupported downstream elicitation request.")
    message, requested_schema = fields
    digest = _interaction_digest(exc.interaction)
    key = f"{_MODERN_ELICITATION_KEY_PREFIX}{exc.interaction_index}"
    state = {
        "version": _MODERN_ELICITATION_STATE_VERSION,
        "invocation_digest": invocation_digest,
        "responses": replay_responses,
        "pending": {"key": key, "digest": digest},
    }
    return mcp_types.InputRequiredResult(
        input_requests={
            key: mcp_types.ElicitRequest(
                params=mcp_types.ElicitRequestFormParams(
                    message=message,
                    requestedSchema=requested_schema,
                )
            )
        },
        request_state=json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def register_codex_runtime_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Register the persistent Codex runtime surface."""

    @mcp.tool(
        name="codex_runtime_open",
        title="Open Codex Runtime",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Create a persistent Codex App Server thread for one cwd and sandbox policy. "
            "This starts the runtime thread but never starts a Codex LLM turn. "
            "Omit sandbox to use the server-configured default policy. Prefer "
            "codex_runtime_acquire for recurring logical workers so existing runtimes are reused."
        ),
    )
    def codex_runtime_open(
        cwd: Annotated[
            str,
            Field(description="Directory to bind to this runtime."),
        ],
        sandbox: Annotated[
            SandboxMode | None,
            Field(description="Sandbox policy: read-only, workspace-write, or full-access; omit for server default."),
        ] = None,
        name: Annotated[
            str | None,
            Field(description="Optional human-readable runtime name."),
        ] = None,
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        effective_sandbox = sandbox or cast(SandboxMode, ctx.codex_runtime_default_sandbox)
        return _invoke(
            lambda: manager.open_runtime(
                cwd=resolve_cwd(cwd, ctx.workspace_root),
                sandbox=effective_sandbox,
                name=name,
            )
        )

    @mcp.tool(
        name="codex_runtime_list",
        title="List Codex Runtimes",
        annotations=READ_ONLY_TOOL,
        description=(
            "List persisted Codex runtime bindings, newest-used first. Optional exact name, cwd, "
            "and status filters help callers find reusable runtimes before creating new ones. "
            "Expired idle bindings are garbage-collected before the list is returned."
        ),
    )
    def codex_runtime_list(
        name: Annotated[
            str | None,
            Field(description="Optional exact logical runtime name."),
        ] = None,
        cwd: Annotated[
            str | None,
            Field(description="Optional exact bound working directory."),
        ] = None,
        status: Annotated[
            str | None,
            Field(description="Optional status filter: ready, detached, or error."),
        ] = None,
        offset: Annotated[
            int,
            Field(ge=0, description="Zero-based result offset."),
        ] = 0,
        limit: Annotated[
            int,
            Field(ge=1, le=100, description="Maximum number of runtimes to return."),
        ] = 50,
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root) if cwd else None
        payload = _invoke(
            lambda: manager.list_runtimes(
                name=name,
                cwd=resolved_cwd,
                status=status,
                offset=offset,
                limit=limit,
            )
        )
        return _bounded(payload, ctx.tool_output_token_budget, fields=("runtimes",))

    @mcp.tool(
        name="codex_runtime_acquire",
        title="Acquire Codex Runtime",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Acquire a stable logical Codex runtime by exact name, cwd, and sandbox. Reuse/resume "
            "the most recently used matching binding when one exists; otherwise create it. "
            "Use stable names such as manager or plm-worker-01 instead of per-run timestamp names."
        ),
    )
    def codex_runtime_acquire(
        cwd: Annotated[
            str,
            Field(description="Directory to bind to this logical runtime."),
        ],
        name: Annotated[
            str,
            Field(description="Stable logical runtime name used for future reuse."),
        ],
        sandbox: Annotated[
            SandboxMode | None,
            Field(description="Sandbox policy; omit for the server-configured default."),
        ] = None,
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        effective_sandbox = sandbox or cast(SandboxMode, ctx.codex_runtime_default_sandbox)
        return _invoke(
            lambda: manager.acquire_runtime(
                cwd=resolve_cwd(cwd, ctx.workspace_root),
                sandbox=effective_sandbox,
                name=name,
            )
        )

    @mcp.tool(
        name="codex_runtime_resume",
        title="Resume Codex Runtime",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Resume a persisted Codex runtime by runtime_id, or bind an explicit thread_id. "
            "A runtime_id uses its persisted cwd and sandbox and rejects mismatches. "
            "When the original thread has no rollout, the runtime_id is retained and a "
            "same-policy replacement thread is created explicitly in the result. "
            "An unknown thread_id requires cwd and sandbox so the project binding is verified."
        ),
    )
    def codex_runtime_resume(
        runtime_id: Annotated[
            str | None,
            Field(description="Persisted runtime identity; mutually exclusive with thread_id."),
        ] = None,
        thread_id: Annotated[
            str | None,
            Field(description="Codex thread identity; requires cwd and sandbox when unbound."),
        ] = None,
        cwd: Annotated[
            str | None,
            Field(description="Expected bound cwd, required when resuming an unbound thread_id."),
        ] = None,
        sandbox: Annotated[
            SandboxMode | None,
            Field(description="Expected sandbox, required when resuming an unbound thread_id."),
        ] = None,
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root) if cwd else None
        return _invoke(
            lambda: manager.resume_runtime(
                runtime_id=runtime_id,
                thread_id=thread_id,
                cwd=resolved_cwd,
                sandbox=sandbox,
            )
        )

    @mcp.tool(
        name="codex_runtime_status",
        title="Codex Runtime Status",
        annotations=READ_ONLY_TOOL,
        description=(
            "Return bounded metadata for one Codex runtime without returning thread history. "
            "Persisted runtimes are detached after an MCP server restart and can be resumed explicitly."
        ),
    )
    def codex_runtime_status(
        runtime_id: Annotated[str, Field(description="Runtime identity to inspect.")],
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        return _invoke(lambda: manager.runtime_status(runtime_id))

    @mcp.tool(
        name="codex_runtime_close",
        title="Close Codex Runtime",
        annotations=LOCAL_STATE_TOOL,
        description=(
            "Mark a local Codex runtime binding detached while preserving its runtime_id and Codex thread. "
            "This is detach-only: it does not call thread/delete or thread/archive, and the same runtime_id "
            "can be resumed later until the detached binding is reclaimed by idle-TTL GC or capacity LRU."
        ),
    )
    def codex_runtime_close(
        runtime_id: Annotated[str, Field(description="Runtime identity to detach.")],
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        return _invoke(lambda: manager.close_runtime(runtime_id))

    @mcp.tool(
        name="codex_mcp_inventory",
        title="List Codex MCP Tools",
        annotations=READ_ONLY_TOOL,
        description=(
            "List the MCP servers, auth status, resources, and tools connected to one Codex thread. "
            "The upstream catalog is paginated and the response is bounded; use next_cursor to continue. "
            "Optional server and tool_query filters are applied without starting a Codex LLM turn."
        ),
    )
    def codex_mcp_inventory(
        runtime_id: Annotated[str, Field(description="Runtime identity whose Codex thread is queried.")],
        server: Annotated[
            str | None,
            Field(description="Optional exact MCP server name filter."),
        ] = None,
        tool_query: Annotated[
            str | None,
            Field(description="Optional case-insensitive tool name or description filter."),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="Opaque cursor returned by a prior inventory call."),
        ] = None,
        limit: Annotated[
            int,
            Field(ge=1, le=100, description="Maximum upstream server page size."),
        ] = 20,
    ) -> dict[str, object]:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        payload = _invoke(
            lambda: manager.mcp_inventory(
                runtime_id=runtime_id,
                server=server,
                tool_query=tool_query,
                cursor=cursor,
                limit=limit,
            )
        )
        return _bounded(payload, ctx.tool_output_token_budget, fields=("servers",))

    @mcp.tool(
        name="codex_mcp_call",
        title="Call Codex MCP Tool",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Call one MCP tool connected to the selected Codex thread without starting turn/start. "
            "The downstream content, structuredContent, isError, and _meta fields are retained. "
            "Form elicitations are normally forwarded to the current MCP client. An explicitly "
            "configured prototype policy can auto-approve only exact allowlisted Computer Use "
            "app-access requests."
        ),
    )
    async def codex_mcp_call(
        runtime_id: Annotated[str, Field(description="Runtime identity whose Codex thread is used.")],
        server: Annotated[str, Field(description="Connected Codex MCP server name.")],
        tool: Annotated[str, Field(description="Connected MCP tool name.")],
        context: Context,
        arguments: Annotated[
            dict[str, Any] | None,
            Field(description="JSON object passed to the downstream MCP tool."),
        ] = None,
        _meta: Annotated[
            dict[str, Any] | None,
            Field(description="Optional MCP _meta object passed to the downstream tool."),
        ] = None,
    ) -> dict[str, object] | mcp_types.InputRequiredResult:
        manager = _manager_or_error(ctx)
        if isinstance(manager, dict):
            return manager
        event_loop = asyncio.get_running_loop()
        outer_session = context.session
        outer_request_id = context.request_id
        modern_input_required = _uses_modern_input_required(outer_session)
        invocation_digest = _invocation_digest(
            runtime_id=runtime_id,
            server=server,
            tool=tool,
            arguments=arguments,
            meta=_meta,
        )
        replay_responses = (
            _prepare_modern_elicitation_replay(context, invocation_digest)
            if modern_input_required
            else []
        )
        interaction_index = 0
        approval_mode = getattr(ctx, "codex_runtime_cua_approval_mode", "interactive")
        allowed_apps = frozenset(getattr(ctx, "codex_runtime_cua_allowed_apps", ()))

        def handle_interaction(interaction: dict[str, Any]) -> dict[str, Any]:
            nonlocal interaction_index
            policy_response = _computer_use_approval_response(
                interaction,
                approval_mode=approval_mode,
                allowed_apps=allowed_apps,
            )
            if policy_response is not None:
                return policy_response
            if modern_input_required:
                current_index = interaction_index
                interaction_index += 1
                digest = _interaction_digest(interaction)
                if current_index < len(replay_responses):
                    saved = replay_responses[current_index]
                    if saved["digest"] != digest:
                        raise ValueError(
                            "Modern elicitation replay mismatch; downstream interaction changed."
                        )
                    return cast(dict[str, Any], saved["response"])
                raise _ModernInteractionRequired(interaction, current_index)
            future = asyncio.run_coroutine_threadsafe(
                _bridge_elicitation(outer_session, outer_request_id, interaction),
                event_loop,
            )
            try:
                return future.result(timeout=_ELICITATION_TIMEOUT_SECONDS)
            except Exception:
                future.cancel()
                raise

        try:
            payload = await asyncio.to_thread(
                _invoke,
                lambda: manager.mcp_call(
                    runtime_id=runtime_id,
                    server=server,
                    tool=tool,
                    arguments=arguments,
                    meta=_meta,
                    interaction_handler=handle_interaction,
                ),
            )
        except _ModernInteractionRequired as exc:
            return _modern_input_required_result(
                exc,
                replay_responses,
                invocation_digest,
            )
        if modern_input_required and interaction_index != len(replay_responses):
            raise ValueError(
                "Modern elicitation replay state was not fully consumed by downstream execution."
            )
        return _bounded(payload, ctx.tool_output_token_budget, fields=("content", "structuredContent", "_meta"))

    return {
        "codex_runtime_open": codex_runtime_open,
        "codex_runtime_list": codex_runtime_list,
        "codex_runtime_acquire": codex_runtime_acquire,
        "codex_runtime_resume": codex_runtime_resume,
        "codex_runtime_status": codex_runtime_status,
        "codex_runtime_close": codex_runtime_close,
        "codex_mcp_inventory": codex_mcp_inventory,
        "codex_mcp_call": codex_mcp_call,
    }


def _computer_use_approval_response(
    interaction: dict[str, Any],
    *,
    approval_mode: str,
    allowed_apps: Collection[str],
) -> dict[str, Any] | None:
    if approval_mode == "interactive":
        return None
    if interaction.get("method") != "mcpServer/elicitation/request":
        return None
    params = interaction.get("params")
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict) or meta.get("connector_id") != "computer-use":
        return None
    if approval_mode == "deny":
        return {"action": "decline", "content": None, "_meta": None}
    if approval_mode != "prototype":
        return None

    requested_schema = params.get("requestedSchema")
    tool_params = meta.get("tool_params")
    if (
        params.get("serverName") != "cua_repl"
        or params.get("turnId") is not None
        or not isinstance(params.get("threadId"), str)
        or not params["threadId"]
        or params.get("mode", "form") != "form"
        or requested_schema != {"type": "object", "properties": {}}
        or meta.get("codex_approval_kind") != "mcp_tool_call"
        or meta.get("tool_name") != "get_app_state"
        or meta.get("riskLevel") not in {"low", "high"}
        or not isinstance(tool_params, dict)
        or set(tool_params) != {"app"}
    ):
        return None
    app = tool_params.get("app")
    if not isinstance(app, str) or app not in allowed_apps:
        return None

    persist = meta.get("persist")
    if (
        not isinstance(persist, list)
        or not persist
        or not all(isinstance(item, str) for item in persist)
        or len(set(persist)) != len(persist)
        or not set(persist).issubset({"session", "always"})
    ):
        return None
    persistence = "always" if "always" in persist else "session" if "session" in persist else None
    if persistence is None:
        return None
    return {
        "action": "accept",
        "content": {},
        "_meta": {"persist": persistence},
    }


async def _bridge_elicitation(
    session: Any,
    request_id: str,
    interaction: dict[str, Any],
) -> dict[str, Any]:
    fields = _form_elicitation_fields(interaction)
    if fields is None:
        return {"action": "cancel", "content": None, "_meta": None}
    message, requested_schema = fields

    # The session API preserves the upstream JSON Schema; Context.elicit expects a Python type.
    result = await session.elicit_form(
        message=message,
        requested_schema=requested_schema,
        related_request_id=request_id,
    )
    action = getattr(result, "action", None)
    if action not in {"accept", "decline", "cancel"}:
        raise ValueError("Outer MCP client returned an unsupported elicitation action.")
    content = getattr(result, "content", None) if action == "accept" else None
    return {"action": action, "content": content, "_meta": None}


def _manager_or_error(ctx: ToolContext) -> Any:
    manager = ctx.codex_runtime_manager
    if manager is None:
        return {
            "success": False,
            "error": {
                "code": "runtime_disabled",
                "message": "Codex runtime support is not configured.",
            },
        }
    return manager


def _invoke(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
    try:
        return operation()
    except _ModernInteractionRequired:
        raise
    except CodexRuntimeError as exc:
        payload: dict[str, object] = {
            "success": False,
            "error": {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            },
        }
        if exc.details:
            payload["error"]["details"] = exc.details  # type: ignore[index]
        if exc.side_effects:
            payload["side_effects"] = exc.side_effects
        return payload
    except (OSError, TypeError, ValueError) as exc:
        return {
            "success": False,
            "error": {
                "code": "runtime_invalid_operation",
                "message": str(exc),
            },
        }
    except Exception:
        return {
            "success": False,
            "error": {
                "code": "runtime_internal_error",
                "message": "The Codex runtime operation failed unexpectedly.",
            },
        }


def _bounded(
    payload: dict[str, object],
    max_tokens: int,
    *,
    fields: tuple[str, ...],
) -> dict[str, object]:
    budget = ResponseBudget(max_tokens=max_tokens)
    complete, measurement = with_budget_metadata(
        payload,
        budget=budget,
        truncated=False,
        stop_reason="end_of_result",
    )
    if measurement.fits:
        return complete
    for fraction in (0.75, 0.5, 0.35, 0.2, 0.1, 0.05, 0.0):
        candidate = dict(payload)
        for field in fields:
            if field in candidate:
                candidate[field] = (
                    _scale_inventory(candidate[field], fraction)
                    if field == "servers"
                    else _scale_value(candidate[field], fraction)
                )
        candidate["truncated_fields"] = list(fields)
        result, result_measurement = with_budget_metadata(
            candidate,
            budget=budget,
            truncated=True,
            stop_reason="token_budget",
        )
        if result_measurement.fits:
            return result
    return complete


def _scale_inventory(value: Any, fraction: float) -> list[Any]:
    if not isinstance(value, list):
        return []
    if fraction >= 1:
        return value
    keep = int(len(value) * fraction)
    if fraction > 0 and keep == 0 and value:
        keep = 1
    scaled_servers: list[Any] = []
    for server in value[:keep]:
        if not isinstance(server, dict):
            scaled_servers.append(server)
            continue
        scaled: dict[Any, Any] = {}
        for key, item in server.items():
            # Server identity and tool names are used as the next call's
            # arguments, so budget reduction must never corrupt them.
            if key in {"server", "name", "authStatus", "runtimeStatus"}:
                scaled[key] = item
            elif key == "tools" and isinstance(item, dict):
                scaled[key] = _scale_inventory_tools(item, fraction)
            else:
                scaled[key] = _scale_value(item, fraction)
        scaled_servers.append(scaled)
    return scaled_servers


def _scale_inventory_tools(value: dict[Any, Any], fraction: float) -> dict[Any, Any]:
    scaled_tools: dict[Any, Any] = {}
    for tool_name, tool in value.items():
        if not isinstance(tool, dict):
            scaled_tools[tool_name] = tool
            continue
        scaled_tool: dict[Any, Any] = {}
        for key, item in tool.items():
            if key == "name":
                scaled_tool[key] = item
            else:
                scaled_tool[key] = _scale_value(item, fraction)
        scaled_tools[tool_name] = scaled_tool
    return scaled_tools


def _scale_value(value: Any, fraction: float) -> Any:
    if fraction >= 1:
        return value
    if isinstance(value, str):
        return _truncate_text(value, int(len(value.encode("utf-8")) * fraction))
    if isinstance(value, list):
        if not value:
            return []
        keep = int(len(value) * fraction)
        if fraction > 0 and keep == 0:
            keep = 1
        return [_scale_value(item, fraction) for item in value[:keep]]
    if isinstance(value, dict):
        if not value:
            return {}
        keep = int(len(value) * fraction)
        if fraction > 0 and keep == 0:
            keep = 1
        keys = list(value)[:keep]
        return {key: _scale_value(value[key], fraction) for key in keys}
    return value


def _truncate_text(value: str, max_bytes: int) -> str:
    max_bytes = max(0, min(max_bytes, _MAX_TEXT_BYTES))
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    marker = b"\n...[truncated]...\n"
    if max_bytes <= len(marker):
        return marker[:max_bytes].decode("utf-8", errors="ignore")
    head_size = (max_bytes - len(marker)) // 2
    tail_size = max_bytes - len(marker) - head_size
    head = raw[:head_size].decode("utf-8", errors="ignore")
    tail = raw[-tail_size:].decode("utf-8", errors="ignore") if tail_size else ""
    return head + marker.decode("ascii") + tail
