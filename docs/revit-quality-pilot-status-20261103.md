# Quality pilot continuation: verified source and deployed coordinator

Date: 2026-11-03. **PILOT_OPEN = NO.** No new native Full acceptance is claimed.

## Completed

- Revit-MCP source integrated by clean fast-forward to
  `c8d9b92d12ed483bfe02d0f37db4a234ed98d637`.
- Opss source integrated and service restarted from implementation commit
  `705316e84d68ef5b9753dad47194dabe85bdee6e` after a quiescent health check.
- Before restart: no running jobs/delegates, only the current maintenance
  foreground request and no UI fence. The Windows gateway and Revit process
  were not restarted or altered by this deployment.
- Running Opss after deployment: PID `1472890`, HTTP 200, no health observation
  errors. Revit catalog exposes `revit_workspace_pilot_check` through the same
  three public router tools.
- Actual negative-gate call on the historical R2 workspace returned
  `REQUEST_UNCERTAIN: existing non-pilot work must be reconciled` before any
  test submission. This is a control-plane safety test, not native E2E PASS.

## Tests

Full Opss regression job `job_1793730881113_tdUtgjAc`: **2074 passed,
3 skipped, 5 dependency warnings, 18 subtests passed**, exit 0. Output read
and consumed. After the final complete-inventory check and MCP wire test,
the post-commit focused suite passed **49 cases**. Compilation and diff
checks passed. The previous unchanged Node suite remains accepted:
**38 passed, 1 Windows-only handoff test skipped**.

## Candidate artifact, not an installed Windows release

```text
/home/vooxerg/.local/state/chatgpt-web-oauth-mcp/artifacts/
revit-pilot-quality-20261103-c8d9b92-705316e.tar.gz
```

Archive SHA-256:
`ff1ff44c16068859e3c9e3b9149e64135737e52c86e36a4886801882d2df7dbf`.
Node bundle SHA-256:
`2c3043718de77f3fc5b28a298c93f0c04a3958962fb0ab703a1d2ee07852ab0e`.
Includes exact component hashes, source versions and operator runbooks.
The manifest explicitly says `BUILT_NOT_INSTALLED_ON_WINDOWS` and
`pilot_open=false`. Do not copy it into the active immutable R2 tree.

## Runtime boundary and required next step

The old protected native instance was re-observed alive as Revit PID `6736`,
instance `316705f85012438b99efd877121b76b7`, port `8080`. Historical request
`r2-native-code-none-04` still has state `unknown`. Its workspace record SHA
before and after coordinator restart is identical:
`5803f5b5eed6733baa68ae29ae635fd2caee4b2049e5002a679ad6d0ccf26ff3`.
No migration, deletion, replay or successful native result was invented.

Raw Windows FileSystem inspection was denied by `WORKSPACE_ISOLATION` while
the native workspace remained active. No alternate channel bypass was used.
The active selector was therefore not independently re-read in this slice.

The operator must end only the disposable QA Revit session without saving.
Then obtain fresh complete process inventory and empty native discovery,
perform audited stopped-session retirement for explicitly authorized records,
verify RVT hashes and restore R2 using the reviewed recovery path. Stage the
new Node bundle into a fresh immutable release. On a clean QA session,
manual Load Once and one fresh Full grant precede the bounded pilot check;
operator revocation and final integrity/rollback evidence remain mandatory.
