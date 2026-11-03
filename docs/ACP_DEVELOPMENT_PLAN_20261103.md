# ACP development plan: measured completion before broader autonomy

Date: 2026-11-03. Baseline: accepted pilot implementation `abff8de`, documentation
HEAD `5cc4a13`. Stage 1 is accepted and deployed on runtime implementation
`d9cd07c`. Scope remains explicit ACP on the attested `antigravity3` account and
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

The installed native archive already implements `_meta.antigravity.enabledTools`
on session creation and loading, with a persisted tool-filter sidecar. Opss does
not currently set this profile. The runtime's default built-in surface is wider
than the actions permitted by the sandbox. Explicit role profiles are therefore
a concrete next optimization candidate; a measured speed or context improvement
has not yet been demonstrated.

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
| 2: role-specific native tools | Versioned explore/code allowlists on new and resumed sessions | Exact SDK keyword forwarding, native tool-filter proof, legacy-session compatibility policy, useful live writer and exploration; no sandbox weakening | 1–2 focused implementation sessions |
| 3: directed source navigation | Bounded symbol/location hints for code tasks, compact actionable task contracts | Existing/new scopes and missing-symbol tests, no out-of-scope content, useful two-file task with parent-owned checks | 1 focused implementation session |
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

### Stage 2: concrete native-tool profile design

The provider-specific extension is supported by the pinned archive, not guaranteed
by the base ACP protocol. Keep it inside the Antigravity adapter and validate
against the actual pinned SDK/runtime before enabling it.

Proposed initial built-in profiles:

| Role | Proposed native built-ins | Existing capability boundary |
| --- | --- | --- |
| Explore | `view_file`, `finish`, and `ask_question` when owner control is attached | Read-only workspace plus the existing scoped read gateway |
| Code | `view_file`, `create_file`, `edit_file`, `finish`, and controlled `ask_question` | Private writable copy; existing independent verifier and CAS promotion |

Continue offering the existing scoped gateway separately. Native `view_file`
should remain available because the runtime documents a trained output format;
removing it in favor of a gateway-only design is not an established optimization.
Disable native shell, autonomous child-agent, web, scheduling and unrelated tool
families through advertisement while retaining the enforcement boundary.

Bind a stable profile identifier/hash to session identity and checkpoint metadata.
New-session and load-session code must supply the same profile. Define legacy
behavior explicitly: an existing session without a profile binding cannot silently
resume with a different tool surface. Test a compatible legacy path or require an
explicit new session; never replay an uncertain turn to migrate it.

First acceptance should use one small useful edit with an exact expected diff and
a read-only counterpart. Record offered and invoked tool kinds, native event count,
phase timings, prompt bytes and raw usage. Compare equivalent contracts, the same
account/model/thinking level, comparable native timeout/tool budgets, source
baseline and verifier before concluding there is an improvement. Do not use one
fast run as proof of causality or relax acceptance to obtain a success.

### Stage 3: bounded task context

The executor currently supplies source-location hints for exploration but not code.
Reuse the existing Code Map mechanism for code tasks before considering a new
context engine. Include at most a bounded set of relevant symbols and file/range
locations, pinned to the task's source revision. Keep task intent, exact write scope,
acceptance and offline verifier explicit. Prefer one coherent small fix over an
open-ended instruction to inspect an entire subsystem and then improvise a change.

Do not duplicate skills, add provider system prompts wholesale, or inject full
repository files just because they are available. Measure reduced unnecessary
reads and completed useful edits, not the smallest textual prompt.

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

Stage 1 is complete. Stages 2–5 are planned, with role-specific native tool profiles
next. No measured speedup, broader-account activation or ACP v2 support is claimed.
