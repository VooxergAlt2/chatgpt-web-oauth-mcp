# ACP source navigation from the execution baseline

Date: 2026-11-03. Stage 3 of
[ACP_DEVELOPMENT_PLAN_20261103.md](ACP_DEVELOPMENT_PLAN_20261103.md).
Baseline: accepted runtime `9effae6`, repository/documentation `2b1b2a0`.
Accepted navigation implementation: `a6c8ff8ac5bd1cda533d87e491c55df98dc90b8b`.
Accepted and deployed bounded-reader follow-up:
`dac731bb79c0d942fed5ee7a482d182a24e9f9d5`.

Source navigation and controlled application of a reviewed two-file patch passed.
The earlier behavioral two-file assignment timed out and remains failed. This
release does not establish autonomous implementation from a behavioral contract.

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
800000 bytes per source, 8 symbols per file, 24 hints total, 500 lines per selected
span and the first 12000 characters for task matching. The initial navigation
commit retained the existing pre-read stat check. The accepted follow-up also
opens the source in binary mode and requests at most 800001 bytes. It rejects an
oversized result before UTF-8 decoding. A stale small stat therefore cannot cause
an unbounded Python-level read or oversized decode; this is not a claim about
kernel or buffered-stream read-ahead. Existing handling of missing, unsupported
or malformed sources remains best-effort.

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
This release does not fix that composition rule or truncate steering. The next
patch is an atomic mailbox operation that composes and byte-checks all queued
steering before claiming any message; see the development plan.

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

Both assignments used the attested `antigravity3` account, the existing Flash
High / high route, a fresh clean linked worktree at `a6c8ff8`, exact scope
`src/chatgpt_web_oauth_mcp/delegate_navigation.py` and
`tests/test_delegate_navigation.py`, forbidden native commits, at most 80 added
and 20 deleted lines, 12 tool calls and a 240-second lifetime. The worker reserved
60 seconds for verification/export and 3 seconds for worker safety.

### Independent verifier preparation

The actual isolated verifier mounts the system runtime and private candidate,
not the host virtualenv or QA interpreter. Its system Python 3.12.3 has no pytest
or server dependencies. The parent therefore prepared a stdlib witness using the
actual navigation module (excluding only its relative Code Map import) and the
actual `_PythonSymbolVisitor` and `python_symbol_spans` source definitions.
It does not substitute a mock symbol parser.

The five checks cover valid UTF-8 at the exact limit, a stale small stat, growth
between stat and open, byte length differing from character count, and malformed
UTF-8. A read meter requires explicit binary reads of at most N+1 bytes and rejects
unbounded/read-like escape methods. A temporary baseline fixture failed this
witness; a temporary reference fix passed all five checks inside the actual
`verification_command` sandbox. Both fixtures were removed without repository
changes. The baseline failure proves the old implementation violates the witness
at its text-mode read, not that an oversized file was actually fully read.

The original witness command is 4050 characters / 4055 UTF-8 bytes including
heredoc and trailing newline, SHA-256
`019842ccd06d094bc8012d7e6475d3218758c459d12a0a60bd7e02a27200cfd2`.
It fits the stricter 4096-character code-contract limit. Repository pytest runs
remain a separate gate in the prepared test environment after export.

### Preserved failed behavioral assignment

Delegate `582fe0bcda56`, job `job_1793713719653_dAx57FjB`, received a behavioral
description of the bounded-read fix and required regression. It timed out after
172.397 seconds with nine tool calls and 32 accepted native events.
`outcome_uncertain=true` and the native stop reason was `cancelled`.
Verification and promotion did not run. The actual full source snapshot remained
clean and unchanged at `a6c8ff8` (271 files, manifest digest
`d2dd3201dad9597c4db369a3d050e20a21af0b7003e0c4d29b60ab78c515499c`).

The single gateway call was rejected with `acp_gateway_path_outside_scope` for
`src/chatgpt_web_oauth_mcp/code_map.py`. This is one observed obstacle; it does
not by itself explain the whole timeout. The scoped gateway was revoked.
The parent reviewed the actual worker/checkpoint/source evidence and explicitly
abandoned session `c0bc426f5748daf05d9bc56b72886d38`: continuation is forbidden,
a new session is required and the mailbox is closed. The original uncertain
outcome and history remain preserved. The result was consumed without export.

### Accepted controlled patch application

After that review, the parent prepared and reviewed the exact replacement and
regression text. A temporary git-archive fixture passed all five repository
navigation tests in 0.99 seconds; it was deleted without changing the task source.
The next assignment used a fresh worktree and fresh session, the same route and
budgets, the unchanged five-check witness, a regression-presence check and exact
whole-file SHA-256 checks. This was an explicit manual replan, not an automatic
retry or replay of the uncertain session.

Delegate `11d8496b6827`, job `job_1793714559542_-GqEnUTM`, succeeded in
117.201 seconds with ten tool calls and 44 accepted native events. It changed
exactly the two declared files, +65/-1. All three declared verifiers passed
inside `acp_verification_sandbox`, then CAS promotion completed.
`outcome_uncertain=false`. All four scoped gateway calls were accepted
(`read_text` twice, `git_status` once and `git_diff` once); the gateway was revoked.

The actual exported files were:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `src/chatgpt_web_oauth_mcp/delegate_navigation.py` | 5401 | `20cff8f80d62d661020d1bee99a5e391dc5491416ebeb9b4de289ac6c1765311` |
| `tests/test_delegate_navigation.py` | 6164 | `abd21fabdffec0466ce1bdc5e93181dfef93358dfe04688b67eaa96cf1627132` |

The new regression checks the explicit N+1 read, stale stat, valid Unicode
symbol hints and rejection before decoding an oversized result. The parent
independently checked actual exported bytes, clean/full worktree status, worker
stdout/stderr, verifier commands/results, snapshot receipt, completed checkpoint
v2, scope, budget/phase accounting and gateway revocation. The proof printed
`ACP_SOURCE_NAVIGATION_PARENT_PASS`; a separate source review found no blocker.
The exact export was committed as `dac731b` and received its own full gate.

Successful session `e48916130bc47af558bbd3992422159e` used conversation
`7c0dcb92-8988-4919-ab51-1e22ef2fb6ba`. Its promoted snapshot digest was
`0dace64da05fde6de4ffe8de9708a69086d8e445184987f623ebb3a60ad374f7`.
The successful delegate result was consumed.

### Observed timings and prompt accounting

Both turns retained the code native-tool profile and reported five navigation
hints, an 1185-byte full prefix and execution revision `a6c8ff8`. The successful
checkpoint retained that metadata. Failure metadata did not upgrade the first
turn into success.

| Quantity | Behavioral task | Exact-patch task |
| --- | ---: | ---: |
| Outer elapsed, seconds | 172.397 | 117.201 |
| Setup, seconds | 3.166 | 3.269 |
| Native allocation, seconds | 173.834 | 173.731 |
| Worker native phase, seconds | 168.885 | 112.960 |
| Actual native session, seconds | 168.850 | 112.924 |
| Session prompt phase, seconds | 164.065 | 108.095 |
| Candidate validation, seconds | Not reached | 0.065 |
| Independent verification, seconds | Not reached | 0.371 |
| Promotion, seconds | Not reached | 0.204 |
| Worker cleanup, seconds | See retained failed diagnostics | 0.018 |
| Original saved Opss prompt, UTF-8 bytes | 7682 | 11021 |
| Native tool calls | 9 | 10 |
| Accepted native events | 32 | 44 |

Native cancel grace is 5 seconds within the native allocation. The original
saved prompt sizes above exclude the separately observed 1185-byte prefix and
do not measure the complete provider wire/system context.

Raw terminal usage retains `usage_scope=provider_reported_unspecified`:

| Provider field | Behavioral task | Exact-patch task |
| --- | ---: | ---: |
| input | 201547 | 228816 |
| output | 1226 | 2155 |
| thought | 15305 | 5799 |
| cached read | 124457 | 99917 |
| cached write | 0 | 0 |
| total | 218078 | 236770 |
| `usage_context.used` | 62086 | 47657 |

These values are preserved observations with the previously documented provider
semantics caveat. Do not sum them across turns or derive prices. The two contracts
are materially different and there is no no-navigation control. Neither the
duration nor context difference demonstrates a navigation/profile speedup.
Controlled patch application is accepted; autonomous behavioral implementation
remains a separate stage-4 acceptance item.

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

| Gate | Observed result |
| --- | --- |
| Focused navigation/worker regressions for `a6c8ff8` | 46 passed in 11.30 seconds; agent-owned job `job_1793713138325_uHHzaqdt`, terminal logs forwarded by its owner and consumed |
| Full gate for `a6c8ff8` | Parent-owned `job_1793713288544_3Ze-w_3V`: 2028 passed, one optional SDK skip, five existing warnings and 18 subtests in 203.09 seconds; exit 0 |
| Navigation deployment | `job_1793713580675_ghApHEgD`: fast-forward from `2b1b2a0`, exit 0 in 27.756 seconds, PID 948413 active/running, authenticated HTTP 200, no observation or error-priority journal errors |
| Full gate for `dac731b` | Parent-owned `job_1793714827221_y8UcIsBE`: 2029 passed, one optional SDK skip, five existing warnings and 18 subtests in 204.64 seconds; exit 0 |
| Source/static gates | Both executable candidates passed `compileall src tests`, `git diff --check` and clean unchanged tested HEAD checks; the configured real SDK/helper path remained enabled |
| Bounded-reader deployment | `job_1793715287297_aEPxUbml`: fast-forward from `a6c8ff8`, exit 0 in 28.860 seconds; new PID 987843 active/running, ExecMainStatus 0, authenticated HTTP 200, no observation errors or error-priority journal records |
| Rollback references | `rollback/acp-source-navigation-20261103` preserves `2b1b2a0`; `rollback/acp-bounded-navigation-read-20261103` preserves `a6c8ff8` |

Both cutovers verified the global delegate/queue drain and allowed only their
own durable deployment job in the global running-job count. Parent-owned gate
and deployment logs were inspected to terminal EOF and consumed. Documentation
updates after `dac731b` do not change tested executable source.

No provider/model/account fallback, scope expansion, automatic replay or
reclassification of the failed behavioral task occurred. The next development
step is atomic prompt/steering composition, followed by a representative matrix
that records controlled-patch and behavioral tasks as separate capabilities.
