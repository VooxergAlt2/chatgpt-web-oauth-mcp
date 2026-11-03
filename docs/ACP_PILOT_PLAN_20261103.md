# ACP pilot: readiness plan and observed results

Date: 2026-11-03. Scope: the attested `antigravity3` account, explicit ACP
transport, the approved Opss repository and its dedicated linked worktrees.

## Starting point

Production implementation `1803710` and documentation `fbccbfe` already provide
durable exploration, native writers, scoped gateway reads, continuation, live
question replies, queued next-turn instructions and explicit abandonment.
The main checkout was clean and the service active when this pilot started.
The accepted regression baseline is 1756 passed, one optional SDK skip and five
existing warnings. Do not repeat previous gates merely to recover context.

The earlier worker-loss blocker is closed. The original
`final-native-20261103-f/final-acceptance-report.json` records PASS with exact
worker loss, public reconciliation, frozen abandoned reference, retained native
history, no replay and completed cleanup. Its checkpoint remains abandoned with
one recorded turn. The c/e/f composite acceptance is described accurately in
[ACP_BACKEND_QA.md](ACP_BACKEND_QA.md); earlier failed reports stay unchanged.

## Fast completion plan

1. Close the newly reproduced cancellation window during verified promotion.
   Check cancellation while staging and exporting, roll back catchable partial
   exports, and keep the async controller responsive without racing cleanup.
2. Resolve the ACP admission conflict between required explicit account pins
   and medium/high complexity. Preserve the same account/transport and the
   minimum model class; do not weaken CLI admission or continuation identity.
3. Add source provenance to new acceptance reports and independently review it.
   The three-file native attempt timed out before any edit; finish that helper
   through the normal reviewed implementation path. For actual pilot acceptance,
   assign one small useful change in one file with parent-owned offline checks.
4. Independently review all changes, run focused regressions and the required
   complete gate once on the assembled candidate, then fast-forward deployment
   with a rollback ref and authenticated health verification.
5. Verify the changed path through actual MCP, consume results, retain evidence
   and remove only owned disposable worktrees once their changes are integrated.

## New findings

| Finding | Evidence at the starting revision | Required completion |
| --- | --- | --- |
| Cancellation can arrive after the last worker check and still allow promotion | `acp_worker._finish_writer` calls synchronous `AcpWorkspace.promote` without a cancellation callback; disposable reproduction changed source despite a cancel file | Entry/staging/export/commit-boundary regressions and rollback/cleanup safety |
| Medium/high complexity conflicts with ACP's mandatory explicit account | Actual MCP submission returned `complexity_conflicts_with_harness` before launching a worker | ACP-specific same-account model selection with clear incompatible-override errors |
| Standalone acceptance JSON lacks the promised implementation revision | `acp_final_acceptance.exercise` creates its report without HEAD/tree/dirty metadata | New reports capture source identity before provider work; historical records remain unchanged |

## Observed practical tasks

| Task | Observation | Disposition |
| --- | --- | --- |
| `b0d11b3dc7e1`, admission review, medium thinking, 8 tools, 180-second lifetime | `timed_out` / `acp_timeout`; eight completed tool calls and no usable final manifest; source stayed clean | Explicitly abandoned through public `delegate_reconcile`; no replay, old reference frozen, result consumed |
| `9c521c928d74`, report-provenance implementation, high thinking, 24 tools, 360-second lifetime | `timed_out` / `acp_timeout`; 13 calls, no native edit/write steps, original source clean; native duration 259.03 seconds, total worker 262.376 seconds | Public abandonment freezes the reference, history retained, result consumed; no replay |
| `4dadba311864`, one-file readonly report wiring | Operator cancelled after discovering that an earlier MCP-timed-out worktree merge had not executed; the launched baseline lacked its required helper | Native cancellation confirmed, `outcome_uncertain=false`, source unchanged, result consumed; this is a preflight sequencing error, not a provider timeout |
| `d3a593e609be`, bounded readonly-report wiring on verified base `3174737` | Succeeded in 117.049 seconds; six native tools; one Python file, +12/-4 lines; parent-owned isolated check passed | Independently reviewed and committed as `8874900`; no scope/budget/commit violations; candidate fingerprint matched after verification |

These timeouts are failed practical samples, not accepted results. A
short synthetic read or writer smoke does not establish the duration or quality
of a larger investigation. Do not infer a success rate from this small sample.

Read-only native histories show fast successful file reads in both tasks. The
writer additionally made four empty scoped searches, tried an unavailable shell
command and read one nonexistent test file. The final gap without a completed
native step was about 160 seconds; the evidence does not distinguish provider
latency from model generation time. The worker intentionally reserved 90 seconds
of the 360-second lifetime for independent verification. Keep that safety reserve.
The code prompt now states that shell access is unavailable and asks for one
read of relevant ranges; this removes a known futile action, not all latency.

Concurrent unrelated Revit work advanced production to `9ac7708` and restarted
the service at 14:22 MSK. Its three changed files are disjoint from this slice.
The pilot branch fast-forwarded to that revision before its combined gate.

The useful native contribution's durable job is `job_1793705647428_fNgbLcPV`.
Its independent check printed `ACP_PILOT_LEGACY_PROVENANCE_PASS`; verified
promotion included only `scripts/acp_mcp_acceptance.py`. The source baseline
and the production runtime are separate evidence: the worktree was `3174737`,
while this pre-cutover task used the existing production ACP runtime on `9ac7708`.
The candidate contains the new cancellation fix; the live changed-path check
follows cutover after the full gate.

## Reviewed implementation and focused evidence

- Promotion cancellation: four stage regressions, persisted cancel, mailbox,
  SIGTERM and repeated coroutine cancellation after an actual file replacement.
  Workspace/worker/code-verification tests: 92 passed; the strengthened repeated
  cancellation case also passed independently. The async cleanup and commit point
  received a separate reviewer pass.
- ACP complexity: 73 admission/backend tests and seven existing CLI complexity
  tests passed. Pinned accounts, minimum High thinking and native resume identity
  are retained without changing the CLI route.
- Acceptance provenance: 11 provider-free tests passed on real SHA-1/SHA-256
  repositories, dirty/unborn/error/race cases, sanitized Git environment and
  report ownership. A separately authored real-Git parent check also passed.
- Changed Python files compile, and `git diff --check` is clean. The complete
  regression gate and production cutover remain the next acceptance steps.

Direct in-process cancellation of the verifier coroutine is outside the current
worker entry point: external SIGTERM/mailbox cancellation uses the cooperative
event. Any future embedding with `Task.cancel()` must also join the verifier
thread before removing its private copy; that path is not enabled by this pilot.

## Operating envelope for the pilot

- Pin `backend="acp"` and the independently verified `harness="antigravity3"`.
  CLI remains the default and shares the account's slots and quota gate.
- Give a concrete question or edit, exact relevant source files and observable
  acceptance criteria. Small scopes reduce unnecessary native exploration.
- Use a clean dedicated linked worktree for each writer and declare independent
  verification commands that work with installed system dependencies offline.
- Treat `checks_passed` as test evidence. The parent still reviews correctness,
  diff, scope and the intended behavior before integration.
- Continuation preserves the latest terminal conversation, owner, account,
  workspace, task kind and model/thinking. Queued steering applies next turn.
- Inspect and explicitly abandon uncertain sessions. Never automatically replay
  an unknown writer outcome or convert a failed report into success.
- Drain or cancel tasks before disabling the backend. With admission disabled,
  existing workers can drain, but live reply/steer/reconcile operations require
  the configured account/controller context.
- Hard termination during a multi-file export is not a global filesystem
  transaction. Inspect the dedicated source and checkpoint before new work.

## Pilot exit criteria

The candidate is ready for a bounded pilot when the three new findings above
are closed, affected tests and full regression gate pass, the service is healthy
on the recorded implementation, and at least one practical ACP contribution is
independently accepted. Broader account activation, arbitrary project rollout,
mid-turn steering, automatic retry and success-rate claims are separate work.
