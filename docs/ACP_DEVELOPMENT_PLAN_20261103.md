# ACP development plan: measured completion before broader autonomy

Date: 2026-11-03. Baseline: accepted pilot implementation `abff8de`, documentation
HEAD `5cc4a13`. Stages 1 and 2 are accepted; current deployed runtime is
`9effae6`. Scope remains explicit ACP on the attested `antigravity3` account and
clean dedicated linked worktrees of the approved repository.

## Decision

Develop a predictable, observable executor for bounded useful tasks. First expose
where time and budget go; then narrow the native tools offered to each role; then
improve source navigation and measure larger tasks. Preserve the existing parent
review, offline verification, scoped export and uncertain-session rules. More
accounts, automatic retries, recursive orchestration and an ACP protocol migration
have weaker evidence of immediate benefit and introduce separate lifecycle risk.

The accepted pilot is already usable for bounded tasks. The next release is an
incremental improvement, not a restart of the earlier audit or a claim that a
small smoke test proves general autonomous development.

## Evidence behind the ordering

The previous pilot ledger is [ACP_PILOT_PLAN_20261103.md](ACP_PILOT_PLAN_20261103.md).
Two useful one-file edits completed in 117.049 and 68.111 seconds. A three-file
assignment exhausted its native window after 259.03 seconds without edits. That
sample also contained empty searches, an unavailable shell attempt and a missing
test-file lookup. The sample is too small and heterogeneous for a success rate or
latency SLA. Increasing every timeout would mask rather than isolate the causes.

Inspection of the saved task prompts and the pinned native runtime produced:

| Task | Opss prompt, UTF-8 bytes | Approximate `o200k_base` tokens | Native tools | Observed `usage_context.used` |
| --- | ---: | ---: | ---: | ---: |
| `d3a593e609be` | 6192 | 1407 | 6 | 75606 |
| `b480d2906aa2` | 3532 | 819 | 4 | 37943 |
| `9c521c928d74` | 5382 | 1270 | 13 | 69207 |

The token estimates use a different tokenizer from the provider. They measure the
Opss request text only, not the provider's complete input. These values do not
justify blindly truncating task instructions or verification commands. The code
prompt lists context file paths; it does not inline all their contents. The
authenticated runtime sees an empty tmpfs workspace; only the isolated native
helper sees the source workspace. Repository instruction discovery in the
authenticated runtime must therefore not be assumed.

The installed native archive implements `_meta.antigravity.enabledTools` on
session creation and loading, with a persisted tool-filter sidecar. The pilot
did not set this profile; stage 2 now supplies and immutably binds explicit
role profiles. Live new/load/writer acceptance confirmed the requested filter,
persisted binding and forwarded helper configuration. A measured speed or
context improvement has not yet been demonstrated.

Provider usage needs separate interpretation. In this pinned runtime, some prompt
and total counters exclude cached tokens, while `usage_context.used` is derived
from the latest model call using cumulative-counter deltas and may lag asynchronous
updates. This derived value also includes output/thinking counters; it is not a
measurement of the initial system prompt. The SDK labels terminal usage unstable
and mixes per-turn/session wording.
Keep raw provider values with a semantics qualifier; do not add them across resumed
turns or turn them into a price estimate.

## Delivery sequence and acceptance

Effort below is an engineering estimate after prerequisites are available, not a
calendar promise or provider latency guarantee.

| Stage | Scope | Acceptance gate | Indicative effort |
| --- | --- | --- | --- |
| 1: truthful runtime diagnostics | Phase timings, declared budget, safe live progress, terminal usage on failed turns | Deterministic regressions, full repository gate, production health, one useful live writer showing the new fields | Completed; release evidence below |
| 2: role-specific native tools | Versioned explore/code allowlists on new and resumed sessions | Exact SDK keyword forwarding, native tool-filter proof, legacy-session compatibility policy, useful live writer and exploration; no sandbox weakening | Completed; release evidence below |
| 3: directed source navigation | Bounded symbol/location hints from the execution baseline, compact actionable task contracts | Revision/scope/missing-symbol and budget tests, no out-of-scope content, useful two-file task with parent-owned checks | Next; concrete patch plan below |
| 4: representative pilot matrix | Small read, one-file fix, two-file fix with regression, continuation and cancellation | Every intended edit independently reviewed; rejected/uncertain outcomes preserved; report timing, tools and context per task | Several bounded live tasks after stages 1–3 |
| 5: broader rollout | Additional approved projects/accounts and optional orchestration | Explicit independent identity proofs, capacity/quota and recovery tests on each new route | Decide from stage-4 evidence |

### Stage 1: current implementation

Session phases are `connect`, `initialize`, `new_session` or `load_session`,
`configure`, `prompt`, `cancel` and `teardown`. Worker phases are `setup`, `native`,
`validation`, `verification`, `promotion` and `cleanup`. Both record bounded phase
elapsed times and the first failure phase. Session diagnostics additionally record
the first/last accepted native event and its fixed kind. Replayed setup history,
foreign-session updates and late callbacks do not count as live activity.

The worker starts its internal deadline before backend validation and private-copy
setup. Previously those setup operations ran before the internal budget began,
although the outer durable-job deadline was already running. The allocation is:

```text
native session budget = requested lifetime - 3 seconds - setup time - verifier reserve
verifier reserve = min(300, max(8, lifetime / 4)) for writers; 0 for exploration
```

The native session budget includes connection, initialization, configuration,
prompt, cancellation and teardown. Its 5-second cancellation grace is contained
inside that allocation, not an additional extension. The verifier reserve is a
minimum allocation withheld from the native session; a quickly completed native
turn can leave more time for verification, subject to the existing command limit.
The outer durable-job deadline still owns process lifetime, including process
startup before worker entry. These diagnostics do not enlarge either deadline.

`harness_metadata.execution_budget` publishes version, total seconds, worker safety,
verification reserve, native cancel grace, setup elapsed and native session
allocation. Final metadata contains `acp_session_diagnostics` and
`acp_worker_diagnostics`. Failed, incomplete and confirmed-cancel responses retain
valid provider terminal usage and stop reason when available. Absence means unknown,
not zero. This never changes failure, cancellation or uncertain status into success.

`delegate_status.acp_progress` is explicitly advisory. Live views read at most
72 KiB of the worker's regular stdout file: an 8 KiB prefix retains the allocation,
and a 64 KiB tail supplies the latest complete progress observation. The view
exposes fixed phase/event names, numeric counters and elapsed times. It ignores
malformed, unterminated, over-nested and unrecognized records, rejects non-regular
files and symlinks, and never returns prompts, thoughts, tool arguments or raw
native content. Terminal views use only sanitized final metadata. Legacy records
report diagnostics unavailable rather than inventing values.

Progress is emitted on real phase transitions or accepted native updates. There
is no periodic heartbeat and no assertion that quiet time proves a stalled model.
The `prompt` phase can include provider waiting, generation and waiting for a live
question answer; `input_required` distinguishes the last case. Advisory output
failures must not invalidate a verified result. Existing lifecycle/activity,
owner filtering, cancellation, session locking and promotion authority remain
unchanged.

### Stage 2: accepted native-tool profiles

The provider-specific extension remains inside the Antigravity adapter. Actual
SDK 0.12.1 serialization, the pinned native archive and live new/load/writer
behavior have been checked. The full design and evidence are in
[ACP_NATIVE_TOOL_PROFILES_20261103.md](ACP_NATIVE_TOOL_PROFILES_20261103.md).

Accepted built-in profiles:

| Role | Native built-ins | Existing capability boundary |
| --- | --- | --- |
| Explore | `view_file`, `finish`, and `ask_question` when owner control is attached | Read-only workspace plus the existing scoped read gateway |
| Code | `view_file`, `create_file`, `edit_file`, `finish`, and controlled `ask_question` | Private writable copy; existing independent verifier and CAS promotion |

The existing scoped gateway remains separate. Native `view_file` remains
available for the runtime's file-reading format. Native shell, autonomous
child-agent, web, scheduling and unrelated families are excluded from the
advertised profile; the existing enforcement boundary retains its authority.

The stable canonical profile ID/list/hash is bound in checkpoint v2 under the
session lease before provider connection. The existing session identity and
control mailbox remain unchanged. New and load send the same allowlist.
Malformed bindings, profile drift and old provider conversations without a
binding fail before connection. Legacy conversations require a fresh task;
uncertain turns are never replayed for migration. Older workers reject v2
checkpoints on rollback, so those histories require a profile-aware release
before continuation or reconciliation.

Acceptance covered one exact useful edit and a read-only new/load pair. The
canonical requested filters matched private checkpoint bindings and native
sidecars; observed helper flags matched both roles. `finish` has no observable
flag, and this observation is not a full model-tool inventory. Timings, tool/event
counts, prompt bytes and raw usage are recorded without claiming a causal speedup.
Equivalent contracts, account/model/thinking, baseline, verifier and budgets
remain prerequisites for a performance comparison.

### Stage 3: execution-baseline source navigation (next patch)

Source inspection after stage-2 acceptance found that `_build_prompt` already
accepts navigation hints, but `executors.py` collects them only for exploration.
Its project context is captured at submission; code baseline capture happens
later at execution. Merely widening the submission-time condition to include
writers can therefore supply stale ranges after a queued checkout advances.

Build the optional hints inside `acp_worker._execute`, after
`AcpWorkspace.prepare` and its existing baseline-head equality check. Reuse
`delegate_navigation.scoped_navigation_hints` against `writer.baseline_root`,
the immutable private baseline already validated against Git blob IDs. This
keeps the first production patch within `acp_worker.py`; no new RPC fields,
Code Graph service or profile changes are required.

The concrete implementation contract is:

1. Start from `request["scope"]["files_in_scope"]` and remove paths matched by
   `out_of_scope` using the existing `_scope_matches` semantics. Supply
   `context_files=[]` so optional context cannot widen the approved scope.
2. Pass the existing prompt as task text to the bounded Code Map helper.
   Prepend a compact block containing the full execution-baseline commit,
   relative symbol ranges and the instruction to inspect source before citing.
   Preserve the original task/acceptance/verifier text intact after this block.
3. Add only complete UTF-8 hint lines that fit both an 8 KiB prefix ceiling and
   the remaining capacity under the existing 131072-byte prompt limit.
   Empty/unavailable hints or ordinary helper errors preserve normal execution;
   cancellation must retain its normal propagation.
4. Count navigation work as setup before native-budget allocation. Optional
   content-free metadata may report version, `source=private_baseline`, revision,
   hint count/bytes and `advisory=true`.

The reused helper already caps files at 6, individual files at 800000 bytes by
pre-read stat, hints at 8 per file/24 total, symbol spans at 500 lines, and task
matching at the first 12000 characters. It returns symbol names and ranges,
not function bodies. A separate observed weakness is that the subsequent
`read_text` is not itself byte-bounded if that stat becomes stale.

Add worker regressions for the execution snapshot/revision, queued revision
changes, excluded file/directory/glob overlap, context that cannot widen scope,
missing symbols/unsupported files, optional navigation failure, UTF-8/prompt
ceilings and setup time reducing native budget. Existing profile, independent
verification and promotion tests must retain their meaning. Check that the
deployed worker interpreter can import the reused helper before live acceptance.

A useful two-file live acceptance assignment is already identified: make the
actual source read in `delegate_navigation.py` respect
`MAX_NAVIGATION_FILE_BYTES` even after a stale small stat, and add its regression
in `tests/test_delegate_navigation.py`. Keep valid UTF-8 hints working.
Use exact scope, at most two changed files and 100 added/deleted lines, 12 tool
calls and a 240-second lifetime (the existing formula reserves 60 seconds for
verification/export). Confirm the isolated verifier's test dependencies before
submission; do not assume it can use the host virtualenv. Require actual changes
to both files, independent targeted checks and parent review before promotion is
accepted.

The goal is fewer unnecessary source reads and completed useful edits. Compare
equivalent tasks before claiming an efficiency improvement. These stage-3 changes
are planned; they are not part of the accepted stage-2 executable release.

## Deferred protocol and lifecycle work

ACP v1 cancellation requests cancellation; it is not a standardized command to
summarize, finish early or apply new instructions during generation. Queued steering
continues to apply to the next turn. Do not advertise a graceful-final-summary
control until the pinned provider exposes and verifies that behavior.

ACP v2 is still marked draft in the current official documentation. Its prompt
response acknowledges acceptance; completion moves to state updates. Add a separate
negotiated adapter with independent lifecycle tests if v2 becomes a concrete need.
Changing the current v1 completion path in place would risk premature acceptance.

The pilot's documented hard-kill limitation during multi-file export remains.
Catchable failures roll back; hard termination does not make multiple filesystem
replacements globally atomic. Direct in-process task cancellation during verifier
threads is also a separate prerequisite for future embedded workers; the current
worker uses cooperative signal/mailbox cancellation. Keep unknown outcomes frozen
until reviewed and explicitly reconciled.

## Source and validation record

Internal evidence:

- `acp_worker.py`, `acp_session.py`, `acp_progress.py`, `acp_operations.py` and their
  provider-free tests in this repository.
- `executors.py`: `_build_prompt`, `_project_prompt_context` and explore-only
  navigation-hint wiring, inspected at `5cc4a13`.
- Installed `agent-client-protocol` SDK 0.12.1 and the pinned
  `agy_acp_server.par`, inspected read-only: `tool_filter.py`, native `server.py`
  and SDK `types.py`. Configuration, secrets and archives are not committed here.
- Ready Code Graph for baseline tree `df9605c2b6e68a7892038c2b3833e5d56f3be229`;
  `_finish_writer` reverse call path checked against source. Generic Python
  same-name links are not treated as proof without source verification.

Primary protocol references, checked 2026-11-03:

- [ACP v1 prompt turn](https://agentclientprotocol.com/protocol/v1/prompt-turn)
- [ACP v1 schema](https://agentclientprotocol.com/protocol/v1/schema)
- [ACP v1 configuration](https://agentclientprotocol.com/protocol/v1/session-config-options)
- [ACP v2 migration](https://agentclientprotocol.com/protocol/v2/migration)

## Stage-1 accepted release evidence

The parent independently reviewed the integrated implementation and the exported
native contribution. A separate review reproduced and closed three observer-edge
cases: progress callback failure affecting finality, missing legacy counters on
phase events, and terminal metadata arriving during local task cleanup. These
fixes do not upgrade failed or uncertain outcomes to success.

| Gate | Observed result |
| --- | --- |
| Session/helper focused regressions | 97 passed, 8.28 seconds; observer exceptions, phase compatibility and late terminal usage included |
| Public progress/operations regressions | 81 passed, 3.09 seconds; bounded reads, malicious/legacy records and terminal metadata boundary |
| Worker/writer integration | 28 passed, 5.78 seconds; setup cost, exhaustion before provider connection, verifier/promotion cancellation and final cleanup timings |
| Complete repository gate | `job_1793708960714_6dEDl701`: 1910 passed, one optional SDK skip, five existing warnings and 18 subtests passed in 195.02 seconds; exit 0 |
| Static/source checks | `compileall src tests`, `git diff --check` and unchanged clean tested HEAD `d9cd07c6a07ba9e5d458ab36337bb13f1172b727` passed |
| Production deployment | `job_1793709189006_oLOLRmgc`: fast-forward from `5cc4a13`, exit 0 in 29.56 seconds; new PID 782313 active/running, ExecMainStatus 0, authenticated HTTP 200, no observation errors, no error-priority journal records |
| Rollback | `rollback/acp-observability-20261103` preserves `5cc4a13e73dbed3bded8f8f9b5a055270786bd12` |
| Useful live writer | `bd0674dd7f21`, job `job_1793709331895_yK4dBSJP`: same ACP AGY3 / Flash High / high route, 96.994 seconds, six native tools, one file +25/-0 |
| Live public status | While running, `delegate_status.acp_progress` reported `source=worker_stdout`, `phase=prompt`, counters, observation age and the explicit execution budget |
| Final public status | `source=final_metadata`, both diagnostics components, all worker phases, no failure phase and `outcome_uncertain=false` |
| Independent acceptance | Server printed `ACP_OBSERVABILITY_WRITER_PASS`; parent checked actual worker output and exported bytes and printed `ACP_OBSERVABILITY_PARENT_PASS` |

The live task added the runtime-progress contract to `docs/acp-subagents.md`.
The exact accepted export was SHA-256
`b7506ceb54c46fa4e7836e7cbf1ee516d62a2fe7b3d5ba9947de933fa22e9835` and is preserved
in documentation commit `f0d7c7a11341eb56a90d377d0d4994f4a9c09af8`. Parent-owned
verification compared the complete expected bytes, scope, native event counters,
phase totals, budget conservation, gateway revocation and the independent receipt.
Documentation commits after `d9cd07c` do not change executable source.

### Observed live allocation and duration

| Quantity | Seconds |
| --- | ---: |
| Requested outer lifetime | 240 |
| Worker safety | 3 |
| Setup | 3.169 |
| Reserved for verification/export | 60 |
| Native session allocation | 173.831 |
| Native cancel grace, inside that allocation | 5 |
| Actual worker duration | 96.666 |
| Actual native session duration | 93.120 |
| Actual prompt phase | 88.057 |
| Actual candidate validation | 0.061 |
| Actual independent verification | 0.093 |
| Actual promotion | 0.180 |
| Actual cleanup | 0.018 |

The session observed 28 accepted native events. Raw terminal usage was retained
with `usage_source=prompt_response` and `usage_scope=provider_reported_unspecified`.
This task shows working diagnostics and useful verified promotion. Its prompt phase
dominates its elapsed time; it does not distinguish model generation from provider
waiting and is not a controlled speed comparison with the earlier pilot tasks.

### Preserved rejected preflight

The first post-cutover submission, `e47729519523`, was rejected before any provider
connection with `acp_writer_source_dirty`. The parent had selected the worktree
used by the full gate. Git considered it clean, but test/compile caches remained
as ignored files; the writer's full filesystem manifest correctly rejected them.
No worker or native prompt started, and no result was promoted.

The parent created a fresh linked worktree and independently ran the actual
`capture_source_snapshot(require_clean=True)` check before the accepted task.
The rejected record remains rejected and consumed; this was a preparation error,
not a provider failure or an automatic replay. Future live writer acceptance uses
a fresh source worktree separate from the one used for tests and compilation.

Stages 1 and 2 are complete. Stage 3 has a concrete implementation/acceptance plan
above. No measured speedup, broader-account activation or ACP v2 support is claimed.

## Stage-2 accepted release evidence

Implementation `9effae647a91cad54e69b7aed91896b3bd055234` passed the assembled
repository gate: 1999 tests, one optional SDK skip, five existing warnings and
18 subtests in 196.19 seconds. Compilation, diff checks and unchanged tested
HEAD passed. Production deployment preserved rollback
`rollback/acp-native-profiles-20261103`, restarted to PID 902673 and passed
authenticated health with no observation or error-priority journal errors.

| Live task | Result |
| --- | --- |
| New exploration `a2f04977b2f7` | 21.093 seconds, one tool call; exact explore profile and checkpoint v2 |
| Continuation `4696a2b880a2` | 25.298 seconds, zero tools; same native conversation/profile/hash through `load_session` |
| Writer `cee9f1abf819` | 53.122 seconds, four tools; exact one-file +23/-0 export, offline verification and parent byte comparison |

The native contribution is documentation commit
`3efc66b61e0010203f8843a82a18e4974da2f3cb`. All three turns had confirmed
terminal success, no uncertain outcome and no route fallback. The parent checked
the actual worker logs, private profile bindings, saved native filters and
forwarded helper flags independently of the agents' summaries.

The complete job IDs, source/hash evidence, observed flags, timings, raw usage and
launch-failure record are preserved in
[ACP_NATIVE_TOOL_PROFILES_20261103.md](ACP_NATIVE_TOOL_PROFILES_20261103.md).
