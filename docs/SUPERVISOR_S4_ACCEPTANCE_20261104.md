# DeM Supervisor S4: operational pilot acceptance — 2026-11-04

## Decision and scope

**S4 BOUNDED PILOT ACCEPTED** for the explicitly identity-verified ACP account
`antigravity3`, Flash High/high, owned scoped tasks and dedicated clean linked
worktrees. This is **not** an autonomous, multi-provider production platform, a
completion-rate benchmark, or an assertion of faster work than an unassisted
model. All three substantive pilot cases below were actually run and verified;
their earlier receipts are reused rather than replaying successful provider
work solely to enlarge the test count.

The approved workflow is: one bounded engineering goal; read-only discovery;
optional supervisor checkpoint with unverified agent report separated from
server observations; explicit revision-bound `review` or `steer`; independent
acceptance; and explicit consumption of the terminal result. An interrupted
writer may save its unaccepted draft and start a new worker from an exact
snapshot. Provider history and saved draft bytes are independent choices.

## Functional scenarios and evidence

| Scenario | Actual evidence | Acceptance |
| --- | --- | --- |
| S2 source review and same-turn correction | ACP delegate `d19ee829fcc7`, 98.401 s, two native calls. Supervisor requested a distinction between known pinned timeout and uncertain outcome. A unique correction marker absent from the original prompt appeared **once** in the final answer. The semantic answer was independently compared with `routing/replan.py`. Original receipt: `supervisor-native-read-20261104.json` | **PASS** |
| S2 candidate draft, supervisor review, correction and export | ACP delegate `7877666ed6dd`, 189.360 s, 11 calls. The owner caught two unsupported claims in the draft; the source worktree was still clean at the checkpoint. The same turn corrected the wording. Independent verifier passed, then exactly one allowed document was promoted; parent byte comparison SHA-256 `62546d5468d5b6eb2140be136955b9124b9200b006122d9a598a681442b4007c`. Receipt: `supervisor-native-writer-20261104.json` | **PASS** |
| S3 suspend unaccepted work; restore in a fresh worker and conversation | First delegate `69061cd1519a`, acknowledged cancellation without export. Saved draft `edec5d0df37119cc69ab7ff9340152d3`. Second delegate `15190790ff6b` restored the exact bytes, completed the document with independent verification and exported one file, 888 bytes, SHA-256 `eb92120a2e09aa0d004b9c87509434f6e80eef82fae8f032198bf768d3c0f2e9`. The second ACP conversation was **new**, not replayed. Receipt: `supervisor-drafts-native-acceptance-20261104.json` | **PASS** |

These runs were performed in the preceding S2/S3 implementation cycles. Their
logs, checksums, durations and limitations are recorded in
[`SUPERVISOR_PILOT_20261104.md`](SUPERVISOR_PILOT_20261104.md) and
[`SUPERVISOR_DRAFTS_20261104.md`](SUPERVISOR_DRAFTS_20261104.md).
No controlled unassisted Flash baseline was established; no comparative
quality, cost, latency or throughput advantage is claimed.

## Latest S3 fault handling and final release gate

After S3, the owner reproduced an additional valid-checksum but structurally
invalid draft manifest that raised a raw `KeyError('baseline_head')` on list.
Code commit **`a810c9d0a670306223840fafe20e7092c4442244`** validates the existing
v1 structure before indexing it. Corrupt JSON, missing or malformed nested
fields, inconsistent saved-byte accounting and malformed change records now
return typed `draft_manifest_invalid`. A real checksum mismatch retains
`draft_manifest_changed`; account binding is still checked separately.
The change introduces no new snapshot format or compatibility wrapper.

The preceding Flash High code attempt `60b2f5e7cc02` reached an actual
supervisor checkpoint and received senior steering in the same turn, but then
timed out during native execution. It produced **no accepted export** and had
`outcome_uncertain=true`. The source worktree was confirmed clean, the provider
session was explicitly abandoned, the resulting empty retained snapshot
discarded, and the failed result consumed. The parent implemented this narrow
correction directly. This is a failed model-execution sample, not a fourth
successful S4 task.

Focused and affected-module checks: **79 passed**. Initial incomplete fake
snapshot fixture in `test_acp_operations.py` was corrected to the actual v1
shape rather than weakening production validation. Full integrated gate
`job_1793801285144_kDySYQsY`: **2155 passed, 1 skipped, 18 subtests passed**,
five existing warnings, 222.62 seconds of pytest. All 247 Python files
compiled, exact HEAD/source bytes were stable, `git diff --check` passed.

Independent Revit commit `415a7be` entered the deployment branch during this
cycle; the S3 schema patch was rebased onto that base with no conflicts and the
integrated source was the one tested. Guarded rollout
`job_1793801753056_w2JWz8Jy`: **PASS**, 31.876 seconds, old PID 2779643
to new PID **2856282**, active/running, `ExecMainStatus=0`, authenticated
health success, zero observation errors and no new error-priority journal lines.
The rollback ref for this rollout is `rollback/acp-draft-schema-20261104`
pointing to `415a7be`.

After the restart, the **public MCP** `delegate_draft(action="status")`
successfully returned an empty list for both the previously accepted-and-cleaned
S3 writer and the subsequently abandoned code writer. A second
`delegate_draft(action="discard", snapshot_id=...)` for the previously
discarded S3 artifact returned `already_discarded=true`. These establish
live read/idempotent-discard behavior, not a live injection of malformed
production state. Malformed manifests are covered by the focused tests and
full integrated gate; no production record was deliberately corrupted.

## Explicit constraints of the accepted pilot

- Checkpoints are **cooperative**, not a general preemptive native-tool stop.
  Current in-flight native edit operations may complete before the checkpoint.
- Only published immutable draft snapshots survive a hard process death.
  Unsaved changes since the latest checkpoint may be lost.
- A hard kill during multi-file promotion still requires examining source
  state and explicit reconciliation; no unsafe automatic retry is authorized.
- ACP per-turn timeout and call ceiling still apply. An engineering task can
  continue through explicitly bound turns, not a single unbounded native turn.
- The first delivered provider account was `antigravity3`. Extra Gemini
  accounts and external API agents require registration, admission/credentials,
  concrete execution adapters and independent acceptance. The provider-neutral
  supervisor and draft-binding core is ready for their later integration.
- Senior-model decisions currently come from the **active supervising chat**.
  There is no always-on GPT-6 API controller, autonomous cross-account
  delegation, or automatic senior-model invocation.
- The pilot has three accepted substantive functional cases, not a statistically
  meaningful success-rate or economics comparison. A long Flash High attempt
  demonstrably timed out even with a checkpoint and senior correction.

## Closeout and next product milestone

Treat S1/S2/S3/S4 as a **finished limited operational pilot**, not an excuse
for another generic test cycle. For new actual work, collect per-task accepted
outcome, elapsed time, resource usage and supervisor interventions from the
existing receipts. The next separate product milestone is an autonomous
event-triggered supervisor adapter with explicit account registration and cost
control, plus improved native-tool preemption/progress diagnostics if practical
deployments prove those are needed. Do not mix those future features into the
accepted S4 runtime without new requirements.
