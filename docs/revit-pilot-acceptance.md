# Protected Revit pilot: bounded native acceptance

## Operator scope

One operator, Revit 2023.1, one owner-bound disposable QA `model.rvt`.
Existing Windows-MCP 8000 and private QA gateway 8841 are not switched.
Use an immutable reviewed Node/gateway release with the already accepted
Revit DLL; source builds and fixture tests are not runtime acceptance.

The operator personally approves the exact QA add-in's `Load Once` prompt
and grants `QA Full access` in Revit. Neither action is automated. The
four-minute native permission lifetime, revocation rules, owner proof and
same-socket native identity checks are unchanged.

## One bounded native consumer

After opening the prepared copy and releasing the UI lease, call:

```text
revit_workspace_pilot_check(workspace_id=<owned QA workspace>, run_id=<fresh label>)
```

This virtual Revit tool uses the **same** canonical workspace manager and
Node/native route as normal commands. It is not a second client, raw TCP
channel, permission issuer or alternative gateway. The caller cannot supply
C# or change the test list.

The fixed sequence is:

1. Complete protected `bind_prepared` when needed.
2. Read `document.PathName` with `transactionMode=none`.
3. Read it with `transactionMode=auto`.
4. Throw a predetermined exception inside an automatic transaction.
5. Read the path again to verify worker recovery.
6. Unbind only after the sequence has confirmed terminal native receipts.

All paths are compared with the exact owned workspace. Transport success is
insufficient: the native domain result must be successful and parseable.
The expected-error step must actually reach the code execution error path;
an expired grant or pre-execution authorization error is not accepted as it.
The test does not change model parameters or save any RVT. An actual mutated
parameter rollback has separate existing native evidence for the same DLL;
this bounded route test does not claim to repeat it.

## Time bound and recovery

The tool waits at most 18 seconds per invocation, below the outer router
deadline. A pending result retains its actual request ID and state. Invoke
the **same run ID** to poll those records, not to resubmit them. Request IDs
are namespaced by workspace and run, and their code/argument fingerprints
are checked before using any cached receipt.

A typed Node `not_sent` refusal becomes `approval_required`. The same run
remains rejected even after reapproval. A deliberately new run ID is needed
for a new attempt. This never issues a grant or retries automatically.

Unrelated old `unknown` work blocks the run. The historic R2 request
`r2-native-code-none-04` is not migrated, deleted or treated as unsent. Its
old session requires native reconciliation or explicit stopped-session
retirement preserving its unknown outcome.

## Acceptance is not deployment

`phase=ready_for_revoke` means the fixed native code path and unbind passed.
The response still says `pilot_open=false`, `model_mutation_tested=false` and
`operator_revoke_required=true`. Before opening a pilot:

- Revoke the grant through Revit's UI and verify access is denied.
- Close only the disposable QA documents without Save; verify source and
  workspace file SHA-256 and confirm no lingering Full authorization.
- Verify the intended production gateway identity, preserved 8000/8841 and
  a clear UI fence. Exercise the reviewed rollback where required.
- Record actual native receipts, release version and final configuration.

Do not label retired unknown requests, expired grants, fixture tests or a
successful bundle build as production E2E PASS.

## Regression coverage

`tests/test_revit_pilot_check.py` covers successful bounded execution,
manual approval absence, exact document verification, blocked foreign/UI
access, old unknown requests, lost replies, no replay, a bounded polling
timeout, changed code behind an existing ID, grant rejection and an
authorization failure masquerading as the expected exception.
