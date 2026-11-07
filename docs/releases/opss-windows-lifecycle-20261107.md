# Windows operation lifecycle pilot — 2026-11-07

## Scope and release status

This change separates Windows execution from the lifetime of the requesting MCP
connection. It is a paired Opss coordinator / Windows gateway change. It does not
change Revit API behavior or authorize a production service cutover.

The current release is a development candidate. Native Windows acceptance and
shared-service activation remain separate, explicit checkpoints. A successful
Linux fake-backend test must never be reported as native Windows acceptance.

## Migration contract

1. Preserve the existing workspace ledger and the local Windows journal.
2. Do not manufacture durable metadata for legacy inflight/draining fences:
   their original Windows operation identity and outcome cannot be inferred.
3. Inspect and handle any pre-existing unknown operation through its existing
   owner/operator recovery procedure. A service restart alone does not prove
   the Windows action stopped.
4. Stage the gateway script and adjacent journal module from the same revision.
   Choose one authoritative local state directory per active gateway process.
   A second process using that directory must fail its lifetime lock.
5. Verify the gateway's submit/status contract before enabling the new Opss
   coordinator. The default router rejects fenced Windows actions on an old
   gateway before reserving the desktop; headless diagnostics remain available.
6. Keep the prior scripts and service configuration for rollback. Rollback must
   preserve unknown receipts and deduplication history, and must not replay work.

The gateway journal belongs on local Windows storage, not a network share.
Release artifacts may use the existing immutable NAS transfer channel, with
hash verification on Linux, Windows UNC and the final local Windows copy.

## Failure interpretation

- A completed receipt is usable only after exact protocol, operation ID, request
  digest and MCP result validation.
- A client disconnect or coordinator restart may be reconciled against the
  journal. Retrieval does not submit the action again.
- A gateway crash during execution leaves an unknown outcome after restart.
  There is no replay and no timer-based desktop unlock.
- Failure to persist a terminal result must be surfaced as journal failure,
  never as success or an unexplained running worker.
- Unknown diagnostics expose a bounded stable reason code and an operator
  inspection action. Arbitrary upstream exception text is not public metadata.
- A missing operation after an uncertain send does not prove that dispatch
  could not still occur. Do not silently clear or automatically resubmit it.
- Discarding an old result payload must never discard the corresponding
  deduplication identity while that identity can still be resubmitted.

## Bounded pilot

Use one isolated QA gateway and controlled test operations. Record process IDs,
ports, release source hashes and per-case receipts. The QA harness must stop
only children it created, await upstream test completion, and finish cleanup
before writing a successful final receipt. Do not start Revit merely to test
the Windows journal.

Pilot acceptance requires:
- duplicate/conflicting submissions and exact result readback;
- real HTTP caller/controller interruption with later reconciliation;
- gateway crash during a confirmed active operation and no replay after restart;
- successful output containing timeout wording and genuine timeout uncertainty;
- terminal database write/commit failure and observed worker failure;
- ownership isolation and stale-receipt protection;
- install/rollback readiness with state preservation.

Before broader use, validate Windows ACLs, a storage/free-space budget, retention
of result payloads independently from deduplication tombstones, and the remaining
legacy fixtures. A limited pilot is not an unbounded-retention production policy.

## Integration evidence

The previously uncommitted implementation was preserved in:
- c3e62b6: gateway-owned journal and contract tests;
- d7fe354: coordinator result reconciliation and ownership checks;
- d86a3bc: isolated native acceptance harness and protocol documentation.

Integration base de6201b includes deploy/gip-core-linux at b25b685.
Diagnostics commit 9b042ec adds stable unknown-outcome reason codes.
Compileall and 124 focused coordinator/router tests passed after integration.
Later acceptance results and the final candidate revision are recorded below.

## Development acceptance, 2026-11-07

- 7cfa00c adds explicit worker supervision and a latched unhealthy journal.
  A terminal UPDATE/commit failure returns an unknown receipt with
  JOURNAL_WRITE_FAILED even if SQLite becomes unreadable. New submissions fail
  closed. Duplicate failed operations retain identity and never replay.
- Four fault regressions cover UPDATE failure, commit failure, unexpected worker
  exception and cancellation before worker dispatch. The journal/coordinator/
  gateway set plus the isolated tmux retry passed: 64 tests.
- ef2a705 extends native QA with an owned gateway crash during confirmed active
  PowerShell work, a no-replay count, upstream completion marker and candidate
  source hashes. Eight portable helper tests passed. Native Windows execution
  remains NOT RUN, independently of these tests.
- 917e26d adds actual TCP HTTP and separate gateway/controller processes. A
  file barrier holds the fake backend active until the controller is killed.
  The restarted controller reconciles the same ledger and retrieves the exact
  saved result. Execution count remains one. This is Linux fake-backend
  acceptance, not Windows or real OAuth acceptance. The first harness run
  exposed an expected FastMCP disconnect exception on context teardown; the
  corrected test handles only RemoteProtocolError after its own proven kill.
- The first integrated full suite produced 2917 passed, 3 skipped and one tmux
  round-trip failure (pane_dead=true with exit_code=None). That test passed in
  an isolated retry. Neither tmux source nor its test was changed by this work;
  the cause is not claimed as proven. A final full suite is run after the new
  code and acceptance fixtures are committed.

Three bounded ACP writers were attempted with isolated worktrees and server-owned
pytest checks. Their dependency preflights passed, but all native sessions timed
out around 11 minutes without an implementation or behavioral verification. One
planning checkpoint had zero changed files; it was steered to implementation.
There was no usable code draft to promote. Failed statuses were retained, the
sessions explicitly abandoned, and the three clean worktrees/branches removed.
The owner completed and independently tested the scoped changes; no model/provider
escalation or repeated identical inference was used.

## Final automated acceptance and candidate

Final compileall plus complete pytest passed: **2931 passed, 3 skipped,
5 warnings, 29 subtests passed in 330.48 seconds**. The prior tmux failure
was not reproduced in the final full run. No tmux implementation/test change
was made. Source code at 917e26d and source archive at 9cc5902 are identical;
9cc5902 adds only this release record.

Immutable paired candidate:
`/mnt/rag-reference/Astra/ReleaseCandidates/Opss/WindowsLifecycle-20261107-9cc5902-Candidate2/`.
- WindowsLifecycle-Candidate2.zip SHA256:
  `07d62e647a6c8bea14d5832839355df8e04ff4d9a0fc873b3fccdf42a1633929`.
- Opss-Lifecycle-Source.tar.gz SHA256:
  `af6b4089bfd1fe01a5565e2fbe90b4ae08298339c52cc3ca381c1d4023492797`.
- Linux/NAS readback hashes passed. Native Windows/UNC hash verification,
  local staging, execution and shared-service cutover are **NOT RUN**.
- The legacy foreign unscoped draining fence remains present. Its missing
  historical journal receipt cannot be reconstructed. Do not bypass it with a
  shadow ledger or direct Windows launch; use the existing local operator
  recovery procedure before native acceptance.

All six task-owned temporary delegate worktrees/branches were removed. The
three older dirty drafts were preserved as private binary patches before
removal; all their paths exist in the published integrated revision. The
feature worktree is clean and the feature branch is pushed to the user fork.
