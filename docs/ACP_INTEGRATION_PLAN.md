# ACP subagents: architecture and implementation gates

Updated: 2026-11-03. Original pre-ACP production baseline: `31274aa`.
Runtime contract: official Antigravity ACP 1.4.5, ACP v1, Python SDK 0.12.1,
WebSockets 17.4. The first deployed slice was durable resumable read-only
research/review. Current code adds native writers with verified promotion,
an authenticated scoped MCP gateway, durable live-question replies, queued
next-turn steering and explicit abandonment. Their task contract is documented
in [acp-subagents.md](acp-subagents.md); deployment and live acceptance evidence
must name the exact candidate revision.

The [current acceptance ledger](ACP_BACKEND_QA.md#final-release-acceptance-2026-11-03)
tracks native question/gateway/continuation, verified writer, reconciliation
and production rollout evidence, preserving the status of every recorded run.

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
Current activation and live acceptance remain limited to the attested AGY3
binding; additional accounts require their own proof and acceptance.

### Conversation lifecycle

A session identity binds logical owner, account, resolved workspace, scope
fingerprint, canonical Flash model and thinking level. The stable session
reference names both the checkpoint and native conversation directory; each
new turn gets a new delegate/turn ID. An OS lease is acquired before starting
the transport. Parallel use of one session, duplicate turns and mismatched
identity fail closed.

Under the same lease, continuation compares the caller's expected previous
turn with the latest persisted turn. If another queued continuation has already
advanced history, `acp_resume_source_stale` prevents a provider connection.
Old delegates cannot rewind or branch the conversation or restore broader scope.

A completed ACP turn is continued through `resume_from_delegate_id`. Omitted
model/thinking and file scope inherit the source. Explicit scope changes may
narrow the assigned files or add exclusions. The hard filesystem boundary
for native exploration remains the workspace. Writer changes are confined to a
private copy and checked against exact authorized files before promotion;
gateway reads separately enforce their file allowlist.

The client checks advertised session-load/configuration capabilities. History
replayed during `session/load` is excluded from the new answer, usage and tool
budget. Late callbacks cannot modify a finished result.

Current interaction questions suspend the original native permission RPC while
an owner-bound durable mailbox awaits one advertised option. Requests bind
turn/request IDs and expiry; replies are consumed once. A main MCP restart can
reattach to the same question while the worker lives. Broad permissions are
never granted. Legacy `cancelled_requires_continuation` records still require
a new turn. Steering is explicitly `queued_next_turn`, not a runtime interrupt.

### Durable recovery and cancellation

The worker survives a main MCP restart. Recovery adopts its original durable
job and ACP parser; disabling new ACP admission does not prevent draining an
existing worker. It never launches CLI as a replacement for an ACP task.

The ledger records `running` before sending a prompt. A lost in-flight worker
or unconfirmed outcome remains fenced as uncertain. There is no automatic
replay. `delegate_reconcile(action="abandon")` requires owner, terminal worker
and session-lease checks, preserves the uncertain evidence and freezes the old
reference. Continue work as a separately reviewed new task without a resume ID;
do not edit a checkpoint to make a retry look safe.

Writers keep that fence between native completion and server verification/
promotion. A successful model response alone cannot create a completed writer
checkpoint. A hard kill during export requires review of the dedicated worktree.

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
view. It cannot load repository or global hooks or skills; only the trusted
worker's fixed task MCP descriptor is admitted.
OAuth refresh may update the original private profile atomically.

The helper sees the approved workspace read-only for exploration or a private
copy for editing, its own conversation files
and private temporary storage. It does not see the OAuth profile, host home,
Opss task store or host process namespace. Its network namespace has no host
or external interfaces. Bounded relays carry the fixed runtime and task-MCP
channels; authenticated initialization must name the exact loopback inference
endpoint and, if present, the worker-approved task gateway. Hooks, arbitrary
child MCP servers and custom subagents are rejected in that initialization.

Native child processes additionally cannot create AF_UNIX sockets/socketpairs,
use io_uring or borrow file descriptors with pidfd_getfd. This closes pathname
Unix-socket access through a read-only workspace mount. The trusted bootstrap
owns the fixed relay sockets and does not pass them to the native child.
Process inspection, namespace and mount manipulation are denied by seccomp.

These controls are a Linux deployment boundary, not a portable or per-file
sandbox. Native tools cannot use Unix IPC in this backend; the separate read-only
verification sandbox permits ordinary local IPC. Repository files
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

## Current acceptance and wider rollout gates

1. **Scoped gateway:** prove the actual native helper can use the bounded
   read/search/Code Map/diff methods while paths, tokens, call limits and arbitrary
   endpoints fail closed. Child job/delegate spawning and credential access remain absent.
2. **Live questions:** observe a real native pending request, restart the test MCP,
   select an offered option and complete the same prompt. Model-written question
   text is insufficient. Steering remains a later explicit turn.
3. **Reconciliation:** prove a live owner cannot be abandoned, an uncertain
   terminal owner can be frozen, and its old reference cannot resume. No raw
   native-history inference may fabricate a successful result.
4. **Native writers:** prove independent isolated verification and exact-file CAS
   promotion in a dedicated linked worktree, plus unchanged source for denied or
   failed candidates. Check the native-completion/promotion crash fence.
5. **Wider rollout:** verify additional profiles independently, retain shared
   CLI/ACP capacity and gather real task evidence before considering automatic
   routing. The isolated verifier currently uses installed system dependencies;
   host virtualenvs and network installation are not an implicit escape hatch.

## Protocol references

Consulted 2026-11-03:

* https://agentclientprotocol.com/protocol/v1/session-setup
* https://agentclientprotocol.com/protocol/v1/prompt-turn
* https://agentclientprotocol.com/protocol/v1/cancellation
* https://agentclientprotocol.github.io/python-sdk/

The protocol does not supply the host filesystem/network sandbox or guarantee
that a completed turn met the parent task's acceptance criteria.
