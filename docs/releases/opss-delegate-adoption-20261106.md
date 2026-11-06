# Delegate adoption: usable results and verification environment

Baseline: `18e647ef7632f47e1057649999e55ef2c085d046` (2026-11-06).

## Confirmed defects and scope

A single 600-character finding was returned as 512 characters with `packet.truncated=false`. Review, triage and continuation packets must report actual preview loss, keep byte budgets, and preserve the distinction between server evidence and unverified agent claims.

An invalid ACP manifest is correctly rejected, but its precise formatting diagnosis is lost. Historical task `5e19e5cdb996` still retains the 3652-character answer in its durable job stdout; JSON decoding fails at position 3524. New diagnostics must point at existing evidence, remain bounded, and never convert malformed output into acceptance or trigger automatic research/replay.

The canonical delegate guide should assign eligible substantial implementation to Flash before the manager repeats deep investigation. Parallelism follows independent file scopes, clean worktrees and available capacity. Deterministic Git, process control, known mechanical edits and checks remain direct work.

## Environment evidence

The actual ACP verification namespace clears the environment, uses `PATH=/usr/bin:/bin`, mounts a detached regular-file workspace read-only and has no host HOME/venv or network access. A live probe found `/usr/bin/python3` 3.12.3, no `python` alias and no pytest. The configured worker Python environment also lacks pytest; mounting it alone would not solve acceptance.

A real sandbox probe successfully imported `delegate_packets` with system Python after adding `src` to `sys.path`. This stage therefore uses stdlib unittest checks for each writer and the existing service venv for independent integration tests. It does not add host execution fallback or pretend full project dependencies are available to the writer.

## Next implementation slice

Add an explicit bounded verification-environment preflight contract, separate from the final behavioral checks. Carry it through public schema, task/contract identity, ACP worker request and worker startup before inference. Do not parse arbitrary shell heuristically or run potentially failing baseline tests as a readiness probe. Define a separately configured trusted read-only runtime profile when third-party dependencies are required, with namespace/path binding tests and no inherited credentials.

After that, extend approved product roots and exercise real Astra/FlowMEP writers with project-specific commands. Count accepted implementation and controller repairs; provider token totals do not measure expensive-model quota savings.

## Implementation and delegate outcomes

Integrated source commits: guidance `6c14f6b`, packet completeness `1d74d27` with known-artifact-failure correction `7ed1f21`, manifest diagnostics `8d9220c`. Integration code head: `ff5acc1`. The guide example uses a behavioral packet test; tests coupled to exact guide wording were removed at integration.

Three independent Flash Medium writers ran concurrently in isolated worktrees. Original outcomes remain unchanged:

| Task | Provider result | Evidence and intervention |
| --- | --- | --- |
| `f9961ee039dc`, packet | timed_out, total 880539 | Only tracker scaffolding was written. Main-model delegate completed wiring and regression tests from the validated snapshot. |
| `b7e122e2f25e`, guide | timed_out, 443s, 31 calls, total 1324012 | Guide draft retained; wording scoped specifically to ACP. Call threshold 24 was reviewed and rearmed at 25; no count-based stop. |
| `4b66faaef0ff`, manifest | diff-budget failure, 578s, 17 calls, total 801425 | Code and tests retained; tests contained wrong JSON offsets and omitted edge cases. |
| `94b69a406da9`, localized manifest correction | verification failure, 409s, 18 calls, total 966040 | Most corrections written; one incorrect escaped-surrogate assertion fixed mechanically before independent acceptance. |

These are provider-reported unspecified-scope token totals, not measured quota costs. None of these provider outcomes is silently upgraded to success. Exact retained drafts were inspected and restored with owner/scope/baseline bindings and CAS; independent checks precede integration. Results were consumed and temporary drafts discarded.

Observed additional friction: a status request timed out while the actual worker continued; two cooperative checkpoint requests were delayed by minutes. A native transcript showed `supervisor_checkpoint_requested` on a read-only Git call, not proof of a broken edit helper. No common root cause of latency is claimed. Prefer a short tool-less proposal or a known mechanical patch for small localized corrections instead of repeating a full repository writer.

## Acceptance record

Focused packet, manifest and guide checks passed. An independent 54-case ASCII/Cyrillic/emoji matrix confirmed packet byte budgets and source immutability. Deleted private packet helpers were checked on the baseline semantic graph: only local packet-builder callers were resolved; general Python dynamic-dispatch limits still apply.

Integration `ff5acc1`: compileall and diff-check passed; full suite **2607 passed, 3 skipped, 5 warnings, 29 subtests**, 266.66s, durable job `job_1793996009909_lhEyxfej` consumed. No failing test remained. Committed diff impact paginated all 40 symbols (25 + 15), two deleted helpers reviewed on the baseline graph; module import changes inspected. Static resolution remains partial for dynamic/dunder candidates and does not replace the suite or runtime checks.

Fast-forward deployment to `6dd67d5` completed; rollback ref `rollback/delegate-adoption-20261106-18e647e` preserves the baseline. The user service restarted from PID 2564664 to 2746388, active/running, ExecMainStatus=0. Authenticated `/internal/health` returned HTTP 200, success=true, state=active and no observation errors.

Live MCP `get_guide(delegate-use)` returned version 1.12 and the new first delegation rule. Live owned `delegate_packet(272c3aea1d71, review)` returned `truncated=true`, `truncated_fields=[agent_claims.findings[2]]`, a 512-character preview and existing digest-bound full-result references. The complete short result `94b69a406da9` correctly remained `truncated=false`. The deployed parser replayed historical malformed stdout `5e19e5cdb996`: `acp_result_manifest_invalid`, structured output absent, JSON position 3524 / column 3525, exact 3652-byte answer SHA256 reference; no new model call or acceptance was introduced.

One remaining lifecycle issue was observed: the older audit result `f99187fa77bf` was accessible before restart but refused with `delegate_packet_owner_required` afterwards; it is absent from the restored bounded owned-result order. No ownership gate was bypassed. The retained owned result above supplied the live truncation acceptance instead. Diagnose ownership retention separately before claiming arbitrary historical result retrieval survives restart.

Systemd journal warnings referred to an independently running durable RAG test job in another worktree. Its process tree and job identity were inspected; it intentionally survived the MCP restart and was not terminated or cleaned as part of this task. Current-stage delegate results/drafts were consumed/discarded; feature worktree cleanup follows the documentation-only final fast-forward.
