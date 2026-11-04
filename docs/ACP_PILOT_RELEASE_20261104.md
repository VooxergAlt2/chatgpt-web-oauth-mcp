# ACP operational pilot release — 2026-11-04

Scope: explicit ACP on the identity-verified `antigravity3` account, the approved
Opss repository and its dedicated linked worktrees. Times in this record are MSK.

## Accepted release

The bounded operational pilot is accepted and active on runtime commit
`f1dda27c534328f62dd49ae5268c98251feb162c`. The composition guard was first
activated at `8ac902e`; the final release adds the native writer's verified
steering-summary implementation. Parent review, focused and full regressions,
service readiness, actual native question/reply, and a post-restart queued
continuation passed. Later documentation commits do not change executable source.

## What the pilot does

- Read a bounded source scope and return source-anchored findings.
- Implement a small behavioral change in an isolated private copy, run the
  declared independent verification, and promote only allowed files after the
  baseline and candidate checks pass.
- Show actual execution phases, native tool count and deadline allocation.
- Suspend an actual native question, accept an exact offered option from its
  owner, and continue the original prompt.
- Retain a conversation for explicit continuation and queue instructions for
  that next turn without replaying an in-flight prompt.
- Preserve the entire queued instruction batch when the composed prompt exceeds
  131072 UTF-8 bytes; report a known pre-send failure that permits an explicit
  shorter continuation.

The new derived `acp_control.steering_summary` reports `queued_count` and
`queued_text_utf8_bytes`, excluding claimed/cancelled records. It counts only
message bodies and publishes no message text. It neither persists new state nor
changes ownership, message claiming or provider-delivery guarantees.

## Practical acceptance

| Check | Observed evidence |
| --- | --- |
| Composition regression baseline | Previously accepted integrated gate `job_1793731536842__Sldwez2`: 2099 passed, one optional skip, five existing warnings and 18 subtests; executable source unchanged through `8ac902e` |
| Composition activation | `job_1793751112905_cLyN5VEm`, exit 0 in 29.817 s; clean fast-forward `759100f` → `8ac902e`; fresh worker-module imports and SHA-256 checks |
| Service after activation | PID `1961926`, active/running, ExecMainStatus 0; authenticated health HTTP 200, matching PID, success true, no observation errors or error-priority journal lines |
| Autonomous behavioral writer | `8cbec5aae60d`, job `job_1793751211662_2VHLdkDL`; Flash High/high, 180.425 s, 20 tools; two scoped files, +54/−1 lines in the original exported receipt |
| Independent writer verification | `ACP_STEERING_SUMMARY_VERIFIED`; isolated system-Python witness checks UTF-8 counting, no content disclosure, no snapshot mutation, overflow preservation and shorter continuation |
| Parent review | Exported file SHA-256 values matched the receipt; compile passed; `tests/test_acp_control.py` and `tests/test_acp_operations.py`: 69 passed in 6.39 s; independent review found no blocker |
| Parent normalization | Removed one extra blank line at test-file EOF after `git diff --check` reported it; no behavior change; final source/test diff +53/−1, and diff check passed |
| Complete candidate gate | `job_1793751455900_ARazjiaq`: 2099 passed, 3 skipped, 5 warnings and 18 subtests in 210.24 s; 241 Python files compiled; executable source unchanged throughout; exit 0 |
| Environment-dependent follow-up | The two checks skipped without `OPSS_ACP_TEST_PYTHON` and `OPSS_ACP_TEST_NATIVE_HELPER` passed against the existing pinned QA interpreter/helper: 2 passed in 0.92 s. The remaining optional in-process SDK smoke is unavailable in the host pytest environment; no dependency was installed |
| Live native question | `c2d8131b2dba`, job `job_1793751302163_mpn-Hm-m`, 43.656 s and three tools; question `interaction_54757a25`, observed option `1`, mailbox consumed with `response_committed`; original prompt succeeded |
| Finding review | Parent read the actual source at lines 204–232 and verified both reported overflow facts; read-only repository audit reported no drift |
| Final runtime activation | `job_1793751765182_jONdVAKM`, exit 0 in 30.868 s; clean fast-forward `8ac902e` → `f1dda27`; source hashes matched the tested candidate; new PID `1985958`, authenticated matching-PID health, no observation errors or error-priority journal lines |
| Public queue after restart | `delegate_status(c2d8131b2dba)` returned `queued_count=2`, `queued_text_utf8_bytes=656`; both stored messages retained their IDs/digests and exposed no text |
| Post-restart continuation | `7d4718447a8c`, job `job_1793751838508_ke8PhkUP`, succeeded in 12.312 s with one native tool; same owner, account, scope, model/thinking, session and provider conversation; both messages claimed for that turn, summary became 0/0, `outcome_uncertain=false` |
| Delivery and content witness | Control marker appeared exactly once in the final structured result and was absent from both task prompt files; source findings independently verified; artifact SHA-256 matched; read workspace remained fully clean |

Native writer source was the clean `8ac902e` baseline. The task supplied a
behavioral contract and independent acceptance checks, not a prepared patch.
Exported source `acp_control.py` SHA-256 is
`e8bdd2481f088b3bd676988aa7aec75b6d0dbed98c02144f34c2ccf9059c4a45`.
After whitespace normalization, `tests/test_acp_control.py` SHA-256 is
`b1f3450a2c94f0d373ef2e76c8cc9764e343cced005d0175ada97104f92bbb71`.

The read session is `e397700a14fec45983eaf19ce0f3a6a8`; its provider conversation
is `df7f130c-c26d-4498-ba95-c954b9dd0437`. The current logical owner fingerprint
is `9938097d78c2`. Identifiers here are evidence, not permission to impersonate
an owner or bypass the public result-access checks.

The latest read turn is `7d4718447a8c`, with structured-result SHA-256
`d8624fe5b5fc3b8acb683835367110f10745632334f4b101ed41312d36fbea5c`.
Its workspace remains at
`/home/vooxerg/worktrees/opss-acp-composition-live-20261103` to permit an explicit
future continuation with the original owner. Its inspected source baseline is
`852cee3`; the executing MCP/worker runtime is the separately accepted
`f1dda27`. Terminal jobs/delegates from this accepted cycle were consumed.

## Operating workflow

Use the `opss` group through the three existing MCP routers and discover the
current exact schema before submitting a task. For a bounded source review:

~~~json
{
  "backend": "acp",
  "harness": "antigravity3",
  "kind": "explore",
  "complexity": "medium",
  "cwd": "/home/vooxerg/src/chatgpt-web-oauth-mcp",
  "task": "Explain how acp_control.py preserves queued instructions on prompt overflow. Return two findings with exact source lines. No edits.",
  "files_in_scope": ["src/chatgpt_web_oauth_mcp/acp_control.py"],
  "max_tool_calls": 8,
  "execution_timeout_seconds": 180,
  "wait_seconds": 0
}
~~~

For code work, create a clean dedicated linked worktree and use `kind="code"`.
Start with one or two exact files, a concrete behavioral contract,
`commit_mode="forbidden"`, explicit line/file limits and independent
`verification_commands`. The successful task used a 420-second total deadline
and 24-tool ceiling. Those are bounded starting settings, not a latency promise.
The worker reserved 105 seconds for verification; the native session received
308.685 seconds after setup and safety margin, and finished earlier.

The isolated verifier has `/usr/bin/python3` and the standard library. Do not
assume the host virtual environment or pytest exists inside that sandbox.
Use checks that can actually run offline there. The parent separately runs the
project's host tests against the exported source, reviews the diff and commits
only after acceptance. The full writer contract remains in
[acp-subagents.md](acp-subagents.md#native-writers).

While a task runs, use `delegate_status` and its advisory `acp_progress`.
An `input_required` question must be answered with the observed request/turn and
option IDs using `delegate_message(action="reply")`. A steering receipt means
`queued_next_turn`; deliver it through a new `delegate_task` naming the latest
terminal `resume_from_delegate_id`. Preserve owner, account, kind, workspace
and model/thinking. Review and consume terminal results explicitly.

## Deployment and recovery

Deployment uses a durable job so the service can restart without killing the
rollout controller. Before changing main, verify the clean expected HEAD,
tested source hashes, fast-forward ancestry, global health activity and the
full durable job registry, including `starting` records. Stop the service,
confirm its listener is closed, and repeat the registry guard before moving Git.
Exclude only the deployment job identified by its PGID and live process identity.
Invalid records or another nonterminal job block the change.

`rollback/acp-prompt-composition-20261103` preserves `759100f`, including the
parallel Revit release. The final release also preserves `8ac902e` as
`rollback/acp-pilot-ready-20261104`. After activation require a new PID, active/running,
ExecMainStatus 0, authenticated health from that same PID, no observation errors,
a journal check and actual live MCP acceptance.

The first activation attempt `job_1793749150027_Nq7ApBAw` rolled back after a
readiness timeout; it remains a failed attempt. The successful diagnostic rollout
observed `PermissionError` when the surviving old job tried to read the new
service's `/proc/<pid>/environ`. Retaining the token already authenticated before
stop, while leaving unit/environment configuration unchanged, allowed the normal
authenticated health check to succeed. No token value is logged or persisted
in this document and no process-access or health-authentication guard is weakened.

On readiness failure, stop again and repeat the complete registry guard before
resetting only the rollout's own clean target. If newly admitted work or a
changed HEAD prevents safe rollback, restore service availability on the
remaining version and report the deferred rollback. A lost MCP reply alone
does not establish that a durable job stopped.

## Limits and next work

- This is one-account, bounded-task acceptance. CLI remains the default; other
  accounts/projects need their own identity and deployment gates.
- Earlier behavioral timeouts and failed/deferred rollout attempts remain failed.
  The earlier writer `22bf9087a45f` produced no exported repository diff; its
  terminal result became inaccessible after the logical MCP owner changed and
  is not accepted as a successful task.
- The current owner successfully retrieved durable rollouts and continued its
  provider conversation with preserved queued messages after service restart.
  That does not solve an upstream change of `x-openai-session`.
  `session_resume` restores only the currently authenticated logical owner.
  It does not transfer ownership. Previous-owner results were not read through
  private files, replayed, claimed or made public.
- Queue claiming is not a provider acknowledgment. Uncertain outcomes require
  the existing explicit reconciliation/abandonment workflow, with no automatic
  prompt replay. Existing cancellation/worker-loss acceptance is retained from
  the earlier pilot ledger; it was not repeated or relabeled in this release.
- One successful two-file behavioral task does not establish a general success
  rate or larger-task SLA. Provider usage counters remain qualified observations;
  do not sum resumed counters or infer costs from them.

The next development step is a short series of real small maintenance tasks
within this accepted boundary, recording verified outcome, elapsed time and tool
use. Improve the dominant observed failure before increasing task scope or
opening more accounts. The current pilot does not need recursive agents,
automatic retries or an ACP protocol migration to serve that workflow.

