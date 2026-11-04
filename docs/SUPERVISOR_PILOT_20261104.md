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

The first full gate (`job_1793794575509_JxyVGsnt`) reported 3 failures and
2129 passes. Two assertions still expected 56 private tools instead of 57;
the third required descriptions for every new MCP argument. The assertions
were updated to the intentional new tool count, explicit supervisor presence
was added, and all parameter descriptions were supplied. The three affected
tests then passed in 7.51 seconds.

Final gate: `job_1793794978392_sUhX87OK`, exit 0. **2132 passed, 1 skipped,
18 subtests passed**, five existing dependency/fork warnings, 212.66 seconds
of pytest. All 245 Python files compiled, source hashes remained unchanged
during the gate, and `git diff --check` passed. The retained manifest digest is
`4b6620c8fde4cd340ac3ba807332e4a79762f70d332e6e67900b26997bc99c4d`.

## Deployed runtime

Runtime commit: `d4bfc40dfc6d94e21fd20251eb6f2a927de01240`.
Guarded deployment: `job_1793795250704_Q1EP_uCx`, exit 0 in 27.657 seconds.
The source manifest matched the tested candidate before and after fast-forward.
Service PID changed from `2536683` to `2658953`; active/running,
ExecMainStatus=0, authenticated health from the new PID succeeded, observation
errors were empty, and the error-priority journal had no new records.
Rollback: `rollback/supervisor-pilot-20261104` points to `4d90edd`.

The two transient connector 502s occurred during restart. The durable rollout
completed and its result was retrieved and consumed normally. The live router
advertised `delegate_supervisor` after restart; no client catalog assumption
was used as proof of availability.

## Native acceptance on the deployed service

Both tasks used the pinned identity-verified `antigravity3` account, ACP,
Flash High/high, with no provider fallback and no uncertain outcome.

### Read/review/correct, same native turn

Delegate `d19ee829fcc7`, durable job `job_1793795349761_LuOMfgNO`:
98.401 seconds, two native tool calls (one source read and one checkpoint).
Session `e349a47b6d0db4747525fecb47810db4`, provider conversation
`3248a614-6c3c-4e34-8b83-f6454afc66f1`.

The model read `routing/replan.py`, published its actual proposed answer, and
waited at checkpoint `7d325ad49db0cc6019de2d9efb5a59ff`. The owner put it into
`review_requested`, then sent a free-text `steer` against the fresh revision.
The final answer distinguished uncertainty reconciliation from known pinned
timeout reassessment and included the supervisor-provided delivery marker
exactly once. The marker was absent from the original task prompt. There was
one native invocation and no next-turn steering message. Parent source review
confirmed the answer, independently of the anchor-existence check.

Structured result SHA-256:
`aae27b6a6a1557a377d292d69e091164ffc8803f9ce614f8507a4c64650bf87b`.
The first operator witness command read the result envelope as if it were the
inner payload and failed with KeyError; inspecting the envelope and reading
`structured_output` corrected that witness without rerunning the native task.

### Draft/inspect/correct/verify/export, same private writer

Delegate `7877666ed6dd`, durable job `job_1793795634652_2JS_WH9M`:
189.360 seconds, 11 native tool calls, one supervisor checkpoint.
Session `ed042ae18db0e6fa321a6a5aec72791c`, provider conversation
`bf106f8f-5669-4fcc-9c2c-5c1d8c88623f`.

The model created an operator-guide draft in its private workspace, then
published checkpoint `db5f3b59740250c1432deebce86fbcc1`. Worker observations
contained the exact baseline commit, candidate digest, one changed file and
the complete 17-line draft diff. While paused, the caller's worktree was still
clean and the new document did not exist there.

Parent review found overclaims in the draft: review was described as starting
a reviewer, and versioned decisions were called tamper-evident. The owner sent
corrected wording through the checkpoint, clarifying the cooperative pause,
explicit revision/command IDs and separate implementation acceptance. The same
native turn edited the private draft and finished. Independent offline SHA-256
verification passed, and only then was the authorized file promoted.

Actual exported bytes were independently compared to the parent-held expected
document outside the worker scope: one file, 13 added lines, no deletions,
823 bytes, SHA-256
`62546d5468d5b6eb2140be136955b9124b9200b006122d9a598a681442b4007c`.
The accepted [operator quickstart](SUPERVISOR_QUICKSTART.md) is retained in the
repository. The final document wording was delivered through the checkpoint;
the initial task/verifier supplied only its expected hash, not the wording.

These are functional acceptance cases, not a controlled benchmark or proof of
a general task-completion rate. Durations include waiting for the supervising
controller. Cancellation, stale revisions, concurrent decisions, account/scope
isolation, unchanged snapshot requirements and the simulated API adapter
contract were checked in tests; no extra real API account was enabled.

All jobs and delegates created in this cycle were explicitly consumed. The
post-runtime commits update documentation only; no executable changes were made
after the accepted gate and native checks.
