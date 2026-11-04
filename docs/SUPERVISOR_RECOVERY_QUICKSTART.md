# Recovering a suspended ACP writer

1. Ask the writer for a supervisor checkpoint after a useful partial change.
2. Confirm that the checkpoint contains a retained draft with an exact snapshot ID.
3. Choose `suspend` and await the terminal worker result.

Paused work is not accepted code.

4. Inspect the saved artifact with `delegate_draft(action="status")`.
5. Resolve an uncertain outcome before starting replacement work.
6. Start a code task with the source delegate and exact draft snapshot IDs.
7. Keep the same account, thinking, workspace, baseline and explicit file scope.
8. Use provider-history resume only for a known terminal conversation.
9. Review the complete diff after independent verification and promotion.

Only the selected checkpoint bytes are restored; later unsaved edits may be lost.
Discard obsolete snapshots explicitly after reviewing the accepted result.
