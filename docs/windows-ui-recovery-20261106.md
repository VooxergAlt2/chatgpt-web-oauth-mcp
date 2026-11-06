# Windows QA interface recovery — 2026-11-06

All incident times below are Europe/Moscow. This audit concerned the existing
QA VM and Opss coordinator, not a new Revit binary or a production cutover.

## Finding and live recovery

At 20:49:13 a scoped Windows PowerShell call reserved the desktop for
`ws_93a3bce560fd4b6d92eb6ba61d37250a`. At 20:49:38 the Opss journal records
`Request 'tools/call' timed out`. The original Linux controller PID 875478
subsequently exited, and native Revit PID 13740 was also gone. Two records
remained `ready`, and the UI fence remained `draining`. The second record was
`ws_48e748ff1d774096b2f332eacadd84ea`.

Windows itself answered independent diagnostics; its complete 166-process
inventory contained no Revit, native discovery was empty, ControlStatus was
ready, and a fresh screenshot showed no pending dialog. The orphan's exact
fence ID was `f6521f888e2414d0dc33e5ed`. This was a coordinator dead end:
normal owner-bound recovery could not access the abandoned chat's lease,
and the existing local stopped-process path only handled a native
`verifying` fence, not a Windows `draining` fence.

Commit `c198f032ed0afc899397d5ca335a7ed563adfb16` adds the missing local
operator procedure. It requires fresh no-Revit and remote-idle attestations,
exact fence/native identities, a dead controller, a ten-minute minimum age,
and no outstanding native work/readers/lifecycle transitions. It retires
only the exact matching scope and fence atomically. Other records are
reviewed separately. Unknown outcomes remain unknown; no RPC is replayed.

The procedure was applied from this feature worktree after dry-run and
tests. Both stopped records are retired, the UI fence is clear, and every
workspace's pre-existing request history compares equal before/after.
A private pre-action ledger backup and observation remain under
`~/.local/state/chatgpt-web-oauth-mcp/maintenance/scoped-ui-recovery-20261106`.
No secrets or ledger state were copied to Git or NAS.

Live Windows acceptance then passed:
- PowerShell returned process/session information, then successful port and signature probes.
- Explorer PID 6812 was responsive; Win+E opened a new Explorer window.
- A full Snapshot found the focused Explorer window and its interactive UI tree.
- ControlStatus returned ready; there were no active native workspaces.

Fast Screenshot's initial empty window summary was not evidence that
Explorer hung: it skipped UI-tree extraction. A full Snapshot succeeded.
One intervening Snapshot call coincided with an independently initiated Opss
service restart; after service health recovered, the repeat succeeded.
This task did not restart the shared Opss service.

## Timeout contract defect

Windows PowerShell's advertised default was 30 seconds while the router
stopped waiting at 25 seconds. That mismatch can abandon a still-valid
response. The historical timed-out command's exact requested timeout was
not recovered, so this mismatch is a separately proven defect, not a claim
about the complete cause of that command's delay.

Commit `ad4b54c5de12b9272cfa550c4a1b3eeae3aed835` advertises and supplies a
20-second default/maximum. Invalid values are rejected before transport
connection and UI reservation. Actual timeouts still retain the conservative
fence. Longer work requires a durable Windows script with receipt polling.

A standalone candidate-router probe used the real authenticated Windows
gateway and the real shared workspace ledger. It rejected an explicit
30-second request before reserving the UI, executed a default Get-Date
request successfully, and finished with the UI clear (2.05 seconds total).
It neither replaced the installed service nor used a shadow ledger to
avoid the live desktop fence.

## Revit-MCP status

The completed R7 signed native acceptance was already recorded on
2026-11-05 in Revit-MCP commit `23fa67f`, including autostart, scoped QA Full,
error-path/readback, revoke, and controlled restore. It was not repeated.

Fresh Windows inspection of both R7 DLL copies:
- Authenticode: Valid.
- SHA-256: `2CEF297D6212C4630BDA1524C5BCB57BCF319DE2E5182AA80AABDA71A97E30A3`.
- Signer: `E5EABC80B2661060CC8E04669D7B766D2ABC5D4F`.
- Registered original add-in SHA-256:
  `61A2006DCB2147068515AACD87BB19FD0A753C7C766B2F1C1BFC904E0C8C1F4A`.
- Ports 8000 / 8840 / 8841 retained Windows PIDs 9812 / 10816 / 7388.
- Revit was not launched, no RVT was opened or modified, no certificate changed.

## Validation and handoff

- Python compileall: PASS.
- Recovery / workspace / dispatch / router regression: 149 passed.
- Subsequent timeout/router focused regression: 44 passed (overlaps the prior run).
- Diff review and git diff --check: PASS.
- Live recovery and candidate router against Windows: PASS.
- New Revit native test: NOT RUN; no Revit implementation changed.

Feature: `fix/scoped-ui-recovery-20261106`.
Worktree: `/home/vooxerg/src/opss-scoped-ui-recovery-20261106`.
Base: `18e647ef7632f47e1057649999e55ef2c085d046`.
The canonical Opss checkout advanced during the audit, so it was not reset,
overwritten or restarted by this task. The recovery is already applied to
the live ledger; the new default timeout is tested but is not installed in
the shared running service. Next integration should carry the two commits
into the next coordinated Opss release, rather than create another variant
of the recovery implementation. Native engineering writeback and additional
Revit versions remain separate acceptance work.
