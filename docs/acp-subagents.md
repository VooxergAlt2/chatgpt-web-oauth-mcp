# ACP subagents: task, control and writer contract

Updated: 2026-11-04.

ACP is an explicit execution transport for an existing Antigravity account.
Opss keeps ownership of scheduling, account admission, durable jobs, cancellation,
artifacts and verification. The authenticated ACP runtime owns the provider
conversation; a separate sandboxed native helper performs task work.

This guide describes the implemented contract. Deployment evidence is recorded
separately in [ACP_BACKEND_QA.md](ACP_BACKEND_QA.md); its earlier read-only
acceptance record does not establish acceptance of a later writer, gateway or
live-question change. Current operational evidence and the bounded pilot workflow
are in [ACP_PILOT_RELEASE_20261104.md](ACP_PILOT_RELEASE_20261104.md).

## Availability and admission

Discover the account's `execution_backends.acp` entry through
`delegate_harnesses` or `server_info`. A configured account must have a verified
ACP/CLI identity binding, a permitted workspace and the Linux sandbox runtime.
The configuration and identity-attestation procedure is in
[ACP_BACKEND_QA.md](ACP_BACKEND_QA.md#operator-configuration-and-identity-verification).

The current activation and native acceptance scope is the attested
`antigravity3` account only. Other account labels do not inherit that proof or
acceptance; each additional account requires its own binding and rollout gate.

CLI remains the default. Submit `backend="acp"` and pin `harness` explicitly;
the examples use `antigravity3`. ACP and CLI share that account's scheduler
slots and quota gate. ACP does not introduce another account pool or silently
switch transport, account or model.

Supported models are `gemini-3.8-flash` and its `-low`, `-medium` and
`-high` thinking aliases. With `complexity="low"`, the default thinking is medium.
ACP `complexity="medium"` and `"high"` keep the explicit account and select Flash
High; lower model/thinking overrides are rejected before dispatch. A continuation
retains its original model/thinking: continue a lower-thinking session with
`complexity="low"`, or start a fresh High task. CLI admission remains unchanged.
An execution deadline is
bounded to 15–900 seconds for exploration and 30–900 seconds for a writer.
An explicit tool budget is 1–64. If omitted, ACP supplies 24 for low complexity
and 48 for medium/high, including code tasks. Omission does not disable the cap.
Custom output schemas and batch/group plans are not part of this transport.
The generic `long_running` flag does not extend ACP's maximum; if supplied,
override its eight-hour default with an explicit timeout within the ACP bounds.

The deadline covers setup, native work, verification and export. A writer reserves
`min(300, max(8, timeout / 4))` seconds for server-owned verification and export;
native work receives the remainder after startup and the worker's safety margin.
For example, a 360-second writer does not get 360 seconds of model execution.
Native shell commands are unavailable. Use native file tools and the scoped
gateway; only the server runs the declared verification commands.

The tools below are entries in the `opss` group, invoked through the existing
three MCP routers. The current lean group has 56 private Opss tools; the public
exclusive endpoint still exposes only the three routers. Use catalog discovery
for their live schemas.

## Fixed native tool profiles

Opss selects a fixed, versioned native built-in profile from the task role.
Exploration offers `view_file` and `finish`; code adds `create_file` and
`edit_file`. `ask_question` is included only when owner control is attached.
The scoped MCP gateway remains separate. Sandbox confinement and server-owned
verification still apply.

The profile is bound durably before connecting to the provider. Both
`session/new` and `session/load` send the same bound allowlist. Changing the
role, question capability, profile version or tool set requires a new session.
An older provider conversation without that binding fails with
`acp_tool_profile_legacy_requires_new_session`; start a new task without
`resume_from_delegate_id`. Uncertain turns are never replayed to migrate them.

`harness_metadata.native_tool_profile` records the requested profile.
When available, `harness_metadata.native_tool_configuration` records the
latest bounded observation of helper configuration after it was forwarded,
with source `forwarded_native_initialization`. Its fixed tool flags are
`true`, `false` or `null`; `null` means unknown. `finish` has no observable
configuration flag. This observation covers the forwarded helper configuration;
it does not establish the provider's complete tool inventory or an efficiency gain.

## Read-only investigation

Example arguments for `delegate_task`:

~~~json
{
  "backend": "acp",
  "harness": "antigravity3",
  "kind": "explore",
  "cwd": "/operator/approved-repository",
  "task": "Inspect src/parser.py and report the boundary-condition defect with file:line evidence.",
  "files_in_scope": ["src/parser.py"],
  "max_tool_calls": 8,
  "execution_timeout_seconds": 120,
  "wait_seconds": 0
}
~~~

The approved workspace is mounted read-only for native tools. Assignment
scope and evidence checking use `files_in_scope` and `out_of_scope`; this
native mount is a workspace boundary. The scoped MCP gateway separately
enforces its file allowlist on gateway reads.

Use `delegate_status`, `await_delegate` and `mark_result_consumed` normally.
Read the structured-result artifact and its evidence before accepting a task.
A valid manifest, a verified artifact hash and an existing source anchor are
different checks from determining that the agent's conclusion is correct.

## Source navigation for code tasks

Before native execution, a code task may receive advisory Python symbol
locations from its prepared private baseline. The recorded revision is the
checked execution baseline commit, even when the submitted task mentions an
older revision. Candidates come from explicit `files_in_scope` after applying
`out_of_scope` exclusions. Hints contain quoted symbol locations and do not
expand the task's scope or replace source inspection before citing or editing.

The worker prepends at most 8192 UTF-8 bytes of navigation text, including its
header and footer. A prefix is added only when it and the original task prompt
together fit within 131072 UTF-8 bytes. The original prompt remains an unchanged
suffix at this augmentation step. The deployed composition guard includes queued
steering in the [final composed prompt limit](#prompt-size-and-queued-delivery).
The separate 8192-byte prefix budget does not reserve space for that queue.

Navigation is best effort: missing or invalid inputs, no usable hints,
insufficient prefix space or ordinary generation errors leave the original
prompt unchanged. Its elapsed time is charged to worker setup and reduces the
remaining native session allocation.

When available, optional `harness_metadata.source_navigation` contains:

| Field | Meaning |
| --- | --- |
| `version` | Schema version `1`. |
| `source` | `"private_baseline"`. |
| `revision` | Execution baseline commit used to generate the hints. |
| `hint_count` | Number of attached symbol hints. |
| `hint_bytes` | UTF-8 bytes of the complete prefix, including header and footer. |
| `advisory` | `true`. |

Source reads are bounded independently of file metadata. After an optional
early rejection by `stat` size, the reader opens a file in binary mode and
reads at most `N + 1` bytes, where `N = 800000`. It rejects data larger than
`N` before UTF-8 decoding or parsing, so stale size metadata or file growth
cannot trigger an unbounded source read. Invalid UTF-8 files are skipped.

## Runtime progress and budget

`delegate_status.acp_progress` is an advisory view of the current ACP phase.
While a task runs, `runtime` identifies the session or worker phase and its
elapsed time; `seconds_since_progress` measures the age of the last observation.
Progress comes from actual phase transitions and accepted native events, without
periodic heartbeats. Quiet time alone does not establish that the model is stuck.

`execution_budget` reports the requested lifetime, setup time, worker safety
margin, native session allocation, cancellation grace and verification reserve.
The native allocation includes connection, configuration, prompt and teardown;
the cancellation grace is inside that allocation. The verification reserve does
not extend the outer task deadline.

Terminal views use `source="final_metadata"` and contain sanitized session and
worker diagnostics with phase durations and the original `failure_phase`.
Legacy tasks may report `available=false`. Available provider usage is retained
even on failed or cancelled turns with `usage_scope="provider_reported_unspecified"`;
missing usage is unknown, and counters must not be summed across resumed turns.
These diagnostics do not replace the terminal result, independent verification
receipt or parent review.

The next development stages and their acceptance gates are recorded in
[ACP_DEVELOPMENT_PLAN_20261103.md](ACP_DEVELOPMENT_PLAN_20261103.md).

## Continuing a conversation

Submit a new `delegate_task` with `resume_from_delegate_id` referencing the
terminal source turn. Keep the logical MCP owner, account, workspace and task
kind. Omitted model, thinking and scope inherit the source. An explicit scope
change may narrow the assigned files or add exclusions.

Each follow-up gets a new delegate/turn ID. Its stable ACP session reference
and provider conversation are retained. Native history is stored by session
reference, rather than by delegate ID. Replayed history is excluded from the
new answer and tool budget.

A resumed writer must also satisfy current writer admission, including a clean
dedicated worktree. A previously promoted dirty worktree is not implicitly
approved as the next baseline. Review and explicitly commit or otherwise
resolve it before starting another writer under a newly checked baseline.

One OS lease protects a session. Concurrent turns, reused turn IDs, changed
owner bindings and uncertain or abandoned sessions are rejected.

A continuation must name the latest completed source turn when it acquires
that lease. Two queued continuations can both refer to the same source, but
after one advances the conversation the other fails with
`acp_resume_source_stale` before connecting to the provider. Select the latest
eligible delegate explicitly; an older source cannot branch or rewind native
history or restore an earlier, broader assignment scope.

## Answering a live native question

A native `ask_question` call can suspend its original prompt while waiting
for the owner. This uses the actual ACP `request_permission` exchange with
the pinned runtime's question shape; it is not a synthetic new prompt.

Read `delegate_status`. The delegate snapshot contains `acp_control`:

~~~json
{
  "available": true,
  "turn_id": "0123456789ab",
  "turn_state": "active",
  "pending_request": {
    "kind": "question",
    "turn_id": "0123456789ab",
    "request_id": "interaction_12345678",
    "title": "Which case should be investigated?",
    "options": [
      {"id": "first", "name": "First case", "kind": "allow_once"},
      {"id": "second", "name": "Second case", "kind": "allow_once"}
    ],
    "expires_at": 1793700000,
    "status": "pending",
    "disposition": "awaiting_reply"
  }
}
~~~

Copy the observed IDs and select exactly one advertised `option_id`:

~~~json
{
  "delegate_id": "0123456789ab",
  "action": "reply",
  "turn_id": "0123456789ab",
  "request_id": "interaction_12345678",
  "option_id": "second"
}
~~~

Send these arguments to `delegate_message`. Arbitrary answer text, invented
option IDs and multiple selections are not supported by this native exchange.
An offered `reject_once` answer is also selected by its exact ID.

The response `delivery="recorded"` acknowledges the durable mailbox write.
The worker later marks the request `consumed` with
`disposition="response_committed"` before returning that one option to the
SDK. These statuses do not independently prove that the whole task succeeded.
The original prompt's terminal result remains the completion evidence.

Requests expire within the remaining task budget, with a maximum question
lifetime of 300 seconds. The deadline is an epoch timestamp. Duplicate replies,
expired requests, wrong turns, wrong owners, dead workers and closed native
turns are rejected. A fresh MCP controller can read and answer the same mailbox
after the main MCP server restarts while its durable worker remains alive.

Reply handling is at most once. If the worker dies after consuming a response
but before sending it to the runtime, the system retains an uncertain outcome;
it does not resend the prompt or claim guaranteed network delivery.

Question answers do not grant general tool permissions. Broad
`allow_always` grants are never used. A non-question ACP permission callback
remains denied for exploration. A verified writer may receive only the exact advertised
single-use `allow` option under its worker-supplied confinement policy.
Question-shaped requests still require an explicit owner reply.

Older turns without a mailbox report `acp_control.available=false`. A legacy
`cancelled_requires_continuation` record describes an already cancelled RPC;
answer it in a new explicit continuation.

## Steering is queued for the next turn

Use `delegate_message(action="steer")` to record an instruction for the next
explicit continuation of this session:

~~~json
{
  "delegate_id": "0123456789ab",
  "action": "steer",
  "message_id": "review-timeout-branch-1",
  "text": "On the next turn, also inspect the timeout cleanup branch."
}
~~~

The stable `message_id` is required for duplicate detection. The receipt says
`delivery="queued_next_turn"`; this operation does not interrupt or alter
an in-flight prompt. Submit the next `delegate_task` using
`resume_from_delegate_id` to deliver the queue.

A session permits at most eight queued messages, each at most 8192 UTF-8 bytes,
with a bounded record of prior message IDs. Queued text does not widen the
server-approved file or tool scope.

The control snapshot includes a derived `steering_summary`:

~~~json
{"queued_count": 2, "queued_text_utf8_bytes": 656}
~~~

Both fields are integers and count only messages whose status is `queued`.
The byte count covers UTF-8 message bodies, excluding separators, the instruction
heading, the task prompt and conversation history. Claimed/cancelled messages do
not contribute; an empty queue reports both values as zero. The summary is
computed under the mailbox lock, is not persisted, and exposes no message text.
It is queue visibility, not a guarantee that a future composed prompt will fit.

### Prompt size and queued delivery

The composition guard was activated on 2026-11-04 at `8ac902e`, preserving the
parallel Revit release. The original design and regression evidence remain in
[the composition record](ACP_PROMPT_COMPOSITION_20261103.md); current deployment
and live acceptance are in [the pilot release](ACP_PILOT_RELEASE_20261104.md).

The final ACP text prompt is limited to 131072 UTF-8 bytes. This includes the
complete task prompt, any navigation prefix, the queued-instruction heading,
all message texts and their separators. The limit measures this text content;
SDK JSON encoding, stored conversation history and provider-added system or
hidden context have separate sizes.

Under one mailbox lock, the worker composes all queued messages in their
recorded order and checks the complete UTF-8 size before claiming any message.
If the composition fits, it claims the entire queue for the new turn. There is
no partial batch or truncation of the task or messages.

An oversized queued composition fails with `acp_prompt_composition_too_large`
before the provider prompt is sent. Public delegate metadata reports
`harness_metadata.outcome_uncertain=false`. The raw worker/session result
additionally records `prompt_sent=false`; this field is not part of public
delegate metadata. The failed turn is recorded in both the session and mailbox;
every queued message and its ID, text, digest and order remain intact.
Submit an explicit, shorter continuation with `resume_from_delegate_id` naming
this latest failed delegate. The existing messages remain queued, so they do
not need to be submitted again. Opss does not automatically retry the attempt.

A claimed message records its target turn and retains its content digest.
`delivery="claimed_for_turn"` acknowledges the local claim, not delivery to the
provider. Claimed messages are not automatically replayed after a crash; the
existing uncertain-outcome and reconciliation rules still apply.

## Supervision within the current turn

Scoped ACP tasks now expose the cooperative `supervisor_checkpoint` gateway
tool. The model can report its proposed approach and wait for the owning
controller's decision. `delegate_supervisor` inspects the checkpoint and provides
revision-checked `continue`, free-text `steer`, `review`, `escalate` or `stop`.
Unlike `delegate_message(action="steer")`, a checkpoint correction returns in
the same active native turn. `review` and `escalate` keep the checkpoint paused;
they do not call another model automatically.

The checkpoint separates unverified agent claims from worker-observed baseline,
candidate digest and bounded diff. It neither expands scope nor accepts code.
`request_checkpoint` is cooperative, not an immediate process freeze. The
current execution deadline still applies while waiting; unfinished workspace
retention across worker death is not implemented by this release.

See [Supervisor Mode pilot](SUPERVISOR_PILOT_20261104.md) for exact controller
semantics, acceptance evidence and the provider/account/API extension contract.

## Native writers

An ACP writer is admitted only with:

- A clean, registered linked Git worktree dedicated to this task. The repository's
  main worktree is excluded.
- An explicit, nonempty `files_in_scope` list of at most 256 exact relative
  file targets. Directory grants, glob patterns, traversal paths, backslashes
  and Git control paths are rejected.
- Nonempty `verification_commands`, at most 16 commands, supplied by the parent.
- `commit_mode="forbidden"`, an explicit bounded task and a deadline of at least
  30 seconds.

Clean means that the complete source file manifest matches the committed tree,
including ignored files. Test caches, bytecode and local environments can fail
this check even when Git reports clean. Use a fresh dedicated worktree for a
writer after running tests or compilation in a separate validation worktree.

Example arguments:

~~~json
{
  "backend": "acp",
  "harness": "antigravity3",
  "kind": "code",
  "complexity": "medium",
  "cwd": "/operator/approved-worktrees/parser-fix",
  "task": "Fix the empty-input branch and add its regression. Do not execute the declared acceptance command.",
  "files_in_scope": ["src/parser.py", "tests/test_parser.py"],
  "verification_commands": [
    "/usr/bin/python3 -B -m unittest discover -s tests -p test_parser.py"
  ],
  "acceptance_criteria": [
    "Empty input returns the documented empty result.",
    "Existing nonempty cases retain their behavior."
  ],
  "commit_mode": "forbidden",
  "max_tool_calls": 64,
  "execution_timeout_seconds": 900,
  "wait_seconds": 0
}
~~~

Scope a writer by one coherent behavior and the exact affected files, rather
than a fixed file count. Include navigation, implementation and regression work
in its tool budget. The example uses the current maximum execution envelope;
inspect actual phase budgets and progress rather than assuming all 900 seconds
belong to the native prompt. A limit is not a completion guarantee. The
[2026-11-04 follow-up](ACP_SESSION_RESUME_20261104.md) records timeout/tool-budget
failures and the resulting operating policy.

The native helper edits a private working copy. It does not write directly to
the caller's worktree. This copy contains the repository's regular tracked
files and is writable inside the helper sandbox; the exact file allowlist
is enforced during verified promotion to the caller's worktree.

Admission rejects untracked or ignored files, symlinks, hardlinks, submodules
and special files. Raw checkout bytes must agree with the recorded Git
baseline, so transformed checkouts such as filtered or differing-EOL files
are not silently normalized. Copy limits are 50,000 files, 16 MiB per file and
256 MiB in total.

After native completion, the server checks the proposed changes against the
explicit scope and immutable baseline. It runs the declared verification
commands independently with the proposed copy mounted read-only. The verifier
has private temporary/home directories and installed system tools; it receives
no host virtualenv, `.env`, Git control directory, OAuth profile or Opss state.
Network dependency installation is unavailable. Tests must use already available
system dependencies and direct their generated output to temporary storage.

Passing verification permits compare-and-swap promotion of only the authorized
changes, provided the source worktree still matches the original baseline.
Cancellation, failed verification, scope violations or concurrent source
changes prevent promotion.

The native successful response is not itself a promotion receipt. The session
stays frozen with `acp_writer_postprocessing_incomplete` until the server records
the verification/promotion decision. This closes the crash window between
native completion and host finalization.

Promotion is atomic per file and rolls back catchable export failures. It is
not a global filesystem transaction across multiple files. A hard kill during
multi-file export can leave partial changes and an uncertain checkpoint.
Inspect the dedicated worktree and recorded evidence before deciding how to
recover; an abandon operation does not roll those files back.

## Scoped MCP access from the native helper

The worker provides a task-owned read-only MCP gateway with these capabilities:

| Tools | Purpose |
| --- | --- |
| `read_text`, `search` | Bounded source reads with file/line evidence |
| `code_map_symbols`, `code_map_references`, `code_map_imports` | Lightweight source navigation |
| `git_status`, `git_diff` | Scoped workspace/baseline evidence |

The gateway is authenticated with an ephemeral task token, fixed to the approved
workspace and file scope, and revoked during teardown. Path validation, output
limits and call budgets apply. Writer diff evidence compares the private working
copy to its immutable baseline; it does not authorize host Git mutation.

The gateway has no write, shell, job, delegate, commit, credential or deployment
methods. It is not a tunnel to arbitrary MCP servers. The worker supplies its
server descriptor; public task arguments cannot replace its URL or token.
The runner requires the runtime's advertised HTTP MCP capability.

After validating that exact server descriptor, the trusted initialization relay
adds native static permission rules for these seven tools on that gateway only.
Rules match the runtime's actual server and tool identity; they do not authorize
a display title or a tool argument that claims to be an MCP call. Other ACP
permission callbacks remain denied for exploration, and native shell or file
mutation does not acquire permission through a gateway-shaped argument.
Existing applicable deny rules and explicit gateway ask rules fail closed;
the seven rules override only the ordinary global permission fallback.
Native questions still require an explicit owner reply.

Native processes remain in a private network namespace. A trusted relay allows
only the fixed inference and approved task-gateway channels. Native AF_UNIX
creation, process inspection and namespace/mount manipulation remain blocked.
The verifier has a separate policy that permits ordinary local Unix IPC, such
as Python asyncio, while retaining its read-only filesystem and isolated network.

## Cancellation, restart and uncertain outcomes

A main MCP restart adopts the same durable worker/job and parser. It does not
start a replacement prompt. Explicit cancellation sends native ACP cancellation
first and uses the bounded process fallback only when necessary.

Verified writer promotion checks cooperative cancellation while staging, between
file replacements and after final source validation. An interrupted export rolls
back files still matching its own output. The worker waits for that rollback
before deleting the private copy. The final successful cancellation check is the
commit point; cancellation arriving after it does not undo a completed export.
Hard process termination during multi-file export still requires inspection of
the dedicated source and uncertain checkpoint.

The pinned runtime can drop a cancellation received before native prompt
initialization. The runner repeats only the control notification every 250 ms
within its existing grace period and total deadline; it never replays the
prompt. A terminal prompt response is required to confirm cancellation.

An unconfirmed cancellation, transport loss, unfinished tool or interrupted
writer finalization leaves the old session frozen. Status and preserved
artifacts are evidence for review, not permission to assume success.

After the worker is terminal and the owner has inspected the result and files,
explicitly abandon the uncertain session:

~~~json
{
  "delegate_id": "0123456789ab",
  "action": "abandon",
  "reason": "Reviewed the failed worker, preserved artifacts and inspected the dedicated worktree. Start a fresh reviewed task."
}
~~~

Send these arguments to `delegate_reconcile`. It requires matching ownership,
a terminal failed/cancelled/unknown delegate, a terminal durable worker and
an available session lease. A successful response reports
`outcome_uncertain=true`, `resume_allowed=false` and
`requires_new_session=true`.

Abandonment preserves the previous result and native history and permanently
freezes the old reference in the authoritative checkpoint. The controller also
closes the control mailbox. If the receipt reports `mailbox_closed=false`, that
secondary write failed; the checkpoint still forbids replay and continuation.
Start new work without `resume_from_delegate_id`. There is no automatic replay,
fabricated success, history-to-result inference or implicit rollback.

## Validation and release evidence

Core regressions cover concurrent reply submission from separate processes,
controller recreation, request expiry, wrong-owner/turn rejection, cancelled
question waits, steering delivery, dead-worker fencing and explicit abandonment.
Registered public-tool tests check the real FastMCP schema and controller
dispatch. Writer and gateway tests cover the independent isolation boundaries.
Resume regressions also serialize two queued continuations of one source and
verify that only the first can advance history. Gateway authorization tests
reject forged MCP display metadata and preserve native policy restrictions.

A live acceptance gate must observe actual native behavior: a real pending
question before replying, the same durable job across controller restart,
native continuation without a second prompt, verified writer promotion and
unchanged source on rejected/failed changes. A model describing a question or
printing code in an answer is not evidence for those operations.

Keep the complete repository test result, static checks and live acceptance
records with the exact candidate revision. The earlier read-only release record
remains useful historical evidence and must not be relabelled as a writer or
live-question acceptance run.

The [current acceptance ledger](ACP_BACKEND_QA.md#final-release-acceptance-2026-11-03)
records each observed native phase, the full-suite result, optional SDK coverage
and production rollout evidence. Failed exploratory runs retain their actual
status, with only their explicitly completed phases counted toward acceptance.
