# ACP integration into Opss delegates

Status: implementation slice 1; production routing remains on the existing CLI.
Baseline: Opss 31274aa; official Antigravity ACP 1.4.5; Python ACP SDK 0.12.1.
Flash-only is the intended provider contract, not a blocker or a reason to
change providers. The pilot advertised `gemini-3.8-flash` with low/medium/high
thinking, Google AI Pro, session load/list, cancellation and MCP tools.

## Goal and ownership

Provide Codex-like subagents backed by an external model: a persistent logical
task, isolated conversation, bounded autonomy, steering and independent review.
Do not install another scheduler, GitHub Issue workflow, or second LLM manager.

* Opss owns task identity, scheduler lanes, account admission, budgets, durable
  process ownership, recovery decisions, artifacts and acceptance.
* ACP owns the provider conversation and structured protocol exchange.
* Gemini owns reasoning and tool selection within capabilities granted to the task.
* The managing chat owns objectives, changes in scope and independent acceptance.

Use the existing `DelegateHarness` -> `Invocation` -> durable job ->
`ParsedHarnessOutput` boundary. The SDK is optional and lives in the ACP worker,
not the main MCP event loop. CLI and ACP share logical account capacity; they
must NOT each acquire an independent allowance for the same Google account.

### Implemented in this slice

`acp_session_store.py`: owner/account/workspace/scope/model identity, private
atomic checkpoints, nonblocking cross-process lease and bounded turn-ID journal.
`acp_session.py`: transport-independent state machine, capability/config checks,
history replay suppression, native cancellation, strict budgets, pending-question
record and uncertain outcome fence. `acp_sdk_transport.py`: lazy, version-pinned
SDK transport with a trusted launcher and explicit environment. `acp_result.py`:
normalization to the existing `ParsedHarnessOutput`/durable acceptance boundary.

The result boundary is tested end-to-end through the existing delegate scheduler
and durable artifacts using a deterministic worker fixture. Live acceptance
exercises the real authorized QA ACP runtime, new/load/recall and cancellation
through the new core. A public `AcpHarness`, worker control API and production
registration are NOT enabled yet; these remain slice 2 after isolation/admission.

## Non-negotiable invariants

1. A session is bound to owner/task, account profile, resolved workspace,
   permission/scope fingerprint, model and thinking level. No silent rebinding.
2. One active prompt per session. Different sessions may run concurrently only
   within shared account admission limits. Acquire an OS lock before restoring
   or prompting; a duplicate operation does not send another provider request.
3. Persist `running` before sending a prompt. A lost worker leaves an uncertain
   outcome. Recovery must reconcile that state; it must not blindly replay an
   edit, command or MCP mutation. Deliberate continuation gets a new turn ID.
4. `session/load` may replay past messages. Suppress those events from the new
   answer, usage and tool budget. Install handlers before initialize/load/prompt.
5. `end_turn` means the agent finished a turn, not that the task passed review.
   Cancelled, denied, blocked, empty and invalid-JSON replies are not successes.
6. `request_permission` for a tool is not the same as an Antigravity
   `interaction_*` question. Preserve the latter as a pending question, never
   invent a human decision. Do not authorize by a substring in a displayed title.
7. Permissions are server-owned. No broad `allow_always`, YOLO or automatic
   privilege escalation. Gateway authorization is checked again at execution.
8. ACP client-side file callbacks are NOT the only file access path: the native
   runtime has its own file tools. An OS sandbox, not a prompt, is the boundary.
9. Auth profiles stay outside repositories, artifacts and logs. No copying of
   existing CLI OAuth tokens. Persist sanitized summaries, not raw credentials
   or model thinking chunks. A Google tier or rounded quota is not a money meter.
10. Provider EOF can trigger bounded reconciliation, not provider/account
    switching or acceptance of an advisory. Location refusals are excluded from
    reliability measurements, not bypassed. Explicit cancellation is separate.

## Architecture

```
ChatGPT -> existing Opss task/scheduler/admission
                -> AcpHarness (explicit opt-in, no automatic routing)
                  -> durable ACP worker + stable session store
                     -> official SDK / Antigravity ACP over stdio
                        -> scoped MCP Gateway + isolated native helper
                -> existing artifacts / evidence checks / acceptance
```

The first implementation deliberately exposes no production configuration flag
that could enable an incompletely isolated worker by accident. The reusable
session core and tests are introduced first. A supervisor-provided transport
factory is the seam; it cannot be supplied by a model prompt. Authentication and
secret handling remain outside the session core.

## Implementation slices and gates

### 1. Protocol/session core (current slice)

Implement typed session identity, private atomic state, cross-process lease,
capability/config validation, new/load/prompt/cancel, replay suppression,
bounded answer/usage/tool aggregation, default-deny permission handling, and
explicit uncertain outcome after a lost in-flight turn. No global singleton,
no model change, no duplicated subprocess or quota scheduler.

Acceptance: fake-transport deterministic tests for replay, duplicate turns,
owner/account/scope changes, no-response, provider error, unknown capabilities,
cancel races, late events, output budget, question and denied tool. Real SDK
tests use only the isolated authorized QA profile; never production credentials.

### 2. Durable read-only delegate adapter

Add opt-in `AcpHarness` through the existing adapter boundary. Keep all three
public MCP routers unchanged. The child worker is the only ACP SDK importer;
pin SDK versions in an optional dependency group. A killed main MCP process
must not kill a durable worker. On reconnect recover the job first, not launch
a second prompt. Native ACP cancel precedes process-group termination.

Before exposure, implement account->quota-pool mapping shared with the CLI,
real filesystem isolation (including native helper), resource limits, explicit
workspace scope and profile/session storage isolation. Never claim read-only
enforcement based on denied ACP callbacks alone. Auth broker versus tool-helper
separation must be tested with a synthetic credential marker before enabling
real account credentials in an untrusted code task.

### 3. Tool Gateway and user interactions

Expose a small read-only allowlist (read/search/Code Map/Git) with task-scoped
capability tokens, schema versions, output budgets and structured errors.
Do not expose delegate/job spawning, arbitrary shell, credentials, commits or
deploy to child agents. Re-check scope and authorization at each tool call;
deduplicate mutations with operation IDs when writes are eventually added.

Persist pending questions with an expiry and session/turn ownership. Deliver
them through existing result/inbox machinery; accept a reply only from the
owning task/controller. Distinguish a suspended live request from a cancelled
question that requires a new continuation turn. Invalid/late replies fail closed.

### 4. Subagent interface and progress

Add spawn/message/status/wait/cancel/resume semantics as thin task operations,
not a new orchestrator. Preserve work evidence across turns, list unresolved
criteria, and checkpoint before budgets are exhausted. Parent and child have
different contexts. No assumption of server-side fork/close: the tested runtime
did not advertise them. Feature negotiation decides method use per connection.

### 5. Editing and acceptance

Only after read-only gates pass, enable an isolated writable worktree with
server-owned scope and baseline hashes. A fresh diff and independently run
tests belong to a specific HEAD/tree. Runtime/Windows/Revit QA remains in Opss.
Permit bounded repair from concrete test failures; no self-awarded acceptance.

## Evaluation, rollout and rollback

Use fixed small real tasks plus injected EOF, process kill, restart, permission
denial, duplicate prompt, incorrect session ID and missing capabilities. Compare
ACP and CLI at the same model/effort/account, not marketing defaults. Measure
task completion with evidence, recovery success, manual interventions, time,
total tokens/context reuse, permission violations and result loss. Keep location
refusals and explicit cancellations out of the technical-failure denominator.

Start with one operator-selected account and read-only tasks. Do not enlarge
subscription capacity by introducing a second transport. No fallback to an API
key or premium model. Expand only after faults and isolation pass, then A/B
real tasks before considering default routing. Rollback disables ACP admission;
existing live workers drain or are explicitly cancelled, session records remain.

## Evidence and constraints

Pilot: two independent concurrent sessions retained their own phrases; cancel
returned `cancelled`; one MCP call was denied and another explicitly allowed;
interactive A/B reply and native file read worked. These are protocol proofs,
not proof of production reliability, secure isolation or improved code quality.

Official protocol/SDK references (consulted 2026-11-02):
* https://agentclientprotocol.github.io/python-sdk/quickstart/
* https://agentclientprotocol.github.io/python-sdk/
* https://agentclientprotocol.github.io/typescript-sdk/types/SessionCapabilities.html

Use the tested ACP v1/SDK 0.12.1 interface; v2/remote transports and experimental
fork semantics are out of scope. SDK helper lifecycle must be verified by live
process tests; static capabilities cannot substitute for runtime acceptance.

## Slice-1 acceptance record

* 38 focused ACP tests passed, including the real existing scheduler/durable
  result boundary with a deterministic worker fixture.
* Focused ACP + existing HTTP transport tests: 78 passed.
* Final full repository suite: 1410 passed, 5 existing dependency/fork warnings.
* The earlier full run had 1408 passes and one HTTP-test-server shutdown timeout
  in `test_real_mcp_disconnect_cleans_foreground_process`. It passed in the
  focused rerun and final full suite; the HTTP implementation was not changed.
  Do not claim the transient test condition was fixed by ACP code.
* Real official ACP QA: new session, JSON manifest, separate-process load and
  correct context recall, protocol cancellation, one native file read of a
  random marker, and unchanged source file all passed through the new core.
* An optional independent CLI delegate review hit its first 120s timeout and
  began account fallback; it was cancelled to bound quota use. It supplied no
  completed review and is NOT counted as acceptance evidence.

The QA transport retains the previously tested pilot isolation, not a complete
production credential/tool security boundary. The production server and
default Gemini CLI route were not changed. Keep this slice on its feature
branch until the account-admission and native-helper isolation gates in slice 2
are met. Do not enable general tasks merely because synthetic acceptance passed.
