# Opss-MCP Delegate Recovery Pilot

Date: 2026-11-01. Scope: Gemini-first AGY1/AGY3/AGY2 delegate runner.

## Goal and boundaries

Reduce **terminal** delegate refusals without weakening evidence, Git scope,
quota admission, or server-side acceptance. Location/eligibility failures are
excluded from reliability comparisons and never retried by this controller.

The pilot has three independently auditable single-retry paths:

1. **Interrupted read-only response.** `antigravity_stream_truncated`,
   `empty_harness_result`, or a known interrupted AGY stream: continue the
   same conversation/account once, retaining the remaining time/tool budget.
2. **Blocked Git inspection.** If a read-only delegate reports *only* that
   AGY shell cannot run `git diff`, and Opss already supplied a bounded
   scoped Git diff whose checksum still matches the working tree, continue
   in the same conversation using the supplied diff and `read_file`.
   Other blockers or stale/missing evidence stop the recovery.
3. **Code verification failed.** A verified AGY code writer may receive
   **one** repair attempt after actual non-timeout server-owned test failures.
   Preconditions: original clean baseline, explicit <=8-file scope, <=4
   checks, `commit_mode=forbidden`, no scope/change/commit violations,
   verified postflight fingerprint unchanged before retry, valid same-account
   conversation and fresh quota approval. Original server checks are run again.

All retries require a valid AGY conversation ID, fresh quota admission and
at least 30 seconds remaining. Retry subprocess lifetime is capped at 120
seconds. Read-only tool ceilings are cumulative and require at least two
remaining calls. No automatic third attempt, premium-provider switch,
scope relaxation, manual cancellation override, or claim of semantic
acceptance. Explicit code scope violations and mutated Git state fail closed.

## Evidence and observability

- `delegate_status.recovery` reports an in-progress/terminal action.
- `route_chain.attempt_count`, `recovery_count`, `fallback_count` separate
  same-account continuation from actual provider changes.
- Private `recovery-attempt-1-metadata.json`,
  `recovery-attempt-1-prompt.txt`, and when present
  `recovery-attempt-1-structured-result.json` retain the rejected result.
- The canonical durable metadata retains the original `code_baseline`,
  contract, `recovery_attempts` and restored source scope.
- `delegate_telemetry` shows process-level recovery rates, original error
  categories and location-adjusted reliability. It does not prove semantic
  correctness. Previously unclassifiable historical records are excluded,
  not guessed. Track at least 30 eligible recovered samples before treating
  recovery rates as a trend.

## Acceptance matrix

| Scenario | Expected |
|---|---|
| AGY read-only stream truncated | one resumed attempt, source scope retained |
| Explicit Git shell/PTY refusal, valid diff | read_file fallback, no shell bypass |
| Missing/changed Git diff or unrelated blocker | no recovery |
| Verified code test failure, fingerprint unchanged | one repair, same baseline, rerun checks |
| Repaired code writes outside allowed scope | rejected, no third attempt |
| Cancellation, location block, quota denial, timeout | no recovery |
| Restart during durable resumed task | restored attempt + original constraints |
| Real Gemini source review | succeeded with source anchors, no file edits |
| Real Gemini code pilot on isolated test repository | server verification PASS |

Focused tests: `tests/test_delegate_pilot_recovery.py`,
`tests/test_delegate_recovery_controller.py`,
`tests/test_delegate_code_verification.py` and
`tests/test_delegate_durable.py`.

Full regression:

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider tests --tb=short
```

Before production cutover: clean feature worktree, all tests PASS, committed
feature SHA and rollback branch. Cutover via fast-forward only; restart
`chatgpt-web-oauth-mcp.service` after tasks and owned jobs are idle. Verify
service active, new PID, `server_info.delegate_runtime.status=ready`,
Gemini-first/no premium fallback, real read-only research, isolated verified
code task, private artifacts and Git state. Remove feature worktree after
acceptance. Roll back by restoring the recorded pre-cutover Git ref and
restarting service **only after confirming no active delegated writers**.

## Deferred (not in pilot)

General autonomous remediation of arbitrary `blocked` errors; multi-step
graph planning, cross-account conversation transfer, automatic cleanup of
unscoped source changes, auto-installation of dependencies, and replacing AGY
CLI with ACP. These require distinct safety and reliability acceptance.
