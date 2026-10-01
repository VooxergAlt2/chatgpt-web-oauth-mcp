#!/usr/bin/env bash
set -euo pipefail

PATCHER_DIR="${CHATGPT_MCP_AGY_PATCHER_DIR:-${HOME}/eligibility-antigravity-patcher}"
AGY_BIN="${CHATGPT_MCP_AGY_BIN:-}"
PATCHER_PYTHON="${CHATGPT_MCP_AGY_PATCHER_PYTHON:-}"
MODEL="${CHATGPT_MCP_AGY_RECOVERY_MODEL:-gemini-3.8-flash}"
PATCH_TIMEOUT_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_PATCH_TIMEOUT_SECONDS:-60}"
PROBE_TIMEOUT_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_PROBE_TIMEOUT_SECONDS:-30}"
PROBE_WALL_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_PROBE_WALL_SECONDS:-45}"

if [[ -z "$AGY_BIN" ]]; then
    AGY_BIN="$(command -v agy || true)"
fi
if [[ -z "$AGY_BIN" ]]; then
    AGY_BIN="${HOME}/.local/bin/agy"
fi
if [[ -z "$PATCHER_PYTHON" ]]; then
    if [[ -x "$PATCHER_DIR/.venv/bin/python" ]]; then
        PATCHER_PYTHON="$PATCHER_DIR/.venv/bin/python"
    else
        PATCHER_PYTHON="$(command -v python3 || true)"
    fi
fi

if [[ ! -d "$PATCHER_DIR" ]]; then
    echo "[agy-recovery] patcher directory not found: $PATCHER_DIR" >&2
    exit 10
fi
if [[ ! -x "$AGY_BIN" ]]; then
    echo "[agy-recovery] AGY binary not executable: $AGY_BIN" >&2
    exit 11
fi
if [[ -z "$PATCHER_PYTHON" || ! -x "$PATCHER_PYTHON" ]]; then
    echo "[agy-recovery] Python interpreter unavailable" >&2
    exit 12
fi
if ! command -v timeout >/dev/null 2>&1; then
    echo "[agy-recovery] timeout command unavailable" >&2
    exit 13
fi

echo "[agy-recovery] patcher=$PATCHER_DIR"
echo "[agy-recovery] agy=$AGY_BIN"

cd "$PATCHER_DIR"

echo "[agy-recovery] applying CLI eligibility patch"
set +e
timeout --signal=TERM --kill-after=5s "${PATCH_TIMEOUT_SECONDS}s" \
    "$PATCHER_PYTHON" manager.py patch cli --path-cli "$AGY_BIN"
patch_rc=$?
set -e
if [[ $patch_rc -ne 0 ]]; then
    echo "[agy-recovery] patch command failed with exit code $patch_rc" >&2
    exit 14
fi

echo "[agy-recovery] verifying authoritative patch state"
set +e
patch_state="$(
    timeout --signal=TERM --kill-after=5s "${PATCH_TIMEOUT_SECONDS}s" \
        "$PATCHER_PYTHON" - "$AGY_BIN" <<'PY'
import sys
import manager

_path, state = manager.state("cli", {"cli": sys.argv[1]})
print(state)
raise SystemExit(0 if state == "patched" else 1)
PY
)"
state_rc=$?
set -e
printf '[agy-recovery] patch_state=%s\n' "$patch_state"
if [[ $state_rc -ne 0 || "$patch_state" != "patched" ]]; then
    echo "[agy-recovery] patch state verification failed" >&2
    exit 20
fi

probe_output="$(mktemp)"
trap 'rm -f "$probe_output"' EXIT

echo "[agy-recovery] probing live AGY eligibility"
set +e
printf 'Return OK only.\n' | timeout --signal=TERM --kill-after=5s "${PROBE_WALL_SECONDS}s" \
    "$AGY_BIN" \
    --output-format stream-json \
    --print-timeout "${PROBE_TIMEOUT_SECONDS}s" \
    --model "$MODEL" \
    --effort low \
    --mode plan \
    --sandbox \
    >"$probe_output" 2>&1
probe_rc=$?
set -e
cat "$probe_output"

if [[ $probe_rc -ne 0 ]]; then
    echo "[agy-recovery] live AGY probe failed with exit code $probe_rc" >&2
    exit 21
fi

set +e
"$PATCHER_PYTHON" - "$probe_output" <<'PY'
import json
from pathlib import Path
import sys

result = None
for raw_line in Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines():
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        continue
    if event.get("event") == "result":
        result = event.get("result")

if not isinstance(result, dict):
    print("[agy-recovery] live AGY probe has no final result event", file=sys.stderr)
    raise SystemExit(1)

error = str(result.get("error") or "")
if "eligibility check failed" in error.lower():
    print("[agy-recovery] live AGY probe still reports eligibility failure", file=sys.stderr)
    raise SystemExit(1)

if result.get("status") != "SUCCESS":
    print(
        f"[agy-recovery] final AGY result status is {result.get('status')!r}, not SUCCESS",
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
result_rc=$?
set -e
if [[ $result_rc -ne 0 ]]; then
    exit 22
fi

echo "[agy-recovery] AGY_RECOVERY_VERIFIED"
