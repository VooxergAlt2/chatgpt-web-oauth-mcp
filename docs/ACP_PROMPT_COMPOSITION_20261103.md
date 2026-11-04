# ACP atomic prompt composition and queued steering

2026-11-04 update: the guard is deployed at `8ac902e`; guarded activation job
`job_1793751112905_cLyN5VEm` passed with fresh imports, a new service PID and
authenticated health. [ACP_PILOT_RELEASE_20261104.md](ACP_PILOT_RELEASE_20261104.md)
is the current release record. The sections below retain the original design,
failed/deferred attempts and the historical 2026-11-03 checkpoint.

Date: 2026-11-03. Stage 3a of
[ACP_DEVELOPMENT_PLAN_20261103.md](ACP_DEVELOPMENT_PLAN_20261103.md).
Baseline: accepted navigation runtime `dac731b`, repository/documentation
`0257683b5417008bd795a98952d9156766c70cf8`.
Implementation candidate: `852cee30e25180cc9c3136178d689b9e05e03a45`.
Integrated tested tree: `1ad6574928ee8cf5104a005e42c0912f94bd7477`.
Release candidate including parallel documentation:
`84a1b097ed43b083bf389b14a0093f278e319f8c`.
Release status is recorded below; a source commit alone is not deployment evidence.

## Problem and reproduced baseline

`AcpSessionRunner.run` validated the original prompt against 131072 UTF-8 bytes,
then `AcpControlStore.begin_turn` claimed queued steering before the runner
appended its heading, separators and message text. The complete prompt was not
checked. A later check after claiming would risk consuming an instruction that
had never been sent.

The parent reproduced this on unchanged baseline `0257683`, using the actual
runner and control store with an in-process FakeAgent and temporary private state.
A valid 130966-byte input plus queued Unicode steering reached the fake provider
as 131073 bytes, one byte above the limit. `prompt_sent=true`; the message was
already `claimed`. The witness printed `ACP_COMPOSITION_BASELINE_GAP_REPRODUCED`.
No real provider was called and the temporary state was deleted.

## Accepted implementation contract

`AcpControlStore.begin_prompt_turn(reference, identity, *, turn_id, prompt,
max_prompt_bytes=131072)` returns `(composed_prompt, claimed_messages)`.
The original `begin_turn` signature and result remain available through the
same internal `_begin_turn` transition.

Within one existing short mailbox lease, the new operation:

1. Validates the session identity, frozen-state rule and new turn identifier.
2. Selects every queued message in its existing order.
3. Composes the exact original prompt, steering heading, separators and all
   selected text, then measures the complete UTF-8 bytes before any claim.
4. If the text fits, claims the complete selected batch for this turn and
   returns the composed prompt and message IDs/text.
5. If the text exceeds the limit, claims nothing and returns `(None, [])`.
   Every message record, including text, hash, ID, source turn, timestamp,
   order, status and delivery field, remains unchanged.
6. In either case, cancels a previous pending/replied question consistently and
   records the current active turn before the caller performs finalization.

The operation neither truncates instructions nor selects a partial batch.
A message enqueued after the composition lease belongs to a later turn.
Direct callers must supply a nonblank UTF-8 string and an exact integer limit
between 1 and 131072; malformed arguments do not mutate the mailbox.

The size boundary is the text passed to ACP `session/prompt`, including any
navigation prefix, the original task and queued instructions. It is not a bound
on serialized SDK JSON, protocol framing, provider conversation history, hidden
system context or token usage. Navigation retains its separate 8192-byte prefix
ceiling; its earlier worker-stage limit alone did not cover later steering.

## Known failure and explicit continuation

The runner calls the new operation after recording its running checkpoint.
After a successful mailbox transition it marks control as started. A `None`
prompt raises `acp_prompt_composition_too_large` before `client.live`, `sent=True`
or the provider prompt call. Existing finalization records in the raw
worker/session result and private state:

- `status=failed`, `success=false`;
- `prompt_sent=false`, `outcome_uncertain=false`;
- matching current failed turns in the session checkpoint and mailbox;
- no claimed steering IDs and the complete preserved queue.

The public delegate view includes `harness_metadata.outcome_uncertain`.
`prompt_sent` is retained in the raw worker/session result and is not a public
delegate metadata field.

Native connection/session loading/configuration can already have happened.
The guarantee is that the oversized prompt is not sent. The worker preserves
the typed failure and does not run writer verification or promotion on it.

The owner may explicitly continue from the latest failed delegate with a shorter
task. Public continuation admission retains owner/account/project/kind/scope and
model identity. The session lease rejects an older source through
`expected_previous_turn`; stale steering/replies cannot target the new turn.
The shorter turn claims the saved queue once, in order. No automatic retry is
introduced, and a known unsent overflow does not require uncertain-session
abandonment.

A successful claim remains an at-most-once local reservation, not a provider
delivery acknowledgement. A later transport failure retains the existing
uncertain checkpoint/frozen control state and does not requeue claimed messages.
A crash between persisted transitions still requires the existing conservative
review/reconciliation path. This change does not alter multi-file export atomicity.

## Independent source and lifecycle review

The change is limited to `acp_control.py`, `acp_session.py` and their control,
session and operations regressions. The parent reviewed all changed source and
tests. A separate reviewer checked the real public control/status path,
`ExecutorRegistry._resolve_acp_resume_context`, worker-result parsing, failed
writer cleanup and latest-turn checks; no blocking lifecycle mismatch was found.

Code Map located the two implementation entry points and the production
`begin_turn` call. The committed baseline Code Graph lifecycle completed for
tree `2e2b2b403bdc96712847271a41ac4b6e4f59d365`, graph
`6f4e7a1cc5f7683e0e985a44c9794847c4cba7b5e7740ea76de0f3d6ba8d6534`.
Prepare job `job_1793728560678_AFHZqHZW` succeeded in 58.310 seconds, its terminal
stdout/stderr were read and the owned result was consumed.

Impact queries covered `AcpControlStore.begin_turn` and `AcpSessionRunner.run`.
They reported incomplete incoming-call resolution (three and nine unresolved
same-name sites respectively). Source inspection confirmed the actual
worker -> session -> control path and excluded unrelated generic `run` / SQLite
`execute` matches. The graph is navigation evidence, not exhaustive semantic proof
and does not represent uncommitted modifications.

## Regression evidence

Twenty new control/session cases and one operations integration case cover:

- exact byte limit and one-byte overflow, including the heading and separators;
- multibyte Unicode in both prompt and steering;
- a partially fitting queue remaining entirely queued;
- unchanged persisted message records after overflow and across reopening;
- matching active turn, cancelled old question and current reply handling;
- duplicate failed-turn/message rejection and one explicit subsequent delivery;
- deterministic enqueue ordering around a held mailbox lease, without sleeps;
- post-claim transport uncertainty and refusal of unsafe continuation;
- unchanged maximum-size prompts without steering or without a control store;
- invalid helper inputs leaving the mailbox file byte-for-byte unchanged.

The operations integration uses the actual runner, worker-result parser, public
steer/status/enrichment and the actual continuation admission resolver with an
offline transport. It checks that a failed unsent turn remains reachable through
the public path, that no message text leaks in status, that old source operations
and incorrect abandonment are rejected, and that a short explicit continuation
receives the ordered queue in exactly one prompt.

Parent-owned focused job `job_1793729072347_RaMXRrQy` passed **186 tests in
15.36 seconds**, with empty stderr. Its five source/test file hashes remained
unchanged across the run; `git diff --check` passed. The job's terminal streams
were inspected and its result consumed.

## Release and live evidence

### Original candidate gate

Candidate `852cee30e25180cc9c3136178d689b9e05e03a45` contains only the two source
modules and three regression modules, +442/-7 lines. The source SHA-256 values are:

| Source | SHA-256 |
| --- | --- |
| `acp_control.py` | `71d521c3df8c488dacc82c84196b3ee945961be73a7d6e938bdf817b34de899b` |
| `acp_session.py` | `285f1491399a9bd934cebab4c6646491ab2f745a990ed7ed6a7b8571e5790066` |

The separate clean gate worktree ran parent-owned
`job_1793729167304_I1faW9of`: **2050 passed, one skipped, five existing warnings
and 18 subtests in 210.81 seconds**. The job completed with exit 0 in 212.361
seconds. It used the configured QA Python/SDK 0.12.1 and native-helper fixtures.
`compileall src tests`, `git diff --check`, source hashes and the unchanged clean
tested HEAD passed. The parent read both terminal streams and consumed the job.
Its final marker was `ACP_COMPOSITION_FULL_GATE_PASS`.

### Integrated release gate

During closeout, the shared main advanced independently to the accepted Revit
release `705316e84d68ef5b9753dad47194dabe85bdee6e`. Its three commits changed eight
Revit-specific paths and did not overlap the ACP source, tests or documentation.
A separate source/caller review found no new startup dependency, native work at
startup, ACP lifecycle change or cross-router admission change.

The parent merged that release into the candidate, producing
`1ad6574928ee8cf5104a005e42c0912f94bd7477`. Exact Git comparisons verified that
all five ACP source/test files still matched `852cee3` and all eight Revit paths
matched the accepted upstream release. A fresh full gate was necessary for this
changed deployment base; the already passed focused ACP suite was not repeated.

Parent-owned `job_1793731536842__Sldwez2` passed **2099 tests, one skip, five
existing warnings and 18 subtests in 223.94 seconds**. Job duration was 225.496
seconds. It used the same configured SDK 0.12.1 and enabled native-helper fixtures.
Compilation, diff checks and unchanged clean tested HEAD passed. Both terminal
streams were inspected, stderr was empty, and the job is consumed. Its marker
was `ACP_COMPOSITION_INTEGRATED_FULL_GATE_PASS`.

Main then received documentation-only commit
`759100facb616b730da8858dbd33a4c0c47a86ac`, adding
`docs/revit-quality-pilot-status-20261103.md`. That commit was also preserved in
release candidate `84a1b097ed43b083bf389b14a0093f278e319f8c`. The complete diff
from the tested merged HEAD contains only that documentation file; executable
source, tests, scripts and dependency files are unchanged. No further source
gate was needed for this merge.

These gates need not be repeated for documentation-only changes. A change to
executable source, tests, the deployment base or pinned runtime requires review
of which checks must be repeated.

Both completed gate worktrees were removed without force; their committed trees,
durable job evidence and branches remain available. The implementation and clean
live-session worktrees are retained. Final documentation review found no blocking
issue after making pending activation explicit in the public guide and historical
navigation record. All 17 local documentation links resolved, the diff check
passed, and both executable source hashes still matched the tested candidate.

### Deployment remains pending

The implementation has **not been activated**. Main
`/home/vooxerg/src/chatgpt-web-oauth-mcp` remains clean at
`759100facb616b730da8858dbd33a4c0c47a86ac`; its ACP implementation still has the
accepted navigation behavior of `dac731b`, with the separate Revit release
preserved. The integrated candidate `84a1b09` is available on
`fix/acp-prompt-composition-20261103`, in
`/home/vooxerg/worktrees/opss-acp-composition-20261103`.

The first two bounded deployment jobs waited for global delegates, queued delegates and
other durable jobs to drain before any Git or service mutation:

| Owned job | Result |
| --- | --- |
| `job_1793729496688_Nflo9Tp1` | Deferred after 301.123 seconds; exit 2, `changed=false` |
| `job_1793729832872_rKr4e_hr` | Deferred after 600.613 seconds; exit 2, `changed=false` |

Both emitted `ACP_COMPOSITION_DEPLOY_DEFERRED_BUSY`, kept main at `0257683`,
had empty stderr and were inspected and consumed. No composition rollback
reference was created and no service restart was attempted by these jobs.

The 21:39 MSK health observation on 2026-11-03 reported authenticated HTTP 200,
`success=true`, no observation errors, service PID 1168199 active/running and
`ExecMainStatus=0`. Global activity still contained two active delegates, no
queued delegates and two running durable jobs. This is a point-in-time service
observation, not proof of a deployment window.

After the parallel Revit deployment, PID 1472890 was active/running with
authenticated HTTP 200, no observation errors and `ExecMainStatus=0`. A subsequent
quiet snapshot allowed preparation of the integrated release, but another durable
job started before cutover. Parent-owned `job_1793732031691_Uy3CDyhJ` therefore
deferred after 16.383 seconds, exit 2, `changed=false`, with main still `759100f`.
Its final aggregate showed zero active/queued delegates and two running durable
jobs including itself. The complete metadata registry contained 501 terminal
records, no invalid records and one other nonterminal job. Both streams were
inspected, stderr was empty, and the result is consumed. This third attempt used
a maximum 15-second quiet-window wait and did not stop the service, update main
or create a rollback reference.

The final 22:03 MSK observation on 2026-11-03 still showed clean main `759100f`,
PID 1472890 active/running, authenticated HTTP 200, no observation errors and
`ExecMainStatus=0`. There were no active/queued delegates and one running durable
job. The implementation remained unactivated; no owned deployment job was left
waiting in the background.

A worker-only route was also reviewed. The resident server's public control
methods remain compatible with the candidate, and each new ACP worker imports
the two modules from main through its configured `PYTHONPATH`. However, no
supported ACP-only admission pause exists: the backend is loaded/cached by
`server.py` / `ExecutorRegistry`, configuration validates exact keys, and
workers load configuration after importing modules. Updating two source files
while new workers can start leaves an import race. An idle account snapshot or
a configuration-file toggle does not close that race. No unguarded fast-forward,
temporary invalid configuration or process suspension was used.

The prepared full-service procedure closes that import race by stopping the
service before replacing files. It first requires a healthy global quiet window.
After a complete stop (`inactive`, `MainPID=0`, listener closed), it checks every
entry in the effective service `STATE_DIR/jobs` using the existing read-only
`read_job_metadata` helper and `TERMINAL_JOB_STATUSES`. It outputs only aggregate
counts, never another owner's logs, result or task text. Nonterminal `starting`
records must also block cutover: they are persisted before process spawn and can
precede the active-job marker. The deployment's own exact ID is resolved from its
process group and verified process identity; all other nonterminal or invalid
records block the fast-forward. A blocked post-stop check starts the previous
service without changing Git. The owned durable deployment job survives through
the existing `KillMode=process` setting.

This closes the source-import race but is not a neutral admission-pause API.
Shutdown can cancel a queued or pre-durable submission that races the initial
quiet snapshot, so the release still requires a window without new submissions.
The prepared command is retained with owned job `job_1793732031691_Uy3CDyhJ`;
its expected previous HEAD is `759100f`, target `84a1b09`, tested tree `1ad6574`.
The stop/fast-forward/start path has not run and is not reported as acceptance.

### Real pre-cutover continuation baseline

Two read-only native turns ran on the **existing deployed runtime**, while
inspecting the candidate source in a fresh clean linked worktree. They validate
normal steering delivery and supply independent source findings; they are not
acceptance evidence for the new runtime's overflow behavior.

Both used the existing attested `antigravity3` route, Flash High / high,
a 120-second lifetime, a six-tool cap and the exact read-only scope
`src/chatgpt_web_oauth_mcp/acp_control.py`. They shared session reference
`72c8e7a1af089aa006912d32cc11b782`, conversation
`df8b88eb-6fa0-4cce-817f-a33516859e14` and the unchanged
`opss-explore-questions-v1` profile binding.

| Observation | Initial read | Explicit continuation |
| --- | ---: | ---: |
| Delegate | `66bdcdd3bc9f` | `36e69c7d05d5` |
| Durable job | `job_1793730753125_XkkDgEcM` | `job_1793730879899_Oe2rsmU2` |
| Terminal result | Succeeded | Succeeded |
| Job duration, seconds | 30.663 | 18.725 |
| Native session duration, seconds | 30.216 | 18.364 |
| New/load session phase, seconds | 2.963 new | 2.710 load |
| Prompt phase, seconds | 25.044 | 13.474 |
| Native tool calls / accepted events | 1 / 14 | 1 / 12 |
| Saved task prompt, UTF-8 bytes | 2741 | 1956 |
| Raw input / output / thought tokens | 14473 / 280 / 2407 | 19547 / 283 / 898 |
| Raw total tokens | 17160 | 20728 |
| Observed `usage_context.used` | 10552 | 11231 |

The raw token counters retain
`usage_scope=provider_reported_unspecified`. Cached-read/write counters were
zero in both records. These are not comparable performance experiments or
independently additive session totals. Saved task-prompt byte counts exclude
the later mailbox composition and are not the full provider context.

After the first completed turn, the owner queued message
`composition-followup-20261103` (472 UTF-8 bytes), text SHA-256
`281d6d10635e108f6420ecce63da82bd7bb2e349e388c4afc1b88a9835ba0de4`.
Its unique marker `COMPOSED-STEERING-852CEE3-20261103` was absent from both saved
task prompts and appeared exactly once in the second result. The mailbox
contained the one preserved message record, `claimed_for_turn`, source
`66bdcdd3bc9f`, target `36e69c7d05d5`, with the original hash.

The first result correctly located composition admission before claiming.
The continuation correctly located the legacy `begin_turn` delegation and
claimed-list return at `acp_control.py:182`. The parent independently checked
those source anchors, both actual worker logs and empty stderr, unchanged
source hashes and full clean worktree, the shared native identity, checkpoint
v2 in completed state, current completed mailbox turn and the message receipt.
Both raw results recorded `prompt_sent=true` and `outcome_uncertain=false`.
The scoped gateways were revoked. Both delegates are consumed, with no pending
queued instruction.

Parent proof markers were `ACP_COMPOSITION_BASELINE_READ_PARENT_PASS` and
`ACP_COMPOSITION_BASELINE_STEERING_PARENT_PASS`. The live worktree
`/home/vooxerg/worktrees/opss-acp-composition-live-20261103` is retained for an
explicit post-cutover continuation with the same owner, account, scope and
workspace. Its latest completed source delegate is `36e69c7d05d5`.

### Concrete next release step

1. Recheck main HEAD/cleanliness, pinned worker import provenance and authenticated
   global activity. If unrelated work is still active, retain this candidate
   without restarting the shared service.
2. In an available quiet window, use the prepared stop-before-fast-forward
   procedure above. Recheck the complete registry after stopping admission; if
   another job is present, start the previous service without changing files.
   Before fast-forwarding `84a1b09` (or a reviewed documentation-only descendant),
   preserve `759100f` as `rollback/acp-prompt-composition-20261103`. If main has
   changed, reconcile and assess validation for the new base before cutover.
3. Confirm the new service PID, authenticated health, journal status and actual
   fresh-worker import of `begin_prompt_turn` from the deployed candidate.
4. Run one bounded, explicit read-only continuation with newly queued steering,
   then inspect actual logs/checkpoint/mailbox. Preserve all identity/profile
   checks and consume the owned result. This is the remaining new-runtime live
   gate; do not relabel the pre-cutover pair as that gate.
5. Continue the representative stage-4 matrix with a small behavioral one-file
   fix and independent verification. Keep controlled exact-patch application,
   behavioral problem solving and cancellation/recovery as separate capabilities.

A supported admission/drain mechanism or immutable worker release could remove
the shared-service release constraint later. It is a separate operational
improvement, not an untested addition to this already verified candidate.

## Remaining pilot scope

Overflow boundary/recovery is verified with the deterministic offline transport;
an intentionally oversized real provider prompt is unnecessary. The pre-cutover
normal read/steering continuation passed. Activation and the bounded post-cutover
continuation above remain necessary to accept this candidate on the live runtime.

Controlled exact-patch application and autonomous behavioral implementation
remain separate pilot capabilities. This composition fix does not establish
a speedup, a larger autonomy claim, new account activation or ACP v2 support.
