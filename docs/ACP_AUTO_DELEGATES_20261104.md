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

## Release acceptance, 2026-11-04

Production source commit: `95bd76b7cd7f9c44f9ede73928b5b38772a73970`.
Guarded rollout: `job_1793808973035_kQOQMDSa` (terminal success, exit 0,
29.81 seconds). The exact tested source manifest matched before and after
fast-forward. PID changed from `2991588` to `3034262`; service active,
ExecMainStatus=0, authenticated health HTTP 200, observation errors empty,
new error-priority journal lines 0. Rollback branch:
`rollback/agy-acp-auto-20261104` at `d092ae9`.

Focused tests with the proposed route: 174 passed. The complete candidate
gate `job_1793808678612_KUx3uHUJ`: **2168 passed, 1 skipped, 18 subtests
passed**, five existing environment warnings. 248 Python files compiled,
source bytes remained unchanged throughout, `git diff --check` passed.
Tested source manifest digest:
`ba397628345e9c052ee36aa5da875b99520cb0cd3ac6c29159067836a46be889`.
Unit-level account routing verifies AGY1/AGY3/AGY2 selection under artificial
slot occupancy, exact ACP identity, high-thinking selection, real linked
writer admission and explicit CLI override. No production queue manipulation
was used to fabricate account contention.

Live MCP `delegate_task` acceptance omitted `backend` in all three calls:

| Route | Delegate | Actual transport | Status | Source-grounded finding |
| --- | --- | --- | --- | --- |
| Unpinned automatic account | `9d568f68e5ac` → AGY1 | `acp` | succeeded | `steer`, `docs/SUPERVISOR_QUICKSTART.md:7` |
| Explicit AGY3 account, automatic transport | `9f635d2fcb28` | `acp` | succeeded | `steer`, `docs/SUPERVISOR_QUICKSTART.md:7` |
| Explicit AGY2 account, automatic transport | `0e52b9ccb995` | `acp` | succeeded | `steer`, `docs/SUPERVISOR_QUICKSTART.md:7` |

For every task the actual `delegate_status` reported `success=true`,
terminal succeeded, exit 0 and `outcome_uncertain=false`. Source-grounded
findings were independently verified against the repository. All three
terminal delegate results were consumed. This proves the public ACP transport
route for the three configured accounts, **not** comparative coding quality
or native writer success for every account. Previously accepted S3 writer
work verifies the ACP writer protocol independently.
