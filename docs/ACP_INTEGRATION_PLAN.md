# ACP subagents: implementation and remaining scope

Updated: 2026-11-03. Baseline production revision: `31274aa`.
Runtime contract: official Antigravity ACP 1.4.5, ACP v1, Python SDK 0.12.1,
WebSockets 17.4. The first deployable capability is a durable, resumable,
read-only research/review subagent. Native editing is a separate unfinished
capability, not implied by successful code generation in an answer.

## Ownership and execution path

* Opss owns task IDs, the existing scheduler, account admission, durable process
  ownership, cancellation, artifacts, evidence and independent acceptance.
* ACP owns the provider conversation and structured protocol exchange.
* The parent chat owns objectives, follow-up instructions and acceptance.
* A native helper performs file/tool work inside the approved workspace sandbox.
  The authenticated runtime is separated from that helper.

`delegate_task(backend="acp", harness="antigravity3", kind="explore")` follows
the existing `DelegateHarness -> Invocation -> durable job -> ParsedHarnessOutput`
path. It creates neither another orchestrator nor another quota pool. The SDK
is imported only by the worker. The three public MCP routers stay unchanged.
CLI remains the default; ACP is explicit and has no automatic provider, account,
transport or premium-model fallback.

## Implemented contract

### Identity and capacity

A profile must have a private identity attestation produced by
`scripts/acp_account_verify.py`. It verifies the ACP OAuth principal against
the selected CLI account, then stores only hashes and verification metadata.
Admission also compares the configured CLI home with the actual registered
Antigravity harness; an arbitrary operator label is not proof of quota identity.
Stable credential, account or CLI-home changes invalidate the binding. Routine
access-token renewal does not. CLI and ACP use the same scheduler slots and
quota admission for the same logical account.

The audit found the existing QA profile belonged to AGY3, although an earlier
fixture labelled it AGY1. The binding was corrected using the existing authorized
profile, without copying OAuth credentials between accounts.

### Conversation lifecycle

A session identity binds logical owner, account, resolved workspace, scope
fingerprint, canonical Flash model and thinking level. The stable session
reference names both the checkpoint and native conversation directory; each
new turn gets a new delegate/turn ID. An OS lease is acquired before starting
the transport. Parallel use of one session, duplicate turns and mismatched
identity fail closed.

A completed ACP turn is continued through `resume_from_delegate_id`. Omitted
model/thinking and file scope inherit the source. Explicit scope changes may
narrow the assigned files or add exclusions. The hard filesystem boundary
remains the workspace, not each entry in `files_in_scope`.

The client checks advertised session-load/configuration capabilities. History
replayed during `session/load` is excluded from the new answer, usage and tool
budget. Late callbacks cannot modify a finished result.

A pending interaction question is recorded with
`disposition="cancelled_requires_continuation"`: its permission RPC has already
been cancelled. The owner answers in the next task/continuation. There is no
suspended live permission grant and no in-flight steering API.

### Durable recovery and cancellation

The worker survives a main MCP restart. Recovery adopts its original durable
job and ACP parser; disabling new ACP admission does not prevent draining an
existing worker. It never launches CLI as a replacement for an ACP task.

The ledger records `running` before sending a prompt. A lost in-flight worker
or unconfirmed outcome remains fenced as uncertain. There is no automatic
replay or public reconciliation operation yet. Start a separately reviewed
new task after investigating such a failure; do not edit a checkpoint to make
a retry look safe.

Cancellation first requests native ACP cancel and repeats only that control
notification within a bounded grace period until the prompt terminates. The
pinned runtime can lose a notification during native prompt initialization;
no prompt is replayed. Only then is the existing process-signal fallback
permitted. Cancelled and
timed-out results retain their typed status through durable finalization.
Protocol completion, valid JSON and source anchors do not imply independent
task acceptance.

### Isolation

The authenticated runtime and native helper use sibling bubblewrap sandboxes.
The auth side sees only the ACP OAuth subdirectory and an empty workspace
view. It cannot load repository or global hooks, skills or child MCP servers.
OAuth refresh may update the original private profile atomically.

The helper sees the approved workspace read-only, its own conversation files
and private temporary storage. It does not see the OAuth profile, host home,
Opss task store or host process namespace. Its network namespace has no host
or external interfaces. A bounded gateway carries the two fixed runtime
channels; authenticated initialization must name the exact loopback inference
endpoint, not an arbitrary URL. Hooks, child MCP servers and custom subagents
are rejected in that initialization.

Native child processes additionally cannot create AF_UNIX sockets/socketpairs,
use io_uring or borrow file descriptors with pidfd_getfd. This closes pathname
Unix-socket access through a read-only workspace mount. The trusted bootstrap
owns the fixed relay sockets and does not pass them to the native child.
Process inspection, namespace and mount manipulation are denied by seccomp.

These controls are a Linux deployment boundary, not a portable or per-file
sandbox. Native tools cannot use Unix IPC in this backend. Repository files
are still data sent to the configured provider as part of the authorized task;
the approved workspace and assigned investigation scope must be chosen
accordingly.

## Audit findings addressed

| Severity | Defect | Resolution |
| --- | --- | --- |
| P0 | Profile label did not prove the real account; QA used the wrong alias | Remote principal verification, private hash attestation, registered-harness home check |
| P0 | Native helper inherited host networking | Private network namespace and fixed authenticated relays |
| P0 | Unix sockets inside a read-only workspace bypassed network isolation | Native AF_UNIX/io_uring/pidfd gate and host-socket sentinel |
| P0 | Repository/global hooks could run in the authenticated process | Empty auth workspace and minimal ACP-only profile mount |
| P1 | New delegate ID selected a new native conversation directory during resume | Native storage keyed by stable session reference |
| P1 | Cancelled/timed-out tasks lost their status at the generic runner boundary | Typed terminal-status normalization and regression tests |
| P1 | An early cancel could be lost before the native prompt became active | Bounded cancellation notifications until terminal response; no prompt replay |
| P1 | Broker shutdown waited for clients before cancelling their handlers | Listener/handler/transport cleanup order and bounded regression tests |
| P1 | Recovery depended on current ACP admission/configuration | Parser-only adoption of the original durable job |
| P1 | Invalid/empty manifests and malformed optional settings were mishandled | Typed validation, fail-closed results, optional backend isolation |
| P2 | Resume scope and evidence depended on prompt headings | Persisted typed scope and source-evidence assessment |
| P2 | Thinking aliases and omitted continuation options could drift | Canonical level selection and source-option inheritance |

See `ACP_BACKEND_QA.md` for the operator contract, acceptance commands and
the current rollout evidence. Historical slice-1 evidence (1410 repository
tests and low-level new/load/cancel tests) predates these changes and must not
be substituted for acceptance of this implementation.

## Remaining capabilities and their gates

1. **Scoped MCP tool gateway.** Add read/search/Code Map/Git capabilities with
   task-owned tokens and bounded outputs. Do not expose job/delegate spawning,
   credentials, commits or deployment to children.
2. **Live interaction/steering.** Add an owned reply lifecycle with explicit
   expiry, request and turn identity. Current interaction is between terminal
   turns only; do not present it as a suspended RPC.
3. **Uncertain-outcome reconciliation.** Provide a reviewed reconciliation
   operation before allowing replay after a lost in-flight worker.
4. **Native writers.** Require a separate writable worktree, explicit file
   authorization, baseline hashes, independent diff/test acceptance and bounded
   repair. Read-only acceptance does not clear this gate.
5. **Wider rollout.** Verify each additional profile independently, retain
   shared CLI/ACP capacity, and collect real task evidence before considering
   automatic routing. Do not infer reliability or code quality from one fixture.

## Protocol references

Consulted 2026-11-03:

* https://agentclientprotocol.com/protocol/v1/session-setup
* https://agentclientprotocol.com/protocol/v1/prompt-turn
* https://agentclientprotocol.com/protocol/v1/cancellation
* https://agentclientprotocol.github.io/python-sdk/

The protocol does not supply the host filesystem/network sandbox or guarantee
that a completed turn met the parent task's acceptance criteria.
