# ACP follow-up: truthful session recovery — 2026-11-04

## Deployed result

Runtime commit: `ec0fa35984dfc603367c4fc013288e094d22186f`.

The parent implemented and reviewed this change after the two native writer
attempts below failed without exporting code. The change passed the complete
test gate, was fast-forwarded from `3691258`, and passed actual MCP acceptance.
The service changed from PID `1985958` to `2536683`, is active/running
with ExecMainStatus 0, and returned authenticated health HTTP 200 from that same
PID. There were no observation errors or new error-priority journal entries.

This follows the [original operational pilot](ACP_PILOT_RELEASE_20261104.md).
It does not count either failed native attempt as an accepted implementation.

## Fixed behavior

A live reproduction had no pending results and no active owned work, but
`session_resume.next_action` still instructed the controller to consume an old
terminal result. Two related branches could also use a historical action while
another task was active, or treat an already consumed restart interruption as
fresh work.

The canonical pure `resume_guidance(checkpoint)` in
`src/chatgpt_web_oauth_mcp/session_checkpoints.py` now derives the inbox and
recommendation from one checkpoint snapshot. The existing inbox projection is
shared with `SessionCheckpointStore.pending_results`; ordering and explicit
result-consumption semantics are retained.

| Current state | Next action |
| --- | --- |
| Unconsumed terminal results | Consume and verify all pending `(kind, id)` references |
| Pending interrupted delegate among other results | Include every pending result and require partial-work review before deciding whether replacement is necessary |
| No pending results, owned work active | Poll/await the currently owned active work |
| No pending or active work, semantic plan saved | Return that saved plan verbatim |
| No pending/active work or saved semantic plan | Return the canonical `INVOKE_NEXT_TOOL_OR_RETURN_CHECKPOINT` command |

`tools_core.session_resume` and successful/idempotent `mark_result_consumed`
use the shared calculation. The top-level action and returned
`runtime.next_action` / `checkpoint.runtime.next_action` agree. This is a response
projection: historical observation metadata is not written back over newer state.
The semantic `checkpoint.next_action` remains separate. The `jobs` and
`delegates` arrays retain individual refresh observations and their diagnostics;
current obligations are defined by the coherent checkpoint/inbox/guidance snapshot.

There is no automatic result consumption, task replay, ownership transfer,
provider change or altered result-access guard.

## Verification

| Check | Evidence |
| --- | --- |
| Syntax and independent stdlib witness | PASS for stale idle, consumed interruption, active work, mixed pending, separate job/delegate identities and input immutability |
| New regressions | 13 cases in `tests/test_resume_guidance.py`, including registered tool transitions and controlled state changes during refresh/consumption |
| Final focused run | 36 passed, 42 deselected in 6.07 s |
| Independent code review | No blockers; verified snapshot consistency, complete pending priority and response projections |
| First full gate | `job_1793787467142_TDsZySQ-`: 1 failed, 2113 passed, 1 skipped; failure identified an existing canonical idle-command contract |
| Corrective change | Shared `CONTINUE_EXECUTION` between recovery and `execution_state`; the existing HTTP regression test was preserved |
| Final full gate | `job_1793787785153_UHTakXYc`: 2114 passed, 1 skipped, 5 existing warnings and 18 subtests in 219.99 s |
| Source stability | 242 Python files compiled; checked source remained unchanged throughout the final gate; `git diff --check` passed |
| First rollout preflight | `job_1793788063236_goZRhkpl` blocked on another nonterminal durable job, before service stop or Git changes |
| Accepted rollout | `job_1793788197208_iAc9-Dpk`, exit 0 in 30.763 s; global health/full registry guards, tested hashes and clean fast-forward passed |
| Live pending state | Actual `session_resume` named the completed rollout job; all three action projections agreed |
| Live consumption | `mark_result_consumed` returned pending=0 and the canonical continue command; a repeat returned `already_consumed=true` |
| Live resumed idle | pending=0, active=0, `resume_state=runtime_checkpoint`; all three action projections returned the canonical continue command |

The gate used the existing pinned QA interpreter/helper for the optional ACP
environment checks. One optional test remained skipped. The warnings are the
existing Starlette/AnyIO deprecation and four multiprocessing fork warnings.

Rollback ref: `rollback/acp-session-resume-20261104` → `3691258`.
The accepted rollout and all jobs/delegates from this development cycle were
explicitly consumed. A transient MCP error while the service restarted did not
interrupt the durable rollout; its terminal result was retrieved normally.

## Native Flash High observations

All native attempts stayed on the explicitly pinned `antigravity3` account and
Flash High/high. No premium-model fallback occurred.

| Attempt | Observed result |
| --- | --- |
| Earlier `22bf9087a45f`, result recovered through normal API access this turn | Failed `acp_tool_budget_exceeded`: 13 calls against 12, 127.829 s; no accepted export; result consumed |
| `162e70378d7e` | Timed out after 442.410 s with 30 calls. A 600 s total budget reserved 150 s for verification/export and allocated 443.511 s to the native session including initialization/cancel grace; the prompt phase lasted 433.653 s. Outcome was uncertain; reviewed and explicitly abandoned. No export |
| `92aaa79fca65` | Failed `acp_tool_budget_exceeded`: 49 calls against 48 after 520.157 s. Total budget 900 s, native-session allocation 668.631 s, verification reserve 225 s. Known failure, no export |

The provider-reported context observations were 152148 and 188608 tokens for the
two new attempts. Narrower instructions and supplied excerpts did not by
themselves keep observed context small. These are qualified observations, not
a cost calculation or a controlled Flash-versus-Sol comparison. Neither attempt
reached independent code acceptance, so they cannot establish implementation quality.

## Working policy and next development slice

Choose one coherent behavior and discover the exact files it needs. File count
alone is not a proxy for reasoning difficulty. Use explicit High for substantive
implementation, a separate focused regression file where appropriate, and a
parent-owned executable acceptance contract.

ACP currently supplies 24 calls for omitted low-complexity budgets and 48 for
omitted medium/high budgets; its hard ceiling is 64. Omitting the argument does
not remove the cap. For a substantive High writer, explicitly select 64 calls
and up to the supported 900-second deadline when the scope warrants it.
Account for the verification reserve and cancel grace when judging native time.
These are an operating envelope, not proof of a general completion rate.
Review an exhausted attempt before issuing any fresh task; uncertain sessions
still require explicit abandonment.

Priorities grounded in the observed failures:

1. Make recovery recommendations respect the actual backend, pinned account and
   uncertainty state. `routing/replan.py` currently labels timeouts as automatic
   `switch_provider`, even though pinned ACP did not switch providers here.
2. Expose bounded tool-use diagnostics by phase/category and source-read size so
   excessive navigation can be located from facts rather than total counts.
3. Design an explicit checkpoint/continuation path before budget exhaustion,
   including how to retain reviewable partial work without accepting or
   automatically promoting an unverified candidate.
4. Exercise the next coherent implementation task within this measured envelope
   and record its independently verified result. Preserve failed receipts instead
   of reclassifying them as successes.

The present patch fixes session recovery. The budget/default and recovery-policy
items above are documented follow-up work, not silently claimed as implemented.
