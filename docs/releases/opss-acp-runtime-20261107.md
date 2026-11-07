# ACP verification runtime and bounded capture

Baseline `f4fa06a`; implementation head `bfe7ac0`. This closes the verification-runtime and temporary-output debts from the preflight release.

## Runtime and execution boundary

The operator may select one dedicated regular-file Python runtime. Its exact identity (id, canonical root, manifest SHA256, relative interpreter) is bound into the task fingerprint, code contract and verified receipt. Omitted profiles preserve prior identities. The manifest pins every file's bytes, mode and digest; extra files, symlinks, hardlinks, special files, ambiguous venv configuration and credential/workspace/state overlaps are rejected, including linked worktrees approved through their Git common directory.

The trusted worker copies and validates a private runtime snapshot beside its candidate tree. Only independent preflight/final verification namespaces receive the read-only `/opt/opss-verification` mount; the native writer does not receive it. Both verification phases use the same pinned copy. Clearenv and network isolation remain enforced; no host execution fallback or inherited credentials are introduced. Changes to the original profile after staging cannot change the frozen execution environment; changed staged bytes reject acceptance.

A reusable Linux builder creates a new virtualenv from pinned universal wheels, validates public-host SHA256/size, installs locally with no index or dependency resolution and publishes atomically without replacing an existing destination. The built profile `python-pytest-20261107` contains 613 files / 16617989 bytes; manifest SHA256 `51004274811fb3fbcb8c421389b1b9d6a3b41833996a4147021c20580c34ff46`. Versions are pytest 9.1.1, iniconfig 2.3.0, packaging 26.3, pluggy 1.6.0 and Pygments 2.21.0. The only interpreter entry is `bin/python`; use `python -m pytest`. Other project dependencies need a separately built versioned profile.

## Output capture

Verification stdout/stderr now use bounded in-memory pipes instead of temporary files. Each command has a combined 1 MiB output limit and at most 12000 retained bytes per stream. Overflow or capture failure rejects acceptance even if a forged row claims passed=true. Overflow prevents subsequent commands from running. Deadline/cancel is checked again after launcher preparation; cleanup and inherited-pipe handling remain bounded. POSIX selectors were exercised with real processes; the Windows PeekNamedPipe adapter has mocked tests, without a Windows live claim.

Flash proposal `63c08da1a018` supplied the 907-byte capture accumulator, integrated verbatim after review and tests; provider reported 7098 tokens with unspecified usage scope. The result was consumed. Larger profile, worker and contract slices were separated among coordination agents. Provider totals are not converted to quota cost or claimed savings.

## Validation

Compile and diff-check passed. Root reviewed production diffs. Focused runtime/backend, contract/receipt, adversarial capture, builder and real worker/sandbox checks passed; overlapping counts are not summed. Root's final focused bundle passed 52 tests, and worker/sandbox integration passed 70. An independent real pytest test ran successfully inside the new namespace with the pinned runtime, while the original runtime path and auth profile remained absent.

Full suite at `bfe7ac0`: **2837 passed, 3 skipped, 5 warnings, 29 subtests**, 284.36 seconds. Durable job `job_1794046987606_jn8tNAWS` succeeded and was consumed. Committed semantic diff review covered all 126 changed symbols (44 production including one deleted helper, 82 tests) across 12 pages, with complete symbol pagination and zero analysis errors. Three module-scope changes/imports were checked. Twelve generic-method groups retain unresolved sites and seven candidate queries were truncated; static impact is partial and those candidates are not asserted runtime edges. The deleted helper reuses the prior exact baseline proof: only _run_verification_commands called it, with complete resolution and zero unresolved sites. No deploy blocker was found. Graph job job_1794047031765_TNCJEIBF passed and was consumed. Runtime cutover and live code-writer acceptance are not yet claimed.

An isolated linked-worktree acceptance case at `ef87036` has an intentionally broken UTF-8 prefix helper. Before delegation, the same namespace/pytest command produced 5 failures and 7 passes; tests are outside the writer's permitted change scope. The live writer must fix only the implementation, keep tests unchanged, pass all 12 cases and return a matching verified runtime receipt before this release is closed.

## Rollback preparation

The private ACP config candidate validated successfully for the existing three accounts. The live config is unchanged at this checkpoint. Original config backup: `/home/vooxerg/.config/chatgpt-web-oauth-mcp/acp-backend.pre-runtime-20261107.json`; SHA256 `07ed6f82c2d5e5aa5bbc376fa82aa0c0d9c1e98ef6b3753d0ae98f2d25881bea`. Activation requires the source fast-forward and atomic profile selection followed by service restart and live acceptance.
