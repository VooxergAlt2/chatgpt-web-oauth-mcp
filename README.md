# chatgpt-web-oauth-mcp

[![中文文档](https://img.shields.io/badge/docs-中文-blue.svg)](README-zh.md)

> Thanks to the [LINUX.DO community](https://linux.do/).

A local [FastMCP](https://github.com/jlowin/fastmcp) server that lets **ChatGPT Web** call trusted tools on your computer through a remote HTTPS MCP endpoint protected by OAuth.

It exposes bounded filesystem and code-search tools, structured Git operations, short shell commands, durable background jobs, persistent tmux sessions, and persistent Codex runtime/MCP access while keeping ChatGPT Web in the architect / manager / reviewer role.

## Why this project exists

ChatGPT cannot connect directly to a process bound to `127.0.0.1`. A custom MCP app needs a reachable remote endpoint, an authentication flow, and tools whose output is small enough to remain useful inside the model context window.

This project provides that bridge:

- a Streamable HTTP MCP endpoint at `/mcp`;
- ChatGPT-compatible OAuth discovery, dynamic client registration, PKCE, and bearer-token validation;
- optional Cloudflare Tunnel and macOS `launchd` helpers;
- local-operation tools with explicit pagination, token budgets, and bounded output;
- separate execution paths for short commands, durable jobs, interactive terminal sessions, and persistent Codex runtime/MCP access.

## Architecture

```text
ChatGPT Web
    │
    │ HTTPS + OAuth
    ▼
Public MCP endpoint (/mcp)
    │
    ▼
FastMCP server on 127.0.0.1:8766
    │
    ├── Direct tools
    │   ├── files / search / read / patch
    │   ├── code maps / environment inspection
    │   └── Git / worktrees
    │
    ├── Execution tools
    │   ├── run_command     bounded foreground commands
    │   ├── job_*           durable non-interactive jobs
    │   └── tmux_*          persistent interactive TTY sessions
    │
    └── Codex runtime
        ├── codex_runtime_* persistent runtime bindings
        └── codex_mcp_*     connected MCP inventory and calls
```

ChatGPT Web should inspect, plan, edit, and verify through direct MCP tools. Persistent Codex access is provided by `codex_runtime_*` and `codex_mcp_*`; these runtime tools do not start a Codex LLM turn.

## Core capabilities

| Area | Capabilities |
| --- | --- |
| Remote MCP access | Streamable HTTP `/mcp`, discovery metadata, server card, HTTPS tunnel support |
| Authentication | `none`, shared bearer token, or OAuth authorization-code flow with PKCE and dynamic client registration |
| Bounded context retrieval | Token-aware pagination, common continuation metadata, shared batch budgets, ignore-aware traversal |
| Files and code | Glob/regex/text search, text and multimodal file reads, lightweight symbol/reference/import maps |
| Safe mechanical edits | Full-file writes, structured patches, CAS-protected batch replacement with atomic writes and format preservation |
| Git | Status, diff, commit, log, show, blame, and a small worktree lifecycle |
| Local execution | Bounded commands, durable background jobs, and persistent tmux sessions |
| Codex runtime | Persistent Codex App Server bindings plus connected MCP inventory/calls |
| macOS operations | Development tunnel, persistent `launchd` install, status, doctor, reload, restart, and uninstall helpers |

## Operating model

Use the narrowest tool that matches the task:

1. Inspect with `list_files`, `search`, `read_text`, `read`, `code_map_*`, `git_status`, or `git_diff`.
2. Make deterministic changes with `apply_patch`, `replace`, `write_file`, or structured Git tools.
3. Use `run_command`, `job_*`, or `tmux_*` according to process lifetime and interactivity.
4. Use `codex_runtime_*` and `codex_mcp_*` when persistent Codex runtime or connected MCP access is required.
5. Verify results directly before declaring completion.

Computer Use approval defaults to `interactive`. For the restricted local prototype from
[Issue #12](https://github.com/escapeWu/chatgpt-web-oauth-mcp/issues/12), set
`CHATGPT_MCP_CODEX_RUNTIME_CUA_APPROVAL_MODE=prototype` and provide exact app bundle IDs in
`CHATGPT_MCP_CODEX_RUNTIME_CUA_ALLOWED_APPS`. This bypasses nested elicitation only for the
`cua_repl` `get_app_state` app-access gate; later side-effect confirmations remain interactive.

## Requirements

- Python 3.11 or newer
- Git
- `ripgrep` for the preferred search backend
- `tmux` for persistent interactive sessions
- Codex CLI when using the persistent Codex runtime integration
- `cloudflared` only when using the included tunnel helpers
- macOS only for the included `launchd` scripts; the Python server itself is not launchd-specific

Only install the optional binaries required by the tools you intend to use.

## Quick start

```bash
git clone https://github.com/escapeWu/chatgpt-web-oauth-mcp.git
cd chatgpt-web-oauth-mcp

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

Set at least the following values in `.env` for ChatGPT OAuth mode:

```bash
CHATGPT_MCP_WORKSPACE_ROOT="/absolute/path/to/workspace"
CHATGPT_MCP_AUTH_MODE=oauth
CHATGPT_MCP_PUBLIC_BASE_URL="https://your-public-mcp-host.example"
CHATGPT_MCP_OAUTH_LOGIN_TOKEN="replace-with-a-long-random-token"
```

`CHATGPT_MCP_OAUTH_LOGIN_TOKEN` is required in OAuth mode and is entered on the authorization page. `CHATGPT_MCP_AUTH_TOKEN` is only used by `shared_token` mode and is not accepted as a bearer credential in OAuth mode.

Start the local server and configured tunnel:

```bash
./scripts/dev-tunnel.sh
```

Local MCP endpoint:

```text
http://127.0.0.1:8766/mcp
```

Public MCP endpoint:

```text
https://your-public-mcp-host.example/mcp
```

## Add it to ChatGPT

ChatGPT currently describes custom MCP integrations as **apps**. The exact UI and plan permissions can change; the server-side sequence is:

1. Enable ChatGPT developer mode for an eligible account or workspace.
2. Open **Settings → Apps → Create**, or the corresponding workspace-admin Apps page.
3. Enter the public MCP endpoint, for example `https://your-public-mcp-host.example/mcp`.
4. Select OAuth authentication.
5. Scan the tools.
6. Complete the authorization flow with `CHATGPT_MCP_OAUTH_LOGIN_TOKEN`.
7. Create or publish the app according to the workspace policy.

Official references:

- [Apps in ChatGPT](https://help.openai.com/en/articles/11487775-connectors-in)
- [Developer mode and MCP apps in ChatGPT](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta)

ChatGPT plan, workspace, approval, and write-action availability are controlled by ChatGPT, not by this server.

### OAuth token lifecycle

The OAuth implementation supports the authorization-code grant with PKCE plus rotating refresh tokens. Access-token lifetime is controlled by `CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS`; refresh-token lifetime is controlled separately by `CHATGPT_MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS`. A successful refresh invalidates the previous refresh token and returns a replacement. The server does not advertise `offline_access`.

## OAuth endpoints

```text
/.well-known/oauth-protected-resource
/.well-known/oauth-protected-resource/mcp
/.well-known/oauth-authorization-server
/.well-known/openid-configuration
/oauth/register
/oauth/authorize
/oauth/token
```

The MCP endpoint is:

```text
/mcp
```

## Smoke test

```bash
curl -sS https://your-public-mcp-host.example/.well-known/oauth-protected-resource/mcp
curl -sS https://your-public-mcp-host.example/.well-known/oauth-authorization-server
curl -i https://your-public-mcp-host.example/mcp
```

Expected behavior:

- the first two commands return OAuth metadata as JSON;
- an unauthenticated request to `/mcp` returns `401`;
- the `WWW-Authenticate` header includes `resource_metadata` in OAuth mode.

## Local-only run

```bash
source .venv/bin/activate
chatgpt-web-oauth-mcp
```

This starts the server without creating a public route. It is useful for local testing, but ChatGPT cannot connect directly to the loopback endpoint.

## Existing Cloudflare Tunnel

If another service already maps your public hostname to `http://127.0.0.1:8766`, do not start a second tunnel. Configure OAuth normally and install only the MCP service:

```bash
CHATGPT_MCP_PUBLIC_BASE_URL="https://your-existing-host.example"
CHATGPT_MCP_EXTERNAL_CLOUDFLARED=1
./scripts/install-launchd.sh --mcp-only
```

In `--mcp-only` mode, this project installs and watches the MCP process but does not create, restart, or monitor a `cloudflared` launchd service.

## Persistent macOS launchd install

A full installation requires a named Cloudflare Tunnel configuration:

```bash
./scripts/install-launchd.sh
```

Operational commands:

```bash
./scripts/launchd-status.sh
./scripts/launchd-doctor.sh
./scripts/launchd-doctor.sh --fix
./scripts/launchd-reload.sh
./scripts/launchd-restart.sh mcp
./scripts/launchd-restart.sh all
./scripts/uninstall-launchd.sh
```

The watchdog checks service health. The doctor script applies targeted restarts with a failure threshold and capped exponential backoff.

## Tool reference

Set `CHATGPT_MCP_TOOL_PROFILE=lean` to hide the advanced `codex_runtime_*`, `codex_mcp_*`, and `tmux_*` surfaces from `tools/list` while keeping their implementations available for a later `full` profile restart. The repository default is `full`.

### Runtime and environment

| Tool | Purpose |
| --- | --- |
| `server_info` | Inspect runtime configuration and registered MCP tools |
| `get_guide` | Load one progressive-disclosure guide by name: delegate, file, Code Graph, process, runtime, or Git |
| `get_code_graph_use` | Compatibility shortcut for the Code Graph guide |
| `set_default_cwd` / `get_default_cwd` | Set or read the session-wide default working directory |
| `env_snapshot` / `env_diff` | Collect a small read-only environment snapshot and compare two inline snapshots |

### Files, search, and code context

| Tool | Purpose |
| --- | --- |
| `list_files` | Ignore-aware directory listing with filters, sorting, stable pagination, and token budgets |
| `search` | Glob, regex, literal-text, or batch search; parallel batches are capped at three workers |
| `read_text` | Backward-compatible single or batch text reader with line pagination |
| `read` | Unified text/encoding, image metadata/reference, PDF-page text, and binary-hex reader |
| `code_map_symbols` | Lightweight Python, JavaScript, or TypeScript definition discovery |
| `code_map_references` | Bounded textual reference lookup using identifier word boundaries |
| `code_map_imports` | Lightweight import discovery |
| `write_file` | Create or fully overwrite a file, with dry-run support |
| `replace` | Locked CAS batch replacement with dry-run, atomic writes, and encoding/newline/BOM/permission preservation |
| `apply_patch` | Apply structured patches to existing files |

### Git and worktrees

| Tool | Purpose |
| --- | --- |
| `git_status` | Structured repository status |
| `git_diff` | Per-file bounded unstaged or staged diff |
| `git_commit` | Stage selected paths or all changes and create a commit |
| `git_log` | Recent commit history |
| `git_show` | Commit metadata and bounded per-file diff |
| `git_blame` | Per-line commit, author, summary, and content |
| `git_worktree_create` | Create a clean-branch or detached worktree |
| `git_worktree_list` | List registered worktrees |
| `git_worktree_status` | Inspect one or all worktrees |
| `git_worktree_remove` | Remove a registered worktree; dirty worktrees are refused unless forced |

### Commands and durable jobs

| Tool | Purpose |
| --- | --- |
| `run_command` | Run one coherent bounded command or sequential/parallel batch; direct/local clients may use up to 900 seconds, while ChatGPT/OpenAI sessions use the configured safe foreground budget (30 seconds by default) |
| `job_start` | Start a non-interactive background process with persisted metadata and separate logs |
| `job_list` | Discover jobs from the state directory, including after server restart |
| `job_status` | Read process state, exit status, timing, resources, and log paths |
| `job_output` | Read one stdout/stderr stream using an independent raw-byte cursor |
| `job_tail` | Read the latest lines as a compatibility API |
| `job_kill` | Stop a running job |

### Persistent tmux sessions

| Tool | Purpose |
| --- | --- |
| `tmux_list` | List sessions on the configured tmux socket |
| `tmux_start` | Start one detached session with a primary pane |
| `tmux_status` | Inspect pane commands, directories, PIDs, dimensions, and exit state |
| `tmux_capture` | Capture a bounded terminal screen/history snapshot |
| `tmux_send` | Paste UTF-8 text through a tmux buffer and send a small allowlist of keys |
| `tmux_kill` | Remove one exact session |

`tmux_capture` is not a lossless application log. Full-screen TUIs, progress bars, carriage-return updates, and tmux history limits can change what is visible. Prefer application log files, or use modes such as `--no-alt-screen` when terminal history matters.

### Operating guides

Call `get_guide(name=...)` before the first workflow in an unfamiliar tool family. The accepted names are `delegate-use`, `file-use`, `code-graph-use`, `process-use`, `runtime-use`, and `git-use`. `get_code_graph_use` remains as a compatibility shortcut.

The same authoritative content is also exposed through standard MCP resources:

| Resource | Purpose |
| --- | --- |
| `skill://chatgpt-web-oauth-mcp/index` | Machine-readable guide index, triggers, and tool/resource routing |
| `skill://chatgpt-web-oauth-mcp/file-use` | Complete Markdown file workflow guide |
| `skill://chatgpt-web-oauth-mcp/process-use` | Complete Markdown command, job, and tmux guide |
| `skill://chatgpt-web-oauth-mcp/git-use` | Complete Markdown Git and worktree guide |

Guide content remains exposed as MCP resources as well; the separate per-guide loader tools were consolidated to keep the public tool catalog small.

## Choosing an execution tool

| Need | Use | Do not use it for |
| --- | --- | --- |
| Bounded non-interactive work expected to finish within the safe client foreground window | `run_command` | Longer coherent work should be one durable job rather than being split merely for duration; interactive TUIs belong in `tmux_*` |
| A durable non-interactive process with inspectable logs | `job_*` | Interactive input |
| A persistent interactive terminal or manually attachable session | `tmux_*` | Lossless stdout/stderr capture |
| One bounded agent investigation or implementation slice | `delegate_task` | Deterministic local work that direct tools can do more cheaply |
| Several independent read-only investigations in one project | `delegate_batch` | Concurrent writers |

`delegate_task` and `delegate_batch` support `long_running=true` for substantial tasks. It opts into an **8-hour subprocess lifetime** instead of the usual 15-minute explore / 60-minute code defaults; `wait_seconds` remains only a short response window, so an in-progress delegate continues as a durable job and can be checked with `delegate_status`. An explicit `execution_timeout_seconds` takes precedence and may be set as high as **86,400 seconds (24 hours)**. Keep tasks bounded, use `delegate_cancel` when needed, and inspect results and Git state before accepting code changes.

To reserve more of an otherwise identical second AGY account for other projects, set `CHATGPT_MCP_DELEGATE_AGY1_PREFERENCE_WEIGHT=3`. This gives AGY1 a threefold *routing weight* in fallback/complexity selection and a soft preference in the active Planner. It does **not** guarantee a fixed percentage or block AGY2: the two accounts remain available, and balancing still accounts for quota headroom, active load, and complexity. AGY1 can run Flash High for difficult jobs, while explicit `harness="antigravity2"` and AGY2 conversation resume keep using AGY2. Default `1` leaves historical routing unchanged.

**DeM strict routing (when `CHATGPT_MCP_DELEGATE_AGY_CAPACITY_FIRST=1`):** the AGY1 account runs as many as its four active worker slots allow, including code and explore. Only after its slots fill or the account is unavailable is AGY3 selected; AGY2 is used after AGY3 fills or becomes unavailable. If all three accounts are full, ordinary jobs queue at their existing model tier; capacity never upgrades work to Claude or Codex. Simple code and discovery use Gemini Flash Medium; medium code/research uses Gemini Flash High on all three AGY accounts. This strict routing overrides the legacy AGY1 *weight*, and takes precedence over the active Planner's suggestion. Explicit model/harness pins are preserved. Claude Opus 4.6 Thinking via AGY is selected only for high-complexity tasks; if its account quotas are unavailable, GPT-6.1 Sol Medium is the next tier. GPT-6.1 Sol Extra High is an explicit later escalation via Codex; Astra Medium remains a manual ChatGPT review stage, since no verified Astra MCP CLI route exists.

**AGY3 (optional third account):** set `CHATGPT_MCP_ANTIGRAVITY3_ENABLED=1` and `CHATGPT_MCP_ANTIGRAVITY3_HOME=<STATE_DIR>/antigravity3-home`, then authenticate AGY interactively under this isolated HOME, with matching XDG_CONFIG_HOME, XDG_CACHE_HOME and XDG_DATA_HOME. Never copy AGY1/AGY2 OAuth tokens. The server disables AGY3 until its own `.gemini/antigravity-cli/antigravity-oauth-token` exists. With capacity-first enabled, admission proceeds AGY1 -> AGY3 -> AGY2 -> wait, reserving AGY2 for other projects as long as possible. The third account has independent `antigravity3_gemini_5h` / `_weekly` and `antigravity3_claude_gpt_5h` / `_weekly` quota buckets. Its eligibility recovery watchdog uses the **same canonical patcher and global lock** as AGY1, but performs its live `OK` verification using only AGY3's HOME and stores status/logs separately. An AGY3 auth failure requires login, not unlocker patching. After login, restart Opss MCP to re-probe the third account. The current watcher runs only on an actual AGY3 eligibility failure, not simply because the account lacks OAuth credentials.

The `agy3` helper (`scripts/agy3-account.sh`, installed to `~/.local/bin/agy3`) opens an interactive AGY session using only the third account's private HOME/XDG directories. Run `agy3` for the browser-based login, then `agy3 models` to verify. This helper never reads or copies AGY1/AGY2 credentials. The MCP harness continues to invoke `agy` with isolated HOME.\n\n**Delegate reliability gates:** The server checks Claude CLI OAuth status (60-second cache) and the account-isolated AGY model catalog (120-second cache) before dispatch. Known unauthorized or model-unavailable routes fail before running a paid agent. Gemini Flash efforts must match model suffixes; failed catalog reads remain explicitly unverified. An interrupted durable job or absent terminal exit becomes `durable_job_interrupted`, never a bogus model-format error, with partial logs retained. Durable activity reflects supervisor stdout/stderr, not empty in-memory buffers. For code tasks, inspect the Git tree before retrying an interruption.

**Verified .NET changes:** Only Git-ignored generated NuGet/MSBuild outputs (NuGet dgspec, AssemblyAttributes, Up2Date, SourceLink, FileListAbsolute and coverage source mappings) are excluded from source-scope checks. Tracked or non-ignored changes, arbitrary files under `bin/obj` and out-of-scope source edits remain prohibited. AGY1/2/3 eligibility watchdogs retain separate HOME-scoped live probes, cooldown, logs and routing state.

**AGY3 MQTT and Home Assistant quota sensors:** The existing `chatgpt-web-oauth-mcp-ha-monitor.service` publishes four independent AGY3 remaining-quota values (`antigravity3_gemini_5h`, `antigravity3_gemini_weekly`, `antigravity3_claude_gpt_5h`, `antigravity3_claude_gpt_weekly`) in `gip-core/ops-mcp/state` and advertises them through retained Home Assistant MQTT device discovery. The four matching threshold `number` entities use `gip-core/ops-mcp/control/quota_threshold/<bucket>/{state,set}` and the canonical shared admission policy. A separate `antigravity3_quota_status` diagnostic sensor distinguishes `ok`, `stale`, and `verification_required`. Having an OAuth token and seeing models in `agy3 models` does not prove that the provider's `/usage` endpoint is available. When AGY replies `PERMISSION_DENIED (403): Verify your account to continue`, all four quotas remain `unknown`, never an invented percentage. Resolve the account verification via its official sign-in flow, not a patch or reused credentials. While a provider reports `verification_required`, the canonical quota admission gate returns `account_verification_required` and prevents new AGY delegates from being dispatched to that account, including explicitly pinned requests. The other AGY accounts remain available; unlike a generic quota probe timeout, this is a provider-confirmed account lock. The gate resumes AGY3 automatically when the provider changes its status to `ok`; it does not cancel already running jobs.

For a repeatable **single-AGY-account research team**, pin `delegate_batch(harness="antigravity", max_concurrency=4)` and submit three or four read-only child specs with distinct `task_id`, `model`, and `reasoning_effort`. Suggested roles: architect (Flash High), researcher (Flash Low), tester and independent reviewer (Flash Medium). Each child gets its own AGY process and conversation while consuming the same account quota. The per-account explore cap defaults to **4 across batches** and is configured by `CHATGPT_MCP_ANTIGRAVITY_DELEGATE_MAX_CONCURRENCY`; the separate `antigravity2` account has its own cap. Follow `get_guide(name="delegate-use")` for a complete invocation and result collection. Team batches cannot edit code; implementation uses `delegate_task(kind="code")` and independent verification.

`server_info` and `delegate_harnesses` expose deterministic delegate routing hints. They do not automatically choose or replace an explicitly requested harness. Ordinary bounded exploration prefers the low-cost read-only route, independent second-pass review can use Antigravity, and implementation remains one project-scoped writer slice. A second Antigravity account can be exposed as `antigravity2`; it uses the same CLI binary with an isolated HOME/OAuth store, so each account has independent conversations and quota windows. On Linux, Codex read-only routing is enabled only when a local no-LLM sandbox probe succeeds; when the sandbox runtime is unavailable, bounded exploration falls back to another available read-only harness without weakening the read-only contract. Repository-backed delegate prompts include only a compact project root/submission-HEAD context; full diffs are not injected automatically.

On Ubuntu hosts that enforce restricted unprivileged user namespaces, sandboxed
Antigravity also needs an explicit AppArmor transition. Run
`python scripts/install_antigravity_linux_sandbox.py` first for a non-mutating
preflight, then rerun it with `--apply` to install the dedicated `agy` /
`unpriv_agy` profiles, preserve the distro `bwrap` transition, and set the MCP
user service to `NoNewPrivileges=false`. The nested profile grants only
`sys_admin` and `net_admin`, which AGY uses while constructing its private
mount/network namespaces; `setpcap` remains explicitly denied. Re-run the
installer if the AGY executable path changes.

**Delegate research evidence and failure forensics:** Non-local read-only delegates with explicit file scopes now receive a bounded `research_evidence` manifest. It independently checks source-line existence per finding, supports cited files under a scoped directory or glob, records the observed Git HEAD and marks semantic verification as **false**. This is advisory quality instrumentation and does not change `succeeded` into failure. When the caller provides no explicit source scope, only exactly cited Git-tracked files are checked and the result is marked `tracked_unscoped` with `scope_verified=false`; no citations remain `not_evaluated`. Neither outcome proves a source claim, and a clean 'no findings' report is not incorrectly marked a defect. Telemetry persists only counts/statuses, not finding text, and exposes `recent_windows` for 24 hours and 7 days with an explicit small-sample warning. The scheduler publishes the terminal delegate result before its telemetry callback finishes, so immediately queried telemetry may lag briefly; it is eventually consistent, not an atomic part of the result. An interrupted durable supervisor records available stdout/stderr sizes, recent log activity and shared-cgroup OOM-kill counter changes for investigation. Shared cgroup counters are **not proof** that the interrupted agent was killed by OOM.\n\nDelegate outcome telemetry is stored at `<STATE_DIR>/delegate-telemetry.json`. It is bounded and records lifecycle, route, durations, provider usage metadata when available, and whether a terminal result was explicitly consumed. It does not persist task, goal, or prompt text. Raw events remain capped at 2,000 records/30 days. Upon pruning, daily aggregates are retained for up to 180 days with an ID ledger preventing double counting on reimports; `server_info` exposes `daily_history` for the last 90 days, including observed outcomes, errors, account counts, code-check results, and token totals. Historical events already discarded before activation cannot be reconstructed, and daily summaries are explicitly labelled as an observed sample, not a complete accounting. Existing schema-version-1 state is read without migration. Legacy delegate records migrated from the former temporary state location remain available for recovery/status but are intentionally excluded from the telemetry baseline, because historical temporary state may include pre-isolation test artifacts and has no trustworthy consumption history.

Delegate quota admission is controlled by `<STATE_DIR>/delegate-quota-policy.json` and, in the Home Assistant monitor, by retained MQTT-backed `number` entities. Each configured threshold is the **remaining quota percentage at or below which new delegate submissions are rejected**. Thresholds may be any value from 0 to 100 and are separate for 5-hour and weekly windows and for each Antigravity account/model family. The gate is admission-only: tasks already created (running or queued) continue normally, and dedupe/attach to an already active matching delegate remains allowed. A provider-reported quota exhaustion is also treated as a forced admission block until its reset/retry window has passed.

**Live delegate activity in Home Assistant:** The existing `chatgpt-web-oauth-mcp-ha-monitor.service` publishes separate running counts for AGY1, AGY3, AGY2, Codex, Claude and the optional local/Pi harnesses, plus total running and queued delegates. The diagnostic `Current delegate work` sensor exposes a bounded list of current tasks via retained `gip-core/ops-mcp/delegates/detail` attributes: harness, project basename, code/explore type, elapsed minutes, latest output age and activity state. No prompt text, full paths or log content is published. Running counts include quiet/stalled jobs, unlike the older `Active delegates` sensor that counts only active-output tasks. Incomplete health observations yield `unknown`, not a false zero; offline health clears retained delegate details. The feature reuses the existing authenticated health endpoint, MQTT service and polling interval (`OPS_MCP_HA_POLL_INTERVAL`), requiring no new daemon or MCP restart.

## Output budgets and pagination

Token-aware read-only responses use the `o200k_base` encoding and expose a common result contract including:

- `complete` / `partial`;
- `estimated_tokens` and the effective budget;
- `truncated` and `stop_reason`;
- a continuation offset when more results are available.

Batch `read_text`, `search`, and `run_command` calls use one shared response budget rather than multiplying the configured limit by the number of child requests. On ChatGPT/OpenAI sessions, `run_command` is additionally capped by `CHATGPT_MCP_OPENAI_FOREGROUND_TIMEOUT` so the response completes before the upstream command-response deadline; use one durable `job_start` for longer coherent commands.

## Environment variables

### Server and authentication

| Variable | Required | Default / behavior |
| --- | --- | --- |
| `CHATGPT_MCP_HOST` | no | `127.0.0.1` |
| `CHATGPT_MCP_PORT` | no | `8766` |
| `CHATGPT_MCP_WORKSPACE_ROOT` | recommended | `$HOME`; relative-path anchor and default cwd, **not a sandbox** |
| `CHATGPT_MCP_STATE_DIR` | no | `~/.chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_DELEGATE_STATE_DIR` | no | `<STATE_DIR>/delegates`; persistent delegate metadata/logs. The old temp-directory state is imported on startup when this override is unset |
| `CHATGPT_MCP_AUTH_MODE` | recommended | Explicit `none`, `shared_token`, or `oauth`; when empty, shared token is selected if `AUTH_TOKEN` exists, otherwise none |
| `CHATGPT_MCP_AUTH_TOKEN` | required for `shared_token` | Empty; bearer token used only in `shared_token` mode |
| `CHATGPT_MCP_PUBLIC_BASE_URL` | required for OAuth | Empty; stable public issuer/resource base URL |
| `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` | required for OAuth | No fallback; secret entered on the OAuth authorization page |
| `CHATGPT_MCP_OAUTH_SCOPES` | no | `local-ops` |
| `CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS` | no | `86400` |
| `CHATGPT_MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS` | no | `2592000` (30 days) |

### Search, output, and execution

| Variable | Required | Default / behavior |
| --- | --- | --- |
| `CHATGPT_MCP_RIPGREP_BINARY` | no | `rg` |
| `CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET` | no | `8500` |
| `CHATGPT_MCP_READ_TOKEN_BUDGET` | no | Inherits the global tool budget |
| `CHATGPT_MCP_RUN_TOKEN_BUDGET` | no | Inherits the global tool budget |
| `CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET` | no | Inherits the global tool budget |
| `CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES` | no | `1048576` bytes |
| `CHATGPT_MCP_CODEX_COMMAND` | no | `codex` |
| `CHATGPT_MCP_LOCAL_DELEGATE_ENABLED` | no | `0`; set to `1` to register the optional explore-only `local` OpenAI-compatible scout |
| `CHATGPT_MCP_LOCAL_DELEGATE_ENDPOINT` | no | `http://127.0.0.1:8081`; workstation llama.cpp/OpenAI-compatible base URL |
| `CHATGPT_MCP_LOCAL_DELEGATE_MODEL` | no | `qwen80`; exact model alias expected from `/v1/models` |
| `CHATGPT_MCP_LOCAL_DELEGATE_HEALTH_TIMEOUT_MS` | no | `350`; bounded availability probe before automatic routing |
| `CHATGPT_MCP_LOCAL_DELEGATE_REQUEST_TIMEOUT_SECONDS` | no | `120` per local model HTTP request |
| `CHATGPT_MCP_LOCAL_DELEGATE_MAX_TURNS` | no | `12` read-only tool-loop turns |
| `CHATGPT_MCP_LOCAL_DELEGATE_MAX_TOKENS` | no | `1200` output-token limit per model turn |
| `CHATGPT_MCP_LOCAL_DELEGATE_ENABLE_THINKING` | no | `0`; local scout requests `enable_thinking=false` by default |
| `CHATGPT_MCP_LOCAL_DELEGATE_UNAVAILABLE_COOLDOWN_SECONDS` | no | `60`; short routing cooldown while the workstation/model server is offline |
| `CHATGPT_MCP_ANTIGRAVITY2_ENABLED` | no | `0`; set to `1` to expose a second isolated Antigravity account as `antigravity2` |
| `CHATGPT_MCP_ANTIGRAVITY3_ENABLED` | no | `0`; opt in to third isolated AGY account, pending its own OAuth login |
| `CHATGPT_MCP_ANTIGRAVITY3_COMMAND` | no | Inherits primary `agy` executable |
| `CHATGPT_MCP_ANTIGRAVITY3_HOME` | no | `<STATE_DIR>/antigravity3-home`; private OAuth, XDG, and conversation data |
| `CHATGPT_MCP_ANTIGRAVITY3_DEFAULT_MODEL` | no | Inherits primary AGY model, override independently |
| `CHATGPT_MCP_ANTIGRAVITY3_DEFAULT_REASONING_EFFORT` | no | Inherits primary AGY effort; `low`, `medium` or `high` |
| `CHATGPT_MCP_ANTIGRAVITY3_DELEGATE_MAX_CONCURRENCY` | no | `4` active explore/code slots for AGY3 |
| `CHATGPT_MCP_ANTIGRAVITY3_ELIGIBILITY_WATCHDOG_SCRIPT` | no | Canonical `~/.local/bin/agy-eligibility-recovery.sh`, probing AGY3 HOME when triggered |
| `CHATGPT_MCP_DELEGATE_AGY1_PREFERENCE_WEIGHT` | no | `1` (legacy routing); `3` makes AGY1 more likely for automatically routed delegate tasks while leaving AGY2 eligible |
| `CHATGPT_MCP_DELEGATE_AGY_CAPACITY_FIRST` | no | `0`; set to `1` to fill AGY1 then AGY3 then AGY2 slots, including code work, and never escalate routine work solely because accounts are busy |
| `CHATGPT_MCP_ANTIGRAVITY_DELEGATE_MAX_CONCURRENCY` | no | `4`; maximum active read-only AGY agents on the first account, across all batches |
| `CHATGPT_MCP_ANTIGRAVITY2_DELEGATE_MAX_CONCURRENCY` | no | `4`; independent limit for the second AGY account |
| `CHATGPT_MCP_ANTIGRAVITY2_COMMAND` | no | Inherits `CHATGPT_MCP_ANTIGRAVITY_COMMAND` |
| `CHATGPT_MCP_ANTIGRAVITY2_HOME` | no | `<STATE_DIR>/antigravity2-home`; isolated HOME/OAuth/state for the second account |
| `CHATGPT_MCP_ANTIGRAVITY2_DEFAULT_MODEL` | no | inherits the primary Antigravity model when unset; for the second, higher-effort account use `gemini-3.8-flash-high` |
| `CHATGPT_MCP_ANTIGRAVITY2_DEFAULT_REASONING_EFFORT` | no | inherits the primary Antigravity effort when unset; `low`, `medium`, or `high` |
| `CHATGPT_MCP_ANTIGRAVITY_ELIGIBILITY_WATCHDOG_SCRIPT` | no | `~/.local/bin/agy-eligibility-recovery.sh`; executable hook launched once per AGY eligibility-failure cooldown. Its canonical implementation, `scripts/agy-eligibility-recovery.sh`, uses installed Antigravity Unlocker v1.5+ to patch the **current** CLI in place, verifies the reported patched state, and accepts recovery **only** after a successful live Flash Low stream-json result with the expected response and matching pre/post-probe SHA-256. It uses an exclusive lock and bounded operations. There is no automatic downgrade or last-known-good rollback. Git/Python package updates are disabled during incident recovery; operators may explicitly opt in with `CHATGPT_MCP_AGY_RECOVERY_UPDATE_ON_RUN=1` (a Git/pip failure then fails closed). The default model can be changed via `CHATGPT_MCP_AGY_RECOVERY_MODEL` |
| `CHATGPT_MCP_AGY_RECOVERY_STATE_DIR` | no | `<STATE_DIR>/agy-recovery`; private recovery state holding the watchdog lock and transient probe output, removed after each run. No previous-version binary rollback is used |
| `CHATGPT_MCP_AGY_RECOVERY_LOCK_TIMEOUT_SECONDS` | no | `2`; a concurrent recovery waits up to two seconds for the lock and fails without patching when another invocation is active |
| `CHATGPT_MCP_ANTIGRAVITY_ELIGIBILITY_WATCHDOG_TIMEOUT_SECONDS` | no | `120`; hard wall-clock limit for the whole recovery hook. A timeout terminates its verified process group and keeps the AGY runtime block active |
| `CHATGPT_MCP_QUOTA_ADMISSION_POLICY_PATH` | no | `<STATE_DIR>/delegate-quota-policy.json`; MQTT-controlled admission thresholds |
| `CHATGPT_MCP_HEALTH_USAGE_LIMITS_ENABLED` | no | `1`; include cached Antigravity, Claude, and Codex usage windows in Ops Health |
| `CHATGPT_MCP_HEALTH_USAGE_LIMITS_REFRESH_SECONDS` | no | `300` seconds |
| `CHATGPT_MCP_HEALTH_USAGE_LIMITS_COMMAND_TIMEOUT_SECONDS` | no | `10` seconds |
| `CHATGPT_MCP_HEALTH_USAGE_LIMITS_HTTP_TIMEOUT_SECONDS` | no | `5` seconds |
| `CHATGPT_MCP_QUOTA_PRIMING_ENABLED` | no | `1`; automatically start eligible 5-hour CLI quota windows with a tiny verified turn |
| `CHATGPT_MCP_QUOTA_PRIMING_CHECK_INTERVAL_SECONDS` | no | `30` seconds |
| `CHATGPT_MCP_QUOTA_PRIMING_POST_RESET_DELAY_SECONDS` | no | `120` seconds after the recorded 5-hour reset |
| `CHATGPT_MCP_QUOTA_PRIMING_VERIFICATION_DELAY_SECONDS` | no | `5` seconds before rereading quota after a priming turn |
| `CHATGPT_MCP_QUOTA_PRIMING_VERIFICATION_PROBE_DELAY_SECONDS` | no | `3` seconds between Codex reset-stability probes when `usedPercent` still rounds to zero |
| `CHATGPT_MCP_QUOTA_PRIMING_RETRY_SECONDS` | no | `300` seconds between failed attempts |
| `CHATGPT_MCP_QUOTA_PRIMING_COMMAND_TIMEOUT_SECONDS` | no | `90` seconds |
| `CHATGPT_MCP_QUOTA_PRIMING_MAX_ATTEMPTS_PER_CYCLE` | no | `3`; prevents repeated quota hammering when activation cannot be verified |
| `CHATGPT_MCP_QUOTA_PRIMING_ANTIGRAVITY_GEMINI_MODEL` | no | `gemini-3.8-flash-low` |
| `CHATGPT_MCP_QUOTA_PRIMING_ANTIGRAVITY_THIRD_PARTY_MODEL` | no | `gpt-oss-120b-medium` |
| `CHATGPT_MCP_QUOTA_PRIMING_CLAUDE_MODEL` | no | `haiku` |
| `CHATGPT_MCP_CODEX_RUNTIME_CUA_APPROVAL_MODE` | no | `interactive`; also supports restricted `prototype` and `deny` |
| `CHATGPT_MCP_CODEX_RUNTIME_CUA_ALLOWED_APPS` | no | Empty comma-separated exact app bundle-ID allowlist |
| `CHATGPT_MCP_PI_COMMAND` | no | `pi` |
| `CHATGPT_MCP_COMMAND_TIMEOUT` | no | `300` seconds |
| `CHATGPT_MCP_DEBUG_MCP_LOGGING` | no | `0` |
| `CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS` | no | `30` seconds |
| `CHATGPT_MCP_RELOAD_READY_TIMEOUT_SECONDS` | no | `15` seconds |

### tmux

| Variable | Required | Default |
| --- | --- | --- |
| `CHATGPT_MCP_TMUX_BINARY` | no | `tmux` |
| `CHATGPT_MCP_TMUX_SOCKET_NAME` | no | `default` |
| `CHATGPT_MCP_TMUX_CONTROL_TIMEOUT` | no | `10` seconds |

### Tunnel and launchd helpers

| Variable | Required | Default / behavior |
| --- | --- | --- |
| `CHATGPT_MCP_CLOUDFLARED_CONFIG` | for full tunnel install | Empty; named tunnel config path |
| `CHATGPT_MCP_TUNNEL_NAME` | no | Empty; optional named-tunnel override |
| `CHATGPT_MCP_EXTERNAL_CLOUDFLARED` | no | `0`; set to `1` when cloudflared is managed externally |
| `CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS` | no | `60` |
| `CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD` | no | `3` |
| `CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS` | no | `300` |
| `CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS` | no | `3600` |
| `CHATGPT_MCP_LAUNCHD_LABEL_PREFIX` | no | `com.chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_LAUNCHD_DIR` | no | `~/Library/LaunchAgents` |
| `CHATGPT_MCP_LAUNCHD_LOG_DIR` | no | `~/Library/Logs/chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_LAUNCHD_PATH` | no | Current shell `PATH` captured by the install scripts |
| `CHATGPT_MCP_DOCTOR_LOCAL_WAIT_SECONDS` | no | `20` |
| `CHATGPT_MCP_DOCTOR_PUBLIC_WAIT_SECONDS` | no | `30` |
| `CHATGPT_MCP_DOCTOR_STATE_FILE` | no | Internal default under the state directory when empty |

`CHATGPT_MCP_READY_FD` is an internal supervisor-to-child handoff and should not be configured manually.

## Security model

This server exposes powerful local capabilities. Treat the public endpoint and all credentials as sensitive.

Important boundaries:

- `CHATGPT_MCP_WORKSPACE_ROOT` is a relative-path anchor and default working directory. It is **not** a filesystem sandbox.
- Absolute paths remain absolute.
- `run_command`, `job_*`, `tmux_*`, write tools, and Git writes can modify the local machine with the permissions of the server process.
- Only connect trusted ChatGPT accounts/workspaces and only expose the tools you are prepared to authorize.
- OAuth mode does not accept `CHATGPT_MCP_AUTH_TOKEN` as a bearer credential; configure a dedicated `CHATGPT_MCP_OAUTH_LOGIN_TOKEN`.
- Keep `CHATGPT_MCP_PUBLIC_BASE_URL` stable and do not rely on untrusted Host headers for OAuth issuer metadata.
- Point the default cwd at a dedicated workspace rather than your whole home directory.
- Rotate leaked tokens and clear `~/.chatgpt-web-oauth-mcp/oauth.json` when OAuth state must be invalidated.
- Use an isolated tmux socket when MCP-created sessions should not share the normal local tmux server.

## Development

```bash
source .venv/bin/activate
pytest -q
python -m compileall src tests
```

Project rules and architecture notes are documented in [`AGENTS.md`](AGENTS.md).

## Upstream project

This repository was extracted from [`catoncat/notion-local-ops-mcp`](https://github.com/catoncat/notion-local-ops-mcp).

It keeps the reusable local-operations MCP server concepts and ChatGPT-compatible OAuth layer, while removing the original product-specific workflows, screenshots, prompts, TaskBoard integration, product skills, and branding.

Major changes include:

- renamed package, CLI, launchd labels, and environment prefix to `chatgpt-web-oauth-mcp` / `CHATGPT_MCP_*`;
- a focused ChatGPT Web OAuth MCP architecture;
- bounded, token-aware local context tools;
- generic Git, job, tmux, and persistent Codex runtime/MCP workflows.

## License

MIT
