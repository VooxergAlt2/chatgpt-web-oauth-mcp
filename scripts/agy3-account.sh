#!/usr/bin/env bash
# Interactive Antigravity CLI for the third isolated OAuth account.
# Use as "agy3" after installing this script under ~/.local/bin.
set -euo pipefail

agy_bin="$(command -v agy || true)"
if [[ -z "$agy_bin" || ! -x "$agy_bin" ]]; then
    printf 'agy3: agy CLI is not available on PATH\n' >&2
    exit 1
fi

account_home="${CHATGPT_MCP_ANTIGRAVITY3_HOME:-${HOME}/.local/state/chatgpt-web-oauth-mcp/antigravity3-home}"
if [[ "$account_home" != /* ]]; then
    printf 'agy3: third-account HOME must be an absolute path\n' >&2
    exit 2
fi
mkdir -p "$account_home"
chmod 700 "$account_home"
export HOME="$account_home"
export XDG_CONFIG_HOME="$account_home/.config"
export XDG_CACHE_HOME="$account_home/.cache"
export XDG_DATA_HOME="$account_home/.local/share"
exec "$agy_bin" "$@"
