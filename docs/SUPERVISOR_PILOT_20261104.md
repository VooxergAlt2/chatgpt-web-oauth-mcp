# Supervisor Mode: cooperative checkpoint pilot

## Scope of this release

S1 + S2: a worker publishes a checkpoint, the current logical owner inspects
its report and server observations, and an explicit decision returns to the
same native turn. No second prompt is injected into an active ACP session.
The worker's existing sandbox, scope, account binding and independent verifier
remain authoritative. This release does not add autonomous senior-model API
calls, migrate conversations between accounts, or retain an unfinished private
writer after process death. Those are separate execution-adapter / S3 work.

The existing 900-second ACP execution ceiling and tool-call limits are unchanged.
Checkpoint waiting counts against the native turn's current deadline. The native
MCP tool timeout is extended only by the trusted gateway configuration; ordinary
HTTP reads and tools retain their short limits. A long-lived engineering task
will require persistent candidate workspaces in S3, not silent task replay.

## Implemented components

| Component | Responsibility |
| --- | --- |
| `supervision.py` | Provider-neutral endpoint/binding, durable checkpoint store, revision-checked decisions and async checkpoint channel |
| `acp_supervision.py` | Maps an already validated ACP execution identity into the neutral contract |
| `acp_gateway.py` | Exposes `supervisor_checkpoint` on the task-owned capability; waits for an owner decision |
| `acp_network.py` | Admits only that additional fixed tool and a host-selected tool-wait timeout |
| `acp_operations.py` / `tools_delegate.py` | Derive identity from owned server metadata and expose `delegate_supervisor` |
| `acp_worker.py` | Lifecycle binding, private candidate digest/diff observations, and unresolved-checkpoint promotion guard |
| `routing/replan.py` | Pinned transport failures do not promise automatic provider switching; uncertain outcomes require reconciliation |

## Controller contract

Use `delegate_status` (field `supervision`) or
`delegate_supervisor(delegate_id=..., action="status")` to obtain the current
binding, state and revision. `checkpoint.agent_report` is explicitly labelled
`unverified_agent_claims`. `checkpoint.observations` is constructed by the
trusted worker, not by the model.

For code tasks, observations contain the baseline commit, whole-candidate digest,
changed-file count and a bounded diff excerpt. `diff_truncated=true` means that
the excerpt cannot support an exhaustive review. For read-only tasks, the
observation binds the scope but explicitly does not claim an immutable workspace
snapshot. A supervisor may inspect the original scoped sources separately.

| Action | Effect |
| --- | --- |
| `request_checkpoint` | Record a request; subsequent scoped gateway reads ask the worker to invoke the checkpoint tool |
| `continue` | Release the waiting tool without accepting the implementation |
| `steer` | Release the waiting tool with a required free-text correction |
| `review` | Keep the checkpoint waiting while the owner performs review |
| `escalate` | Keep it waiting while the owner seeks another model or human decision; no automatic new provider call |
| `stop` | Signal cancellation and prevent promotion of the current candidate |

Every mutation requires a unique `command_id` and the observed
`expected_revision`. Decisions additionally require the current `checkpoint_id`.
A repeated identical command is acknowledged idempotently; conflicting reuse,
stale revisions and mismatched checkpoints are rejected. Review/escalation
increment the revision, so the final decision must use a fresh status snapshot.

Example decision (IDs and revision must come from the live status):

```json
{
  "delegate_id": "<owned delegate>",
  "action": "steer",
  "checkpoint_id": "<observed checkpoint id>",
  "expected_revision": 1,
  "command_id": "architecture-correction-1",
  "instruction": "Preserve the public response contract. Fix the canonical implementation; do not introduce a compatibility wrapper."
}
```

The tool returns the correction to the same active native invocation. The
checkpoint decision has `accepted=false` and `scope_changed=false`. Recording
or issuing a decision is not proof that the provider received it. A worker crash
does not authorize automatic replay. Parent acceptance still requires evidence,
and code export still requires the independent verifier.

## Cooperative, not a hidden process freeze

Ask for a specific checkpoint in the task, for example: "After reading the
listed source, call `supervisor_checkpoint` with phase=plan, a short proposed
solution and source anchors. Do not run other tools in parallel with it. Apply
the returned correction, then finish the task."

`request_checkpoint` is not an instantaneous pause. Native actions already in
flight may finish, and native file tools do not pass through the read gateway.
Only a published `paused_for_supervisor` checkpoint proves the cooperative wait
was reached. Code promotion rejects an unresolved checkpoint/request. A
read-only worker can finish before reaching a requested gateway boundary; the
closed record is not a supervisor approval. Candidate changes during a wait
invalidate the observation digest and cancel the waiting execution.

No claim is made that a model's stream of events or growing diff proves useful
progress. Semantic no-progress detection is not part of S1 + S2.

## Additional Gemini accounts and API agents

Supervision separates identity axes:

```text
provider + transport + account_id + model + reasoning
owner + execution_id + session_ref + workspace + scope_digest
```

The new core has no hard-coded model names, account count or provider list.
Tests cover an additional Gemini-style account and a separate simulated API
provider using the same store/channel. This demonstrates the adapter contract,
not a working external API connection or authentication.

The existing ACP scheduler/account registry remains the admission authority.
Its currently supported aliases and OAuth proofs are unchanged; no extra
account is enabled by inventing an `account_id`. A future account must be
registered through that authority, with its own verified credentials, resource
pool and supported capabilities. Existing quota modules are not modified here.

A future API execution adapter must:

1. Resolve an operator-configured endpoint and credential reference. Never put
   raw credentials, URLs or provider secrets in a supervisor checkpoint.
2. Construct `AgentEndpoint` and `SupervisorBinding` from trusted task metadata;
   account and model remain fixed for the provider conversation.
3. Attach `SupervisorChannel.checkpoint` to its own tool loop and expose the
   same owner-checked controller operations/capability declaration.
4. Feed actual baseline/diff/check receipts as observations; model assertions
   stay separately labelled. Reject stale decisions and unknown write outcomes.
5. Preserve the scheduler's account/quota/concurrency rules. A new provider or
   account requires a new execution/conversation plus an explicit handoff.

The controller surface presently dispatches to ACP only. Adding an API worker
requires its execution/controller adapter and live acceptance, not changes to
the neutral checkpoint state machine. Automatic calls to a senior model are
disabled; in this pilot the active ChatGPT controller provides decisions.

## Acceptance record

Focused checks: the initial 66 store/gateway/owner checks passed. Worker/network
checks found an incomplete clock test double (it mocked monotonic time only);
the test double was updated to retain wall time without changing its assertions.
The corrected cases plus new HTTP/owner/network/replan checks passed (6 tests).

Full candidate gate: `job_1793794575509_JxyVGsnt`.
Deployment and native acceptance receipts are recorded after the actual runs.
