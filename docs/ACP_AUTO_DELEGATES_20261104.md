# ACP-first delegating (AGY1 / AGY3 / AGY2)

## Contract

The public `delegate_task` input defaults to `backend="auto"`. The existing
capacity/quota router selects AGY1, then AGY3, then AGY2. For any admitted
Gemini Antigravity account the selected execution transport is ACP, using
that **same** identity-attested account and quota pool. There is no automatic
provider switch, no extra slot and no CLI retry after an ACP failure.

`backend="cli"` is an explicit opt-out and retains established CLI behavior.
`backend="acp"` is an explicit pinned-account request. Non-AGY routes
(such as deliberately pinned Codex) keep their existing CLI transport.
The `delegate_batch` flow is still CLI: native ACP does not implement batch
plans, and it is not implicitly reinterpreted as several independent ACP tasks.
ACP selection is reported in `delegate_status.execution_backend` and
`delegate_harnesses.execution_backends.acp.automatic_routing`.

The default is safest for `kind="explore"`: read-only tasks run in the
bounded ACP workspace with the same owner and checkpoint rules. The agent
must still provide evidence and pass independent acceptance.

For `kind="code"`, the default ACP path **requires**:

- a clean dedicated linked Git worktree in an approved repository;
- exact, explicit `files_in_scope`;
- independent `verification_commands` run only by the server;
- `commit_mode="forbidden"` and an ACP deadline within 30–900 seconds;
- independent review/commit of the accepted scoped diff by the parent.

An incomplete writer contract returns an explicit error, **not** a CLI
fallback. Callers can deliberately choose CLI for tasks not yet structured
for the ACP isolated writer. Existing code delegates running before cutover
keep their original transport.

Automatic account routing does not change native ACP limitations: custom
output schema and batch plans unsupported; 64 native calls per automatic
turn; bounded turn lifetime; cooperative supervisor checkpoints; immutable
draft snapshots for explicit continuation; no automatic cross-account
conversation migration. An ACP eligibility/location error is **not** treated
as proof of recovery by the existing AGY CLI watchdog. Separate ACP
watchdog/probe integration remains an operational follow-up.

## Release acceptance

Target scenarios: bounded automatic exploration on AGY1; capacity selection
of AGY3 then AGY2; preserved explicit CLI and fail-closed ACP code admission;
authenticated MCP health and independent representative real ACP execution.
Document exact tests, release SHA, runtime PID, rollback and real results
after release, not before.
