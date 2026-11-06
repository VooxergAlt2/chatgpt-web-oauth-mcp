from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from . import session
from .acp_operations import reconcile as reconcile_acp, send_message as send_acp_message, supervise, draft_operation
from .pathing import resolve_cwd
from .delegate_packets import build_delegate_packet
from .delegate_harnesses import AntigravityHarness
from .session_continuation import (
    delegate_group_access_scope,
    foreign_owned_result_ids,
    observe_delegate_group_result,
    observe_delegate_result,
    project_delegate_attention,
    refresh_session_owned_work,
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
            "Run one bounded task through a configured agent harness such as Codex, Claude Code, "
            "Antigravity, or Pi. kind=explore is enforced read-only; CLI exploration also audits Git "
            "status and ACP uses a read-only sandbox. kind=code uses a worktree-scoped exclusive writer lane "
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
        work_role: Annotated[
            Literal["analysis", "review", "implementation", "proposal"] | None,
            Field(description="Scheduler role. explore accepts analysis (default), review, or explicit proposal. Proposal requires backend=acp and a pinned account: fresh tool-less contract-to-code output only, no repository scope, verification, resume, draft or batch; it consumes analysis capacity and is unverified. code uses implementation. Same-kind continuation inherits its role except proposal cannot resume."),
        ] = None,
        backend: Annotated[
            Literal["auto", "cli", "acp"],
            Field(description="Execution transport. auto (default) picks AGY1 -> AGY3 -> AGY2 under shared account quotas, then uses the selected account's identity-verified ACP. Non-AGY routes remain CLI. Specify backend='cli' to opt out; no silent ACP-to-CLI fallback. Explicit ACP requires a pinned harness. ACP code requires a clean dedicated linked Git worktree, exact files_in_scope, independent verification_commands, commit_mode=forbidden, and timeout>=30s. ACP remains separately sandboxed, owner-bound and verified; it does not support custom schemas or batch plans. Resuming a task preserves its original account and transport."),
        ] = "auto",
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
        verification_preflight_commands: Annotated[
            list[str] | None,
            Field(description=(
                "Optional ACP code environment probes, run by the server before model inference "
                "in a fresh read-only verification sandbox. At most 4 commands, each at most "
                "4096 UTF-8 bytes. A failed probe prevents inference and candidate promotion. "
                "These probes do not replace final verification_commands; no host fallback."
            )),
        ] = None,
        max_changed_files: Annotated[int | None, Field(description="Maximum changed files allowed by server-owned code verification.", ge=0)] = None,
        max_added_lines: Annotated[int | None, Field(description="Maximum added lines allowed by server-owned code verification.", ge=0)] = None,
        max_deleted_lines: Annotated[int | None, Field(description="Maximum deleted lines allowed by server-owned code verification.", ge=0)] = None,
        depends_on_group_ids: Annotated[
            list[str] | None,
            Field(description="Groups owned by this session. The reader or writer waits until every listed group finishes, including failed or cancelled groups. Inspect the results before relying on their evidence."),
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
                    "Use explicit harness='codex' for premium work. ACP keeps its required account pin: "
                    "medium/high defaults to Flash High and rejects lower model/thinking overrides. "
                    "ACP continuation preserves the source model/thinking; use low to continue a lower "
                    "thinking session or start a new task for High. CLI reasoning_effort remains independent."
                )
            ),
        ] = "low",
        max_tool_calls: Annotated[
            int | None,
            Field(
                description=(
                    "Optional tool-call attention threshold (1-64) for ACP tasks, read-only local "
                    "OpenAI or durable Antigravity exploration. Reaching it requests owner review "
                    "without terminating the delegate or invoking a premium model. AGY counts unique "
                    "stream tool steps excluding finish; ACP counts distinct reported tool IDs, which "
                    "do not attest to execution. Scoped low/medium AGY Python investigations default "
                    "to 24/48. The owner can inspect, acknowledge_attention to rearm, request an ACP "
                    "checkpoint to steer, or explicitly cancel. CLI code tasks do not use this threshold."
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
                    "ACP requires the latest terminal turn, the same kind, backend, logical owner, "
                    "account, workspace and model/thinking, with preserved or narrower scope; omitted "
                    "model/thinking inherit the source. Answer an active ACP question with "
                    "delegate_message(action='reply'). An uncertain interrupted ACP session requires "
                    "explicit delegate_reconcile(action='abandon') and a fresh task."
                )
            ),
        ] = None,
        draft_from_delegate_id: Annotated[
            str | None,
            Field(description="Terminal owned ACP code delegate whose saved checkpoint draft should seed this private writer. Requires draft_snapshot_id and identical account, thinking, workspace and scope. This is not provider-history resume; use resume_from_delegate_id separately for a known terminal session. Unknown outcomes require explicit abandonment first."),
        ] = None,
        draft_snapshot_id: Annotated[
            str | None,
            Field(description="Exact immutable snapshot_id from delegate_draft or checkpoint observations. Never inferred as latest. Restored bytes remain unverified and go through the current acceptance contract."),
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
                    "An explicit execution_timeout_seconds overrides the 8-hour preset. "
                    "ACP requires an explicit timeout within its 900-second limit when this is enabled."
                )
            ),
        ] = False,
        execution_timeout_seconds: Annotated[
            int | None,
            Field(description="Hard delegate subprocess lifetime in seconds. CLI permits up to 24 hours; ACP permits 15-900 seconds and code tasks require at least 30 seconds, within operator limits. Overrides long_running.", ge=1, le=86400),
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
                work_role=work_role,
                backend=backend,
                depends_on_group_ids=depends_on_group_ids,
                files_in_scope=files_in_scope,
                out_of_scope=out_of_scope,
                context_files=context_files,
                acceptance_criteria=acceptance_criteria,
                done_means=done_means,
                verification_commands=verification_commands,
                verification_preflight_commands=verification_preflight_commands,
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
                draft_from_delegate_id=draft_from_delegate_id,
                draft_snapshot_id=draft_snapshot_id,
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
                    "and may set work_role (analysis or review), task_id, model, reasoning_effort, files_in_scope, "
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
        if delegate_id and watch_seconds > 0 and result_access_scope(
            ctx, kind="delegate", result_id=delegate_id,
        ) == "owned_here" and ctx.checkpoint_store.owned_result(
            session.get_current_session_id(), kind="delegate", result_id=delegate_id,
        ) is not None:
            refreshed = refresh_session_owned_work(
                ctx, session_key=session.get_current_session_id(), kind="delegate",
                result_id=delegate_id, wait_seconds=watch_seconds,
            )
            result = refreshed["delegates"][0]
        else:
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
        return project_delegate_attention(ctx, result)

    @mcp.tool(
        name="delegate_packet",
        title="Delegate Evidence Packet",
        annotations=READ_ONLY_TOOL,
        description="Read bounded review/handoff evidence for an owned delegate. Preserves historical checks, compares ACP source freshness, separates agent claims. Does not run tests or accept work.",
    )
    def delegate_packet(
        delegate_id: Annotated[str, Field(description="Exact owned delegate identifier.")],
        purpose: Annotated[Literal["review", "handoff", "triage"], Field(description="Triage for compact status and usage, review for evidence, handoff for continuation.")] = "review",
    ) -> dict[str, object]:
        if result_access_scope(ctx, kind="delegate", result_id=delegate_id) != "owned_here":
            return {"success": False, "error": {"code": "delegate_packet_owner_required"}}
        source = ctx.registry.delegate_evidence_snapshot(delegate_id)
        if not isinstance(source, dict):
            return {"success": False, "error": {"code": "delegate_packet_source_unavailable"}}
        if source.get("completed") is not True:
            return {"success": False, "error": {"code": "delegate_packet_source_not_terminal"}}
        return {"success": True, "packet": build_delegate_packet(source, purpose=purpose)}

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
        name="delegate_message",
        title="Message ACP Delegate",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Control an owned ACP session. action=reply selects an exact option_id from the "
            "pending question in delegate_status.acp_control using request_id and turn_id; "
            "the original prompt continues without replay. action=steer requires a stable "
            "message_id and text, and queues instructions for the next explicit continuation "
            "with resume_from_delegate_id. Steering never changes an in-flight prompt. "
            "Wrong owners, stale questions, dead workers and frozen sessions are rejected."
        ),
    )
    def delegate_message(
        delegate_id: Annotated[str, Field(description="Exact owned ACP delegate identifier.")],
        action: Annotated[Literal["reply", "steer"], Field(description="Reply to a question or queue the next-turn instruction.")],
        turn_id: Annotated[str | None, Field(description="Question turn identifier, if supplied must equal delegate_id.")] = None,
        request_id: Annotated[str | None, Field(description="Exact pending question request_id; reply only.")] = None,
        option_id: Annotated[str | None, Field(description="Exact option id advertised by the question; reply only.")] = None,
        message_id: Annotated[str | None, Field(description="Stable ID for deduplicating a steering message.", max_length=128)] = None,
        text: Annotated[str | None, Field(description="Instruction for the next explicit continuation; steer only.", max_length=8192)] = None,
    ) -> dict[str, object]:
        denied = delegate_access_error(delegate_id=delegate_id)
        if denied is not None:
            return denied
        return send_acp_message(ctx.registry, delegate_id, session.get_current_session_id(),
                                action=action, turn_id=turn_id, request_id=request_id,
                                option_id=option_id, message_id=message_id, text=text)

    @mcp.tool(
        name="delegate_reconcile",
        title="Reconcile ACP Delegate",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Explicitly abandon an owned ACP session after a terminal failed/cancelled worker "
            "left an uncertain outcome. Inspect logs and the dedicated worktree first, then "
            "provide the review reason. Requires a dead worker and an available session lease. "
            "Preserves prior results/history, freezes the old reference, and requires a new "
            "task without resume_from_delegate_id. Does not claim success, replay or roll back edits."
        ),
    )
    def delegate_reconcile(
        delegate_id: Annotated[str, Field(description="Exact owned ACP delegate with an uncertain outcome.")],
        reason: Annotated[str, Field(description="Reason after reviewing the uncertain result and files.", min_length=1, max_length=1000)],
        action: Annotated[Literal["abandon"], Field(description="Freeze the session and preserve evidence.")] = "abandon",
    ) -> dict[str, object]:
        denied = delegate_access_error(delegate_id=delegate_id)
        if denied is not None:
            return denied
        return reconcile_acp(ctx.registry, delegate_id, session.get_current_session_id(),
                             action=action, reason=reason)

    @mcp.tool(
        name="delegate_supervisor",
        title="Supervise Delegate Checkpoint",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Inspect or control an owned delegate's cooperative supervisor checkpoint. "
            "acknowledge_attention works for every backend: after reviewing observed_calls, "
            "continue running and rearm after the configured threshold of additional calls. "
            "It requires command_id and observed_calls, does not pause or extend a deadline. "
            "CLI/local delegates do not support live steering. "
            "Cooperative checkpoint control is supported by scoped ACP tasks. status returns binding, revision, "
            "agent claims and server observations. request_checkpoint asks for the next safe "
            "gateway boundary; it is NOT an immediate pause. continue/steer release a waiting "
            "checkpoint into the SAME native turn, with free-text instruction for steer. "
            "review/escalate leave it waiting; stop cancels the execution; suspend cancels only "
            "after a checkpoint draft has been durably retained for explicit continuation. Mutations require "
            "a unique command_id; checkpoint mutations require exact expected_revision and decisions require the "
            "observed checkpoint_id. No account switching, permission expansion or code acceptance."
        ),
    )
    def delegate_supervisor(
        delegate_id: Annotated[str, Field(description="Exact owned delegate.")],
        action: Annotated[
            Literal["status", "acknowledge_attention", "request_checkpoint", "continue", "steer", "review", "escalate", "stop", "suspend"],
            Field(description="Inspect status, request a cooperative checkpoint, or decide its next action."),
        ] = "status",
        checkpoint_id: Annotated[str | None, Field(description="Exact checkpoint ID from current status; required for decisions.")] = None,
        expected_revision: Annotated[int | None, Field(description="Exact observed revision; required for checkpoint mutations.", ge=0)] = None,
        observed_calls: Annotated[int | None, Field(description="Tool-call count actually reviewed; required for acknowledge_attention. Rearms at this count plus the configured threshold.", ge=1)] = None,
        command_id: Annotated[str | None, Field(description="Unique idempotency key for the command. Repeat only with identical arguments.", max_length=128)] = None,
        instruction: Annotated[str, Field(description="Supervisor correction or review note. Required for steer; does not change scope or permissions.", max_length=8192)] = "",
    ) -> dict[str, object]:
        denied = delegate_access_error(delegate_id=delegate_id)
        if denied is not None:
            return denied
        if action == "acknowledge_attention":
            if result_access_scope(ctx, kind="delegate", result_id=delegate_id) != "owned_here":
                return {"success": False, "error": {"code": "result_not_owned", "message": "Attention can only be acknowledged by the owning logical session."}}
            session_key = session.get_current_session_id()
            refresh_session_owned_work(ctx, session_key=session_key, kind="delegate", result_id=delegate_id)
            try:
                receipt = ctx.checkpoint_store.acknowledge_delegate_attention(
                    session_key=session_key, delegate_id=delegate_id,
                    observed_calls=observed_calls, command_id=command_id,
                )
            except ValueError as exc:
                return {"success": False, "delegate_id": delegate_id,
                        "error": {"code": str(exc), "message": "Attention acknowledgement rejected; inspect delegate_status for the current count and ownership."}}
            state = receipt["state"]
            return {"success": True, "delegate_id": delegate_id,
                    "already_acknowledged": receipt["already_acknowledged"],
                    "continuation_state": state["continuation_state"],
                    "tool_call_attention": state.get("tool_call_attention"),
                    "terminal": bool(state.get("terminal"))}
        return supervise(ctx.registry, delegate_id, session.get_current_session_id(), action=action,
                         checkpoint_id=checkpoint_id, expected_revision=expected_revision,
                         command_id=command_id, instruction=instruction)

    @mcp.tool(
        name="delegate_draft",
        title="Inspect or Discard Delegate Draft",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=("List durable unaccepted checkpoint snapshots of an owned ACP writer, including after worker death. "
                     "Snapshots are private immutable deltas, not exported code. Restore explicitly with "
                     "delegate_task draft_from_delegate_id and draft_snapshot_id. action=discard requires "
                     "the exact snapshot_id and a terminal source worker; it deletes only that saved artifact, "
                     "not repository files or execution evidence."),
    )
    def delegate_draft(
        delegate_id: Annotated[str, Field(description="Owned source ACP delegate identifier.")],
        action: Annotated[Literal["status", "discard"], Field(description="List retained snapshots or explicitly discard one.")] = "status",
        snapshot_id: Annotated[str | None, Field(description="Exact saved snapshot ID, required for discard.")] = None,
    ) -> dict[str, object]:
        denied = delegate_access_error(delegate_id=delegate_id)
        if denied is not None:
            return denied
        return draft_operation(ctx.registry, delegate_id, session.get_current_session_id(),
                               action=action, snapshot_id=snapshot_id)

    @mcp.tool(
        name="delegate_harnesses",
        title="Delegate Harnesses",
        annotations=READ_ONLY_TOOL,
        description=(
            "List configured delegate harnesses, command availability, read-only support, defaults, "
            "and permission/sandbox modes. Availability does not prove provider authentication. "
            "Opt in with agy_account to return only success and account_model_catalog={account,status,models}: "
            "only that account's cached CLI catalog is queried; status=verified supplies model IDs, "
            "status=unavailable supplies models=null. Catalog membership does not prove inference rights."
        ),
    )
    def delegate_harnesses(
        agy_account: Annotated[
            Literal["antigravity", "antigravity2", "antigravity3"] | None,
            Field(description="Optional single AGY account for account_model_catalog; omitted means no catalog probe."),
        ] = None,
    ) -> dict[str, object]:
        if agy_account is not None:
            adapter = ctx.registry.harnesses.get(agy_account)
            models = adapter.model_catalog() if isinstance(adapter, AntigravityHarness) else None
            return {
                "success": True,
                "account_model_catalog": {
                    "account": agy_account,
                    "status": "verified" if models is not None else "unavailable",
                    "models": sorted(models) if models is not None else None,
                },
            }
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
        "delegate_packet": delegate_packet,
        "delegate_cancel": delegate_cancel,
        "delegate_message": delegate_message,
        "delegate_reconcile": delegate_reconcile,
        "delegate_supervisor": delegate_supervisor,
        "delegate_draft": delegate_draft,
        "delegate_harnesses": delegate_harnesses,
    }
