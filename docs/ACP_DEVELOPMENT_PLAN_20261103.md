# ACP development plan: measured completion before broader autonomy

Date: 2026-11-03. Baseline: accepted pilot implementation `abff8de`, documentation
HEAD `5cc4a13`. Stages 1 and 2 and the stage-3 navigation implementation are
accepted; current deployed runtime remains `dac731b`, with main at `0257683`.
Stage 3a atomic prompt composition is implemented and fully gated at `852cee3`;
activation awaits a quiet shared-service window. Stage 3 demonstrated controlled
application of a reviewed two-file patch; autonomous behavioral implementation
remains unaccepted. Scope remains explicit ACP on the attested `antigravity3`
account and clean dedicated linked worktrees of the approved repository.

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
| 3: directed source navigation | Bounded symbol/location hints from the execution baseline and byte-bounded actual source reads | Revision/scope/budget tests and independently verified two-file exact-patch task; behavioral task remains failed | Implementation and controlled-patch mode completed; evidence below |
| 3a: atomic prompt composition | Compose queued steering with the effective prompt before any message is claimed | Exact-byte boundary, queue preservation, explicit continuation and unchanged uncertain-outcome rules | Implemented at `852cee3`; 2050-test full gate passed; activation and post-cutover live gate pending |
| 4: representative pilot matrix | Small read, behavioral one-file/two-file fixes, exact-patch application, continuation and cancellation | Review every intended edit; preserve rejected/uncertain outcomes; report each capability separately with timing, tools and context | Pre-cutover read/queued continuation passed; new-runtime continuation and behavioral matrix follow stage-3a activation |
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

### Stage 3: accepted execution-baseline navigation

Worker navigation now runs after `AcpWorkspace.prepare` and the execution-head
equality check. It reads the immutable private baseline, filters exact file scope
through exclusions and emits at most 24 validated Python symbol locations as
advisory JSON lines. The complete original task remains the suffix. The prefix
fits both 8192 UTF-8 bytes and available space under the 131072-byte worker prompt
ceiling; setup time is charged before native allocation. In the deployed stage-3
runtime this worker ceiling does not bound later queued-steering composition.
The stage-3a candidate below closes that gap; it is not activated yet.

The initial implementation `a6c8ff8` passed its own source review, 46 focused
regressions, complete gate and deployment. The useful source fix delivered through
ACP, `dac731b`, makes actual navigation reads binary and limited to N+1 bytes
for N=800000, rejecting oversized data before decoding. Its new regression,
independent offline witness, parent byte comparison, complete gate and deployment
also passed. Detailed contracts, raw observations, receipts and rollback references
are in [ACP_SOURCE_NAVIGATION_20261103.md](ACP_SOURCE_NAVIGATION_20261103.md).

The first behavioral assignment timed out with an uncertain outcome, no verifier
or promotion and an unchanged source worktree. One read of `code_map.py` was
rejected by the existing gateway scope; this observation alone does not explain
the full timeout. The parent explicitly abandoned and froze that session.
A manually replanned assignment in a fresh session applied an exact reviewed
patch to both declared files and passed all independent checks. There was no
automatic replay, account/model fallback or expansion of write scope.

Accordingly, the accepted capability is controlled two-file patch application.
General implementation from a behavioral specification still needs stage-4
evidence. The differing contracts and lack of a no-navigation control do not
support a performance or context-efficiency claim.

### Stage 3a: atomic prompt composition implemented; activation pending

The parent reproduced the gap on unchanged main `0257683`: a valid 130966-byte
task plus steering reached an offline provider as 131073 bytes, after the queue
had already been claimed. The limit is 131072 UTF-8 bytes. Navigation shares this
budget but did not cause the original ordering defect.

Candidate `852cee30e25180cc9c3136178d689b9e05e03a45` adds
`AcpControlStore.begin_prompt_turn` and uses it from `AcpSessionRunner.run`.
Within the existing mailbox lease, it composes the exact prompt, heading,
separators and every queued instruction before claiming anything. A fitting
batch is claimed in full. An oversized batch remains completely queued, with
unchanged text, IDs, hashes, order and delivery fields. Both paths record the
current turn so checkpoint/control finality and reply targeting stay consistent.
The original `begin_turn` API retains its claimed-list result through the shared
transition. Later enqueues remain for a subsequent turn.

Overflow produces `acp_prompt_composition_too_large` before the provider prompt
call. The raw worker/session result records `prompt_sent=false`; public metadata
records `harness_metadata.outcome_uncertain=false`. The latest failed delegate
can be explicitly continued with a shorter task, claiming the preserved queue
once. There is no truncation, partial batch or automatic retry. A claim remains
an at-most-once reservation rather than a provider acknowledgement; existing
uncertain-session rules still govern post-claim transport failures.

Twenty-one new regressions cover byte boundaries, Unicode, all-or-none claiming,
persisted queue preservation, failed-turn deduplication, reply/latest-turn rules,
concurrent enqueue ordering, explicit short continuation and transport uncertainty.
The focused suite passed 186 tests; the complete candidate gate passed 2050 tests,
one skip, five existing warnings and 18 subtests. Independent source/lifecycle
review, compilation, diff checks and unchanged tested source also passed.

Two bounded drain attempts deferred without changing main or restarting the
service because unrelated work remained active. An alternative worker-only
cutover was reviewed, but the current runtime has no supported admission pause
to prevent new workers importing a partially updated module pair. The immediate
next step is the existing guarded release procedure in a quiet window, followed
by fresh-worker provenance and one real queued continuation on the new runtime.
No source gate needs repeating for documentation-only changes.

A real read and explicit queued continuation already passed on the old runtime,
using the same native conversation; that evidence is labeled pre-cutover baseline.
After stage-3a activation, continue the small representative pilot matrix. Keep
exact-patch execution and behavioral problem solving separate, and keep account,
model, thinking, baseline, budget and verifier comparable for performance claims.
Unknown outcomes require review and explicit reconciliation, never replay.
Full evidence and the concrete resume checkpoint are in
[ACP_PROMPT_COMPOSITION_20261103.md](ACP_PROMPT_COMPOSITION_20261103.md).

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

Stages 1 and 2 and the stage-3 navigation implementation are complete. Controlled
patch application passed; behavioral implementation remains a separate acceptance
item. Stage-3a composition is implemented and gated, with activation pending.
No measured speedup, broader-account activation or ACP v2 support is claimed.

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

## Stage-3 accepted release evidence

| Gate | Observed result |
| --- | --- |
| Navigation candidate | `a6c8ff8ac5bd1cda533d87e491c55df98dc90b8b`; 46 focused tests, 2028 full-suite tests, one optional SDK skip, five existing warnings, 18 subtests; compilation and clean tested HEAD passed |
| Navigation deployment | `job_1793713580675_ghApHEgD`, exit 0, authenticated health and journal checks passed; rollback preserves `2b1b2a0` |
| Failed behavioral task | `582fe0bcda56`, `job_1793713719653_dAx57FjB`: timed out in 172.397 seconds, nine tools, uncertain native outcome, no verification or export, source unchanged; explicitly abandoned/frozen |
| Accepted exact-patch task | `11d8496b6827`, `job_1793714559542_-GqEnUTM`: 117.201 seconds, ten tools, exact two-file +65/-1 change, all offline witnesses and parent byte checks passed, confirmed terminal success |
| Bounded-reader candidate | `dac731bb79c0d942fed5ee7a482d182a24e9f9d5`; parent-owned `job_1793714827221_y8UcIsBE`: 2029 passed, one optional SDK skip, five existing warnings, 18 subtests in 204.64 seconds, compilation/diff/clean HEAD passed |
| Bounded-reader deployment | `job_1793715287297_aEPxUbml`, exit 0 in 28.860 seconds, PID 987843 active/running, authenticated HTTP 200, no observation or error-priority journal errors |
| Rollback | `rollback/acp-bounded-navigation-read-20261103` preserves `a6c8ff8`; the earlier navigation rollback preserves `2b1b2a0` |

The dedicated [stage-3 record](ACP_SOURCE_NAVIGATION_20261103.md) preserves exact
source/witness hashes, failed-session reconciliation, independent reviewer
evidence, observed navigation metadata, budget phases and qualified raw usage.
Both owned full gates and deployment jobs were inspected and consumed; no older
stage gate was rerun as a substitute for the new candidate's validation.

## Stage-3a verified candidate and pending release

| Gate | Observed result |
| --- | --- |
| Baseline defect | `ACP_COMPOSITION_BASELINE_GAP_REPRODUCED`: offline runner emitted 131073 bytes from a valid 130966-byte task plus queued steering |
| Source candidate | `852cee30e25180cc9c3136178d689b9e05e03a45`, branch `fix/acp-prompt-composition-20261103`; two source and three regression modules |
| Focused gate | `job_1793729072347_RaMXRrQy`: 186 passed in 15.36 seconds; source/test hashes and diff check passed |
| Full gate | `job_1793729167304_I1faW9of`: 2050 passed, one skip, five existing warnings and 18 subtests in 210.81 seconds; compilation, source hashes and unchanged clean HEAD passed |
| Deferred deployment | `job_1793729496688_Nflo9Tp1` and `job_1793729832872_rKr4e_hr`: global activity remained busy, both exited 2 with `changed=false`; no fast-forward, restart or new rollback reference |
| Pre-cutover live read | `66bdcdd3bc9f`, 30.663 seconds, one tool, source findings independently verified |
| Pre-cutover steering continuation | `36e69c7d05d5`, 18.725 seconds, one tool, same conversation loaded; queued nonce absent saved prompts and present exactly once in answer, original message hash/source/target verified |
| Current rollout state | Main remains `0257683`, executable runtime `dac731b`; authenticated health passed. New-runtime activation and post-cutover continuation remain pending |

Every owned job/delegate above is terminal and consumed. The implementation
worktree and a clean live-session worktree are retained; the completed full-gate
worktree was removed without force after recording its evidence. No new account,
fallback,
uncertain replay or repeated completed source gate was used. The dedicated
[stage-3a record](ACP_PROMPT_COMPOSITION_20261103.md) contains exact source hashes,
live raw observations, the latest continuation source and guarded release steps.
