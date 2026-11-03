# ACP native tool profiles: immutable session binding and observed configuration

Date: 2026-11-03. This is stage 2 of
[ACP_DEVELOPMENT_PLAN_20261103.md](ACP_DEVELOPMENT_PLAN_20261103.md).
Baseline: production/documentation `d57247c`, runtime `d9cd07c`.
Implementation `9effae6` is accepted and deployed. The full gate, production
health and live new/load/writer evidence are recorded below.

## Purpose and boundary

Offer the model the built-ins needed for its assigned role. This is a
provider-specific advertisement filter in the pinned Antigravity runtime.
Filesystem isolation, scoped gateway authorization, account identity, deadlines,
tool-call limits, cancellation, independent verification and CAS promotion keep
their existing authority. A native profile is not a replacement for those checks.

The MCP caller cannot supply an arbitrary tool list or profile. The trusted
session runner derives it from `allow_native_tools is True` (writer) and
`control is not None` (owner questions). Production workers attach owner control;
standalone tests and adapters may omit it.

| Profile ID | Exact built-ins, canonical order |
| --- | --- |
| `opss-explore-v1` | `finish`, `view_file` |
| `opss-explore-questions-v1` | `ask_question`, `finish`, `view_file` |
| `opss-code-v1` | `create_file`, `edit_file`, `finish`, `view_file` |
| `opss-code-questions-v1` | `ask_question`, `create_file`, `edit_file`, `finish`, `view_file` |

The existing scoped MCP gateway remains available separately. Native shell,
scheduling, subagents, workflows, web/image tools and redundant native search
families are excluded from these profiles. Native `view_file` is retained for
the runtime's expected file-reading format. Only the server executes the declared
verification commands after the writer exits.

## Correct SDK and native contract

The installed worker SDK is `agent-client-protocol==0.12.1`. Its connection
methods place extra keyword arguments into `field_meta`. The correct Python call
is therefore:

~~~python
await conn.new_session(
    cwd=workspace,
    mcp_servers=servers,
    antigravity={"enabledTools": enabled_tools},
)
~~~

The exact wire shape is:

~~~json
{"_meta": {"antigravity": {"enabledTools": ["finish", "view_file"]}}}
~~~

Passing a Python `_meta=...` argument would incorrectly produce
`_meta._meta`. An isolated regression calls the actual installed SDK, with a
local spy in place of network delivery, for every profile on both new and load.
`OPSS_ACP_TEST_PYTHON` selects the separately installed QA interpreter; if explicitly
configured, an absent or different SDK version is a test failure, not a skip.

Both `new_session` and `load_session` receive the same profile payload. The pinned
archive's restore path gives explicit metadata priority over a warm session,
persisted sidecar or default. No switch to another protocol method or model is
part of this change.

## Immutable binding and compatibility

The canonical binding contains `version`, `id`, `enabled_tools` and `sha256`.
The digest is computed from canonical JSON of the first three fields, using sorted
keys, compact separators and ASCII encoding. Extra fields, unknown IDs, altered
tool lists, duplicate/reordered tools, incorrect hashes and boolean versions are
rejected. Changing a catalog tool set requires a new versioned profile ID.

Under the existing OS session lease, execution checks the prior lifecycle,
stale-turn and deduplication guards, then selects and binds the profile. A newly
bound checkpoint is atomically saved before opening the provider connection.
Its schema version becomes 2. The binding remains immutable even if startup fails
or the process dies before obtaining a provider session ID.

The existing `AcpSessionIdentity`, its digest and ownership fields are unchanged.
The modern session store reads checkpoint versions 1 and 2. Control mailboxes
remain version 1 and use the previous identity, so status, replies and explicit
abandonment retain their existing ownership contract.

| State | Execution behavior |
| --- | --- |
| Fresh v1 state without provider conversation | Bind the derived profile, save v2, then connect |
| Bound v2 state with the identical canonical profile | Preserve binding and send it on load |
| Bound v2 state with a different valid role/question profile | Reject with `acp_tool_profile_mismatch` before connection |
| Malformed, unsupported or partially migrated binding | Reject with `acp_tool_profile_invalid` |
| Legacy v1 conversation with a provider session ID and no binding | Reject with `acp_tool_profile_legacy_requires_new_session` |
| Running/uncertain, abandoned or stale source turn | Preserve the existing earlier rejection; never replay |

Legacy result metadata is not authoritative proof of a checkpoint binding. Start
a fresh task/reference for an old conversation without a profile; do not relabel
or replay the old turn to migrate it. Uncertain sessions retain the normal review
and explicit abandonment requirement.

Checkpoint v2 also closes the downgrade hole: old workers that only understand v1
refuse the new checkpoint instead of silently ignoring the profile. Drain active
tasks before rollback. After rollback, profile-bound histories require a
profile-aware release to resume or reconcile; fresh tasks still use that deployed
release's own contract. Do not manually lower checkpoint versions.

## Requested profile and observed helper configuration

Final `harness_metadata.native_tool_profile` reports the canonical requested
profile. The binding in the private session checkpoint remains authoritative.

The native network boundary additionally observes initialization only after
successfully forwarding it to the helper. Its optional best-effort callback is
passed through sandbox and broker to the worker. The worker retains the latest
bounded snapshot plus `observation_count` in
`harness_metadata.native_tool_configuration`.

The source is `forwarded_native_initialization`. The snapshot includes only these
fixed `HarnessSideTools` enable flags:

~~~text
subagents, runWorkflow, find, userQuestions, runCommand, manageTask, schedule,
fileEdit, viewFile, writeToFile, grepSearch, listDir, generateImage, searchWeb,
readUrlContent
~~~

Each flag is true, false or null. Missing/malformed values remain unknown. The
snapshot excludes prompt/reasoning content, arguments, paths, URLs, headers,
credentials and the original configuration. `finish` has no explicit
HarnessSideTools flag in this pinned SDK and is listed in
`unobservable_builtin_tools`.

This proves which configuration was forwarded to the helper. It is not a full
inventory of tools ultimately presented to the language model. The persisted
native `tool_filter` sidecar independently proves the saved requested filter.
Neither observation changes runtime permissions or declares task success.

Ordinary observer exceptions cannot change transport or finality. Cancellation
and other BaseExceptions retain the existing propagation behavior. Rejected
initialization and failed sends produce no observation.

## Pinned-source evidence

Read-only inspection used:

- Installed SDK 0.12.1 connection serializers and native capability mapping.
- `agy_acp_server.par`, SHA-256
  `7e7019c8989ca066cbac4d6580552b7a4ad39485dd6b6fd81de9daa9a7b39042`.
- Native `tool_filter.py`, `server.py` new/restore paths and SDK `types.py`.
- This repository's profile helper, session store, runner, worker, sandbox,
  helper broker and native network initialization boundary.

The native filter sidecar lives under the task's private conversation directory
as `<native_session_id>.meta`. Acceptance reads only its tool-filter fields; it
does not dump provider conversation databases or authenticated configuration.

The ready baseline Code Graph is keyed to tree
`2be628a342d2a38cf413b8f032b0e041947808ce`. Impact queries covered the session
runner and store loader. Unresolved Python calls were checked against the ACP
operations and worker paths; unrelated same-name stores are not treated as ACP
callers. This graph describes the committed baseline, not the profile implementation.

## Validation and rollout

Focused checks cover immutable binding before connection, failure/cancellation
before session ID, role/question drift, legacy refusal, v1/v2 owner controls,
continued writer verification, exact SDK serialization and observer ordering/
sanitization. The existing optional SDK smoke can remain skipped in the server
test interpreter; the new wire-shape test runs in the configured worker SDK.

The complete gate must include both optional runtime paths:

~~~text
OPSS_ACP_TEST_PYTHON=/home/vooxerg/.local/share/opss-acp-qa-20261102/py/bin/python
OPSS_ACP_TEST_NATIVE_HELPER=/home/vooxerg/.local/share/opss-acp-qa-20261102/runtime/localharness_external
~~~

After review and the full gate, deploy with a preserved rollback ref and health
verification. Live acceptance uses fresh source worktrees, separate from test
worktrees that contain ignored caches. It exercises a new and resumed exploration
session and a useful verified writer. Check saved profile/hash, native sidecar,
observed helper flags and actual exported bytes independently.

Compare speed only on equivalent tasks with the same account, model, thinking,
source baseline, verifier and comparable budgets. This release does not by itself
establish a latency or context-cost improvement.

## Release evidence

Stage 2 is accepted on implementation
`9effae647a91cad54e69b7aed91896b3bd055234`. The parent reviewed the integrated
source, actual worker output, persisted state and exported bytes. Subsequent
documentation commits do not change executable source or tests.

| Gate | Observed result |
| --- | --- |
| Session/controls focused regressions | 167 passed in 13.18 seconds; one later deadline-accounting regression passed in 0.44 seconds |
| Profile, worker, writer and actual SDK regressions | 68 passed, one optional old SDK smoke skipped in 9.47 seconds; all eight new/load serializations in the pinned SDK executed |
| Configuration observer/network/sandbox regressions | 110 passed, one optional skip in 2.98 seconds |
| Complete repository gate | `job_1793711426258_ojATbOXp`: 1999 passed, one optional SDK skip, five existing warnings, 18 subtests passed in 196.19 seconds; exit 0 |
| Static/source checks | `compileall src tests`, `git diff --check`, clean unchanged tested HEAD; full job elapsed 197.813 seconds |
| Production deployment | `job_1793711643285_2juh3Q6u`: fast-forward from `d57247c`, exit 0 in 30.263 seconds; PID 902673 active/running, ExecMainStatus 0, authenticated HTTP 200, no observation errors or error-priority journal records |
| Rollback | `rollback/acp-native-profiles-20261103` preserves `d57247c9604ad1b5b4c795cc22169f16b906ba1e` |
| Independent parent acceptance | `ACP_NATIVE_PROFILES_EXPLORE_PARENT_PASS`, `ACP_NATIVE_PROFILES_RESUME_PARENT_PASS`, `ACP_NATIVE_PROFILES_WRITER_PARENT_PASS` |

The first full-gate submission did not receive a usable MCP confirmation; a later
submission was rejected with `INVALID_ARGUMENT` before creating a job. Durable
job discovery showed no gate job before the corrected launch above. These were
launch failures, not passing tests or provider attempts. The assembled gate
above was the single executed full test run for this candidate.

### Live changed-path acceptance

All three tasks used explicit ACP `antigravity3`, Gemini Flash High with high
thinking, and the same source baseline `9effae6`. There was no route fallback,
automatic replay or uncertain result. The source worktree was newly created
separately from the test worktree and passed the real full-filesystem
`capture_source_snapshot(require_clean=True)` preflight. All stderr logs were empty.

| Scenario | Delegate | Durable job | Outer limit, seconds | Actual job, seconds | Tool calls | Accepted native events | Opss prompt bytes |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| New read-only profile review | `a2f04977b2f7` | `job_1793711714426_AwhD5ZIn` | 180 | 21.093 | 1 | 19 | 2290 |
| Explicit continuation | `4696a2b880a2` | `job_1793711793520_txye1dUL` | 180 | 25.298 | 0 | 15 | 2077 |
| Useful documentation writer | `cee9f1abf819` | `job_1793711856413_nfkhnIhJ` | 240 | 53.122 | 4 | 21 | 4103 |

The read-only task inspected the new helper and correctly reported the canonical
profile, legacy-session rejection and immutable binding. Its continuation recalled
the prior audit marker and distinguished malformed profiles from valid profile
mismatches. Both turns kept session reference
`ac2b2cedac5ef6bb350578b0b09e3a39`, native conversation
`2844abc9-8c1e-4ce4-9188-d4f2f126d077` and profile hash
`b55b9bc98dd5861d355017df776cc8530dd043bb14d8ce9ca856fb723b6765b7`.
Their diagnostics independently showed `new_session` and `load_session`.
The full source manifest remained unchanged after both turns.

The writer inserted the exact 23-line section into `docs/acp-subagents.md`,
with no deletions or other changes. The isolated server verifier printed
`ACP_NATIVE_PROFILES_WRITER_PASS`; parent review compared the complete bytes
against the baseline plus the independently supplied insertion and checked scope,
budget, diagnostics, gateway revocation and promotion receipt. Accepted file
SHA-256 is
`e611533d8ca9788e9cac6f4f1901faa7ed18767e3787926fdca3ccca95fdd8b0`.
Documentation commit `3efc66b61e0010203f8843a82a18e4974da2f3cb` preserves the
native contribution. The writer used profile hash
`aa149b35a27f0f533cf3fbe4cb417393bc8ecc3c8deabc6257093c9054cbdd4e`.

For each task, the parent read actual final worker stdout, checkpoint v2 and only
the `tool_filter` portion of the native sidecar. The canonical requested profile,
checkpoint binding and persisted `enabledTools` matched exactly.

| Forwarded helper flags | Explore new/load | Code |
| --- | --- | --- |
| `viewFile`, `userQuestions` | true | true |
| `fileEdit`, `writeToFile` | false | true |
| `subagents`, `runWorkflow`, `find`, `runCommand`, `manageTask`, `schedule`, `grepSearch`, `listDir`, `generateImage`, `searchWeb`, `readUrlContent` | false | false |
| `finish` | No observable flag | No observable flag |

Every task reported one successfully forwarded initialization observation.
All 15 observed flags were actual booleans in these samples; unknown/null remains
part of the general contract.

### Writer timing and raw usage

The writer allocated 3.157 seconds to setup, 3 seconds to worker safety,
60 seconds to verification/export reserve and 173.843 seconds to the native
session, including its 5-second cancel grace. Actual native duration was
49.260 seconds; the prompt phase took 44.470 seconds. Candidate validation took
0.061 seconds, independent verification 0.097, promotion 0.189 and cleanup 0.018.
Total worker elapsed was 52.781 seconds.

Raw terminal usage is retained with `usage_source=prompt_response` and
`usage_scope=provider_reported_unspecified`:

| Delegate | input | output | thought | cached read | cached write | total | `usage_context.used` |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `a2f04977b2f7` | 14110 | 403 | 1339 | 0 | 0 | 15852 | 9354 |
| `4696a2b880a2` | 8611 | 348 | 2020 | 0 | 0 | 10979 | 10979 |
| `cee9f1abf819` | 82062 | 736 | 3909 | 43008 | 0 | 86707 | 38278 |

These counters are not summed across resumed turns or converted into price.
Prompt bytes measure only the saved Opss request, not the provider's full input.
The timings are operational observations of different tasks, not a controlled
before/after comparison. The accepted result is functional tool filtering and
immutable continuation; a causal latency or context-cost improvement remains
unproven.

Stage 2 is complete. The next bounded change is execution-baseline source
navigation, described in the development plan.
