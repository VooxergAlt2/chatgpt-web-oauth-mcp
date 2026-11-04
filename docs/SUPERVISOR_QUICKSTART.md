# Supervisor quickstart

1. Start a scoped ACP task on an explicitly configured account.
2. Ask the worker to call `supervisor_checkpoint` after its plan or draft.
3. Read `delegate_supervisor(action="status")` and inspect the report and observations.
4. Use the current `checkpoint_id` and `expected_revision` with a unique `command_id`.
5. Send `steer` with a concrete correction, or `continue` to release the same turn.
6. Leave `review` or `escalate` waiting until the owner supplies a final decision.
7. Verify the exported diff independently before committing.

`request_checkpoint` is cooperative; only `paused_for_supervisor` confirms the wait.
A checkpoint decision does not expand scope, change the account, or accept code.
Code checkpoints retain unaccepted drafts. Use `suspend`, then `delegate_draft`
to obtain the exact saved ID; explicitly restore it in a subsequent code task.
API execution adapters remain separate follow-up work.
