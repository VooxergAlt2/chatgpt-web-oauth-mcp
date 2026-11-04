# Supervisor S3: durable private checkpoint drafts

## User-visible workflow

A scoped ACP writer now saves an immutable delta before publishing each
`supervisor_checkpoint`. The checkpoint's server observations include
`retained_draft`, with its exact snapshot ID, original baseline, candidate
digest, changed-file count and stored byte count. `accepted` is always false.
Unchanged checkpoints reuse the same saved artifact.

The owner may `continue`/`steer` as before, or choose
`delegate_supervisor(action="suspend")`. Suspend requires a successfully saved
draft and cancels the current native turn through the existing cancellation
protocol. It does not export files or claim completion. Await and consume the
terminal result; a cancellation without native acknowledgement may still be
uncertain and must be reconciled before any replacement execution.

Use `delegate_draft(delegate_id=..., action="status")` to list saved artifacts,
including after worker death. To continue, submit an ACP code task with:

```json
{
  "backend": "acp",
  "kind": "code",
  "harness": "antigravity3",
  "complexity": "high",
  "draft_from_delegate_id": "<owned terminal source>",
  "draft_snapshot_id": "<exact retained snapshot>",
  "resume_from_delegate_id": "<same source, only if its session is known terminal>",
  "cwd": "<the same clean dedicated worktree>",
  "files_in_scope": ["<the same exact files>"],
  "verification_commands": ["<current independent acceptance command>"]
}
```

The two draft arguments select bytes. `resume_from_delegate_id` separately
selects provider history. After an uncertain interrupted session, inspect the
state and use `delegate_reconcile(action="abandon")`; then omit
`resume_from_delegate_id` and start a new conversation from the explicit saved
draft. No history replay, provider transfer or acceptance is inferred.

The new worker first prepares a fresh private copy of the clean baseline, then
restores the selected delta into that copy only. The original baseline remains
the comparison point for scope, line budgets, verification receipts and final
CAS promotion. The agent is told it is continuing an unaccepted draft and that
baseline navigation line numbers may no longer match the candidate.

After the result has been reviewed and integrated, explicitly delete an obsolete
artifact with `delegate_draft(action="discard", snapshot_id=...)`. Discard is
idempotent, requires a terminal source worker and never modifies repository
files. It is serialized against snapshot restore.

## Integrity and lifecycle

- Capture checks the original source and allowed candidate delta, copies regular
  files without following symlinks, then checks the candidate and source again.
  Excluded paths cannot enter a retained candidate.
- Snapshot bytes and directories are synced before an atomic publication rename.
  The snapshot is outside the native sandbox and separate from its mutable tree.
  Temporary, unpublished captures are not recovery artifacts.
- Restore requires the same logical owner, configured account, thinking level,
  workspace, exact explicit scope, baseline commit and baseline content digest.
  Saved bytes, modes, deletion markers and the reconstructed full-tree digest
  are checked before native execution. There is no implicit rebase or scope
  widening, and changed source files block restore/promotion.
- The server derives the source binding from owned execution metadata, not from
  a caller-selected filesystem path. Active source jobs and still-held session
  leases block recovery. An unknown session outcome requires explicit review
  and abandonment; saving a draft does not clear uncertainty.
- A resumed task receives a distinct request fingerprint including the selected
  immutable draft, and its current verification contract covers the full diff.

`candidate_snapshots.py` is provider-neutral and uses `SupervisorBinding`;
capture/restore is supplied by the Git workspace adapter. Additional registered
Gemini accounts get independent execution-bound artifacts. Future API adapters
may reuse the store and channel; they still need credential admission, owner
authorization and their own live acceptance. Cross-account handoff is not
silently enabled by this release.

## Explicit limits

Only published checkpoint snapshots are durable recovery points. Changes after
the latest checkpoint can be lost on process death. Raw unfinished workspaces
are not adopted automatically. A hard kill during source promotion still
requires inspection of the source worktree and reconciliation.

Storage is bounded to eight distinct snapshots and 64 MiB of saved changed-file
bytes per source execution, with explicit discard and no time-based deletion.
Snapshot overflow preserves earlier artifacts and reports a typed error; it
does not silently evict the recovery point. This is a storage bound, not a new
model-call budget. Existing single-turn ACP call/time limits are unchanged;
explicit continuation starts a new turn with preserved selected bytes.

## Verification record

Focused storage/channel/owner checks: 61 passed in 7.22 seconds. They include an
actual SIGKILL of the snapshot-producing subprocess, recreation of the store,
restoration into a fresh private copy, and proof that source files were unchanged.

Worker/API integration checks: 36 passed in 10.73 seconds. A simulated native
first turn saved a candidate and failed without export; a second turn loaded
the same conversation, read the restored candidate, passed the real isolated
verifier and then promoted it. Owner/account changes, active sources, unknown
outcomes before reconciliation, changed baselines/scopes/bytes and symlinked
snapshot content are rejected.

Committed-base semantic inspection used graph
`d704f6fc17116db0cebdab46cf22fa495aa42dc5eda7687d29994c281a24972d`
for `edd45f8`; `_finish_writer` has the expected `_execute` caller, with complete
resolution and no unresolved sites for that query. Dirty changes were not
treated as part of this graph.

Candidate full gate: `job_1793797784617_fG-uJNwv`.
Runtime deployment and native acceptance are recorded after execution.
