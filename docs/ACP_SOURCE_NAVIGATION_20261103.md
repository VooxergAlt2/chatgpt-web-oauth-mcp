# ACP source navigation from the execution baseline

Date: 2026-11-03. Stage 3 of
[ACP_DEVELOPMENT_PLAN_20261103.md](ACP_DEVELOPMENT_PLAN_20261103.md).
Baseline: accepted runtime `9effae6`, repository/documentation `2b1b2a0`.
This implementation remains a candidate until the release record is completed.

## Purpose

Give an ACP writer a bounded set of useful symbol locations before its native
turn. Reuse the existing Code Map helper. The task, exact write scope, final
response contract, immutable tool profile, independent verification and CAS
promotion remain authoritative.

Submission-time project context can become old while a task waits in the queue.
The worker therefore computes hints only after `AcpWorkspace.prepare` and the
existing execution-baseline equality check. It reads `writer.baseline_root`,
the private immutable copy validated against Git blob IDs, rather than the live
source worktree or the writer's mutable candidate.

## Implementation contract

The optional `_writer_navigation_prompt` helper in `acp_worker.py` returns the
effective prompt and bounded metadata, or the unchanged original prompt with
no metadata when navigation is unavailable.

- Start from exact `files_in_scope`; remove file, directory and glob exclusions
  using the existing `_scope_matches` semantics.
- Accept canonical relative printable Python paths; reject traversal, absolute
  paths, backslash aliases and control characters.
- Call the existing `scoped_navigation_hints` with the immutable baseline,
  original prompt and `context_files=[]`. Additional task context cannot widen
  this automatic source read.
- Admit at most 24 valid hints. Validate each location against the effective
  allowlist, positive ordered line range, 500-line span limit and identifier
  components of the qualified symbol name. Skip malformed or repeated hints.
- Encode repository path and qualified name as data in one JSON line per hint.
  No source bodies, comments, instruction-file contents or snippets are inserted.
- Prepend the full execution-baseline revision and an explicit advisory header.
  Keep the complete original prompt intact as the final suffix.
- Add only complete UTF-8 lines that fit an 8192-byte prefix budget and the
  remaining space under the 131072-byte worker prompt limit.
- Empty hints, unsupported input and ordinary optional-navigation errors preserve
  normal execution. Cancellation and other BaseExceptions propagate.

The reused helper retains its existing bounds: 6 concrete Python files,
800000 bytes per file checked by stat, 8 symbols per file, 24 hints total,
500 lines per selected span and the first 12000 characters for task matching.
Its pre-read size check is not by itself a bound on an actual read after a stale
stat. Closing that separate read boundary is the useful live acceptance task
below; no source-read fix is silently assumed in the initial navigation patch.

Navigation runs during worker setup, before the native allocation is calculated.
Its elapsed time therefore reduces the remaining native budget. No additional
lifetime or tool-call allowance is granted.

## Diagnostics and prompt accounting

When hints are added, final `harness_metadata.source_navigation` contains only:

~~~json
{
  "version": 1,
  "source": "private_baseline",
  "revision": "<full execution commit>",
  "hint_count": 1,
  "hint_bytes": 512,
  "advisory": true
}
~~~

The example numbers above are illustrative. `hint_bytes` counts the entire
prepended UTF-8 prefix, including header, separators and closing marker.
No hints or paths are repeated in metadata. The worker attaches it before writer
verification, so a successful verified checkpoint also retains it. Available
metadata on a failed native result does not change the outcome into success.

The existing saved `prompt.txt` is the original submitted Opss prompt.
It does not silently become a copy of the augmented provider prompt; account for
the observed prefix separately when reporting request bytes.

The 131072-byte bound here applies to the augmented worker prompt. An existing
separate session path appends queued steering after its initial prompt-size
check (`AcpSessionRunner.run`). The continuation matrix must define/test the
combined limit and preserve or explicitly defer messages when space is exhausted.
This patch does not claim to fix that composition rule or truncate steering.

## Meaningful regression gate

Provider-free checks must cover actual immutable-baseline symbols and execution
revision, a queued submission revision that differs, exclusions and context
boundaries, absent/unsupported symbols, errors and cancellation, malformed hint
output, UTF-8/full-line budgets, near-limit prompts and setup-time accounting.
Worker integration must retain native profiles, failed-result semantics,
independent verification, promotion and cleanup behavior.

The deployed QA interpreter imports the existing Code Map/navigation helper.
Its SDK is the previously pinned `agent-client-protocol==0.12.1`. The complete
repository gate retains the configured real SDK wire-shape and native helper
tests; prior accepted gates are not a substitute for this candidate's gate.

## Useful two-file acceptance

After the navigation implementation is reviewed, tested and deployed, give the
native writer one concrete task: enforce `MAX_NAVIGATION_FILE_BYTES` on the actual
read in `delegate_navigation.py`, and add a regression in
`tests/test_delegate_navigation.py`. A stale small stat must not permit an
unbounded read; valid UTF-8 source must still produce useful hints.

Use a newly created clean linked worktree, exact two-file scope, forbidden native
commits, at most 80 added and 20 deleted lines, 12 tool calls and 240 seconds.
The current worker formula reserves 60 seconds for verification/export.
Require actual changes to both files, review the exact diff and keep uncertain
outcomes frozen under the existing reconciliation policy.

The isolated verifier mounts the system runtime and private candidate, not the
host virtualenv. System Python has no pytest or server dependencies. The
acceptance therefore uses an independent stdlib witness that executes the actual
navigation code and actual source-symbol parser while excluding unrelated
unavailable package imports. It measures binary reads and rejects unbounded or
excess reads under a stale small stat; it also checks valid UTF-8 behavior.

That isolated witness is followed by the real repository tests in the prepared
test environment after export. Because the live task changes executable source,
its reviewed result is a second candidate and needs its own full gate before
integration and deployment.

## Source-analysis evidence

The Code Map identified the worker execution entry and the current explore-only
navigation call in `ExecutorRegistry._make_task`. Baseline Code Graph lifecycle
completed for tree `c0eb2ab50d4ad46cdc17566a69537c8b1e82af3b`,
graph `7f72310e4eab3ac94db2d32c0cb031841b5d5d78a8e1b4ed5b485a07dedb0296`.
Impact queries covered `acp_worker._execute` and `scoped_navigation_hints`.

The graph reported no unresolved same-name sites for those queries. Source
inspection nevertheless excluded an unrelated SQLite `db.execute` test that
appeared in a reverse path; graph resolution is not treated as complete semantic
proof for dynamic Python calls. The new worker-to-navigation call is reviewed
in source and tests, not attributed to the older committed graph.

## Release evidence

Pending source review, focused/full gates, deployment and useful live acceptance.
