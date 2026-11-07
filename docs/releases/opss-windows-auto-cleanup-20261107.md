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

## Final acceptance

Tested source revision: 4117a4ce11f1f6f21a5d4613cddbef234068a8f4.
Compileall completed successfully. Full regression passed: 2994 tests, 3 skipped,
29 subtests, 5 warnings, in 307.02 seconds. Later changes only record evidence
and merge unrelated deployment documentation; they do not change tested code.

Candidate 3 passed all ten native Windows lifecycle checks and cleaned up its
owned children. An additional native admission test persisted NOT_DISPATCHED,
then delivered the original submit late: the tombstone was returned and the
execution marker was never created. Reading the actual candidate SQLite journal
confirmed the distinction used by automatic reconciliation:

| Native receipt | Outcome state | Execution status | Release eligibility |
| --- | --- | --- | --- |
| Normal result | completed | finished | Exact matching request may release |
| PowerShell timeout reply | unknown / TIMEOUT | finished | Exact matching request may release; verify engineering outcome |
| Gateway killed during active work | unknown / GATEWAY_RESTARTED | unconfirmed | Retain fence; do not replay |

The candidate source hashes observed on Windows were:

| File | SHA256 |
| --- | --- |
| qa_windows_operation_lifecycle.py | a53c4721848391ef18fa7d6723931780b622d2052ac1f3982cf8f7579dbcd4fb |
| windows_revit_gateway.py | 8b48f5b38a4c3e4ba561aaf8dd53eacf1dc9b34adc73c655748fbdd1e904bdec |
| windows_operation_journal.py | 95ba6a1bee19dd1fc1cd722ac78763c1ed2834364f5b32c42b837a9d6a7241e2 |

Controller death and lost-submit recovery passed against real HTTP/process
fixtures on Linux. Native Windows acceptance exercises the gateway contract;
it does not claim an end-to-end cloud coordinator restart or Revit API test.
Revit was not launched. A cross-host candidate probe prompted Windows Firewall;
Cancel was selected, no Allow action was taken, and the exact owned listener
was stopped. No firewall bypass is part of this release. An earlier operator
recovery recorded desktop-idle evidence before inspecting that screenshot;
this evidence error was acknowledged and corrected in a private maintenance
record before continuing. The loopback acceptance above is the accepted run.

Final observation: shared UI fence clear, no Revit process, no candidate listener
on 8842, original loopback listeners 8000/8840/8841 retained. A read-only hash
query timed out on the old live gateway and reproduced its stale-fence defect;
the owned fence was recovered after fresh ControlStatus/screenshot inspection.
The same terminal-timeout path is covered by the new client regression test.

## Paired rollout and rollback

The shared Linux coordinator and original Windows gateway remain on their current
versions pending explicit production cutover authorization. Preparing this
candidate does not activate automatic cleanup in the shared service.

Use a normal paired rollout, not a hotpatch: quiesce admissions, require no UI
operation/native outstanding work, back up the current gateway scripts, journal,
coordinator revision and workspace ledger, then install the tested gateway and
journal together before restarting the coordinator at this feature revision.
Retain authentication, TLS verification, SSH transport and local state paths.
The gateway must advertise submit, status and reconcile before admissions resume.
Do not migrate an old unknown journal row to finished or replay a pending action.

After restart verify gateway capabilities, journal health, UI admission/release
and a disposable timeout recovery through the existing authenticated transport.
If any check fails, stop admissions and restore the previous paired code and
backed-up state only while quiescent; retain candidate evidence separately.
Never replace a journal underneath a running gateway or drop accepted requests.
