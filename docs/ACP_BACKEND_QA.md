# ACP backend: operations and acceptance

Updated: 2026-11-03. This document covers the explicit read-only backend.
The final assembled and live acceptance gates passed as recorded below;
a historical pilot result is not a substitute for the current gates.

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

Use `await_delegate`, `delegate_status`, `delegate_cancel` and
`mark_result_consumed` normally. To continue a terminal turn, submit another
ACP task with `resume_from_delegate_id`, the same owner/account/workspace, and
the follow-up instruction in `task`. Omitted model/thinking and scope inherit
the source. A pending question with `cancelled_requires_continuation` is
answered this way; there is no live permission grant or in-flight steering.

Supported models are `gemini-3.8-flash` and its `-low/-medium/-high` aliases.
Aliases select the corresponding thinking level; the default is medium.
Timeouts are 15–900 seconds and tool budgets 1–64. Native writers, custom output
schemas and batch/group plans are rejected. A final manifest and valid
file:line anchors still require independent acceptance.

The whole approved workspace is mounted read-only. `files_in_scope` and
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
so repository and global hooks cannot execute with its credentials. Child MCP
servers, hooks and custom subagents are also disabled in native initialization.
The helper has a read-only workspace, private conversation state, private tmp,
a private process namespace and no OAuth/host-home/Opss-state access.

A private network namespace blocks direct host/external TCP and UDP. The
trusted bootstrap owns two fixed Unix relays; a bounded authenticated gateway
validates and rewrites only the official loopback inference endpoint.
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
There is no public reconciliation operation; investigate it and use a new
reviewed task rather than modifying the ledger.

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
and cross-backend denial, writer denial and protocol cancellation without
forced worker termination. It never restarts production as a test fixture.
Only the existing hash attestation is copied into its disposable state; no
OAuth credentials are copied.

### Current audit acceptance record

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
