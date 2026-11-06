from __future__ import annotations

import json


SKILL_GUIDANCE_VERSION = "1.11"
SKILL_NAMESPACE = "chatgpt-web-oauth-mcp"
SKILL_INDEX_URI = f"skill://{SKILL_NAMESPACE}/index"
DELEGATE_USE_URI = f"skill://{SKILL_NAMESPACE}/delegate-use"
FILE_USE_URI = f"skill://{SKILL_NAMESPACE}/file-use"
CODE_GRAPH_USE_URI = f"skill://{SKILL_NAMESPACE}/code-graph-use"
PROCESS_USE_URI = f"skill://{SKILL_NAMESPACE}/process-use"
RUNTIME_USE_URI = f"skill://{SKILL_NAMESPACE}/runtime-use"
GIT_USE_URI = f"skill://{SKILL_NAMESPACE}/git-use"


DELEGATE_USE_GUIDE = """---
name: delegate-use
description: Use delegate_task, delegate_batch, delegate_status, delegate_cancel, delegate_message and delegate_reconcile with configured CLI or ACP harnesses. Load before the first delegate call, when choosing a harness or task kind, or when recovering from queued, failed, timed-out, or cancelled work.
---

# Delegate Use

## Critical rules

1. Use direct MCP tools first for inspection, planning, small deterministic edits, Git checks, and short verification commands.
2. Delegate exactly one bounded execution slice. Do not use a delegate as an opaque planning loop or unlimited autonomous worker.
3. Use `kind=explore` for read-only discovery and `kind=code` for implementation. Never ask an explore task to write.
4. Read the returned status and logs, then independently review and verify the result before declaring completion.
5. Treat `wait_seconds` as an MCP response window, not a process lifetime. A queued or running response means the delegate is still alive.

## Choose the tool

| Need | Tool |
| --- | --- |
| One bounded read-only or coding task | `delegate_task` |
| Several independent read-only investigations in one project | `delegate_batch` |
| Resume monitoring by delegate, group, project, or global state | `delegate_status` |
| Stop one delegate or every child in an exploration group | `delegate_cancel` |
| Answer an owned ACP question or queue its next-turn instruction | `delegate_message` |
| Freeze an uncertain ACP session after reviewing a failed worker | `delegate_reconcile` |
| Inspect/request a cooperative checkpoint and steer its current turn | `delegate_supervisor` |

Use `run_command` for coherent bounded non-interactive work expected to finish within the safe client foreground window. For ChatGPT/OpenAI sessions on this deployment the safe observed foreground budget is 30 seconds. For a coherent command expected to take longer, use one durable `job_start` instead of splitting the command merely to satisfy the foreground window. Use `tmux_*` for an interactive terminal. Do not delegate work that direct tools can perform more clearly and cheaply.

## Choose a harness and task kind

- Use `delegate_harnesses` or `server_info` to discover configured harnesses; built-ins may include `codex`, `claude`, `antigravity`, `pi`, and an optional `local` OpenAI-compatible scout.
- **ACP transport:** Check the account's `execution_backends.acp` capability. Use `backend="acp"` with an explicitly pinned identity-verified account and approved workspace. ACP shares CLI account slots/quotas and has no automatic transport or premium fallback. Explore mounts the workspace read-only. Code requires a clean dedicated linked worktree, exact file targets, declared verification commands and `commit_mode="forbidden"`; native edits a private copy, independent checks run in a separate read-only sandbox, and scoped CAS promotion precedes canonical receipt verification. Deadlines are 15–900 seconds for explore, 30–900 for code. The task-owned read/search/CodeMap/Git MCP gateway has a strict read allowlist; arbitrary MCP endpoints, hooks, batch plans and custom schemas remain disabled.
- **ACP control and continuation:** `delegate_status.acp_control.pending_request` exposes a live native question. Reply through `delegate_message(action="reply", delegate_id=..., request_id=..., option_id=...)` using exact observed IDs; this continues the original prompt and survives a main-MCP restart. Arbitrary free-text answers are unsupported. `delegate_message(action="steer", message_id=..., text=...)` records `queued_next_turn`, delivered only by the next explicit continuation. Resume the latest terminal delegate under the same logical owner/account/workspace/kind; omitted model/thinking/scope inherit, and scope may narrow. Stale source turns cannot branch history or restore a wider scope. A writer must again have a clean worktree. For an uncertain failed worker, inspect logs/files and explicitly use `delegate_reconcile(action="abandon", reason=...)`; the old reference stays frozen and history is retained. Start a new task without resume. Never replay automatically or equate a native answer with verified acceptance.
- A deployment may expose a second isolated Antigravity account as `antigravity2`. Treat it as a separate account for conversation resume, usage limits, and quota admission; never resume a conversation across the two Antigravity accounts. The second account may intentionally use a stronger default model than `antigravity`; when it does, treat `antigravity2` as the next escalation tier before Codex rather than skipping directly from the fast Antigravity tier to Codex.
- Optional `antigravity3` is a **third independent AGY account**, with its own HOME/OAuth store, quotas, conversation resumes, 4-slot account capacity, and isolated eligibility watchdog status/log. In capacity-first mode route AGY1 -> AGY3 -> AGY2; an unauthenticated AGY3 is unavailable and never borrows another account's credentials. The AGY3 watchdog shares the canonical, globally locked AGY binary patch script but explicitly supplies `CHATGPT_MCP_AGY_RECOVERY_PROBE_HOME` for its live eligibility probe. Do not interpret a successful AGY1 probe as AGY3 recovery. Set up AGY3's OAuth interactively under `CHATGPT_MCP_ANTIGRAVITY3_HOME` before delegating to it.
- When both AGY accounts have identical model capabilities but AGY2 is needed by other projects, configure `CHATGPT_MCP_DELEGATE_AGY1_PREFERENCE_WEIGHT=3` to favor AGY1 **within Opss MCP delegation only**. This is a soft, load- and quota-aware weight, not a fixed 75%/25% allocation: AGY2 is still selected for some unpinned tasks. Medium/high AGY1 tasks use Flash High when needed. Both accounts retain independent limits, and `harness="antigravity2"` or AGY2 conversation resume remains pinned to AGY2. A weight of `1` retains the legacy routing. Do not treat this as a claim that AGY2 has higher provider pricing or different model entitlements.
- **Gemini-first default policy:** For unpinned code and research, use AGY1 -> AGY3 -> AGY2 (shared account slots, quota-aware admission). `complexity=low` uses Gemini Flash Medium, `complexity=medium/high` uses Gemini Flash High, with high reasoning for complex work. If accounts are busy, wait in the AGY lane; if quotas are blocked, fail with an explicit quota reason. Never switch automatically to Codex or Claude solely for occupancy, quota failure, timeout or provider error. Explicit harness pins, account-pinned resumes and writer isolation remain enforced. `AGY1_PREFERENCE_WEIGHT` applies only to the legacy non-capacity router.
- **Account verification quarantine:** If any isolated AGY account has a valid OAuth credential but the provider rejects `/usage` with `verification_required`, its quota admission gate rejects new tasks with `account_verification_required` until the provider reports healthy quota data. Never treat OAuth presence or `agy models` as proof of model eligibility, and never use AGY3 while verification is required. Unaffected accounts keep their normal capacity priority; this does not upgrade model capability because of account occupancy.\n- **Evidence and reliability metrics:** Cloud explore results attach `research_evidence` with per-finding line-anchor checks against explicitly scoped files/directories and the observed Git HEAD. `all_findings_anchored` means cited lines exist, **not** that the finding is semantically correct. For broad unscoped tasks, only explicitly cited Git-tracked files may be checked and the result is labelled `tracked_unscoped` with unverified task scope; uncited claims remain `not_evaluated`, and no findings is not an automatic failure. Agents should cite real repository-relative `file:line` anchors they inspected, not invent references. Preserve process success separately from quality: source anchors prove existence only. `accepted` requires explicit independent review; code checks with no verification commands are `scope_only` rather than behavioral acceptance. Recovered terminal results link to private structured-result.json artifacts with SHA-256 and exact log sizes. Telemetry carries only counts, error codes, classified failures and 24h/7d rolling summaries, plus bounded 180-day daily aggregates on pruning (`daily_history` last 90 days); the archived ID ledger prevents duplicate aggregation on recovered/replayed records, and missing pre-activation history is never invented; sparse post-release samples cannot justify reliability claims. Interrupted durable supervisor diagnostics record log sizes/timestamps and optional shared-cgroup OOM deltas, not individual root cause proof.\n- **Measured recovery and reliability:** `delegate_telemetry` now reports `recovery` (attempted tasks, process successes after same-conversation recovery, remaining failures, initial error-code counts) and `reliability_excluding_location_and_cancellations` for retained records and rolling 24h/7d windows. A reported success means the process completed, **not** independent semantic acceptance. Exact location refusals and explicit cancellations remain in raw operational counts, but are omitted from the reliability denominator; generic account verification problems still count. The bounded 90-day `daily_history` retains aggregate recovery/location/cancellation counts when records are pruned, without paths, prompts or conversation IDs. Existing telemetry records lacking an original error-message classification are labelled `pre_activation_unclassified` and omitted from location-adjusted success rates, rather than guessing whether they were location refusals. Sparse samples and pre-feature history must never be treated as measured recovery rates.
- **Delegate reliability and acceptance:** Claude CLI is probed with cached `claude auth status`, not merely command presence. AGY model catalogs are checked per-account with isolated HOME, and Flash effort is aligned with the model suffix. An interrupted supervisor without a verified exit is `durable_job_interrupted`, never `harness_output_invalid` or `empty_harness_result`; inspect partial logs and Git state before retrying a writer. Treat only known ignored generated outputs (Python caches, JS/TS build caches and .NET artifacts) as non-source drift; never suppress tracked files. Large ignored node_modules trees retain a bounded metadata fingerprint. AGY1/2/3 eligibility watchdogs are isolated by account.
- **Premium escalation is explicit, never a silent fallback.** Use `harness="codex", complexity="high"` for an intentional GPT-6.1 Sol Medium route, or specify `model="gpt-6.1-sol", reasoning_effort="xhigh"` when a stronger Codex review is justified. For an explicit Opus run, pin the AGY account, select `backend="cli"`, and pass an ID from `delegate_harnesses(agy_account="antigravity2")` for the selected account (AGY2 in this example). The ACP backend accepts Flash models. Configured model names express operator preferences and may outlive the provider catalog. These paths are for independently justified escalation after reviewing Gemini outputs and failed checks, not automatic quota relief. Astra Medium remains external/manual and is not an MCP CLI harness.
- Read the returned `routing` / `delegate_routing` hints. When `automatic_routing=true`, omit `harness` for normal work: the server prefers its configured primary and quota-balances admissible fallbacks. Set `harness` only when intentionally pinning or diagnosing a provider; an explicit choice always wins. When automatic routing is disabled, use the reported profile guidance as before.
- For ordinary bounded repository discovery and independent review, omit the harness and choose `complexity=low` for Gemini Flash Medium or `complexity=medium/high` for Gemini Flash High. The local Qwen scout/reviewer may be used intentionally via an explicit local harness pin, but it is no longer the automatic default in Gemini-first mode.
- The optional `local` harness is explore-only in this release. It exposes server-enforced read-only repository tools to an OpenAI-compatible model endpoint, disables local thinking by default, and may disappear when the workstation is offline. Automatically routed local runtime failures (`local_openai_unavailable`, `local_openai_error`, or timeout) are retried once on the next admissible cloud tier inside the same delegate task; explicit `harness=local` diagnostics are never silently rerouted. Do not send implementation/code tasks to `local`.
- For issue-sized implementation, prefer the `implementation` profile and a verified `kind=code` delegate. Reserve direct MCP writes for tiny deterministic edits where the exact patch is already known, normally no more than about two files / ~80 changed lines. Use a clean dedicated worktree, `commit_mode=forbidden`, explicit scope, verification commands, and change budgets. ChatGPT owns review/integration.
- Classify code-task complexity before delegation. Use `complexity=low` for simple implementation on Gemini Flash Medium, `complexity=medium` for cross-module work on Gemini Flash High, and `complexity=high` for demanding work on Gemini Flash High with high reasoning. Codex/Claude require an explicit harness choice. `reasoning_effort` remains independently configurable. Stop at the first verified PASS and perform independent review of results.
- When continuing the same Antigravity investigation, prefer `resume_from_delegate_id` over starting a fresh broad-context review.
- Completed historical delegate records are replayed during MCP startup. A transient provider cooldown must retain the original completion time and never restart from recovery time; stale eligibility errors must not relaunch account watchdogs.
- If AGY reports `status=ERROR` after producing a structured response, Opss retains that response as advisory evidence and a durable result artifact while keeping the delegate `failed`. If the provider error is a narrowly recognized HTTP streaming EOF and the read-only response contains unblocked findings with `status=succeeded`, the Recovery Controller may make **one** quota-approved same-account continuation with cumulative tool observations and the original time budget, preserving the previous answer in a private sidecar. This is not acceptance, and source anchors must still be checked. Location refusals, quota failures, unrelated EOFs and completed advisory errors are not upgraded automatically.
- An incomplete final AGY NDJSON line is `antigravity_stream_truncated`, never a successful result. Opss retains valid preceding events and extracts `conversation_id` from init/progress, even when the stream ends before a final `result`. On an exact same-account/same-project continuation, prefer `resume_from_delegate_id`; review pending tool-call attention without resetting its cumulative count.
- **Recovery Controller (pilot):** One automatic same-account AGY continuation is permitted for an incomplete read-only result. A `delegate_reported_blocked` review may also resume if every blocker concerns unavailable AGY shell execution of `git diff`, an Opss-prepared scoped Git packet is present and still matches the worktree, and no other blocker exists; continue with `read_file`, not shell bypass. For verified code writers, one same-conversation `repair_code_and_reverify` is permitted only after a real server-owned test failure, clean captured Git baseline, explicit limited edit scope and tests, no scope/line-budget/commit violations, and unchanged Git fingerprint. Original checks run again after the repair. Fresh quota admission, verified conversation UUID and remaining time are required; read-only tool-call observations remain cumulative and exceeding the attention threshold does not block recovery. No automatic retries for location, quota, permission, timeout, cancellation, exhausted execution time, unsafe Git or a second failure. The first attempt's logs and structured result remain private evidence. Codex/Claude automatic fallback and semantic auto-acceptance remain disabled.
- For slow AGY research, inspect `harness_metadata.tool_call_counts` and `tool_calls` before diagnosing provider latency. Large `read_file` counts often indicate excessive file traversal. Give the reviewer exact changed symbols and bounded relevant line ranges; tool counts contain no input paths or prompts.
- **Autonomous read-only recovery when AGY shell is unavailable:** If a scoped AGY investigation explicitly asks for `git diff`, Opss prepares a bounded, tracked-file diff snapshot at submission time through its own Git subprocess, without relying on AGY `run_command` or PTY. The packet includes HEAD identity, explicit paths, a SHA-256 digest, and limitations (no untracked files, possible staleness, 48 KiB ceiling). The agent should continue with `read_file` and the packet rather than return `blocked` solely because `run_command` cannot spawn. If the diff is too large, reduce scope; never claim the omitted diff was checked. This does not override missing tests, unsafe approvals or source verification.
- Quota thresholds are admission-only. If a harness reports `quota_threshold_reached`, do not create new work on that harness. Never cancel already-running or already-queued work because a threshold changed, and dedupe/attach to an already active matching delegate remains valid.
- Do not cancel a healthy running delegate merely because the current response window ended or a result is taking longer than expected. Use `await_delegate`, `delegate_status`, or the durable result inbox. Public `delegate_cancel` protects running work by default; `force_running=true` is reserved for intentional termination after inspection.
- Codex explore uses a native read-only sandbox and an ephemeral session.
- Claude explore uses print-mode JSON with `permission-mode=plan` and no interactive permission prompts; code mode uses the configured permission policy.
- Antigravity CLI explore uses native JSON, `mode=plan`, and `--sandbox`; CLI code mode uses `accept-edits` and may optionally enable the configured permission bypass. Antigravity delegates use the durable job-registry backend when `delegate_harnesses` reports `durable_execution=true`, so a running delegate may be adopted after an MCP/server reload instead of being killed with the foreground request.
- Antigravity's command stream can surface a transient `read s1: resource temporarily unavailable` on wide recursive shell scans. Keep shell discovery scoped and bounded; if that exact error appears, retry at most once with a narrower path/output bound instead of issuing a redundant task kill.
- Pi explore disables sessions, project trust/context, extensions, and Pi-local skills, and allows only `read,grep,find,ls`. Pi code runs non-interactively with project trust enabled and the normal Pi tool set.
- A custom harness may accept explore work only when its adapter explicitly provides a read-only command. A prompt saying “read only” is not a sandbox.
- Every explore task forces `commit_mode=forbidden`. CLI tasks receive a before/after Git-status audit when available; ACP enforces read-only mounts and avoids host Git status/diff, which could execute repository filters.

Choose explore for search, mapping, evidence collection, code review, or independent verification. Choose code only when file or repository changes are required.

## Build a bounded request

Provide `task` or `goal` and keep `cwd` narrow. Add only the fields that improve the execution contract:

- `files_in_scope`: paths the delegate may inspect or change.
- `out_of_scope`: paths, actions, or topics to avoid.
- `context_files`: important files to read first.
- `acceptance_criteria`: observable conditions that must hold.
- `done_means`: required artifacts or evidence in the final manifest.
- `verification_commands`: server-owned acceptance checks for verified code work. The coding agent must not run these exact declared commands; the MCP server runs them only after the agent exits. The agent may report separate reasoning or narrowly targeted development checks, but self-report is not acceptance.
- `max_changed_files`, `max_added_lines`, `max_deleted_lines`: optional server-owned change budgets for verified code work.
- `commit_mode`: `allowed`, `required`, or `forbidden`; explore always becomes forbidden. Prefer `forbidden` for delegated implementation so ChatGPT remains the integrator.
- `model` and `reasoning_effort`: omit/default to inherit the harness defaults unless a task needs an explicit override.
- `output_schema`: expected JSON shape metadata. It guides the delegate but is not server-side schema validation.
- `resume_from_delegate_id`: for Antigravity only, continue the conversation recorded by a terminal delegate in the same project. The source must have a valid persisted `conversation_id`; this resumes model context, not process execution.
- `long_running=true`: opt into an 8-hour execution lifetime for a substantial `delegate_task` or every `delegate_batch` child. This does not extend the MCP response wait; keep `wait_seconds` short and retrieve progress by `delegate_status` or `await_delegate`. An explicitly supplied `execution_timeout_seconds` takes precedence; the supported maximum is 86,400 seconds (24 hours). Use this only for genuinely long work, not as a replacement for bounded slices.

Prefer one cohesive module or concern per code task. Split unrelated work into separate calls.

For implementation, pass a short execution brief in task: required behavior,
accepted audit facts with source anchors, exact existing/new targets, decisions
already made, exclusions, and one observable completion condition. A missing
fact is an explicit question, not permission to repeat a repository-wide audit.
Use code work only when editing is necessary; deterministic small edits can be direct.

Tool-call counts are attention thresholds, never automatic termination limits.
At the configured threshold, await_delegate and session_resume return
ATTENTION_REQUIRED for running work. Inspect progress, the latest calls, elapsed
time, useful changes and blockers. Continue productive work, correct a loop or
wrong direction, or intentionally stop only after that review. Record the
review through delegate_supervisor(action="acknowledge_attention",
observed_calls=C, command_id=...). The next alert is at C + threshold; a new
alert is not suppressed when calls advance during review. Acknowledgement does
not extend time or scope, and cannot accept a result. Terminal results remain
available for consumption and independent verification.

For ACP correction, request a cooperative checkpoint, inspect its bound diff,
draft and report, then continue/steer/suspend using the exact revision and
checkpoint ID. A checkpoint request is NOT proof of a pause. CLI has no live
steering: acknowledge to keep observing, cancel deliberately, or resume a
terminal task with a corrected brief. Ordinary active-session tool results also
carry pending attention metadata; there is no background wake of an inactive
chat or automatic premium-provider call.

Scoped ACP writers with an attention threshold of at least 16 retain an early
cooperative checkpoint after 4-8 observed native calls and another advisory
review at 75% of the threshold (or an observed event within the last 30 seconds
of a native budget initially at least 60 seconds). Manual requests take precedence.
These checkpoints do not turn the threshold into a cap. The execution deadline,
output bounds, permissions and scope checks still apply. Do not reset counters,
switch accounts or replay an uncertain writer to evade review.

Failed ACP writers also attempt to retain valid scoped edits after native
teardown. Retention is not verification or acceptance; inspect delegate_draft,
reconcile uncertain outcomes, then explicitly select the saved snapshot.
Unsafe/out-of-scope candidates are refused and the original failure is preserved.
Review only the change and specific risks using delegate_packet and existing
receipts. Report a reproducible defect and source evidence for each blocker.
Assess useful results by independent acceptance, elapsed time, intervention
and discarded work, not process success, token volume or generated line count.

For repository-backed delegates the server injects a compact project context containing the project root and submission HEAD. It deliberately does not embed a full diff or repository listing. For read-only AGY tasks with explicitly scoped Python files, the server also injects bounded Code Map symbol ranges ranked against the task wording. These are navigation hints only, not verified evidence, and never walk the full repository. Supply `files_in_scope` and `context_files` when the delegate needs narrower context, and let the delegate inspect Git status only when relevant.
- **Tool-call attention:** the existing `max_tool_calls=1..64` field configures a review interval, not a maximum number of allowed calls. Scoped low/medium AGY read-only tasks with concrete Python files default to 24/48; ACP and local tasks use their configured thresholds. Broad unscoped/high-complexity AGY research has no automatic count threshold unless specified. AGY counts unique tool-step events, deduplicates ACTIVE/DONE updates and excludes `finish`. The task continues after crossing the threshold and may succeed; exceeding it never kills the process, revokes the ACP gateway or discards local tool calls. The controller owns review and the decision to continue, correct or stop. Malformed streams remain errors independently of the count.

Choose `commit_mode` deliberately for code work. The tool default is `forbidden`, which is preferred for delegated implementation: leave reviewable working-tree changes and let ChatGPT own commit/merge/integration.

## Understand scheduling

- Explore tasks are project-scoped readers and may overlap within configured limits.
- Set `work_role="review"` on an explore task or batch child reviewing a change. Other readers default to `analysis`; code tasks use `implementation`. Task IDs are labels only.
- Across linked worktrees, the default repository ceilings are 4 analyses and 2 reviews. Configure these with `CHATGPT_MCP_DELEGATE_ANALYSIS_MAX_PER_PROJECT` and `CHATGPT_MCP_DELEGATE_REVIEW_MAX_PER_PROJECT`. Group, account and global limits still apply; a ceiling does not reserve provider capacity.
- Code tasks are worktree-scoped writers, exclusive and FIFO within one worktree.
- Once a writer is queued in a worktree, later readers in that same worktree cannot overtake it.
- Linked Git worktrees get distinct writer lanes. Repository-level `max_code_per_project` still caps how many worktree writers from the same Git common directory may run concurrently.
- Different repositories schedule independently within global limits. ChatGPT remains responsible for merge/rebase/repo-admin integration across worktrees.
- `delegate_batch` creates a read-only group barrier. Every child is implicitly explore with commits forbidden; do not put `kind` or `commit_mode` in child specifications. `max_concurrency` caps that group within server limits.
- For independent ACP questions, submit separate `delegate_task(kind="explore", work_role="analysis", backend="acp", harness=..., wait_seconds=0)` requests before awaiting their IDs. Choose eligible accounts using `server_info.delegate_harnesses[account].account_concurrency`. Check `delegate_role_limits_per_repository` when deciding how many tasks fit. A capacity-first route does not automatically split one question.
- For a single-account AGY CLI batch, pin the harness and assign distinct tasks, scopes and model effort. Each child has its own session and shares account capacity with other readers and writers. Batch `max_concurrency` also limits the group. Use the review role for independent review and a separate worktree for each concurrent writer.
- Team batches are read-only by design: do not put code-changing tasks in them. Review each child's evidence, have ChatGPT reconcile disagreement, then implement through `delegate_task(kind="code")` in one worktree at a time or isolated feature worktrees. No automatic cross-agent context sharing or consensus is implied by a batch. Remove one child for a quota-saving 3-agent version. Use `long_running=true` only when the scope justifies eight-hour execution.
- A reader or writer using `depends_on_group_ids` waits until all listed groups from its own logical session finish, including groups that failed or were cancelled. Inspect each group's status and independently verify its evidence before using the findings.
- Dependencies control scheduling only. They do not inject one harness's output into another harness's prompt. Read the completed child results/logs, then explicitly summarize the necessary evidence in the follow-on `task`/`goal` or pass existing result paths through `context_files`.

## Wait, monitor, and cancel

- Start with one narrowly scoped task on an eligible Flash account. Give it the acceptance check and only the source context it needs. For independent tasks use separate scopes. If a checkpoint repeats a plan without a changed file, inspect the blocker before authorizing more work.
- For a self-contained change, call `delegate_task(kind="explore", backend="acp", harness="antigravity", work_role="proposal", task=..., wait_seconds=0)` with an eligible AGY account and supply the interface, relevant source excerpt and constraints in the task. The delegate has an empty workspace. Omit file scopes, verification commands and resume IDs. Retrieve the literal candidate from the owned review packet's `proposal` field (up to 8192 UTF-8 bytes). Check availability and the pinned source revision, inspect the entire candidate, and apply and test it independently. Use analysis for repository questions and an isolated writer for direct edits.
- Prefer `await_delegate(view="triage")` and `delegate_packet(purpose="triage")` for first inspection. Open the review packet or inspect the actual diff when deciding whether to accept, especially after failed checks, stale evidence, blockers or omissions. Keep independent checks and count unsuccessful attempts when comparing costs. Provider-reported usage and estimated response tokens are different measurements.

- `wait_seconds` controls only how long the current MCP call waits. On expiry, use the returned `delegate_id` or `group_id` with `delegate_status`.
- `execution_timeout_seconds` is the hard subprocess lifetime. On expiry, the server sends TERM, waits for the cancel grace period, then sends KILL.
- For unusually large CLI work, set `long_running=true` rather than using a short ad-hoc execution deadline. It raises the hard lifetime to eight hours and allows a shorter `wait_seconds` (even zero) without cancelling the worker. A finite 24-hour maximum still applies when explicitly overridden. ACP retains its separate 900-second maximum.
- Use `delegate_status(watch_seconds=...)` for lifecycle long polling; the maximum watch window is 300 seconds.
- Query by exactly one of `delegate_id`, `group_id`, or `project_cwd`, or omit all for global active/recent state.
- Use `delegate_cancel` with exactly one delegate or group identifier. Cancellation is not a rollback; inspect Git state after cancelling code work.
- After a server reload, use `delegate_harnesses.runtime` or `server_info.delegate_runtime` to inspect recovered task/group counts, adopted durable work, persisted-record count, and the last startup recovery summary. `recovered_from_disk=true` on a task means the current registry adopted state reconstructed from persisted metadata.

## Read results and logs

For Release 1 review or handoff, start with `delegate_packet(delegate_id=..., purpose="review"|"handoff")` for an owned terminal result. The bounded packet separates server observations from agent claims and reports whether the candidate still matches. It does not run tests, accept code, extend scope, or select a saved draft. Historical checks with missing environment/fixture identity remain historical evidence, not permission to skip fresh execution or live acceptance. Reuse visible evidence for the same review; run an additional check only for a specific unresolved risk.

ACP gateway diagnostics describe only observed gateway calls. Repeated request/response fingerprints are advisory; native edits, native reads and reasoning are not fully observed. No diff and a quiet heartbeat do not prove wasted work. Existing automatic CLI recovery remains limited to one attempt; diagnostics never start a second repair controller or cancel a worker.

Inspect observed health under each account's `execution_backends` separately. CLI success does not prove ACP health. Unknown/stale observations, account-wide authentication/quota gates and uncertain writer outcomes must remain distinct. An uncertain write requires reconciliation, never blind replay or a transport switch.

Completed results intentionally omit raw stdout and stderr. Use the returned `logs` or `log_read_hint` paths:

- `prompt`: exact execution prompt.
- `stdout`: final manifest or requested structured JSON.
- `stderr`: progress and diagnostic output.
- `metadata`: lifecycle, process, harness, byte counts, and result details.

When `parse_structured_output=true`, the server makes a best-effort parse of stdout first and stderr second. It accepts a final fenced JSON block or a whole-stream JSON value. A parse failure returns `structured_output=null`; read the logs. For legacy/unverified delegates, review files and run direct verification even when the delegate reports success. For verified code delegates, inspect `code_verification`: `status=succeeded` is emitted only after the server-owned scope/budget/verification checks pass.

## Recover from failures

| Error/status | Response |
| --- | --- |
| `unsupported_delegate_harness` | Call `server_info`, choose a configured harness, and retry. |
| `delegate_harness_unavailable` / `codex_unavailable` | Check the configured command and runtime PATH. |
| `readonly_sandbox_unavailable` | Use a harness that explicitly supports read-only explore tasks. Do not bypass the guard. |
| `delegate_queue_full` | Monitor existing work, cancel obsolete work if authorized, then retry later. |
| `process_start_failed` / `process_failed` | Read stderr and metadata, correct the command/task, and submit a new bounded delegate. |
| `code_verification_baseline_dirty` | Use a clean dedicated worktree; verified code delegates do not start from an ambiguous dirty baseline. |
| `code_scope_violation` / `code_change_budget_exceeded` | Inspect the diff, tighten or intentionally revise the contract, then resubmit; do not accept the result as complete. |
| `code_verification_failed` | Inspect the server-run verification command output and fix the implementation before retrying. |
| `readonly_violation` | Stop trusting the explore result, inspect Git state, and determine what changed. |
| `readonly_audit_unavailable` | Inspect repository health and status before retrying. |
| `timed_out` | Read partial logs and decide whether to split the task or use a larger justified execution timeout. |
| `cancelled` | Inspect partial output and repository state before resubmitting. |
| `delegate_resume_not_found` / `delegate_resume_not_terminal` | Resolve the exact source delegate and wait until it is terminal before resuming. |
| `delegate_resume_harness_mismatch` / `delegate_resume_project_mismatch` | Resume only an Antigravity delegate from the same project identity. |
| `delegate_resume_conversation_unavailable` | The source has no valid persisted Antigravity `conversation_id`; submit a new bounded delegate instead. |

Do not assume retries are idempotent, especially for code tasks.

## Minimal examples

Read-only exploration:

```json
{"task":"Map the scheduler entry points and return file:line evidence.","kind":"explore","harness":"pi","cwd":"/path/to/repo","files_in_scope":["src"],"commit_mode":"forbidden"}
```

Independent exploration batch:

```json
{"tasks":[{"task":"Find invocation construction."},{"task":"Find scheduler fairness tests."}],"harness":"codex","cwd":"/path/to/repo","max_concurrency":2,"wait_seconds":30}
```

One-account, four-role Antigravity team (one account, four independent read-only AGY sessions):

```json
{"harness":"antigravity","cwd":"/path/to/repo","max_concurrency":4,"wait_seconds":0,"long_running":true,"tasks":[{"task_id":"architect","task":"Review architecture and return grounded tradeoffs.","model":"gemini-3.8-flash-high","reasoning_effort":"high","files_in_scope":["src"]},{"task_id":"researcher","task":"Map entry points and dependencies.","model":"gemini-3.8-flash-low","reasoning_effort":"low","files_in_scope":["src"]},{"task_id":"tester","task":"Identify coverage gaps.","model":"gemini-3.8-flash-medium","reasoning_effort":"medium","files_in_scope":["tests"]},{"task_id":"reviewer","task":"Independently identify risks.","model":"gemini-3.8-flash-medium","reasoning_effort":"medium","files_in_scope":["src","tests"]}]}
```

Monitor the returned group with `delegate_status(group_id=...)` or `await_delegate(group_id=...)`. Read each child's results independently before synthesizing findings. An AGY batch shares account credentials and quotas, never prompts or conversation history.

Bounded implementation:

```json
{"task":"Implement the approved adapter change.","kind":"code","harness":"pi","cwd":"/path/to/repo","files_in_scope":["src/module.py","tests/test_module.py"],"acceptance_criteria":["Existing behavior remains compatible","Targeted tests pass"],"verification_commands":["pytest -q tests/test_module.py"],"commit_mode":"forbidden","wait_seconds":30}
```

Continue a terminal Antigravity conversation:

```json
{"task":"Continue from the prior review and verify the corrected implementation.","kind":"explore","harness":"antigravity","cwd":"/path/to/repo","resume_from_delegate_id":"abc123abc123","wait_seconds":30}
```
"""


FILE_USE_GUIDE = """---
name: file-use
description: Use the local file discovery, search, reading, code-map, and editing tools safely. Load before the first nontrivial file workflow or file mutation, and when handling pagination, token budgets, encodings, revisions, PDFs, images, or binary data.
---

# File Use

## Critical rules

1. Treat the workspace root as a relative-path anchor, not a sandbox boundary. Keep every path intentionally scoped.
2. Inspect before mutating: discover, search, read the relevant region, assess references/imports when useful, then edit and verify.
3. Follow `next_offset` or the relevant line/page/byte cursor whenever `partial=true`; never assume the first page is complete.
4. Prefer `apply_patch` for semantic edits, `replace` for bounded mechanical replacements, and `write_file` only for deliberate whole-file creation or overwrite.
5. Use dry-run/validation and revision checks for risky or multi-file edits. Preserve user changes and re-read after a revision conflict.

## Choose the tool

| Need | Tool |
| --- | --- |
| Browse a directory with ignore-aware filtering | `list_files` |
| Find paths, literal text, or regex matches | `search` |
| Read line-oriented text, singly or in a batch | `read_text` |
| Read text with encoding metadata, an image, PDF pages, or binary hex | `read` |
| Locate definitions, textual references, or imports | `code_map_symbols`, `code_map_references`, `code_map_imports` |
| Check a C# file's membership in a selected project build | `code_map_csharp_membership` |
| Create or intentionally replace a whole file | `write_file` |
| Apply literal/regex replacements with revision protection | `replace` |
| Apply a structured semantic patch | `apply_patch` |

Use `list_files` for shape, `search` for candidates, and `read_text`/`read` for evidence. Code-map results are lightweight navigation aids: references are identifier-boundary text matches, not a language-server proof of semantic usage.

For C# navigation, search with `code_map_references` and `glob="*.cs"`. Check the solution configuration, project references and evaluated `Compile` items before tracing a call into a candidate file. For reflection, record the selected assembly and type separately from the call site.

Use `code_map_csharp_membership` with the configured repository root, committed ref, explicit project and source paths, and the project's mapped configuration/platform. On a multi-target project, select an inner target framework. Evaluation failures leave membership unknown. Operators enable the pilot with `CHATGPT_MCP_CSHARP_ALLOWED_ROOTS` (exact canonical primary checkout paths separated by the OS path separator) and `CHATGPT_MCP_CSHARP_DOTNET` (absolute operator-owned dotnet executable).

The evaluator runs Linux MSBuild. Pass an optional repository-relative classic `.sln` to resolve project participation and dimensions using Microsoft.Build at the same Git ref. Without it, supply project dimensions directly. A solution exclusion applies to this project. Windows-specific conditions, path casing and build-generated files can require evidence from the Windows build.

## Discover and paginate

- `list_files` hides hidden paths, common junk, and Git-ignored paths by default. Use `filter=all` only when hidden/ignored content is intentionally in scope.
- `search` supports `glob`, literal `text`, and `regex`, plus sequential or parallel batches. A batch has at most 20 queries and parallel concurrency is capped at 3.
- Search output modes are `content`, `files_with_matches`, `count`, and `summary`. Choose the smallest representation that answers the question.
- Results are token-budgeted. Inspect `complete`, `partial`, `truncated`, `stop_reason`, `estimated_tokens`, and `effective_budget`.
- Continue with `next_offset`; for text reads use `start_line`, for PDFs use page ranges/offsets, and for hex use `byte_offset`. Do not invent a cursor from the number of visible items.

## Read the right representation

- `read_text` uses 1-based lines and is the compact choice for source evidence. It may return one oversized line intact while still advancing `next_offset`.
- `read(mode=auto)` selects text/image/PDF/hex handling. Text results include encoding, BOM, and newline metadata.
- Text decoding is conservative: BOMs are recognized and UTF-8 is strict. On `encoding_error`, use the reported candidates or an explicit encoding; when NUL bytes indicate binary data, retry with `mode=hex`.
- PDF page numbers are 1-based. The default reads only an initial bounded set and one call is capped, so continue explicitly when more pages matter.
- Hex reads are byte-based and bounded; use them for inspection, not as a substitute for an appropriate binary parser.

## Edit safely

- `write_file(dry_run=true)` previews a whole-file write without touching disk. Real writes use the shared mutation lock, atomic replace, verification, and rollback path.
- `apply_patch(dry_run=true)` or `validate_only=true` checks a patch without writing. Real multi-file patches hold all target locks through planning and commit, recheck the planned revisions before the first write, and roll back already attempted path mutations on failure. Use `return_diff=true` when review needs the resulting diff.
- `replace` requires nonempty operations and rules. Start with `replace(dry_run=true)` and capture each returned `before_revision`; ordinary read tools do not return this revision. Before the real write, put that value into the matching operation's `expected_revision` for compare-and-swap protection.
- `revision_conflict` means another writer changed a target: re-read, re-plan, dry-run again, and use the new `before_revision`. Never remove the check or blindly retry.
- A replace batch is planned and revision-checked before its first write. Replacement limits are batch-wide; exceeding them rejects the batch. Writes are atomic per file, and a later failure triggers best-effort rollback of already attempted files.
- Replacement writes preserve detected encoding, BOM, newline convention, and permissions, and report `rolled_back`/`rollback_errors` when a write fails.
- After any mutation, re-read the changed region and use Git diff or the relevant verification command.

## Recover from errors

| Error | Response |
| --- | --- |
| `path_not_found` / `not_a_directory` | Recheck the resolved path and current default cwd. |
| `encoding_error` | Retry with a reported encoding candidate or inspect as hex. |
| `revision_conflict` | Re-read the current content/revision, reassess user changes, and rebuild the edit. |
| `budget_exceeded` | Narrow scope or lower result density; do not assume omitted data is absent. |
| `write_failed` | Inspect `rolled_back` and `rollback_errors`, then verify every target before retrying. |
| Invalid search pattern/backend error | Correct the mode/pattern or confirm the required local backend is installed. |

## Minimal examples

```json
{"mode":"glob","path":"src","pattern":"*.py","limit":20,"offset":0}
```

```json
{"path":"src/app.py","start_line":120,"line_limit":40,"include_line_numbers":true}
```

```json
{"operations":[{"path":"src/app.py","rules":[{"pattern":"old","replacement":"new","literal":true}],"expected_revision":"<sha256>"}],"dry_run":true}
```
"""


CODE_GRAPH_USE_GUIDE = """---
name: code-graph-use
description: Use immutable Joern Code Graphs for semantic callers, callees, impact, and bounded call paths. Load before the first code_graph_* workflow when review or refactoring needs semantic call-graph evidence beyond lightweight code_map_* navigation.
---

# Code Graph Use

## Critical rules

1. Use code_map_symbols, code_map_references, and code_map_imports first for cheap navigation and scope discovery. Escalate to code_graph_* only when semantic call relationships, reverse impact, or call paths materially affect the decision.
2. A Code Graph represents one exact committed Git tree. Dirty and untracked working-tree changes are never silently included under a committed identity. Choose ref deliberately.
3. Canonical startup sequence is always: code_graph_status(ref) -> if query_ready=false, code_graph_prepare(ref) -> if job_id is returned, await_job(job_id) -> code_graph_status(ref) again -> semantic query only after query_ready=true. Do not probe Joern with a semantic query to discover readiness.
4. Symbol names may be ambiguous. Read matches, total_matches, and ambiguous; when ambiguous, retry with the exact full_name instead of guessing.
5. Treat the CPG as static semantic evidence, not runtime proof. Dynamic imports, monkey patching, dependency injection, reflection, generated code, and runtime dispatch can escape static resolution. Keep focused tests and runtime acceptance as independent gates.

## Start here

For any Code Graph task, do not search for a shell command or call Joern directly. Use the MCP lifecycle:

    1. code_graph_status({"cwd":"/repo","ref":"HEAD"})
    2. If query_ready=true -> run the semantic code_graph_* query.
    3. If query_ready=false -> follow status.next_action and call code_graph_prepare with the same cwd/ref.
    4. If prepare returns job_id -> await_job(job_id).
    5. Call code_graph_status again; continue only when query_ready=true.

code_graph_prepare is the canonical graph/runtime launcher. It handles both initial CPG build and persistent Joern runtime prewarm.
Query tools never auto-build or auto-prewarm; readiness is established only through the status/prepare lifecycle above.

## Choose the tool

| Need | Tool |
| --- | --- |
| Resolve graph identity and cache/backend state | code_graph_status |
| Build or reuse the immutable CPG for one committed ref | code_graph_prepare |
| Direct semantic callers | code_graph_callers |
| Direct semantic callees | code_graph_callees |
| Bounded transitive reverse-call impact | code_graph_impact |
| Git diff -> changed Python symbols -> affected files/tests | code_graph_diff_impact |
| One deterministic bounded forward call path | code_graph_path |

code_map_* and code_graph_* are complementary. Code-map tools are fast navigation aids; Code Graph queries use Joern CPG semantics and a content-addressed cache keyed by repository identity, Git tree, analyzer identity, schema version, and analysis options.

## Lifecycle

Preferred review/refactor flow:

    cheap code_map discovery
            ↓
    code_graph_status(ref)
            ↓
    query_ready=true? -> no -> code_graph_prepare(ref)
      |                           ↓
      |                  await_job(job_id)
      |                           ↓
      +------------------> code_graph_status(ref)
            ↓
    callers / callees / impact / diff-impact / path
            ↓
    targeted reads and implementation
            ↓
    focused tests -> expanded suite -> runtime acceptance

- code_graph_prepare is idempotent for the same immutable graph identity and prepares both the immutable CPG and its persistent query runtime. A cache hit returns immediately only when the runtime is also ready; otherwise the same durable prepare path prewarms it.
- Build source comes from the committed Git snapshot, not the dirty worktree.
- Query tools require a ready cache entry and fail closed with code_graph_not_ready otherwise.
- Semantic query tools never cold-start Joern. If the persistent runtime is absent or was evicted, they fail closed with code_graph_runtime_not_ready; call code_graph_prepare and await its durable prewarm instead of holding a foreground query open.
- Persistent Joern startup is allowed a bounded 120s by default because larger CPGs can require materially more than 30s to load; this wait belongs to the durable prepare job, not the semantic query call.
- Query servers use network=none, expose no host port, mount the immutable CPG read-only, and are reached only through docker exec to container loopback.
- Cache manifest v2 records the CPG SHA-256. READY validation hashes changed/new payloads, then memoizes validation by bounded stat identity (device, inode, size, mtime, ctime), so unchanged warm queries do not reread the full CPG; same-size replacement with restored mtime still rehashes because inode/ctime changes.
- Warm-server reuse consumes the already validated manifest digest and verifies both graph/content fingerprints and the actual Docker contract (image, command, user, memory/CPU/PID limits, tmpfs, mounts, network, read-only root, capabilities, and no host ports), not labels alone.
- JOERN_QUERY_SERVER_MAX_CONTAINERS bounds resident servers (default 1). Starting another graph evicts older owned query servers when capacity is exceeded.
- Workers reconcile orphaned Joern servers before cache-hit/build handling, and GC evictions remove matching query servers. This makes retry after a failed post-GC cleanup convergent.
- Owned query servers are cleaned up strictly on normal MCP lifespan shutdown when Code Graph is enabled; with Code Graph disabled, shutdown does not require Docker.
- impact includes the seed at depth 0 and follows callers outward up to max_depth.
- code_graph_diff_impact compares committed refs only. For branch/PR review keep use_merge_base=true; for an exact commit-to-commit comparison set it false deliberately.
- diff-impact maps Python hunk ranges to function/method spans from Git blobs, then queries reverse impact only on the READY head graph. It never analyzes dirty/untracked worktree content under the head identity.
- changed test functions are direct affected tests and do not need a semantic reverse-impact query.
- deleted symbols are returned with impact_status=base_graph_required. Their reverse impact is not guessed from the head graph; prepare/query the comparison base graph when that evidence is required.
- module_scope_changes and unsupported_changed_files remain explicit incompleteness signals. Use imports/search or a later module-dependency analysis instead of treating call-graph impact as exhaustive for those changes.
- diff-impact is changed-symbol paginated. Follow next_offset until pagination_complete=true; impact_complete additionally requires no unresolved dynamic calls, deleted-symbol gaps, module-scope gaps, unsupported files, or parse errors.
- unresolved_call_sites remain per-symbol evidence, but diff-impact does not promote dunder same-name candidates such as __init__ into global candidate_affected_files/tests because those names create high-noise cross-class matches. Dunder evidence is sampled to 5 call sites while total_unresolved_call_sites and omitted_unresolved_call_sites preserve the full count. call_resolution_complete remains false, so the uncertainty is still explicit.
- path returns one deterministic bounded path from source to target when found.
- External methods and <operator>.* noise are filtered by default. Set include_external=true only when those nodes are intentionally relevant.
- For callers and impact, inspect call_resolution_complete before treating exact results as exhaustive. false means Joern observed same-name calls with methodFullName=<unknownFullName>; unresolved_call_sites are bounded candidate call sites, not asserted edges to the queried method.
- complete/partial/truncated describe response bounding, not proof that Python dynamic dispatch was fully resolved. Static CPG evidence can still miss reflection, monkey-patching, or other runtime dispatch.
- Results are bounded by limit and the common response budget. Inspect query_truncated, complete, partial, truncated, stop_reason, and returned/total counts before treating output as exhaustive.

## Recover from failures

| Error/status | Response |
| --- | --- |
| code_graph_not_ready | Call code_graph_prepare, await the build if started, then retry after code_graph_status reports ready. |
| code_graph_runtime_not_ready | Call code_graph_prepare and await its durable prewarm; retry only after code_graph_status reports query_runtime_ready=true. |
| code_graph_resolution_failed | Verify the repository cwd and committed ref; do not substitute a different ref silently. |
| code_graph_diff_resolution_failed | Verify both committed refs and merge-base assumptions; do not silently switch to the dirty working tree. |
| code_graph_query_failed | Inspect the reported Joern/backend error; keep the cached graph immutable and retry only after fixing the runtime/query cause. |
| ambiguous=true | Select an exact full_name from matches and retry. |
| not_found=true / target_not_found=true | Recheck spelling/full_name and use code_map_symbols or focused search to discover the intended definition. |
| query_truncated=true / partial=true | Narrow the symbol/scope or raise the bounded limit when justified; do not infer absence from omitted results. |

## Minimal examples

Status or prepare:
    {"cwd":"/path/to/repo","ref":"HEAD"}

Reverse impact:
    {"symbol":"persist","cwd":"/path/to/repo","ref":"HEAD","max_depth":4,"limit":50}

Diff impact for a branch/PR:
    {"cwd":"/path/to/repo","base_ref":"main","head_ref":"HEAD","use_merge_base":true,"max_depth":4,"symbol_limit":10}

Call path:
    {"source":"api","target":"persist","cwd":"/path/to/repo","ref":"HEAD","max_depth":6}
"""


PROCESS_USE_GUIDE = """---
name: process-use
description: Choose and operate synchronous commands, durable background jobs, and persistent tmux sessions. Load before the first run_command, job, or tmux workflow, especially for timeouts, long-running work, streaming logs, interactive input, or termination.
---

# Process Use

## Critical rules

1. Choose the lifecycle first: short synchronous command, durable background process, or interactive terminal session.
2. Use the narrowest cwd and command that satisfies the request. Shell access is not a filesystem sandbox.
3. A timeout or accepted input is not proof of completion. Inspect the returned status, exit code, logs, or terminal capture.
4. Kill only the exact registered job or tmux session requested. Termination does not roll back filesystem or external side effects.
5. Never use `force=true` to exceed the normal command timeout unless the user explicitly approved the longer synchronous execution.

## Execution-loop contract

- After every tool result, immediately choose one of two outcomes: invoke the next concrete tool, or return a checkpoint/final/blocker response.
- Do not stop in reasoning merely because the larger task is unfinished.
- Waiting requires verified observable progress. Use `execution_state` when that evidence is unclear.
- `activity_verdict=ACTIVE` may justify polling because process-group CPU time, output, recent start/output, or group membership shows progress.
- `activity_verdict=QUIET` is not a wait state: recheck or inspect PID/process-group/resource/log evidence instead of waiting blindly.
- `activity_verdict=STALLED_SUSPECTED` means repeated observations exceeded the quiet threshold without CPU/output/group progress; inspect the process or continue independent work. Never auto-kill solely from this verdict.
- `activity_verdict=DEAD` / `TERMINAL` and `state=NEXT_ACTION_REQUIRED` require the next concrete action or response now.
- A persistent or ready Codex runtime is infrastructure state, not evidence that a task is still executing.
- After two polls with no new information, use the verdict/evidence from `execution_state`; do not repeat empty polls.

## Choose the execution model

| Need | Tool family |
| --- | --- |
| Coherent bounded non-interactive work expected to finish within the foreground window | `run_command` |
| Unknown/unbounded non-interactive work or work that must survive disconnects, with durable stdout/stderr logs | `job_start`, `job_status`, `job_output`, `job_tail`, `job_kill` |
| An interactive TTY, persistent shell/application, or key input | `tmux_start`, `tmux_status`, `tmux_capture`, `tmux_send`, `tmux_kill` |
| Decide whether waiting is justified | `execution_state` |

Do not use a delegate for a deterministic command, a job for an interactive prompt, or tmux when lossless stdout/stderr logs are required.

## Run synchronous commands

- Provide exactly one of `command` or `commands`. Batch mode is `sequential` or `parallel`, has at most 20 commands, and parallel concurrency is capped at 3.
- `timeout` is one shared foreground wall-clock budget for the whole call. A single command may use that full budget; a sequential or parallel batch shares it across all child commands. Batch results preserve input order. Inspect `completed`, `failed`, and `timed_out` rather than only the batch envelope.
- Direct/local clients have a normal hard ceiling of 900 seconds and a default of 300 seconds. ChatGPT/OpenAI sessions on this deployment use a 30-second safe foreground budget because longer live MCP calls were observed to disconnect before their local timeout. Do not split coherent bounded work solely to fit that client window; run the whole command once with `job_start` when it is expected to take longer. Above the direct/local 900-second hard ceiling, `force=true` is reserved for explicit user-approved synchronous work.
- On timeout, the server terminates the command process tree. Treat partial output and side effects as real.

## Operate durable jobs

- `job_start` launches a registered background subprocess with private stdout/stderr logs and a durable state record discoverable after MCP/server restarts. It has no execution-time limit; the process runs until it exits or an authorized `job_kill` stops it.
- Jobs do not provide scheduling, automatic restart, dependencies, or artifact tracking. Build those semantics explicitly outside the job API.
- Save the returned `job_id`. If client state is lost, recover it with the newest-first, paginated `job_list`, then confirm identity and lifecycle with `job_status`.
- Use `job_status` for lifecycle/PID/exit information. Use `job_output` for lossless incremental reads with a raw-byte `cursor` and returned `next_cursor`.
- Stdout and stderr have independent cursors and no merged cross-stream ordering. `wait_ms` long-polls one stream for up to 30 seconds.
- Consume stdout and stderr separately: reuse each stream's `next_cursor`, continue while `has_more=true`, and treat `eof=true` as that stream being caught up after the job is terminal. Poll status as well; a quiet stream is not proof the job finished.
- `job_tail` is a quick bounded last-lines view, not a replacement for cursor-based consumption.
- `job_kill` targets only a server-registered job process group. Default to TERM; use KILL only when graceful termination has failed or is inappropriate.

## Operate tmux sessions

- `tmux_start` creates one detached primary-pane workflow. Session names are restricted identifiers; choose a unique stable name. Width and height are bounded.
- `remain_on_exit=true` preserves the pane after its command exits so status/capture can report the exit result.
- `tmux_status` reports panes, PIDs, current commands/paths, dimensions, and dead/exit state.
- `tmux_capture` returns a bounded visible terminal snapshot. It is not a complete or lossless stdout/stderr log and wrapped lines may need `join_wrapped`.
- `tmux_send` passes UTF-8 text through tmux's input buffer and supports only an allowlist of keys. `accepted_by_tmux=true` does not mean the application consumed the input; capture/status must verify progress.
- `tmux_kill` is idempotent and targets one exact session. No tool kills the entire tmux server.

## Recover from failures

| Error/status | Response |
| --- | --- |
| `cwd_not_found` / `cwd_not_directory` | Correct the resolved working directory before retrying. |
| `timed_out` | Inspect partial output/side effects; move long work to a job or use an approved larger limit. |
| Job `failed` / `interrupted` | Read both streams and status before deciding whether a restart is safe. |
| `session_exists` | Reuse/inspect the intended session or choose a new exact name. |
| `session_not_found` | List sessions on the configured socket and check the name. |
| Command/process start failure | Verify executable availability, PATH, cwd, and arguments. |

## Minimal examples

```json
{"command":"pytest -q tests/test_module.py","timeout":120}
```

```json
{"command":"python train.py","name":"training","cwd":"/path/to/project"}
```

Then consume one stream with `{"job_id":"job_...","stream":"stdout","cursor":0,"wait_ms":1000}`.

```json
{"session":"debug-api","cwd":"/path/to/project","command":"python -m app","remain_on_exit":true}
```
"""


RUNTIME_USE_GUIDE = """---
name: runtime-use
description: Manage persistent Codex runtimes with stable logical identities, reuse, bounded concurrency, and automatic detached-binding garbage collection. Load before the first codex_runtime_* or codex_mcp_* workflow.
---

# Runtime Use

## Critical rules

1. For recurring logical workers, use `codex_runtime_acquire` with a stable name such as `manager`, `plm-worker-01`, or `research-worker-02`. Do not create timestamp-suffixed names for every run unless a truly separate runtime is required.
2. Use `codex_runtime_list` to discover existing bindings and `codex_runtime_resume` when a specific known runtime identity must be continued. Use `codex_runtime_open` only for intentionally new isolated runtimes.
3. `codex_runtime_close` detaches the local binding; it does not archive or delete the upstream Codex thread. Any binding with no runtime use beyond the configured idle TTL is eligible for automatic GC, while capacity-driven LRU eviction is detached-only.
4. Capacity LRU never evicts a `ready` runtime. Idle-TTL GC can collect a `ready` binding only after it has had no runtime operation for the full TTL. Check `server_info.codex_runtime` for `max_concurrency`, `max_runtimes`, `idle_ttl_seconds`, and GC counters instead of assuming limits.
5. Runtime GC removes the local resumable binding only. If a binding has been collected, acquire the stable logical name again instead of assuming its old `runtime_id` remains valid.

## Choose the tool

| Need | Tool |
| --- | --- |
| Reuse or create a recurring named runtime | `codex_runtime_acquire` |
| Discover existing bindings | `codex_runtime_list` |
| Create a deliberately distinct runtime | `codex_runtime_open` |
| Resume one known binding/thread | `codex_runtime_resume` |
| Inspect one runtime | `codex_runtime_status` |
| Detach a runtime for later reuse | `codex_runtime_close` |
| Inspect or call MCPs connected through Codex | `codex_mcp_inventory`, `codex_mcp_call` |

## Lifecycle

Preferred recurring-worker flow:

```text
stable worker name
      ↓
codex_runtime_acquire
      ↓
reuse / resume existing binding, or open once
      ↓
work through codex_mcp_* as needed
      ↓
codex_runtime_close when the worker is idle
      ↓
reuse before TTL, otherwise acquire recreates it later
```

The server defaults to an 8-hour idle TTL for every binding. Capacity pressure additionally evicts the least-recently-used detached bindings first. Both policies are local binding lifecycle controls; neither performs destructive upstream thread deletion.

## Stable naming

- Names represent logical workers, not invocations. Prefer `manager` over `manager-20260917-1908`.
- Keep `cwd` and sandbox policy stable for the same logical name. `codex_runtime_acquire` matches all three fields exactly.
- Separate workers that may operate concurrently should have separate stable names, for example `plm-worker-01`, `plm-worker-02`, and `plm-worker-03`.
- If a workflow intentionally needs a fresh isolated context, use `codex_runtime_open` and give it a distinct descriptive name.

## Recover from errors

| Error/status | Response |
| --- | --- |
| `runtime_not_found` | The binding may have been GC/LRU collected; acquire the stable logical name again. |
| `runtime_not_active` | Resume the known runtime or acquire its stable logical name. |
| `runtime_limit_reached` | Inspect runtime counts; capacity LRU does not evict ready runtimes, so release obsolete active work before retrying. |
| `runtime_concurrency_limit` | Wait for one configured concurrent runtime operation to finish; do not create more runtimes to bypass the limit. |
| metadata mismatch | Keep the original cwd/sandbox for that binding or intentionally acquire/open a distinct runtime. |

## Minimal examples

Recurring worker:

```json
{"cwd":"/home/user/workspace","name":"plm-worker-01","sandbox":"full-access"}
```

Discover it later:

```json
{"name":"plm-worker-01","status":"detached","limit":10}
```
"""


GIT_USE_GUIDE = """---
name: git-use
description: Inspect repositories, review and create scoped commits, examine history, and manage Git worktrees safely. Load before the first Git workflow, especially before staging, committing, amending, creating/removing worktrees, or using force.
---

# Git Use

## Critical rules

1. Start with `git_status`; preserve unrelated staged, unstaged, and untracked user changes.
2. Review the relevant unstaged and staged diffs before committing. A clean-looking path filter does not prove the whole index is clean.
3. Stage narrowly with `paths` by default. Use `stage_all=true`, `amend=true`, `allow_empty=true`, or worktree `force=true` only when the requested scope clearly requires it.
4. Repository paths are resolved for convenience but the workspace root is not a sandbox boundary. Verify `repo_root` and every worktree target.
5. Removing a worktree with force can destroy uncommitted and untracked data. Inspect, preserve, or commit it first.

## Choose the tool

| Need | Tool |
| --- | --- |
| Inspect branch and staged/unstaged/untracked state | `git_status` |
| Review bounded unstaged or staged changes | `git_diff` |
| Stage selected paths and create a commit | `git_commit` |
| Browse recent commits | `git_log` |
| Inspect one commit/ref and its bounded diff | `git_show` |
| Attribute a file or line range | `git_blame` |
| Create/list/inspect/remove linked worktrees | `git_worktree_create`, `git_worktree_list`, `git_worktree_status`, `git_worktree_remove` |

## Review and commit

1. Call `git_status(cwd=...)` and confirm `repo_root`, branch/detached state, and all change categories.
2. Call `git_diff(staged=false, paths=[...])` for working-tree changes and inspect the complete index with `git_diff(staged=true)` before a commit. Diffs are byte-bounded and paginated; follow `next_offset` when partial.
3. If the index contains any unrelated staged path, do not call `git_commit`: this structured API has no index-isolation operation. Ask the user to preserve/unstage those entries manually or explicitly authorize a reviewed `run_command` workflow that isolates and restores the index.
4. Once the index is free of unrelated entries, use `git_commit(paths=[...], dry_run=true)` to preview a narrow stage/commit plan when scope is nontrivial.
5. Commit with explicit `paths` and a meaningful message. Re-run status and inspect the created commit afterward.

`git_commit(paths=...)` stages those paths. `stage_all=true` runs the broad equivalent of `git add -A`. Existing staged changes remain staged and may enter the commit, so always inspect the staged diff. A failed commit may also leave paths staged.

`amend=true` rewrites HEAD and may affect already-shared history. `allow_empty=true`, author overrides, and sign-off are specialized options; use them only when requested or required by repository policy.

## Inspect history

- Use `git_log` to select a commit, then `git_show(ref=...)` for metadata, body, parents, and a bounded per-file diff.
- Use `git_blame(path=..., start_line=..., end_line=...)` for targeted attribution, then inspect the referenced commit with `git_show` before drawing conclusions.
- Unknown refs and invalid line/path selections should be corrected, not silently replaced with HEAD or a broader range.

## Manage worktrees

- List existing worktrees before creation. Use an explicit, reviewed target path and base ref.
- `mode=clean` creates a new branch worktree; specify `branch` explicitly when naming matters. `mode=detached` creates no branch and cannot be combined with `branch`.
- Linked worktrees share repository-level Git state, but code delegates use distinct worktree writer lanes. Keep merge/rebase/repo-admin integration serialized and owned by ChatGPT.
- Inspect `git_worktree_status(path=...)` before removal. Normal removal refuses dirty worktrees.
- `git_worktree_remove(force=true)` may discard modified and untracked files. Use it only after explicit confirmation that the data can be lost or has been preserved elsewhere.

## Recover from failures

| Error | Response |
| --- | --- |
| `not_a_git_repo` | Correct cwd and confirm the intended repository root. |
| `git_add_failed` / `git_commit_failed` | Re-run status and staged diff; do not assume the index was restored. |
| `nothing_to_commit` | Recheck scope and staging; do not use `allow_empty` merely to suppress the error. |
| `git_show_failed` / `git_blame_failed` | Validate the ref, path, and requested line range. |
| `worktree_not_found` | List registered worktrees and use the exact registered path. |
| `worktree_dirty` | Inspect and preserve changes; avoid force unless loss is explicitly acceptable. |

## Minimal examples

```json
{"cwd":"/path/to/repo"}
```

```json
{"cwd":"/path/to/repo","staged":true,"paths":["src/app.py"]}
```

```json
{"cwd":"/path/to/repo","message":"feat: add API","paths":["src/app.py"],"dry_run":true}
```

```json
{"cwd":"/path/to/repo","path":"../worktrees/api","base_ref":"HEAD","mode":"clean","branch":"feature/api"}
```
"""


SKILL_INDEX = {
    "version": SKILL_GUIDANCE_VERSION,
    "namespace": SKILL_NAMESPACE,
    "resource_uri": SKILL_INDEX_URI,
    "discovery_tool": "get_guide",
    "usage": (
        "Call get_guide with the listed guide name before the first matching workflow, "
        "then follow its critical rules. Refresh after a server upgrade or when tool "
        "behavior differs."
    ),
    "skills": [
        {
            "name": "file-use",
            "description": (
                "Safe discovery, search, reading, code navigation, and file mutation "
                "with pagination, encoding, dry-run, and revision protection."
            ),
            "triggers": [
                "Before the first nontrivial file workflow or file mutation",
                "When handling pagination, token budgets, encodings, PDFs, images, or binary data",
                "When recovering from revision conflicts or multi-file write failures",
            ],
            "required_before_tools": [
                "list_files",
                "search",
                "read_text",
                "read",
                "code_map_symbols",
                "code_map_references",
                "code_map_imports",
                "code_map_csharp_membership",
                "write_file",
                "replace",
                "apply_patch",
            ],
            "guide_tool": "get_guide",
            "guide_args": {"name": "file-use"},
            "resource_uri": FILE_USE_URI,
        },
        {
            "name": "code-graph-use",
            "description": (
                "Immutable Joern CPG workflows for semantic callers, callees, impact, "
                "bounded call paths, and committed-tree review evidence."
            ),
            "triggers": [
                "Before the first code_graph_* workflow",
                "When call-graph or reverse-impact evidence matters to a review or refactor",
                "When code_map_* navigation is insufficient for semantic relationships",
            ],
            "required_before_tools": [
                "code_graph_status",
                "code_graph_prepare",
                "code_graph_callers",
                "code_graph_callees",
                "code_graph_impact",
                "code_graph_diff_impact",
                "code_graph_path",
            ],
            "guide_tool": "get_guide",
            "guide_args": {"name": "code-graph-use"},
            "resource_uri": CODE_GRAPH_USE_URI,
        },
        {
            "name": "process-use",
            "description": (
                "Selection and lifecycle management for synchronous commands, durable "
                "background jobs, and persistent interactive tmux sessions."
            ),
            "triggers": [
                "Before the first run_command, job, or tmux workflow",
                "When choosing between synchronous, background, and interactive execution",
                "When monitoring output, handling timeouts, or terminating a process/session",
            ],
            "required_before_tools": [
                "execution_state",
                "run_command",
                "job_start",
                "job_list",
                "job_status",
                "job_output",
                "job_tail",
                "job_kill",
                "tmux_list",
                "tmux_start",
                "tmux_status",
                "tmux_capture",
                "tmux_send",
                "tmux_kill",
            ],
            "guide_tool": "get_guide",
            "guide_args": {"name": "process-use"},
            "resource_uri": PROCESS_USE_URI,
        },
        {
            "name": "delegate-use",
            "description": (
                "Bounded CLI-agent delegation with project-scoped readers, worktree-scoped "
                "code writers, a repository-level writer cap, structured results, lifecycle "
                "monitoring, cancellation, and independent review."
            ),
            "triggers": [
                "Before the first delegate_task or delegate_batch workflow",
                "When choosing Codex, Claude, Antigravity, Pi, or another configured harness",
                "When monitoring, cancelling, or reviewing delegated agent work",
            ],
            "required_before_tools": [
                "delegate_task",
                "delegate_batch",
                "delegate_status",
                "delegate_cancel",
                "delegate_message",
                "delegate_reconcile",
                "delegate_supervisor",
                "delegate_harnesses",
            ],
            "guide_tool": "get_guide",
            "guide_args": {"name": "delegate-use"},
            "resource_uri": DELEGATE_USE_URI,
        },
        {
            "name": "runtime-use",
            "description": (
                "Stable Codex runtime acquisition/reuse with idle-TTL GC and "
                "capacity-driven LRU eviction."
            ),
            "triggers": [
                "Before the first codex_runtime_* or codex_mcp_* workflow",
                "When recurring workers need persistent runtime identities",
                "When diagnosing runtime capacity, GC, LRU, or concurrency behavior",
            ],
            "required_before_tools": [
                "codex_runtime_acquire",
                "codex_runtime_list",
                "codex_runtime_open",
                "codex_runtime_resume",
                "codex_runtime_status",
                "codex_runtime_close",
                "codex_mcp_inventory",
                "codex_mcp_call",
            ],
            "guide_tool": "get_guide",
            "guide_args": {"name": "runtime-use"},
            "resource_uri": RUNTIME_USE_URI,
        },
        {
            "name": "git-use",
            "description": (
                "Safe repository inspection, scoped staging and commits, history analysis, "
                "and Git worktree lifecycle management."
            ),
            "triggers": [
                "Before the first Git workflow",
                "Before staging, committing, amending, or creating/removing worktrees",
                "When reviewing history, blame, staged state, or destructive worktree options",
            ],
            "required_before_tools": [
                "git_status",
                "git_diff",
                "git_commit",
                "git_log",
                "git_show",
                "git_blame",
                "git_worktree_create",
                "git_worktree_list",
                "git_worktree_status",
                "git_worktree_remove",
            ],
            "guide_tool": "get_guide",
            "guide_args": {"name": "git-use"},
            "resource_uri": GIT_USE_URI,
        },
    ],
}


def skill_index_payload() -> dict[str, object]:
    return json.loads(json.dumps(SKILL_INDEX))


def skill_index_json() -> str:
    return json.dumps(SKILL_INDEX, ensure_ascii=False, indent=2) + "\n"


def delegate_use_payload() -> dict[str, object]:
    return {
        "success": True,
        "name": "delegate-use",
        "version": SKILL_GUIDANCE_VERSION,
        "resource_uri": DELEGATE_USE_URI,
        "content_type": "text/markdown",
        "content": DELEGATE_USE_GUIDE,
    }


def file_use_payload() -> dict[str, object]:
    return _guide_payload("file-use", FILE_USE_URI, FILE_USE_GUIDE)


def code_graph_use_payload() -> dict[str, object]:
    return _guide_payload("code-graph-use", CODE_GRAPH_USE_URI, CODE_GRAPH_USE_GUIDE)


def process_use_payload() -> dict[str, object]:
    return _guide_payload("process-use", PROCESS_USE_URI, PROCESS_USE_GUIDE)


def runtime_use_payload() -> dict[str, object]:
    return _guide_payload("runtime-use", RUNTIME_USE_URI, RUNTIME_USE_GUIDE)


def git_use_payload() -> dict[str, object]:
    return _guide_payload("git-use", GIT_USE_URI, GIT_USE_GUIDE)


def _guide_payload(name: str, resource_uri: str, content: str) -> dict[str, object]:
    return {
        "success": True,
        "name": name,
        "version": SKILL_GUIDANCE_VERSION,
        "resource_uri": resource_uri,
        "content_type": "text/markdown",
        "content": content,
    }
