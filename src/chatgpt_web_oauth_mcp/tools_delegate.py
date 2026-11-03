from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from . import session
from .pathing import resolve_cwd
from .session_continuation import (
    delegate_group_access_scope,
    foreign_owned_result_ids,
    observe_delegate_group_result,
    observe_delegate_result,
    release_result_claim_capacity,
    reserve_result_claim_capacity,
    result_access_scope,
)
from .tool_context import OPEN_WORLD_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


LONG_RUNNING_DEFAULT_TIMEOUT_SECONDS = 8 * 60 * 60
MAX_DELEGATE_EXECUTION_TIMEOUT_SECONDS = 24 * 60 * 60


def _resolve_execution_timeout(
    *, long_running: bool, execution_timeout_seconds: int | None,
) -> int | None:
    """Only opt-in work gets a longer deadline; explicit limits always win."""
    if execution_timeout_seconds is not None:
        if not 1 <= execution_timeout_seconds <= MAX_DELEGATE_EXECUTION_TIMEOUT_SECONDS:
            raise ValueError("Delegate execution timeout must be between 1 and 86400 seconds.")
        return execution_timeout_seconds
    return LONG_RUNNING_DEFAULT_TIMEOUT_SECONDS if long_running else None


def register_delegate_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Expose the existing project-scoped delegate scheduler through MCP."""

    def record_delegate_resume(
        *,
        tool_name: str,
        result: dict[str, object],
        cwd: str | None = None,
        delegate_id: str | None = None,
        claim: bool = False,
        claim_reservation_id: str | None = None,
    ) -> Exception | None:
        try:
            observe_delegate_result(
                ctx,
                tool_name=tool_name,
                result=result,
                delegate_id=delegate_id,
                cwd=cwd,
                claim=claim,
                claim_reservation_id=claim_reservation_id,
            )
        except (OSError, TypeError, ValueError) as exc:
            result["resume_checkpoint_warning"] = (
                f"Automatic resume checkpoint could not be updated: {type(exc).__name__}: {exc}"
            )
            return exc
        return None

    def delegate_access_error(
        *,
        delegate_id: str | None = None,
        group_id: str | None = None,
    ) -> dict[str, object] | None:
        if delegate_id:
            scope = result_access_scope(
                ctx,
                kind="delegate",
                result_id=delegate_id,
            )
            subject = {"delegate_id": delegate_id}
        elif group_id:
            scope = delegate_group_access_scope(ctx, group_id=group_id)
            subject = {"group_id": group_id}
        else:
            return None
        if scope not in {"owned_elsewhere", "conflict"}:
            return None
        return {
            "success": False,
            "error": {
                "code": "result_owned_by_another_session",
                "message": (
                    "This delegate result belongs to a different logical MCP session and "
                    "cannot be read or mutated from the current session."
                ),
            },
            **subject,
        }

    @mcp.tool(
        name="delegate_task",
        title="Delegate Agent Task",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Run one bounded task through a configured CLI agent harness such as Codex, Claude Code, "
            "Antigravity, or Pi. kind=explore is enforced read-only and audited with before/after Git "
            "status when a repository is present; kind=code uses a worktree-scoped exclusive writer lane "
            "with repository-level concurrency cap. "
            "The returned wait window is not the process lifetime: use delegate_status when status is queued "
            "or running, then independently review files and verification results."
        ),
    )
    def delegate_task(
        task: Annotated[str | None, Field(description="Concrete bounded task for the delegate.")] = None,
        goal: Annotated[str | None, Field(description="Optional goal when task wording is not supplied.")] = None,
        task_id: Annotated[str | None, Field(description="Optional caller task identifier.")] = None,
        cwd: Annotated[
            str | None,
            Field(description="Project working directory. Defaults to the current session cwd."),
        ] = None,
        harness: Annotated[
            str | None,
            Field(
                description=(
                    "Optional explicit harness pin. Omit for normal delegation so server automatic "
                    "Gemini-first routing can use AGY1, AGY3, then AGY2. Pin harness='codex' "
                    "only to deliberately spend Codex quota; pin a specific AGY account "
                    "when required for conversation resume. An explicit local pin is diagnostic "
                    "and disables silent cloud fallback."
                )
            ),
        ] = None,
        kind: Annotated[
            Literal["explore", "code"],
            Field(description="explore is read-only; code may modify the project."),
        ] = "code",
        backend: Annotated[
            Literal["cli", "acp"],
            Field(description="Execution transport. cli remains default. acp is an opt-in durable read-only backend requiring an explicitly pinned, identity-verified AGY account and an operator-enabled workspace. Shares CLI account slots/quotas, never falls back to CLI. Continue a terminal ACP turn with resume_from_delegate_id; omitted model/thinking and scope inherit the source. No native writers, live steering, batch plans or custom schemas."),
        ] = "cli",
        files_in_scope: Annotated[list[str] | None, Field(description="Paths in task scope.")] = None,
        out_of_scope: Annotated[list[str] | None, Field(description="Paths/actions explicitly excluded.")] = None,
        context_files: Annotated[list[str] | None, Field(description="Important files the agent should read first.")] = None,
        acceptance_criteria: Annotated[list[str] | None, Field(description="Observable completion criteria.")] = None,
        done_means: Annotated[list[str] | None, Field(description="Required artifacts/evidence for completion.")] = None,
        verification_commands: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Server-owned acceptance checks for verified code tasks. "
                    "The coding agent must not run these exact declared commands; "
                    "the MCP server runs them after the agent exits."
                )
            ),
        ] = None,
        max_changed_files: Annotated[int | None, Field(description="Maximum changed files allowed by server-owned code verification.", ge=0)] = None,
        max_added_lines: Annotated[int | None, Field(description="Maximum added lines allowed by server-owned code verification.", ge=0)] = None,
        max_deleted_lines: Annotated[int | None, Field(description="Maximum deleted lines allowed by server-owned code verification.", ge=0)] = None,
        depends_on_group_ids: Annotated[
            list[str] | None,
            Field(description="Completed delegate groups that must become terminal before this task may run."),
        ] = None,
        commit_mode: Annotated[
            Literal["allowed", "required", "forbidden"],
            Field(description="Whether a code delegate may or must commit. Explore always forces forbidden."),
        ] = "forbidden",
        model: Annotated[str | None, Field(description="Optional harness-specific model override.")] = None,
        reasoning_effort: Annotated[
            str | None,
            Field(description="Optional reasoning effort override; unsupported values are rejected by the scheduler."),
        ] = None,
        complexity: Annotated[
            Literal["low", "medium", "high"],
            Field(
                description=(
                    "Gemini-first by default for code and exploration: low = Gemini Flash Medium, "
                    "medium/high = Gemini Flash High (high uses high reasoning). AGY1 -> AGY3 -> AGY2; "
                    "busy accounts queue and quota errors never silently escalate to Codex/Claude. "
                    "Use explicit harness='codex' for premium work. reasoning_effort remains independent."
                )
            ),
        ] = "low",
        max_tool_calls: Annotated[
            int | None,
            Field(
                description=(
                    "Optional hard tool-call ceiling (1-64) for read-only local OpenAI or "
                    "durable Antigravity exploration. AGY counts unique stream tool steps, "
                    "excluding finish; excess terminates the investigation as failed, "
                    "without premium fallback. Scoped low/medium AGY Python investigations "
                    "default to 24/48; unscoped/high tasks do not get an automatic ceiling. "
                    "This is not applied to code-writing delegates."
                ),
                ge=1,
                le=64,
            ),
        ] = None,
        output_schema: Annotated[
            dict[str, object] | None,
            Field(description="Optional JSON Schema for the agent final result. Claude/Antigravity use native schema enforcement."),
        ] = None,
        parse_structured_output: Annotated[
            bool,
            Field(description="Parse/normalize structured final output when supported."),
        ] = True,
        resume_from_delegate_id: Annotated[
            str | None,
            Field(
                description=(
                    "Terminal Antigravity delegate whose conversation should continue. "
                    "ACP requires the same backend, logical owner, account, workspace, model/thinking "
                    "and preserved scope; omitted model/thinking inherit the source. "
                    "A pending ACP question was cancelled: answer it in task as a new continuation, "
                    "not as a live permission grant."
                )
            ),
        ] = None,
        wait_seconds: Annotated[
            float,
            Field(description="How long this MCP call waits for completion; does not kill the delegate.", ge=0, le=300),
        ] = 30.0,
        long_running: Annotated[
            bool,
            Field(
                description=(
                    "Opt in to an 8-hour durable delegate lifetime for large tasks, without "
                    "extending the MCP response wait. The delegate continues after wait_seconds. "
                    "An explicit execution_timeout_seconds overrides the 8-hour preset."
                )
            ),
        ] = False,
        execution_timeout_seconds: Annotated[
            int | None,
            Field(description="Hard delegate subprocess lifetime in seconds (up to 24 hours). Overrides long_running.", ge=1, le=86400),
        ] = None,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        claim_reservation_id: str | None = None

        def reserve_before_submit() -> dict[str, object] | None:
            nonlocal claim_reservation_id
            try:
                claim_reservation_id = reserve_result_claim_capacity(ctx)
            except (OSError, TypeError, ValueError) as exc:
                return {
                    "success": False,
                    "error": {
                        "code": "session_ownership_admission_failed",
                        "message": (
                            "The delegate was not started because logical-session "
                            "ownership could not be reserved safely."
                        ),
                    },
                    "ownership_error": f"{type(exc).__name__}: {exc}",
                }
            return None

        def reserve_group_before_submit(slots: int) -> dict[str, object] | None:
            nonlocal claim_reservation_id
            try:
                claim_reservation_id = reserve_result_claim_capacity(
                    ctx,
                    slots=slots,
                )
            except (OSError, TypeError, ValueError) as exc:
                return {
                    "success": False,
                    "error": {
                        "code": "session_ownership_admission_failed",
                        "message": (
                            "The planner split was not started because logical-session "
                            "ownership could not be reserved safely for every shard."
                        ),
                    },
                    "ownership_error": f"{type(exc).__name__}: {exc}",
                }
            return None

        try:
            result = ctx.registry.run_delegate(
                task=task,
                goal=goal,
                task_id=task_id,
                cwd=resolved_cwd,
                wait_seconds=wait_seconds,
                execution_timeout_seconds=_resolve_execution_timeout(
                    long_running=long_running,
                    execution_timeout_seconds=execution_timeout_seconds,
                ),
                harness=harness,
                kind=kind,
                backend=backend,
                depends_on_group_ids=depends_on_group_ids,
                files_in_scope=files_in_scope,
                out_of_scope=out_of_scope,
                context_files=context_files,
                acceptance_criteria=acceptance_criteria,
                done_means=done_means,
                verification_commands=verification_commands,
                max_changed_files=max_changed_files,
                max_added_lines=max_added_lines,
                max_deleted_lines=max_deleted_lines,
                commit_mode=commit_mode,
                model=model,
                reasoning_effort=reasoning_effort,
                complexity=complexity,
                max_tool_calls=max_tool_calls,
                output_schema=output_schema,
                parse_structured_output=parse_structured_output,
                resume_from_delegate_id=resume_from_delegate_id,
                logical_session_id=session.get_current_session_id(),
                before_submit=reserve_before_submit,
                before_group_submit=reserve_group_before_submit,
            )
        except (OSError, TypeError, ValueError):
            try:
                release_result_claim_capacity(ctx, claim_reservation_id)
            except (OSError, TypeError, ValueError):
                pass
            raise
        group = result.get("group")
        group_id = str(result.get("group_id") or "").strip()
        if not group_id and isinstance(group, dict):
            group_id = str(group.get("group_id") or "").strip()
        if result.get("success") is not False and group_id:
            if claim_reservation_id is None:
                return result
            ownership_error: Exception | None = None
            try:
                observe_delegate_group_result(
                    ctx,
                    tool_name="delegate_task",
                    result=result,
                    cwd=str(resolved_cwd),
                    claim=True,
                    claim_reservation_id=claim_reservation_id,
                )
            except (OSError, TypeError, ValueError) as exc:
                result["resume_checkpoint_warning"] = (
                    "Automatic planner-group resume checkpoint could not be updated: "
                    f"{type(exc).__name__}: {exc}"
                )
                ownership_error = exc
            if ownership_error is not None:
                try:
                    release_result_claim_capacity(ctx, claim_reservation_id)
                except (OSError, TypeError, ValueError):
                    pass
                cleanup = ctx.registry.delegate_cancel(
                    delegate_id=None,
                    group_id=group_id,
                    reason="ownership_cleanup",
                )
                return {
                    "success": False,
                    "error": {
                        "code": "session_ownership_persistence_failed",
                        "message": (
                            "The planner split started, but logical-session ownership "
                            "could not be persisted. The server attempted to cancel the group."
                        ),
                    },
                    "group_id": group_id,
                    "ownership_error": (
                        f"{type(ownership_error).__name__}: {ownership_error}"
                    ),
                    "cleanup": cleanup,
                }
            return result

        delegate_id = str(result.get("delegate_id") or "").strip()
        if not delegate_id:
            snapshot = result.get("delegate")
            if isinstance(snapshot, dict):
                delegate_id = str(snapshot.get("delegate_id") or "").strip()
        if result.get("success") is False or not delegate_id:
            try:
                release_result_claim_capacity(ctx, claim_reservation_id)
            except (OSError, TypeError, ValueError) as exc:
                result["resume_checkpoint_warning"] = (
                    "Automatic ownership reservation cleanup failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            return result
        if claim_reservation_id is None:
            return result
        ownership_error = record_delegate_resume(
            tool_name="delegate_task",
            result=result,
            cwd=str(resolved_cwd),
            claim=True,
            claim_reservation_id=claim_reservation_id,
        )
        if ownership_error is not None and delegate_id:
            try:
                release_result_claim_capacity(ctx, claim_reservation_id)
            except (OSError, TypeError, ValueError):
                pass
            cleanup = ctx.registry.delegate_cancel(
                delegate_id=delegate_id,
                group_id=None,
                reason="ownership_cleanup",
            )
            return {
                "success": False,
                "error": {
                    "code": "session_ownership_persistence_failed",
                    "message": (
                        "The delegate started, but logical-session ownership could not "
                        "be persisted. The server attempted to cancel the unowned delegate."
                    ),
                },
                "delegate_id": delegate_id,
                "ownership_error": f"{type(ownership_error).__name__}: {ownership_error}",
                "cleanup": cleanup,
            }
        return result

    @mcp.tool(
        name="delegate_batch",
        title="Delegate Read-Only Batch",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Run several independent read-only exploration tasks through one configured agent harness. "
            "Pin harness='antigravity' for 3-4 parallel agents on ONE AGY account; "
            "each child may set a distinct model, reasoning_effort and task_id. "
            "Children share a project-scoped reader group and commits are always forbidden. Use this for "
            "parallel audits, mapping, evidence collection, or independent review, not implementation."
        ),
    )
    def delegate_batch(
        tasks: Annotated[
            list[dict[str, object]],
            Field(
                description=(
                    "Independent read-only child specifications. Each item takes task or goal, "
                    "and may set task_id (role label), model, reasoning_effort, files_in_scope, "
                    "context_files, acceptance_criteria and output_schema. "
                    "All children use the single batch harness/account."
                ),
                min_length=1,
                max_length=32,
            ),
        ],
        cwd: Annotated[
            str | None,
            Field(description="Project working directory. Defaults to the current session cwd."),
        ] = None,
        harness: Annotated[str | None, Field(description="Optional explicit harness pin; omit to use server routing policy.")] = None,
        max_concurrency: Annotated[
            int | None,
            Field(description="Maximum concurrent children in this group.", ge=1, le=8),
        ] = None,
        wait_seconds: Annotated[
            float,
            Field(description="How long this MCP call waits for the group; does not kill children.", ge=0, le=300),
        ] = 30.0,
        long_running: Annotated[
            bool,
            Field(
                description=(
                    "Use an 8-hour lifetime per batch child instead of the short explore default. "
                    "Children stay alive when the MCP wait window expires. An explicit "
                    "execution_timeout_seconds overrides this preset."
                )
            ),
        ] = False,
        execution_timeout_seconds: Annotated[
            int | None,
            Field(description="Hard lifetime for each child subprocess in seconds (up to 24 hours).", ge=1, le=86400),
        ] = None,
        model: Annotated[str | None, Field(description="Optional default model override for children.")] = None,
        reasoning_effort: Annotated[
            str | None,
            Field(description="Optional default reasoning effort for children."),
        ] = None,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        try:
            claim_reservation_id = reserve_result_claim_capacity(
                ctx,
                slots=len(tasks),
            )
        except (OSError, TypeError, ValueError) as exc:
            return {
                "success": False,
                "error": {
                    "code": "session_ownership_admission_failed",
                    "message": (
                        "The delegate batch was not started because logical-session "
                        "ownership could not be reserved safely for every child."
                    ),
                },
                "ownership_error": f"{type(exc).__name__}: {exc}",
            }
        result = ctx.registry.run_delegate_batch(
            tasks=tasks,
            cwd=resolved_cwd,
            harness=harness,
            max_concurrency=max_concurrency,
            wait_seconds=wait_seconds,
            execution_timeout_seconds=_resolve_execution_timeout(
                long_running=long_running,
                execution_timeout_seconds=execution_timeout_seconds,
            ),
            model=model,
            reasoning_effort=reasoning_effort,
            logical_session_id=session.get_current_session_id(),
        )
        group = result.get("group")
        group_id = str(result.get("group_id") or "").strip()
        if not group_id and isinstance(group, dict):
            group_id = str(group.get("group_id") or "").strip()
        if result.get("success") is False or not group_id:
            try:
                release_result_claim_capacity(ctx, claim_reservation_id)
            except (OSError, TypeError, ValueError) as exc:
                result["resume_checkpoint_warning"] = (
                    "Automatic ownership reservation cleanup failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            return result
        ownership_error: Exception | None = None
        try:
            observe_delegate_group_result(
                ctx,
                tool_name="delegate_batch",
                result=result,
                cwd=str(resolved_cwd),
                claim=True,
                claim_reservation_id=claim_reservation_id,
            )
        except (OSError, TypeError, ValueError) as exc:
            result["resume_checkpoint_warning"] = (
                f"Automatic resume checkpoint could not be updated: {type(exc).__name__}: {exc}"
            )
            ownership_error = exc
        if ownership_error is not None and group_id:
            try:
                release_result_claim_capacity(ctx, claim_reservation_id)
            except (OSError, TypeError, ValueError):
                pass
            cleanup = ctx.registry.delegate_cancel(
                delegate_id=None,
                group_id=group_id,
                reason="ownership_cleanup",
            )
            return {
                "success": False,
                "error": {
                    "code": "session_ownership_persistence_failed",
                    "message": (
                        "The delegate batch started, but logical-session ownership could "
                        "not be persisted. The server attempted to cancel the unowned group."
                    ),
                },
                "group_id": group_id,
                "ownership_error": f"{type(ownership_error).__name__}: {ownership_error}",
                "cleanup": cleanup,
            }
        return result

    @mcp.tool(
        name="delegate_status",
        title="Delegate Status",
        annotations=READ_ONLY_TOOL,
        description=(
            "Inspect one delegate, one group, one project, or global active/recent delegate state. "
            "watch_seconds performs bounded long polling for lifecycle/activity change. A terminal result "
            "is not self-validating: inspect logs/diff/tests before accepting agent work."
        ),
    )
    def delegate_status(
        delegate_id: Annotated[str | None, Field(description="Exact delegate identifier.")] = None,
        group_id: Annotated[str | None, Field(description="Exact exploration group identifier.")] = None,
        project_cwd: Annotated[
            str | None,
            Field(description="Filter by project identity resolved from this cwd."),
        ] = None,
        limit: Annotated[int, Field(description="Maximum recent entries.", ge=1, le=20)] = 10,
        offset: Annotated[int, Field(description="Recent-entry offset.", ge=0)] = 0,
        watch_seconds: Annotated[
            float,
            Field(description="Bounded long-poll window for state changes.", ge=0, le=300),
        ] = 0.0,
        poll_seconds: Annotated[
            float,
            Field(description="Polling cadence within watch_seconds.", ge=0.1, le=60),
        ] = 5.0,
    ) -> dict[str, object]:
        denied = delegate_access_error(
            delegate_id=delegate_id,
            group_id=group_id,
        )
        if denied is not None:
            return denied
        result = ctx.registry.delegate_status(
            delegate_id=delegate_id,
            group_id=group_id,
            project_cwd=project_cwd,
            limit=limit,
            offset=offset,
            watch_seconds=watch_seconds,
            poll_seconds=poll_seconds,
            max_tokens=ctx.tool_output_token_budget,
            exclude_delegate_ids=foreign_owned_result_ids(ctx, kind="delegate"),
        )
        if delegate_id:
            record_delegate_resume(
                tool_name="delegate_status",
                result=result,
                cwd=project_cwd,
                delegate_id=delegate_id,
            )
        elif group_id:
            try:
                observe_delegate_group_result(
                    ctx,
                    tool_name="delegate_status",
                    result=result,
                    cwd=project_cwd,
                )
            except (OSError, TypeError, ValueError) as exc:
                result["resume_checkpoint_warning"] = (
                    f"Automatic resume checkpoint could not be updated: {type(exc).__name__}: {exc}"
                )
        return result

    @mcp.tool(
        name="delegate_cancel",
        title="Cancel Delegate",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Cancel exactly one delegate or one exploration group. Queued work can be cancelled "
            "directly; terminating already-running work requires force_running=true so useful in-flight "
            "results are not discarded by accident. Cancellation does not roll back filesystem changes."
        ),
    )
    def delegate_cancel(
        delegate_id: Annotated[str | None, Field(description="Exact delegate identifier.")] = None,
        group_id: Annotated[str | None, Field(description="Exact group identifier.")] = None,
        rationale: Annotated[
            str | None,
            Field(
                description=(
                    "Optional short human-readable reason for the intentional cancellation. "
                    "Persisted with the terminal delegate metadata for operator diagnostics."
                ),
                max_length=500,
            ),
        ] = None,
        force_running: Annotated[
            bool,
            Field(
                description=(
                    "Set true only after inspecting the active delegate/group and intentionally choosing "
                    "to terminate already-running work."
                )
            ),
        ] = False,
    ) -> dict[str, object]:
        denied = delegate_access_error(
            delegate_id=delegate_id,
            group_id=group_id,
        )
        if denied is not None:
            return denied
        result = ctx.registry.delegate_cancel(
            delegate_id=delegate_id,
            group_id=group_id,
            rationale=rationale,
            force_running=force_running,
        )
        if delegate_id:
            record_delegate_resume(
                tool_name="delegate_cancel",
                result=result,
                delegate_id=delegate_id,
            )
        elif group_id:
            try:
                observe_delegate_group_result(
                    ctx,
                    tool_name="delegate_cancel",
                    result=result,
                )
            except (OSError, TypeError, ValueError) as exc:
                result["resume_checkpoint_warning"] = (
                    f"Automatic resume checkpoint could not be updated: {type(exc).__name__}: {exc}"
                )
        return result

    @mcp.tool(
        name="delegate_harnesses",
        title="Delegate Harnesses",
        annotations=READ_ONLY_TOOL,
        description=(
            "List configured delegate harnesses, command availability, read-only support, defaults, "
            "and permission/sandbox modes. Availability does not prove provider authentication."
        ),
    )
    def delegate_harnesses() -> dict[str, object]:
        return {
            "success": True,
            "default_harness": ctx.delegate_default_harness,
            "harnesses": ctx.registry.harness_info(),
            "routing": ctx.registry.routing_guidance(),
            "runtime": ctx.registry.runtime_info(),
        }

    return {
        "delegate_task": delegate_task,
        "delegate_batch": delegate_batch,
        "delegate_status": delegate_status,
        "delegate_cancel": delegate_cancel,
        "delegate_harnesses": delegate_harnesses,
    }
