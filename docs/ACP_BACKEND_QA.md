# ACP backend: configuration and acceptance history

Updated: 2026-11-03. Operator identity/configuration instructions remain applicable.
The current task, writer and control contract is in [acp-subagents.md](acp-subagents.md).
The current final-candidate ledger and the historical read-only release record
are separate sections below. Historical deployment does not establish rollout
of the later writer, gateway or live-question implementation.

## Public task contract

Use the existing account, scheduler, quota gate, durable jobs and result inbox:

```json
{
  "backend": "acp",
  "harness": "antigravity3",
  "kind": "explore",
  "cwd": "/operator/approved-repository",
  "task": "Inspect sample.py and report one source-grounded finding.",
  "files_in_scope": ["sample.py"],
  "max_tool_calls": 6,
  "execution_timeout_seconds": 120,
  "wait_seconds": 0
}
```

Discover availability in `delegate_harnesses` or `server_info` under the existing
account's `execution_backends.acp`. The default transport remains CLI.
Only explicitly pinned, identity-verified accounts are admitted. ACP adds no
capacity and never falls back to CLI, another account, API keys or premium
models.
Current activation and live acceptance use only attested `antigravity3`;
additional accounts require independent identity verification and acceptance.

Use `await_delegate`, `delegate_status`, `delegate_cancel` and
`mark_result_consumed` normally. To continue a terminal turn, submit another
ACP task with `resume_from_delegate_id`, the same owner/account/workspace, and
the follow-up instruction in `task`. Omitted model/thinking and scope inherit
the source. A current live question is answered with `delegate_message(action="reply")`
using its exact turn/request/option IDs before expiry. The legacy disposition
`cancelled_requires_continuation` describes an already cancelled RPC and still
requires a new turn. `action="steer"` queues a bounded instruction for the next
explicit continuation; it does not modify an in-flight prompt.

The source must still be the latest session turn when the worker acquires the
lease. A queued continuation of an already superseded source fails with
`acp_resume_source_stale` before a provider connection; the backend does not
branch or rewind native history from an older delegate.

Supported models are `gemini-3.8-flash` and its `-low/-medium/-high` aliases.
Aliases select the corresponding thinking level; the default is medium.
Timeouts are 15–900 seconds for exploration and 30–900 seconds for writers;
tool budgets are 1–64. Writers require a clean dedicated linked Git worktree,
exact file targets, independent verification commands and forbidden commits.
Custom output schemas and batch/group plans remain unsupported. A final manifest
and valid file:line anchors still require independent acceptance.

For exploration, the whole approved workspace is mounted read-only. `files_in_scope` and
`out_of_scope` constrain the assigned investigation and evidence checks; they
are not per-file OS access grants.

## Operator configuration and identity verification

Create private directories first. State/profile/proof directories must be
owned by the service user and mode 0700; configuration, proof and OAuth token
files must be 0600. Authorize each ACP profile normally. Never copy OAuth
tokens between account profiles.

```json
{
  "version": 1,
  "python": "/operator/acp-venv/bin/python",
  "server": "/operator/acp-runtime/agy_acp_server.par",
  "helper": "/operator/acp-runtime/localharness_external",
  "state_root": "/operator/private-acp-state",
  "accounts": {
    "antigravity3": {
      "profile": "/operator/private-acp-profile",
      "cli_home": "/operator/registered-agy3-home",
      "workspace_roots": ["/operator/approved-repository"]
    }
  }
}
```

Install the optional ACP dependencies in the worker environment (SDK 0.12.1
and WebSockets 17.4). Preserve the venv Python path rather than resolving its
symlink to the system interpreter. Runtime/helper binaries and config are
trusted operator assets.

Before loading that configuration, run:

```sh
PYTHONPATH=src python scripts/acp_account_verify.py \
  --profile /operator/private-acp-profile \
  --account antigravity3 \
  --cli-home /operator/registered-agy3-home \
  --proof-root /operator/private-acp-state/account-bindings
```

The verifier refreshes the existing authorized ACP identity and compares it
with the selected CLI account through fixed Google endpoints. It prints only
sanitized verification metadata. The saved attestation contains hashes, not
OAuth values or email addresses. Admission checks the actual registered
harness home as well as that proof. Stable credential/principal/path changes
require reverification; ordinary access-token rotation does not.

Set `CHATGPT_MCP_ACP_BACKEND_CONFIG` to the private config path and restart the
service. Invalid optional configuration disables ACP without taking down MCP.
Do not treat a successful load as proof that live runtime acceptance passed.

## Security boundary

The OAuth runtime has an empty workspace view and a minimal ACP-profile mount,
so repository and global hooks cannot execute with its credentials. Arbitrary child
MCP servers, hooks and custom subagents are disabled in native initialization;
the worker may register only its fixed authenticated read-only task gateway.
The helper has a read-only exploration workspace or a private writer copy,
private conversation state/tmp, a private process namespace and no
OAuth/host-home/Opss-state access. Writer promotion and verifier isolation are
specified in [acp-subagents.md](acp-subagents.md#native-writers).

A private network namespace blocks direct host/external TCP and UDP. The
trusted bootstrap owns fixed Unix relays; a bounded authenticated gateway
validates and rewrites only the official loopback inference endpoint and the
worker-approved task MCP endpoint when configured.
The native child and descendants cannot create AF_UNIX sockets/socketpairs,
use io_uring or borrow descriptors with pidfd_getfd, so workspace Unix sockets
do not create another host channel. Native Unix IPC is consequently unavailable.
Seccomp also denies process inspection and namespace/mount manipulation.

## Recovery, failure and rollback

The worker request is private stdin, not command-line text. Native conversations
are stored by stable session reference; checkpoints are private and atomic.
One cross-process lease protects each session before transport launch.

Restart recovery adopts the same durable worker/job and original parser.
New-admission checks do not obstruct adoption after ACP is disabled. Explicit
cancel requests native ACP cancellation before a bounded signal fallback.
While awaiting the terminal prompt response, only the cancel notification is
repeated within the existing grace/deadline; this covers the pinned runtime's
startup race without ever replaying the prompt.
A lost in-flight turn is marked uncertain and is never automatically replayed.
After investigation, `delegate_reconcile(action="abandon")` freezes an owned
uncertain failed session once the worker is terminal and the lease is available.
It preserves the previous result/history and requires a reviewed new task;
it does not replay, declare success or roll back partially promoted files.

To stop new ACP work, unset the backend config and restart MCP. Existing durable
work can drain with its ACP parser or be explicitly cancelled. Keep its runtime,
state and profile available until it drains. Before a code rollback, drain/cancel
ACP tasks: the pre-ACP production revision cannot interpret their new metadata.
Preserve the rollback ref and all session records; do not erase state to retry.

## Automated and live acceptance

Relevant test groups:

* `test_acp_session.py`, `test_acp_worker.py`: lease, history/replay, ownership,
  deduplication, typed failures, native cancellation and uncertain outcomes.
* `test_acp_identity.py`, `test_acp_backend.py`: actual account/harness binding,
  shared capacity, admission, continuation, scope persistence and recovery.
* `test_acp_network.py`, `test_acp_sandbox.py`: authenticated fixed channels,
  hostile initialization, real Linux filesystem/network/seccomp sentinels and
  cleanup with connected clients.
* `test_acp_result.py`, `test_acp_delegate_boundary.py`: final-envelope and
  manifest validation through the existing durable runner.
* `test_acp_mcp_acceptance.py`: disposable real HTTP MCP subprocess restart
  with a durable job, without provider calls.
* `test_acp_control.py`, `test_acp_operations.py`, `test_acp_tools.py`: durable
  live-question replies, owner/worker/turn gates, queued steering and abandonment.
  Writer and gateway regression groups accompany their respective modules.

Run the complete repository suite plus compile/diff checks before rollout.

The live script uses a new private state directory and a fresh nested Git
fixture under an already approved workspace root:

```sh
PYTHONPATH=src python scripts/acp_mcp_acceptance.py \
  --config /operator/private-acp-config.json \
  --state /operator/new-private-acceptance-state \
  --workspace /operator/approved-qa-root \
  --account antigravity3
```

It starts a separate authenticated MCP subprocess, waits for an actual running
ACP turn, kills only that MCP process, verifies the worker survives, restarts
MCP on the same port/state/logical owner, and adopts the same durable job.
It then checks the random file marker, source anchors/commit, unchanged source,
artifact digest, continuation memory without another file read, wrong-owner
and cross-backend denial, invalid writer admission and protocol cancellation without
forced worker termination. It never restarts production as a test fixture.
Only the existing hash attestation is copied into its disposable state; no
OAuth credentials are copied.

### Final release acceptance, 2026-11-03

Implementation release: `1803710b079606d3ef8c0ed9906bb547dbcf70c9`, branch
`feat/acp-subagents-final-20261103`, developed in `opss-acp-final-20261103`.
It was deployed to the production `deploy/gip-core-linux` checkout and passed
authenticated readiness plus a native writer smoke test through the actual
Opss MCP connection. The follow-up documentation commit records that evidence;
it does not change the validated implementation.
The final assembled repository run reported **1756 passed, 1 skipped, 5 existing
warnings in 180.30 seconds**. It includes the real pinned native initialization
test via `OPSS_ACP_TEST_NATIVE_HELPER`. The remaining skip is the optional SDK
test in the main test environment, where that SDK is not installed; the live
native phases execute the SDK in the separately pinned worker environment.
The warnings are one existing Starlette/AnyIO deprecation and four existing
threaded-fork deprecations. No ACP subprocess-close warning remains.
Validation job: `job_1793698800430_52ALIMxv`. All 117 source/script Python files
also compiled without creating cache files; `git diff --check` passed.

Live acceptance combines explicitly observed phases from three fresh fixtures:
`final-native-20261103-c` (question/gateway and continuation),
`final-native-20261103-e` (writer), and `final-native-20261103-f` (reconciliation).
The complete c/e reports retain FAIL because each discovered a subsequent
defect; their completed phase assertions remain evidence. The f report is PASS.
All three reports record successful cleanup. This is a composite gate record,
not a claim that c or e passed their entire script.

| Gate | Observed result | Evidence or remaining requirement |
| --- | --- | --- |
| Repository regressions | PASS, final assembled source | 1756 passed / 1 optional-SDK skip / 5 existing warnings; real native init included |
| Native question and controller restart | PASS, phase 1 | Actual pending question, restart with the same durable job/worker PID, owner reply and continuation of the original prompt |
| Scoped MCP reads | PASS, c phase 1 | Actual `read_text` twice and `code_map_symbols` once; zero rejected calls, source evidence and read-only fixture checks |
| Queued continuation | PASS, phase 2 | Queued instruction delivered on the later turn, same provider conversation, omitted model/thinking inherited and zero subsequent tool calls |
| Native writer verification and promotion | PASS, e phase 3 | Only `calculator.py` changed/exported; exact expected bytes, isolated check exit 0, matching receipt fingerprint, canonical `verified=true` / `checks_passed`, main fixture unchanged |
| Lost-worker reconciliation | PASS, f phase 4 | Exact registered worker KILL, public abandon, unchanged prior checkpoint payload/native history, old reference frozen and `turns_seen=1`; no replay |
| Final commit and production rollout | PASS | Implementation `1803710`, production PID 587587, HTTP 200 / success=true, 56 private tools and actual post-deploy writer acceptance |

Writer audit finding: the fresh writer-only fixture `final-native-20261103-d`
showed the native helper materializing its system workflow skill as
`skills/system/run-workflow/SKILL.md` inside the private repository copy
(10,331 bytes, mode 0600). That path was outside the sole authorized target,
`calculator.py`, so the existing scope check correctly rejected the candidate.
The dedicated source worktree and main fixture retained their original buggy
bytes; no promotion receipt was created and no changes were exported.

The correction pins the official `skillsConfig.materializedSkillsDir` field to
`/home/agent/.local/share/opss-native/skills` in private home. It preserves other
skill settings, `appDataDir` and history storage. Native init without a prompt
checks compatibility and unchanged source/mirror with zero inference connections;
the e writer proves the previous setup artifact no longer contaminates the
candidate. Unauthorized changes still fail the same promotion guard. Bounded
`writer_rejection` diagnostics preserve paths/modes/sizes/hashes, never contents.

Reconciliation audit finding: e then exposed a vocabulary mismatch between
delegate and durable-job terminal statuses. JobRegistry reports `killed` after
TERM/KILL, which the controller previously rejected as nonterminal. Job gates
now use the canonical `TERMINAL_JOB_STATUSES`; delegate statuses remain separate.
Real isolated JobRegistry TERM/KILL regressions preserve owner/lease checks,
and f confirms public abandonment on the real ACP worker without replay.

Native evidence IDs:

* c question: delegate `a884957bcb20`, job `job_1793696955975_r7kVjnnh`.
* c continuation: delegate `b9cf8bfa9193`, job `job_1793697012628_1WYz6zpP`.
* e writer: delegate `c98223bc0c51`, job `job_1793698397132_XlW3fDJN`.
* f reconciliation: delegate `7979fdabb6a8`, job `job_1793698864487_3EcEPKSe`;
  outer acceptance job `job_1793698844750_kDwWkDir`, exit 0 in 26.297 seconds.

The expanded operator gate is `scripts/acp_final_acceptance.py`:

```sh
PYTHONPATH=src python scripts/acp_final_acceptance.py \
  --config /operator/private-acp-config.json \
  --state /operator/new-final-acceptance-state \
  --workspace /operator/approved-qa-root \
  --account antigravity3
```

It writes a sanitized `final-acceptance-report.json` in its fresh private state.
The default run covers all four phases. `--start-at writer` and
`--start-at reconcile` support explicitly selected fresh partial runs; their
reports mark skipped phases, so a partial PASS covers only the exercised gates.
Each report keeps its original status and the recorded implementation revision.

#### Production verification

* Deployment job `job_1793700175800_112jjEn7` succeeded in 32.469 seconds.
  Readiness confirmed the new PID 587587, HTTP 200, `success=true`, and an empty
  `observation_errors` object. The service exposes the existing three public
  routers and 56 private Opss tools, including `delegate_message` and
  `delegate_reconcile`.
* Rollback reference `rollback/acp-final-20261103` preserves the previous
  production commit `177025859c78e70076d4de20b40bd95b107190fe`.
* The actual production connection submitted ACP code delegate `34eb2088eaac`
  on attested `antigravity3`; durable job `job_1793700251384_pWufid97` completed
  successfully in 53.381 seconds. Its dedicated linked worktree used the full
  repository at `1803710` rather than the small synthetic calculator fixture.
* Exactly `acp_deploy_smoke.txt` was created and promoted: 26 bytes, one added
  line, zero deletions, SHA-256
  `834ceba7ac1946c2a5fec68ceb98b99219d4845790fb94dda2d9fc604c94b394`.
  Independent sandbox verification returned `ACP_PRODUCTION_SMOKE_PASS`, exit 0.
  Canonical acceptance reported `verified=true`, `checks_passed`, matching
  snapshot fingerprints, and no scope, budget or commit violations.
* Real scoped gateway calls were `read_text` once and `git_status` twice, with
  zero rejections; the capability was revoked after completion. The parent
  independently checked exact bytes, the sole changed path, and the clean main
  checkout. The owned smoke artifact and clean disposable worktree were then
  removed, and both deployment/delegate results were consumed.

Two preliminary deployment checks rolled back because the surviving deployment
job could not read `/proc/<new-pid>/environ` after service restart. The improved
diagnostic recorded `PermissionError` before any health HTTP request; this was
a readiness-checker defect, not an observed ACP runtime failure. The successful
deployment captures the already-authorized health credential during preflight,
retains it privately in memory, and checks the new service over its normal HTTP
endpoint. It never changes process-access protections or prints the credential.
The controller permits a 90-second readiness window and a 15-second HTTP timeout
for cold startup; the observed application startup alone took about 24 seconds.

### Historical read-only release acceptance record

* Identity verification: the authorized QA ACP profile matches AGY3. The old
  AGY1 fixture label was incorrect and has been removed.
* Real MCP acceptance: PASS on the final candidate with the official runtime
  and verified AGY3 profile. The worker survived a deliberately killed test
  MCP; restart adopted the same durable job. Source/artifact verification,
  anchored Git evidence, unchanged source, same-session memory with zero
  subsequent tool calls, owner/backend/writer guards and native cancellation
  without forced termination all passed.
* The first live run passed restart and continuation but exposed lost early
  cancellation. It is not counted as a successful acceptance run. Source review
  confirmed the runtime startup race; the final run passed with the bounded
  control-notification fix and the original assertions unchanged.
* Final complete repository validation: **1521 passed**, five existing
  dependency/fork deprecation warnings, 159.72 seconds. The same durable job
  also passed compileall for src/tests/scripts and git diff --check.
  Validation job: \`job_1793692318068_LNafqSJV\`.
* Final real-runtime acceptance job: \`job_1793692316943_4oJnBgfl\`.
* Production rollout: fast-forwarded \`deploy/gip-core-linux\` to the audited
  implementation \`e8e4e0e\`; service restarted with new PID 410023, authenticated
  health passed, and the startup journal contained no error-priority records,
  tracebacks or ACP-disabled messages. Rollback ref:
  \`rollback/acp-audit-20261103\` (pre-ACP production \`31274aa\`).
* Initial activation is explicit ACP on AGY3 only, with the Opss repository as
  the approved workspace. Its existing four account slots and quota gate are
  shared with CLI; automatic routing remains off.
* Production source probe \`cab8bc2a52c6\` succeeded in 15.972 seconds with one
  native read and a verified source anchor. Continuation \`938dfa0e0b9c\`
  succeeded in 16.433 seconds, recalled the exact answer with zero tool calls,
  retained both session/provider conversation IDs, and inherited omitted model,
  thinking and scope. Both results were independently inspected and consumed.
  These timings describe two acceptance turns, not a performance benchmark.

Historical two-session code-generation QA (41.304 seconds, two offline test
suites passing) proved only that generated answer artifacts could pass those
tests. It did not test native editing or authorize write-capable delegation.
