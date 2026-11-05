# Delegate effectiveness — 2026-11-05

## Change

Managed ACP code writers with a tool budget of at least 16 receive a bounded execution brief and target inventory (existing files versus new targets). A pure policy requests a cooperative checkpoint after min(8, budget/4) observed tools and at 75% of the tool budget, or during the last 30 seconds of a native execution budget of at least 60 seconds. Requests preserve manual commands through revision checks. Existing checkpoints satisfy the early gate. Tiny tasks bypass automatic gates.

Controllers inspect the checkpoint and choose continue, steer or suspend. No automatic budget reset, account rotation or acceptance is introduced. Continuation beyond a hard budget uses an explicit saved draft and a new bounded turn. Guidance now requires source anchors, acceptance criteria and scoped reviews instead of repeated broad audits.

Failed writer turns retain valid in-scope changes after native teardown, before workspace cleanup. Capture failures preserve the original error. Saved drafts remain unaccepted; restoration still requires exact identity/scope/baseline and normal verification before promotion.

## Evidence

- Full suite: 2228 passed, 3 skipped, 18 subtests passed; 232.10 seconds. Job job_1793828792295_tRu1f3rh.
- Python compile and git diff --check passed.
- Initial focused run exposed three test-fixture owner-format errors, corrected; policy rerun 14 passed, then full suite passed.
- Independent scoped audit f1b9d1b9d266 and review 3a56413b68f6 completed; no blocking findings.
- Isolated live AGY3/MCP pilot job_1793828791369_Ru36Lwwv passed in 117.69 seconds using scripts/delegate_effectiveness_acceptance.py. It ran the candidate source through the existing QA harness.
- Managed writer 6955ed984420: early threshold 4, observed tools 7, one supervisor decision, verified promotion.
- Intentional blocked writer 73c03c43bc83: original workspace unchanged; valid unaccepted draft 3f7e4ff194119565adc6487132d9618e retained and discoverable.
- Recovery e6729e627743 restored that exact draft and passed verification before promotion.
- Private evidence: ~/.local/state/chatgpt-web-oauth-mcp/effectiveness-pilot-20261105a/.

## Limits and operating decision

Checkpoint requests are cooperative and tool-event driven, not native interception or a wall-clock watchdog. A worker killed before teardown may only have its previous checkpoint. Scope-invalid drafts are deliberately not retained. Existing uncertain-outcome reconciliation remains mandatory. The pilot proves the lifecycle, not a statistical increase in productivity.

The earlier implementation delegate 210b4801bb94 timed out without an accepted result; its failure is not counted as successful work. The new policy targets this wasted exploration, but ordinary-task measurements remain necessary: accepted slices per hour, repeated reads, time to first useful draft, recovery success and verification rejection rate.

Deployment setting: raise CHATGPT_MCP_DELEGATE_CODE_MAX_PER_PROJECT from the default 2 to 3 through a service drop-in; keep the global writer cap 4. Each simultaneous writer still requires isolated workspace/scope. This is a scheduling ceiling, not a promise of three productive parallel tasks.

## Repository role limits and production acceptance

The pilot at `52bb3ac` admits up to four analysts, two reviewers and three developers per repository across linked worktrees. Use `work_role="review"` for read-only review. Ordinary reads default to `analysis` and writes to `implementation`. Production also limits a single worktree to four readers and the service to eight readers and four writers. Account capacity and writer exclusivity still apply.

- Unit and integration checks covered role-aware queueing, writer ordering, deduplication, batch mapping and ACP continuation. An isolated scheduler exercise queued the fifth analyst, third reviewer and fourth developer, then released each when a slot opened.
- Real isolated MCP/ACP job `job_1793868385629_54kBToRd` admitted two reviews, queued a third and admitted an analyst. All four completed and returned the exact fixture marker.
- The initial full suite exposed a Revit test that assumed random workspace IDs sort in creation order. The corrected assertion identifies both rows by ID. Final full suite `job_1793868297564_xg44CLvA` passed 2230 tests and 18 subtests with 3 skips.
- The production deployment fast-forwarded from `cf00761` with rollback ref `rollback/delegate-role-limits-20261105`. The service PID changed from 3907463 to 4096979. Protected health returned HTTP 200 with no observation errors. The live MCP catalog exposed the role field and server_info reported 4/2/3.
- Production ACP review `533ef4930aa7` completed with a source citation. Writer `c1f7d3bcb44c` passed server-owned verification and promotion in a disposable worktree. An independent byte comparison confirmed the written file. This closes the prior outstanding production write check.
- The startup journal reported nine leftover bwrap processes. The investigation and fix are recorded below.

## ACP helper teardown closeout

All nine were childless sandbox PID 1 processes from the early-shutdown broker test, adopted by the systemd user manager. The operator validated their cgroup, user, namespace, absence of children and exact test-specific command line, then signalled only the pinned pidfds and verified all nine exits.

The fix in `0c12f87` completes helper ownership transfer when cancellation races process creation, and terminates the private launch group before closing the outer process handle. The new cancellation test failed on the old code and passed on the candidate. In job `job_1793869230351_CmkTlWRm`, 100 early shutdowns of the old code left 28 namespace inits; the same 100 shutdowns of the candidate left zero. All processes created by the baseline experiment were removed.

Full suite `job_1793869212418_w5rEx0PC`: 2231 passed, 3 skipped, 18 subtests passed. Production restarted from PID 4096979 to 4131708 with rollback ref `rollback/acp-helper-teardown-20261105` at `6d63657`. Protected health returned HTTP 200 with no observation errors. The startup journal was clear of leftover warnings. Live ACP analysis `ee161e10a2fb` completed successfully; the subsequent host process inventory had zero bwrap entries.

See [the field pilot](releases/opss-acp-field-pilot-20261105.md) for the first two measured tasks and the next scoped Astra navigation case.
