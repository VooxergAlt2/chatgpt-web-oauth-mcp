# Windows operation lifecycle

The coordinator must not equate the lifetime of an MCP request with the
lifetime of a Windows action. A disconnected caller cannot prove that an
action stopped, and free-form console text is not an execution status.

## Incident and reproduced defect

The previous incident left a scoped desktop fence in `draining` after a
transport timeout and controller exit. Local operator recovery restored the
desktop but did not make lost responses recoverable.

On 2026-11-06 a successful, read-only PowerShell source search reproduced
another defect: the result included the source literal
`Command execution timed out` and ended with `Status Code: 0`.
The router's substring classifier nevertheless retained the desktop fence.
The exact owned fence was recovered after ControlStatus and screenshot
inspection. No Revit or Windows process was terminated.

## Replacement contract

| Responsibility | Owner |
| --- | --- |
| User authorization, native workspace ownership, desktop admission | Opss WorkspaceManager |
| Durable operation identity before sending | Opss workspace ledger |
| Deduplication and execution journal | Windows gateway |
| Execution after client disconnect | Windows gateway task |
| Historical result retrieval | Owner-bound Opss virtual result tool |
| Fence release | Exact operation-token and receipt comparison |
| Native document identity and mutations | Existing Revit workspace backend |

Internal gateway submit/status endpoints are not model-visible tools.
A request digest binds the operation ID to the exact tool and arguments.
Repeated submission with the same identity returns its existing receipt;
different arguments are rejected. Status retrieval never dispatches work.

A completed receipt is persisted before the gateway reports completion.
Opss can reconcile it after a caller disconnect or controller restart,
including when another session merely needs to determine whether the
desktop is available. That session must not receive the previous owner's
command result or credentials.

Unknown completion does not become success after a delay. Gateway death
during execution, malformed receipts and missing evidence remain explicit
uncertainty. The operation is never silently repeated. This is a recovery
protocol, not an exactly-once guarantee across arbitrary Windows crashes.

## Compatibility and release

The normal router requires the journal protocol before reserving the
desktop for a fenced Windows call. An old gateway must be upgraded first;
headless diagnostics remain usable. The explicit legacy compatibility flag
is for existing contract fixtures and controlled migration, not a runtime
environment switch or automatic fallback.

Deploy the Windows gateway and its adjacent journal module together from
one reviewed revision. Their state belongs on local Windows storage, not
the shared NAS. NAS is used only for immutable release transfer, with hashes
checked at both ends. Deploy Opss only after the gateway advertises the
new contract. Preserve old scripts for rollback and preserve journal data.

## Acceptance

Release acceptance must cover duplicate submission, response loss,
reconstructed coordinator, ownership isolation, stale-receipt races,
invalid protocol/result envelopes, genuine upstream uncertainty, and
successful output that contains timeout wording. An actual Windows check
must exercise the installed PowerShell backend without launching Revit.

