# Retire a stopped QA workspace without inventing request outcomes

## Scope

This local operator maintenance path is deliberately **not an MCP tool**.
It neither closes Revit nor authorizes code, unbinds a live workspace, saves
models, modifies request results or removes a foreign UI fence. Use it only
after the operator has ended the disposable QA Revit session without saving.

An old `unknown` request may have no recoverable native receipt. That is not
evidence that it never ran. Once its exact native process is independently
confirmed gone, however, it cannot execute any further. The workspace may be
retired while every original request status and receipt remains unchanged.
This is administrative cleanup, **not native acceptance or successful unbind**.

## Evidence and explicit approval

Collect fresh read-only evidence through the authorized tool routes:

1. `windows_Process(mode=list)` with a complete inventory, not a truncated
   top-N page. Name/PID filtering is not assumed to work: inspect the returned
   full inventory. There must be no `Revit.exe` process.
2. `revit_workspace_instances`: no live native instance.
3. UI fence clear and no current native read slots. The maintenance function
   checks these again under the state-file lock.

The local observation file uses `RevitStoppedProcessObservationV1` and stores
the exact workspace/native identity from its private ledger, capture epoch,
empty `revit_processes` and `native_instances` lists, and
`process_inventory_complete=true`. Tool labels are
`windows_Process:list:all` and `revit_workspace_instances`.

**This file is an operator-reviewed attestation, not a signed native proof.**
Never fabricate observations, infer absence from a missing window, or create
one from a truncated process list. It expires after 60 seconds. An active
Revit, stale/partial observation, changed identity or active UI fence refuses
the operation. An outstanding controller lifecycle transition is refused too.

`scripts/retire_stopped_revit_workspace.py` defaults to dry-run and requires
`--confirm-process-stopped` and `--confirm-unknown-outcome`. `--apply` affects
only the explicitly named workspace. There is no bulk wildcard or tool-route
shortcut. Do not retire another chat's record without operator authorization.

When the exact stopped process left a **scoped verifying UI fence**, the
operator may additionally specify `--confirm-stopped-scoped-ui` after
independently verifying the same fresh complete Windows process inventory
and empty protected native discovery. The fence, workspace owner and
outstanding native activation request must match exactly. No active
process, foreign UI operation, unscoped/draining fence, or mismatched owner
is eligible. The old `unknown` outcome is never reclassified.

The stopped verifying fence and its matching retired workspace are
committed atomically under the existing workspace state lock. Normal
per-chat ownership enforcement and live recovery are not changed.

## Durable semantics

The record becomes `retired` and receives an audit entry containing the old
record hash and observation hash. Every existing request and native receipt
is retained. Unknown remains unknown, and `native_completion_confirmed=false`
and `pilot_acceptance=false` are explicit. Neither a new grant nor a new
process can reactivate that workspace. Cached history remains owner-scoped.
Late native/local replies cannot rewrite or reopen a retired record.

Normal live `release` still requires terminal native work. The retirement
path does not weaken this rule: it is available only after process exit and
operator inspection, when no live native work or authority remains.

## Verification

`tests/test_revit_stopped_retirement.py` covers dry-run, unchanged old request
records, restart behavior, foreign owners, no public MCP registration, late
reply conflicts, missing confirmations, active UI, live/partial/stale process
observations and mismatched native identities. Tests are isolated and never
retire the production workspace as a side effect.
