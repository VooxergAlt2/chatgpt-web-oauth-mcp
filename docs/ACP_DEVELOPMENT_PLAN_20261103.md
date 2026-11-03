# ACP development plan: measured completion before broader autonomy

Date: 2026-11-03. Baseline: accepted pilot implementation `abff8de`, documentation
HEAD `5cc4a13`. Scope remains explicit ACP on the attested `antigravity3` account
and clean dedicated linked worktrees of the approved repository.

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
sandbox also starts the native helper from an empty control directory, so normal
repository instruction discovery must not be assumed without evidence.

The installed native archive already implements `_meta.antigravity.enabledTools`
on session creation and loading, with a persisted tool-filter sidecar. Opss does
not currently set this profile. The runtime's default built-in surface is wider
than the actions permitted by the sandbox. Explicit role profiles are therefore
a concrete next optimization candidate; a measured speed or context improvement
has not yet been demonstrated.

Provider usage needs separate interpretation. In this pinned runtime, some prompt
and total counters exclude cached tokens, while `usage_context.used` is derived
from the latest model call using cumulative-counter deltas and may lag asynchronous
updates. The SDK labels terminal usage unstable and mixes per-turn/session wording.
Keep raw provider values with a semantics qualifier; do not add them across resumed
turns or turn them into a price estimate.

## Delivery sequence and acceptance

Effort below is an engineering estimate after prerequisites are available, not a
calendar promise or provider latency guarantee.

| Stage | Scope | Acceptance gate | Indicative effort |
| --- | --- | --- | --- |
| 1: truthful runtime diagnostics | Phase timings, declared budget, safe live progress, terminal usage on failed turns | Deterministic regressions, full repository gate, production health, one useful live writer showing the new fields | Current implementation slice |
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
phase timings, prompt bytes and raw usage. Compare equivalent contracts and the
same account/model before concluding there is an improvement. Do not use one fast
run as proof of causality or relax acceptance to obtain a success.

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

Release evidence will be appended after independent review, the combined gate and
production changed-path acceptance. Until then stage 1 is a reviewed candidate,
not a claim of deployed readiness; stages 2–5 remain explicitly planned.
