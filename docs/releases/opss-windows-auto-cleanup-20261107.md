# Automatic desktop session cleanup — 2026-11-07

The user authorized automatic cleanup of sessions that unnecessarily block work.
Desktop ownership is now independent from the engineering outcome of a command.

| Evidence | Automatic action |
| --- | --- |
| Expired idle lease, no operation/activation token, readers or native outstanding work | Release only the desktop lease; retain workspace and request history |
| Valid completed journal receipt | Reconcile the exact operation fence |
| Valid unknown outcome with a persisted terminal MCP reply | Reconcile the exact operation fence; preserve unknown outcome and diagnose it |
| Missing admission identity | Atomically write NOT_DISPATCHED before releasing the fence; a delayed submit cannot execute |
| Accepted running request, gateway crash during work, malformed proof or unavailable journal | Keep the fence; never infer completion from age or ControlStatus |

Cleanup runs before the next conflicting admission or status query. No timer
unlocks unknown running work. An expired idle scoped lease requires fresh acquire/
verification before further UI work. Ownership checks and exact token/digest
matching remain mandatory. A stale receipt cannot clear a newer fence.

## Protocol

The gateway persists execution_status separately from state:
running / finished / unconfirmed. Completed state remains a successful persisted
MCP result; unknown never becomes success. Old unknown rows migrate as unconfirmed.
A received terminal tool error or PowerShell timeout reply is finished, whereas a
lost transport reply or gateway restart is unconfirmed.

The internal windows_operation_reconcile endpoint atomically compares operation
identity and request digest with admission. If the ID was never admitted, its
durable NOT_DISPATCHED tombstone wins over any later delivery. If submit won the
race, reconciliation only reports that accepted operation. No action is replayed.
The ordinary status endpoint remains read-only. All three internal endpoints are
hidden and forbidden as direct model-visible tools.

Admission identity is persisted before upstream discovery. Failure to connect
before dispatch becomes a durable NOT_DISPATCHED receipt, so a disconnected
backend does not create an unexplained immortal desktop fence.

## Native baseline acceptance and current recovery

The exact foreign legacy fence 5931480320ff407ff28492dc was recovered with the
existing local operator API after explicit user authorization, fresh ControlStatus,
desktop screenshot and complete no-Revit process observation. All workspace
request history compared equal before/after. Backup and operator receipt stay
private; no ledger credentials are published.

Candidate 2 native Windows acceptance passed with all ten checks, including
duplicate/restart persistence, a confirmed in-flight gateway crash with one
execution, a genuine PowerShell timeout and final owned-child cleanup. Its
source hashes match the previously published candidate. This is baseline native
acceptance; the automatic-cleanup additions require their own runtime acceptance.

## Development checks

The bounded CLI Flash delegate produced the pure idle-lease policy and 41 tests.
The owner independently ran them, integrated the actual WorkspaceManager consumer,
and added overflow/malformed metadata cases. Unlike the earlier timed-out ACP
attempts, this task produced reviewed code. No premium model escalation occurred.

Focused integration passed 198 tests, including real HTTP controller death.
A further 76 policy/client/QA tests passed after final boundary additions.
Full regression and native automatic-recovery results are recorded below.
