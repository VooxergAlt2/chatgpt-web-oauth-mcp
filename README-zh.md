# chatgpt-web-oauth-mcp

[English](README.md)

> 感谢 [LINUX.DO 社区](https://linux.do/)。

一个本地 [FastMCP](https://github.com/jlowin/fastmcp) 服务器，让 **ChatGPT Web** 通过受 OAuth 保护的远程 HTTPS MCP endpoint，调用你电脑上的可信工具。

它提供有界的文件与代码搜索、结构化 Git 操作、短命令执行、持久后台任务、tmux 交互会话，以及持久 Codex runtime/MCP 访问，同时让 ChatGPT Web 保持 architect / manager / reviewer 的角色。

## 为什么需要这个项目

ChatGPT 无法直接连接只监听 `127.0.0.1` 的本地进程。一个自定义 MCP 应用需要可访问的远程 endpoint、认证流程，以及不会无边界占满模型上下文的工具输出。

本项目提供这层桥接：

- `/mcp` Streamable HTTP MCP endpoint；
- 与 ChatGPT 兼容的 OAuth discovery、dynamic client registration、PKCE 和 bearer token 校验；
- 可选的 Cloudflare Tunnel 与 macOS `launchd` 辅助脚本；
- 带明确分页、token budget 和输出边界的本地操作工具；
- 将短命令、持久任务、交互终端和持久 Codex runtime/MCP 访问拆成不同执行通道。

## 架构

```text
ChatGPT Web
    │
    │ HTTPS + OAuth
    ▼
公网 MCP endpoint (/mcp)
    │
    ▼
本地 FastMCP server 127.0.0.1:8766
    │
    ├── Direct tools
    │   ├── files / search / read / patch
    │   ├── code maps / environment inspection
    │   └── Git / worktrees
    │
    ├── Execution tools
    │   ├── run_command     有界且预计在前台窗口内完成的命令
    │   ├── job_*           持久、非交互后台任务
    │   └── tmux_*          持久交互式 TTY 会话
    │
    └── Codex runtime
        ├── codex_runtime_* 持久 runtime 绑定
        └── codex_mcp_*     已连接 MCP inventory/call
```

ChatGPT Web 通过直接 MCP tools 完成检查、规划、编辑和验证。持久 Codex 访问由 `codex_runtime_*` 与 `codex_mcp_*` 提供；这些 runtime tools 本身不会启动 Codex LLM turn。

## 核心能力

| 领域 | 能力 |
| --- | --- |
| 远程 MCP 接入 | Streamable HTTP `/mcp`、discovery metadata、server card、HTTPS tunnel 支持 |
| 认证 | `none`、共享 bearer token，或支持 PKCE 和动态客户端注册的 OAuth authorization-code flow |
| 有界上下文获取 | token-aware 分页、统一 continuation metadata、共享 batch budget、ignore-aware 遍历 |
| 文件与代码 | Glob/regex/文本搜索、文本与多模态文件读取、轻量 symbol/reference/import map |
| 机械式安全编辑 | 完整文件写入、结构化 patch、带 CAS 保护和原子写入的批量替换 |
| Git | status、diff、commit、log、show、blame，以及精简 worktree 生命周期 |
| 本地执行 | 有界命令、持久后台 job、持久 tmux 会话 |
| Codex runtime | 持久 Codex App Server 绑定，以及已连接 MCP 的 inventory/call |
| macOS 运维 | 开发隧道、持久 launchd 安装、状态、doctor、reload、restart 和卸载脚本 |

## 运行模型

始终选择最窄、最匹配当前任务的工具：

1. 使用 `list_files`、`search`、`read_text`、`read`、`code_map_*`、`git_status` 或 `git_diff` 检查上下文。
2. 使用 `apply_patch`、`replace`、`write_file` 或结构化 Git 工具完成确定性修改。
3. 根据进程生命周期和交互需求选择 `run_command`、`job_*` 或 `tmux_*`。
4. 需要持久 Codex runtime 或已连接 MCP 访问时，使用 `codex_runtime_*` 和 `codex_mcp_*`。
5. 在宣布完成前直接验证结果。

Computer Use 授权默认使用 `interactive`。如需启用
[Issue #12](https://github.com/escapeWu/chatgpt-web-oauth-mcp/issues/12) 的受限本地原型，设置
`CHATGPT_MCP_CODEX_RUNTIME_CUA_APPROVAL_MODE=prototype`，并在
`CHATGPT_MCP_CODEX_RUNTIME_CUA_ALLOWED_APPS` 中填写精确的 App bundle ID。该模式仅绕过
`cua_repl` 的 `get_app_state` App-access gate；后续有副作用的操作仍保持交互确认。

## 依赖要求

- Python 3.11 或更高版本
- Git
- `ripgrep`，作为首选搜索后端
- `tmux`，用于持久交互式会话
- 使用持久 Codex runtime 集成时需要 Codex CLI
- 只有使用内置 tunnel helper 时才需要 `cloudflared`
- 只有内置 `launchd` 脚本依赖 macOS；Python server 本身并不绑定 launchd

只需安装你计划使用的工具所依赖的可选二进制程序。

## 快速开始

```bash
git clone https://github.com/escapeWu/chatgpt-web-oauth-mcp.git
cd chatgpt-web-oauth-mcp

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

使用 ChatGPT OAuth 模式时，至少在 `.env` 中配置：

```bash
CHATGPT_MCP_WORKSPACE_ROOT="/absolute/path/to/workspace"
CHATGPT_MCP_AUTH_MODE=oauth
CHATGPT_MCP_PUBLIC_BASE_URL="https://your-public-mcp-host.example"
CHATGPT_MCP_OAUTH_LOGIN_TOKEN="replace-with-a-long-random-token"
```

OAuth 模式必须配置 `CHATGPT_MCP_OAUTH_LOGIN_TOKEN`，并在授权页面中输入。`CHATGPT_MCP_AUTH_TOKEN` 仅用于 `shared_token` 模式，在 OAuth 模式下不会作为 bearer 凭据被接受。

启动本地 server 与已配置的 tunnel：

```bash
./scripts/dev-tunnel.sh
```

本地 MCP endpoint：

```text
http://127.0.0.1:8766/mcp
```

公网 MCP endpoint：

```text
https://your-public-mcp-host.example/mcp
```

## 添加到 ChatGPT

ChatGPT 当前将自定义 MCP 集成称为 **应用（Apps）**。具体界面和套餐权限可能变化，但服务端接入流程是：

1. 为符合条件的账号或工作空间启用 ChatGPT developer mode。
2. 打开 **Settings → Apps → Create**，或工作空间管理员对应的 Apps 页面。
3. 填入公网 MCP endpoint，例如 `https://your-public-mcp-host.example/mcp`。
4. 选择 OAuth 认证。
5. 扫描工具。
6. 使用 `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` 完成授权流程。
7. 按工作空间策略创建或发布应用。

OpenAI 官方参考：

- [ChatGPT 中的应用](https://help.openai.com/zh-hans-cn/articles/11487775-connectors-in-chatgpt)
- [ChatGPT 中的开发者模式和 MCP 应用](https://help.openai.com/zh-hans-cn/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt-beta)

ChatGPT 套餐、工作空间、审批和写操作可用性由 ChatGPT 控制，而不是由本 server 决定。

### OAuth token 生命周期

当前 OAuth 实现支持带 PKCE 的 authorization-code grant，并支持轮换 refresh token。Access token 有效期由 `CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS` 控制，refresh token 有效期由 `CHATGPT_MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS` 单独控制。成功刷新后，旧 refresh token 会立即失效，并返回新的 refresh token。Server 不声明 `offline_access`。

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

MCP endpoint：

```text
/mcp
```

## Smoke test

```bash
curl -sS https://your-public-mcp-host.example/.well-known/oauth-protected-resource/mcp
curl -sS https://your-public-mcp-host.example/.well-known/oauth-authorization-server
curl -i https://your-public-mcp-host.example/mcp
```

预期行为：

- 前两个请求返回 JSON 格式的 OAuth metadata；
- 未认证访问 `/mcp` 返回 `401`；
- OAuth 模式下，`WWW-Authenticate` header 包含 `resource_metadata`。

## 仅本地运行

```bash
source .venv/bin/activate
chatgpt-web-oauth-mcp
```

这会启动 server，但不会创建公网路由，适合本地测试。ChatGPT 无法直接连接 loopback endpoint。

## 使用已有 Cloudflare Tunnel

如果已有服务将公网域名映射到 `http://127.0.0.1:8766`，不要再启动第二个 tunnel。正常配置 OAuth，并只安装 MCP service：

```bash
CHATGPT_MCP_PUBLIC_BASE_URL="https://your-existing-host.example"
CHATGPT_MCP_EXTERNAL_CLOUDFLARED=1
./scripts/install-launchd.sh --mcp-only
```

在 `--mcp-only` 模式下，本项目会安装并监控 MCP 进程，但不会创建、重启或监控 `cloudflared` launchd service。

## 持久化 macOS launchd 安装

完整安装需要 named Cloudflare Tunnel 配置：

```bash
./scripts/install-launchd.sh
```

运维命令：

```bash
./scripts/launchd-status.sh
./scripts/launchd-doctor.sh
./scripts/launchd-doctor.sh --fix
./scripts/launchd-reload.sh
./scripts/launchd-restart.sh mcp
./scripts/launchd-restart.sh all
./scripts/uninstall-launchd.sh
```

watchdog 负责检查服务健康状态。doctor 脚本会按照失败阈值和有上限的指数退避执行定向重启。

## Tool 参考

设置 `CHATGPT_MCP_TOOL_PROFILE=lean` 可从 `tools/list` 隐藏高级 `codex_runtime_*`、`codex_mcp_*` 和 `tmux_*` surface；实现仍保留，需要时可切回 `full` 并重启。仓库默认 profile 为 `full`。

### Runtime 与环境

| Tool | 用途 |
| --- | --- |
| `server_info` | 检查运行时配置和已注册 MCP tools |
| `get_guide` | 按名称加载一个 progressive-disclosure 指南：delegate、file、Code Graph、process、runtime 或 Git |
| `get_code_graph_use` | Code Graph 指南的兼容 shortcut |
| `set_default_cwd` / `get_default_cwd` | 设置或读取 session 级默认工作目录 |
| `env_snapshot` / `env_diff` | 收集小型只读环境快照，并比较两个 inline snapshot |

### 文件、搜索与代码上下文

| Tool | 用途 |
| --- | --- |
| `list_files` | 支持 ignore、过滤、排序、稳定分页和 token budget 的目录列表 |
| `search` | Glob、regex、文本或 batch 搜索；并行 batch 最多三个 worker |
| `read_text` | 向后兼容的单文件或批量文本读取，支持行分页 |
| `read` | 统一读取文本/指定编码、图片 metadata/reference、PDF 页文本和 binary hex |
| `code_map_symbols` | 轻量 Python、JavaScript 或 TypeScript 定义发现 |
| `code_map_references` | 使用 identifier word boundary 的有界文本引用查找 |
| `code_map_imports` | 轻量 import 发现 |
| `write_file` | 创建或完整覆盖文件，支持 dry-run |
| `replace` | 带锁与 CAS 的批量替换，支持 dry-run、原子写入和编码/换行/BOM/权限保持 |
| `apply_patch` | 对已有文件应用结构化 patch |

### Git 与 worktree

| Tool | 用途 |
| --- | --- |
| `git_status` | 结构化仓库状态 |
| `git_diff` | 按文件限制大小的 staged 或 unstaged diff |
| `git_commit` | stage 指定路径或全部改动并创建 commit |
| `git_log` | 最近提交历史 |
| `git_show` | commit metadata 与按文件限制大小的 diff |
| `git_blame` | 每行 commit、author、summary 和内容 |
| `git_worktree_create` | 创建 clean branch 或 detached worktree |
| `git_worktree_list` | 列出已注册 worktree |
| `git_worktree_status` | 检查一个或全部 worktree |
| `git_worktree_remove` | 移除已注册 worktree；默认拒绝 dirty worktree |

### 命令与持久后台任务

| Tool | 用途 |
| --- | --- |
| `run_command` | 执行一个有界、完整的命令或顺序/并行 batch；direct/local client 可使用最高 900 秒，ChatGPT/OpenAI session 使用配置的安全前台预算（默认 30 秒） |
| `job_start` | 启动非交互后台进程，持久化 metadata，并保存独立日志 |
| `job_list` | 从 state directory 发现任务，包括 server 重启后的任务 |
| `job_status` | 读取进程状态、exit status、耗时、资源和日志路径 |
| `job_output` | 使用独立 raw-byte cursor 读取一个 stdout/stderr stream |
| `job_tail` | 以兼容 API 读取最后若干行 |
| `job_kill` | 停止正在运行的任务 |

### 持久 tmux 会话

| Tool | 用途 |
| --- | --- |
| `tmux_list` | 列出配置 socket 上的 session |
| `tmux_start` | 启动一个带 primary pane 的 detached session |
| `tmux_status` | 检查 pane command、cwd、PID、尺寸和退出状态 |
| `tmux_capture` | 捕获有界终端 screen/history 快照 |
| `tmux_send` | 通过 tmux buffer 粘贴 UTF-8 文本，并发送小范围 allowlist key |
| `tmux_kill` | 删除一个精确匹配的 session |

`tmux_capture` 不是无损应用日志。全屏 TUI、进度条、回车覆盖更新和 tmux history limit 都会影响可见内容。需要保留终端历史时，优先使用应用日志，或使用类似 `--no-alt-screen` 的模式。

### 操作指南

在不熟悉的工具族第一次工作流前调用 `get_guide(name=...)`。可用名称为 `delegate-use`、`file-use`、`code-graph-use`、`process-use`、`runtime-use` 和 `git-use`。保留 `get_code_graph_use` 作为兼容 shortcut。

同一份权威内容也通过标准 MCP resources 暴露：

| Resource | 用途 |
| --- | --- |
| `skill://chatgpt-web-oauth-mcp/index` | 机器可读的 guide 索引、触发条件和 tool/resource 路由 |
| `skill://chatgpt-web-oauth-mcp/file-use` | 完整 Markdown 文件工作流指南 |
| `skill://chatgpt-web-oauth-mcp/process-use` | 完整 Markdown command、job 与 tmux 指南 |
| `skill://chatgpt-web-oauth-mcp/git-use` | 完整 Markdown Git 与 worktree 指南 |

指南内容仍然同时通过 MCP resources 暴露；原先每个指南一个 loader tool 的接口已合并，以减小公开 tool catalog。

## 如何选择执行工具

| 需求 | 使用 | 不适合 |
| --- | --- | --- |
| 预计在安全 client 前台窗口内完成的有界非交互工作 | `run_command` | 更长的完整命令应作为一个 durable job 运行，不要仅为时长而拆分；交互式 TUI 使用 `tmux_*` |
| 带可检查日志的持久非交互进程 | `job_*` | 需要交互输入的程序 |
| 持久交互终端或可人工 attach 的 session | `tmux_*` | 无损 stdout/stderr 采集 |
| 一个有界的 agent 调研或实现切片 | `delegate_task` | 直接工具能更便宜、明确完成的确定性本地操作 |
| 同一项目中的多个独立只读调研 | `delegate_batch` | 并发 writer |

`server_info` 与 `delegate_harnesses` 会返回确定性的 delegate routing 提示。它们不会自动改写显式指定的 harness。普通有界探索优先使用低成本只读路径，独立的二次 review 可以使用 Antigravity，而实现保持单个 project-scoped writer slice。第二个 Antigravity 账号可作为 `antigravity2` 暴露；它使用同一 CLI binary，但具有独立的 HOME/OAuth state，因此两个账号的 conversation 与 quota window 相互隔离。在 Linux 上，仅当本地无 LLM 的 sandbox probe 成功时，Codex 才会参与 read-only routing；若 sandbox runtime 不可用，有界探索会回退到其他可用的 read-only harness，而不会削弱 read-only contract。对于 Git 项目，delegate prompt 只注入紧凑的 project root 和提交时 HEAD，不会自动塞入完整 diff。

Delegate 结果 telemetry 保存在 `<STATE_DIR>/delegate-telemetry.json`。它有界保存生命周期、route、耗时、provider 可提供的 usage metadata，以及 terminal result 是否被显式 consumed；不会持久化 task、goal 或 prompt 文本。从旧临时 state 位置迁移的历史 delegate 记录仍可用于 recovery/status，但不会写入 telemetry baseline，因为旧临时 state 可能包含隔离测试之前产生的测试记录，并且没有可信的 consumption 历史。

Delegate quota admission 由 `<STATE_DIR>/delegate-quota-policy.json` 控制；Home Assistant monitor 会把这些值暴露为由 MQTT 驱动的 retained `number` entity。每个 threshold 表示“剩余 quota 百分比小于等于该值时，拒绝新的 delegate submit”，范围可为 0–100，并分别覆盖 5 小时窗口、weekly 窗口以及两个 Antigravity 账号的模型组。Gate 只作用于 admission：已经创建的 running/queued task 不会被取消，对已有 active delegate 的 dedupe/attach 仍然允许。Provider 明确返回 quota exhausted 时，也会强制阻止新的 submit，直到 reset/retry window 结束。

## 输出 budget 与分页

Token-aware 只读响应使用 `o200k_base` 编码，并提供统一结果协议，包括：

- `complete` / `partial`；
- `estimated_tokens` 和实际生效的 budget；
- `truncated` 和 `stop_reason`；
- 存在后续结果时的 continuation offset。

批量 `read_text`、`search` 和 `run_command` 使用一个共享响应 budget，不会按照子请求数量重复放大上限。ChatGPT/OpenAI session 的 `run_command` 还受 `CHATGPT_MCP_OPENAI_FOREGROUND_TIMEOUT` 限制，以便在上游 command-response deadline 前返回；更长的完整命令应通过一次 `job_start` 运行。

## 环境变量

### Server 与认证

| 变量 | 必需 | 默认值 / 行为 |
| --- | --- | --- |
| `CHATGPT_MCP_HOST` | 否 | `127.0.0.1` |
| `CHATGPT_MCP_PORT` | 否 | `8766` |
| `CHATGPT_MCP_WORKSPACE_ROOT` | 建议 | `$HOME`；相对路径锚点和默认 cwd，**不是 sandbox** |
| `CHATGPT_MCP_STATE_DIR` | 否 | `~/.chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_DELEGATE_STATE_DIR` | 否 | `<STATE_DIR>/delegates`；持久化 delegate metadata/log。未显式覆盖时，启动阶段会导入旧的临时目录状态 |
| `CHATGPT_MCP_AUTH_MODE` | 建议 | 显式设置 `none`、`shared_token` 或 `oauth`；为空时，有 `AUTH_TOKEN` 则选 shared token，否则为 none |
| `CHATGPT_MCP_AUTH_TOKEN` | `shared_token` 必需 | 空；仅用于 `shared_token` 模式的 bearer token |
| `CHATGPT_MCP_PUBLIC_BASE_URL` | OAuth 必需 | 空；稳定的公网 issuer/resource base URL |
| `CHATGPT_MCP_OAUTH_LOGIN_TOKEN` | OAuth 必需 | 无回退；OAuth 授权页面中输入的独立 secret |
| `CHATGPT_MCP_OAUTH_SCOPES` | 否 | `local-ops` |
| `CHATGPT_MCP_OAUTH_TOKEN_TTL_SECONDS` | 否 | `86400` |
| `CHATGPT_MCP_OAUTH_REFRESH_TOKEN_TTL_SECONDS` | 否 | `2592000`（30 天） |

### 搜索、输出与执行

| 变量 | 必需 | 默认值 / 行为 |
| --- | --- | --- |
| `CHATGPT_MCP_RIPGREP_BINARY` | 否 | `rg` |
| `CHATGPT_MCP_TOOL_OUTPUT_TOKEN_BUDGET` | 否 | `8500` |
| `CHATGPT_MCP_READ_TOKEN_BUDGET` | 否 | 继承全局 tool budget |
| `CHATGPT_MCP_RUN_TOKEN_BUDGET` | 否 | 继承全局 tool budget |
| `CHATGPT_MCP_JOB_OUTPUT_TOKEN_BUDGET` | 否 | 继承全局 tool budget |
| `CHATGPT_MCP_RUN_CAPTURE_MAX_BYTES` | 否 | `1048576` bytes |
| `CHATGPT_MCP_CODEX_COMMAND` | 否 | `codex` |
| `CHATGPT_MCP_LOCAL_DELEGATE_ENABLED` | 否 | `0`；设为 `1` 后注册可选的只读 `local` OpenAI-compatible explore scout |
| `CHATGPT_MCP_LOCAL_DELEGATE_ENDPOINT` | 否 | `http://127.0.0.1:8081`；工作站上的 llama.cpp/OpenAI-compatible base URL |
| `CHATGPT_MCP_LOCAL_DELEGATE_MODEL` | 否 | `qwen80`；必须与 `/v1/models` 返回的 model alias 一致 |
| `CHATGPT_MCP_LOCAL_DELEGATE_HEALTH_TIMEOUT_MS` | 否 | `350`；自动路由前的短 health probe |
| `CHATGPT_MCP_LOCAL_DELEGATE_REQUEST_TIMEOUT_SECONDS` | 否 | 每次本地模型 HTTP 请求 `120` 秒 |
| `CHATGPT_MCP_LOCAL_DELEGATE_MAX_TURNS` | 否 | `12` 个只读 tool-loop turn |
| `CHATGPT_MCP_LOCAL_DELEGATE_MAX_TOKENS` | 否 | 每个模型 turn 最多 `1200` output tokens |
| `CHATGPT_MCP_LOCAL_DELEGATE_ENABLE_THINKING` | 否 | `0`；local scout 默认请求 `enable_thinking=false` |
| `CHATGPT_MCP_LOCAL_DELEGATE_UNAVAILABLE_COOLDOWN_SECONDS` | 否 | `60` 秒；工作站/model server 离线时的短 routing cooldown |
| `CHATGPT_MCP_ANTIGRAVITY2_ENABLED` | 否 | `0`；设为 `1` 后将第二个独立 Antigravity 账号暴露为 `antigravity2` |
| `CHATGPT_MCP_ANTIGRAVITY2_COMMAND` | 否 | 继承 `CHATGPT_MCP_ANTIGRAVITY_COMMAND` |
| `CHATGPT_MCP_ANTIGRAVITY2_HOME` | 否 | `<STATE_DIR>/antigravity2-home`；第二账号独立 HOME/OAuth/state |
| `CHATGPT_MCP_QUOTA_ADMISSION_POLICY_PATH` | 否 | `<STATE_DIR>/delegate-quota-policy.json`；MQTT 控制的 admission threshold |
| `CHATGPT_MCP_HEALTH_USAGE_LIMITS_ENABLED` | 否 | `1`；在 Ops Health 中包含缓存的 Antigravity、Claude 与 Codex 使用窗口 |
| `CHATGPT_MCP_HEALTH_USAGE_LIMITS_REFRESH_SECONDS` | 否 | `300` 秒 |
| `CHATGPT_MCP_HEALTH_USAGE_LIMITS_COMMAND_TIMEOUT_SECONDS` | 否 | `10` 秒 |
| `CHATGPT_MCP_HEALTH_USAGE_LIMITS_HTTP_TIMEOUT_SECONDS` | 否 | `5` 秒 |
| `CHATGPT_MCP_QUOTA_PRIMING_ENABLED` | 否 | `1`；使用极小且可验证的请求自动启动可用的 5 小时 CLI 配额窗口 |
| `CHATGPT_MCP_QUOTA_PRIMING_CHECK_INTERVAL_SECONDS` | 否 | `30` 秒 |
| `CHATGPT_MCP_QUOTA_PRIMING_POST_RESET_DELAY_SECONDS` | 否 | 已记录的 5 小时重置后 `120` 秒 |
| `CHATGPT_MCP_QUOTA_PRIMING_VERIFICATION_DELAY_SECONDS` | 否 | priming 后 `5` 秒重新读取配额 |
| `CHATGPT_MCP_QUOTA_PRIMING_VERIFICATION_PROBE_DELAY_SECONDS` | 否 | 当 Codex `usedPercent` 仍四舍五入为零时，两次 reset 稳定性检查间隔 `3` 秒 |
| `CHATGPT_MCP_QUOTA_PRIMING_RETRY_SECONDS` | 否 | 失败尝试间隔 `300` 秒 |
| `CHATGPT_MCP_QUOTA_PRIMING_COMMAND_TIMEOUT_SECONDS` | 否 | `90` 秒 |
| `CHATGPT_MCP_QUOTA_PRIMING_MAX_ATTEMPTS_PER_CYCLE` | 否 | `3`；防止无法验证激活时反复请求配额 |
| `CHATGPT_MCP_QUOTA_PRIMING_ANTIGRAVITY_GEMINI_MODEL` | 否 | `gemini-3.8-flash-low` |
| `CHATGPT_MCP_QUOTA_PRIMING_ANTIGRAVITY_THIRD_PARTY_MODEL` | 否 | `gpt-oss-120b-medium` |
| `CHATGPT_MCP_QUOTA_PRIMING_CLAUDE_MODEL` | 否 | `haiku` |
| `CHATGPT_MCP_CODEX_RUNTIME_CUA_APPROVAL_MODE` | 否 | `interactive`；也支持受限的 `prototype` 和 `deny` |
| `CHATGPT_MCP_CODEX_RUNTIME_CUA_ALLOWED_APPS` | 否 | 空；逗号分隔的精确 App bundle ID allowlist |
| `CHATGPT_MCP_PI_COMMAND` | 否 | `pi` |
| `CHATGPT_MCP_COMMAND_TIMEOUT` | 否 | `300` 秒 |
| `CHATGPT_MCP_DEBUG_MCP_LOGGING` | 否 | `0` |
| `CHATGPT_MCP_GRACEFUL_SHUTDOWN_SECONDS` | 否 | `30` 秒 |
| `CHATGPT_MCP_RELOAD_READY_TIMEOUT_SECONDS` | 否 | `15` 秒 |

### tmux

| 变量 | 必需 | 默认值 |
| --- | --- | --- |
| `CHATGPT_MCP_TMUX_BINARY` | 否 | `tmux` |
| `CHATGPT_MCP_TMUX_SOCKET_NAME` | 否 | `default` |
| `CHATGPT_MCP_TMUX_CONTROL_TIMEOUT` | 否 | `10` 秒 |

### Tunnel 与 launchd helper

| 变量 | 必需 | 默认值 / 行为 |
| --- | --- | --- |
| `CHATGPT_MCP_CLOUDFLARED_CONFIG` | 完整 tunnel 安装需要 | 空；named tunnel config path |
| `CHATGPT_MCP_TUNNEL_NAME` | 否 | 空；可选 named-tunnel override |
| `CHATGPT_MCP_EXTERNAL_CLOUDFLARED` | 否 | `0`；cloudflared 由外部管理时设为 `1` |
| `CHATGPT_MCP_WATCHDOG_INTERVAL_SECONDS` | 否 | `60` |
| `CHATGPT_MCP_DOCTOR_FAILURE_THRESHOLD` | 否 | `3` |
| `CHATGPT_MCP_DOCTOR_BASE_BACKOFF_SECONDS` | 否 | `300` |
| `CHATGPT_MCP_DOCTOR_MAX_BACKOFF_SECONDS` | 否 | `3600` |
| `CHATGPT_MCP_LAUNCHD_LABEL_PREFIX` | 否 | `com.chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_LAUNCHD_DIR` | 否 | `~/Library/LaunchAgents` |
| `CHATGPT_MCP_LAUNCHD_LOG_DIR` | 否 | `~/Library/Logs/chatgpt-web-oauth-mcp` |
| `CHATGPT_MCP_LAUNCHD_PATH` | 否 | 安装脚本捕获的当前 shell `PATH` |
| `CHATGPT_MCP_DOCTOR_LOCAL_WAIT_SECONDS` | 否 | `20` |
| `CHATGPT_MCP_DOCTOR_PUBLIC_WAIT_SECONDS` | 否 | `30` |
| `CHATGPT_MCP_DOCTOR_STATE_FILE` | 否 | 为空时使用 state directory 下的内部默认值 |

`CHATGPT_MCP_READY_FD` 是 supervisor 向 child process 传递的内部参数，不应手工配置。

## 安全模型

这个 server 会暴露强大的本地能力。必须将公网 endpoint 和所有认证信息视为敏感数据。

重要边界：

- `CHATGPT_MCP_WORKSPACE_ROOT` 只是相对路径锚点和默认工作目录，**不是文件系统 sandbox**。
- 绝对路径仍会按绝对路径处理。
- `run_command`、`job_*`、`tmux_*`、写入工具和 Git 写操作都能以 server process 的权限修改本机。
- 只连接可信 ChatGPT 账号/工作空间，只暴露你愿意授权的工具。
- OAuth 模式不会接受 `CHATGPT_MCP_AUTH_TOKEN` 作为 bearer 凭据；必须单独配置 `CHATGPT_MCP_OAUTH_LOGIN_TOKEN`。
- 保持 `CHATGPT_MCP_PUBLIC_BASE_URL` 稳定，不要依赖不可信 Host header 生成 OAuth issuer metadata。
- 默认 cwd 建议指向独立 workspace，而不是整个 home directory。
- token 泄露后应立即轮换；需要使 OAuth state 失效时，清理 `~/.chatgpt-web-oauth-mcp/oauth.json`。
- 如果不希望 MCP 创建的 session 与普通本地 tmux server 共用 socket，应配置隔离 socket。

## 开发

```bash
source .venv/bin/activate
pytest -q
python -m compileall src tests
```

项目规则与架构说明见 [`AGENTS.md`](AGENTS.md)。

## 上游项目

本仓库从 [`catoncat/notion-local-ops-mcp`](https://github.com/catoncat/notion-local-ops-mcp) 剥离而来。

它保留了可复用的本地操作 MCP server 思路和 ChatGPT 兼容 OAuth 层，同时移除了原项目中的产品专用工作流、截图、prompt、TaskBoard 集成、产品 skills 和品牌命名。

主要变化包括：

- 将 package、CLI、launchd label 和环境变量前缀统一为 `chatgpt-web-oauth-mcp` / `CHATGPT_MCP_*`；
- 形成聚焦 ChatGPT Web OAuth MCP 的架构；
- 增加有界、token-aware 的本地上下文工具；
- 提供通用 Git、job、tmux 和持久 Codex runtime/MCP 流程。

## License

MIT
