from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from . import session
from .pathing import resolve_cwd
from .tool_context import OPEN_WORLD_WRITE_TOOL, READ_ONLY_TOOL, ToolContext


def register_delegate_tools(mcp: Any, ctx: ToolContext) -> dict[str, object]:
    """Expose the existing project-scoped delegate scheduler through MCP."""

    def record_delegate_resume(
        *,
        tool_name: str,
        result: dict[str, object],
        cwd: str | None = None,
        delegate_id: str | None = None,
    ) -> None:
        session_key = session.get_current_session_id()
        snapshot = result.get("delegate")
        if not isinstance(snapshot, dict):
            snapshot = result
        resolved_delegate_id = (
            delegate_id or str(snapshot.get("delegate_id") or "")
        ).strip()
        if not session_key or not resolved_delegate_id:
            return
        status = str(snapshot.get("status") or "").strip().lower() or "unknown"
        terminal = bool(snapshot.get("completed")) or status in {
            "succeeded",
            "failed",
            "cancelled",
            "timed_out",
        }
        next_action = (
            "Consume and independently verify the terminal delegate result, then continue the current conversation plan."
            if terminal
            else "Poll or inspect the owned delegate until terminal, then continue the current conversation plan."
        )
        delegate_state = {
            "status": status,
            "completed": bool(snapshot.get("completed")),
            "success": snapshot.get("success"),
            "harness": snapshot.get("harness"),
            "activity_state": snapshot.get("activity_state"),
            "terminal": terminal,
        }
        resolved_cwd = str(snapshot.get("cwd") or cwd or "")
        try:
            ctx.checkpoint_store.record_runtime(
                session_key=session_key,
                last_tool=tool_name,
                cwd=resolved_cwd or None,
                delegates={resolved_delegate_id: delegate_state},
                next_action=next_action,
            )
        except (OSError, TypeError, ValueError) as exc:
            result["resume_checkpoint_warning"] = (
                f"Automatic resume checkpoint could not be updated: {type(exc).__name__}: {exc}"
            )

    @mcp.tool(
        name="delegate_task",
        title="Delegate Agent Task",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Run one bounded task through a configured CLI agent harness such as Codex, Claude Code, "
            "Antigravity, or Pi. kind=explore is enforced read-only and audited with before/after Git "
            "status when a repository is present; kind=code uses the project-scoped exclusive writer lane. "
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
            Field(description="Configured harness name, e.g. codex, claude, antigravity, or pi."),
        ] = None,
        kind: Annotated[
            Literal["explore", "code"],
            Field(description="explore is read-only; code may modify the project."),
        ] = "code",
        files_in_scope: Annotated[list[str] | None, Field(description="Paths in task scope.")] = None,
        out_of_scope: Annotated[list[str] | None, Field(description="Paths/actions explicitly excluded.")] = None,
        context_files: Annotated[list[str] | None, Field(description="Important files the agent should read first.")] = None,
        acceptance_criteria: Annotated[list[str] | None, Field(description="Observable completion criteria.")] = None,
        done_means: Annotated[list[str] | None, Field(description="Required artifacts/evidence for completion.")] = None,
        verification_commands: Annotated[list[str] | None, Field(description="Checks the coding agent should run.")] = None,
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
        output_schema: Annotated[
            dict[str, object] | None,
            Field(description="Optional JSON Schema for the agent final result. Claude/Antigravity use native schema enforcement."),
        ] = None,
        parse_structured_output: Annotated[
            bool,
            Field(description="Parse/normalize structured final output when supported."),
        ] = True,
        wait_seconds: Annotated[
            float,
            Field(description="How long this MCP call waits for completion; does not kill the delegate.", ge=0, le=300),
        ] = 30.0,
        execution_timeout_seconds: Annotated[
            int | None,
            Field(description="Hard delegate subprocess lifetime in seconds.", ge=1, le=14400),
        ] = None,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        result = ctx.registry.run_delegate(
            task=task,
            goal=goal,
            task_id=task_id,
            cwd=resolved_cwd,
            wait_seconds=wait_seconds,
            execution_timeout_seconds=execution_timeout_seconds,
            harness=harness,
            kind=kind,
            depends_on_group_ids=depends_on_group_ids,
            files_in_scope=files_in_scope,
            out_of_scope=out_of_scope,
            context_files=context_files,
            acceptance_criteria=acceptance_criteria,
            done_means=done_means,
            verification_commands=verification_commands,
            commit_mode=commit_mode,
            model=model,
            reasoning_effort=reasoning_effort,
            output_schema=output_schema,
            parse_structured_output=parse_structured_output,
        )
        record_delegate_resume(
            tool_name="delegate_task",
            result=result,
            cwd=str(resolved_cwd),
        )
        return result

    @mcp.tool(
        name="delegate_batch",
        title="Delegate Read-Only Batch",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Run several independent read-only exploration tasks through one configured agent harness. "
            "Children share a project-scoped reader group and commits are always forbidden. Use this for "
            "parallel audits, mapping, evidence collection, or independent review, not implementation."
        ),
    )
    def delegate_batch(
        tasks: Annotated[
            list[dict[str, object]],
            Field(description="Independent exploration task specifications.", min_length=1, max_length=32),
        ],
        cwd: Annotated[
            str | None,
            Field(description="Project working directory. Defaults to the current session cwd."),
        ] = None,
        harness: Annotated[str | None, Field(description="Configured harness name.")] = None,
        max_concurrency: Annotated[
            int | None,
            Field(description="Maximum concurrent children in this group.", ge=1, le=8),
        ] = None,
        wait_seconds: Annotated[
            float,
            Field(description="How long this MCP call waits for the group; does not kill children.", ge=0, le=300),
        ] = 30.0,
        execution_timeout_seconds: Annotated[
            int | None,
            Field(description="Hard lifetime for each child subprocess in seconds.", ge=1, le=14400),
        ] = None,
        model: Annotated[str | None, Field(description="Optional default model override for children.")] = None,
        reasoning_effort: Annotated[
            str | None,
            Field(description="Optional default reasoning effort for children."),
        ] = None,
    ) -> dict[str, object]:
        resolved_cwd = resolve_cwd(cwd, ctx.workspace_root)
        return ctx.registry.run_delegate_batch(
            tasks=tasks,
            cwd=resolved_cwd,
            harness=harness,
            max_concurrency=max_concurrency,
            wait_seconds=wait_seconds,
            execution_timeout_seconds=execution_timeout_seconds,
            model=model,
            reasoning_effort=reasoning_effort,
        )

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
        result = ctx.registry.delegate_status(
            delegate_id=delegate_id,
            group_id=group_id,
            project_cwd=project_cwd,
            limit=limit,
            offset=offset,
            watch_seconds=watch_seconds,
            poll_seconds=poll_seconds,
            max_tokens=ctx.tool_output_token_budget,
        )
        if delegate_id:
            record_delegate_resume(
                tool_name="delegate_status",
                result=result,
                cwd=project_cwd,
                delegate_id=delegate_id,
            )
        return result

    @mcp.tool(
        name="delegate_cancel",
        title="Cancel Delegate",
        annotations=OPEN_WORLD_WRITE_TOOL,
        description=(
            "Cancel exactly one delegate or one exploration group. Cancellation terminates registered "
            "agent subprocesses but does not roll back filesystem changes; inspect Git state afterwards."
        ),
    )
    def delegate_cancel(
        delegate_id: Annotated[str | None, Field(description="Exact delegate identifier.")] = None,
        group_id: Annotated[str | None, Field(description="Exact group identifier.")] = None,
    ) -> dict[str, object]:
        return ctx.registry.delegate_cancel(delegate_id=delegate_id, group_id=group_id)

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
        }

    return {
        "delegate_task": delegate_task,
        "delegate_batch": delegate_batch,
        "delegate_status": delegate_status,
        "delegate_cancel": delegate_cancel,
        "delegate_harnesses": delegate_harnesses,
    }
