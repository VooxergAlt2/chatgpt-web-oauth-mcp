# ACP native tool profiles: immutable session binding and observed configuration

Date: 2026-11-03. This is stage 2 of
[ACP_DEVELOPMENT_PLAN_20261103.md](ACP_DEVELOPMENT_PLAN_20261103.md).
Baseline: production/documentation `d57247c`, runtime `d9cd07c`.
Implementation is a candidate until the release evidence below is completed.

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
callers. This graph describes the committed baseline, not the uncommitted edits.

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

Pending the assembled full gate, production health and live changed-path checks.
