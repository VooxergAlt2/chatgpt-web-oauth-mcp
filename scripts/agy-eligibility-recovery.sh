#!/usr/bin/env bash
# Opss MCP eligibility recovery hook. A zero exit code means that a real AGY
# completion was verified, not merely that the binary patcher ran.
set -euo pipefail

UNLOCKER_BIN="${CHATGPT_MCP_AGY_UNLOCKER_BIN:-${HOME}/src/antigravity-cli-unlocker/venv/bin/antigravity-unlock}"
UNLOCKER_REPO="${CHATGPT_MCP_AGY_UNLOCKER_REPO:-${HOME}/src/antigravity-cli-unlocker/antigravity-cli-unlocker}"
UNLOCKER_PIP="${CHATGPT_MCP_AGY_UNLOCKER_PIP:-${HOME}/src/antigravity-cli-unlocker/venv/bin/pip}"
AGY_BIN="${CHATGPT_MCP_AGY_BIN:-$(command -v agy || true)}"
MODEL="${CHATGPT_MCP_AGY_RECOVERY_MODEL:-gemini-3.8-flash-low}"
STATE_DIR="${CHATGPT_MCP_AGY_RECOVERY_STATE_DIR:-${CHATGPT_MCP_STATE_DIR:-${HOME}/.local/state/chatgpt-web-oauth-mcp}/agy-recovery}"
UPDATE_ON_RUN="${CHATGPT_MCP_AGY_RECOVERY_UPDATE_ON_RUN:-0}"
PATCH_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_PATCH_TIMEOUT_SECONDS:-60}"
PROBE_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_PROBE_TIMEOUT_SECONDS:-30}"
PROBE_WALL_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_PROBE_WALL_SECONDS:-45}"
LOCK_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_LOCK_TIMEOUT_SECONDS:-2}"
GIT_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_GIT_TIMEOUT_SECONDS:-20}"
PIP_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_PIP_TIMEOUT_SECONDS:-60}"
PYTHON_BIN="${CHATGPT_MCP_AGY_RECOVERY_PYTHON:-$(command -v python3 || true)}"

fail() {
    printf '[agy-recovery] FAIL: %s\n' "$*" >&2
    exit 1
}

for name in PATCH_SECONDS PROBE_SECONDS PROBE_WALL_SECONDS LOCK_SECONDS GIT_SECONDS PIP_SECONDS; do
    value="${!name}"
    [[ "$value" =~ ^[0-9]+$ && "$value" -gt 0 ]] || fail "invalid positive timeout: $name=$value"
done
[[ "$UPDATE_ON_RUN" == 0 || "$UPDATE_ON_RUN" == 1 ]] || fail "UPDATE_ON_RUN must be 0 or 1"
command -v timeout >/dev/null 2>&1 || fail "timeout is not installed"
command -v flock >/dev/null 2>&1 || fail "flock is not installed"
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is not installed"
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || fail "python3 is unavailable"
[[ -n "$AGY_BIN" && -x "$AGY_BIN" ]] || fail "agy binary is missing or not executable"
[[ -x "$UNLOCKER_BIN" ]] || fail "antigravity-unlock is missing or not executable: $UNLOCKER_BIN"

# The unlocker discovers its own primary agy. Refuse to probe a different one.
path_agy="$(command -v agy || true)"
[[ -n "$path_agy" && "$path_agy" -ef "$AGY_BIN" ]] || fail "configured AGY_BIN differs from agy on PATH"

mkdir -p "$STATE_DIR" || fail "could not create recovery state directory"
chmod 700 "$STATE_DIR"
lock_path="$STATE_DIR/watchdog.lock"
exec {lock_fd}>"$lock_path"
chmod 600 "$lock_path"
flock -w "$LOCK_SECONDS" "$lock_fd" || fail "another eligibility recovery holds the lock"

scratch="$(mktemp -d "$STATE_DIR/run.XXXXXXXX")"
chmod 700 "$scratch"
trap 'rm -rf -- "$scratch"' EXIT

# Fetching software during a provider outage makes recovery depend on GitHub
# and creates a moving target. Updates are opt-in and fail closed.
if [[ "$UPDATE_ON_RUN" == 1 ]]; then
    [[ -d "$UNLOCKER_REPO/.git" && -x "$UNLOCKER_PIP" ]] || fail "unlocker source or pip is unavailable"
    before="$(git -C "$UNLOCKER_REPO" rev-parse HEAD)" || fail "could not read unlocker Git revision"
    echo "[agy-recovery] explicitly requested unlocker update"
    if ! timeout --signal=TERM --kill-after=3s "${GIT_SECONDS}s" \
        git -C "$UNLOCKER_REPO" pull --ff-only origin main >"$scratch/git.log" 2>&1; then
        cat "$scratch/git.log" >&2
        fail "unlocker Git update failed"
    fi
    after="$(git -C "$UNLOCKER_REPO" rev-parse HEAD)" || fail "could not read updated Git revision"
    if [[ "$before" != "$after" ]]; then
        if ! timeout --signal=TERM --kill-after=3s "${PIP_SECONDS}s" \
            "$UNLOCKER_PIP" install --no-input --disable-pip-version-check --quiet "$UNLOCKER_REPO" \
            >"$scratch/pip.log" 2>&1; then
            cat "$scratch/pip.log" >&2
            fail "unlocker installation failed"
        fi
    fi
fi

# Never enforce a pinned strategy here: patched current version stays current.
echo "[agy-recovery] patching current AGY with unlocker (in-place)"
if ! timeout --signal=TERM --kill-after=3s "${PATCH_SECONDS}s" \
    "$UNLOCKER_BIN" patch >"$scratch/patch.log" 2>&1; then
    cat "$scratch/patch.log" >&2
    fail "unlocker patch failed"
fi
cat "$scratch/patch.log"

if ! timeout --signal=TERM --kill-after=3s "${PATCH_SECONDS}s" \
    "$UNLOCKER_BIN" patch --dry-run >"$scratch/state.log" 2>&1; then
    cat "$scratch/state.log" >&2
    fail "unable to verify AGY patch"
fi
if ! grep -Eq '^State:[[:space:]]+patched[[:space:]]+\(' "$scratch/state.log"; then
    cat "$scratch/state.log" >&2
    fail "unlocker did not confirm a patched binary"
fi
echo "[agy-recovery] AGY_PATCH_VERIFIED"

before_probe_sha="$(sha256sum "$AGY_BIN" | cut -d' ' -f1)" || fail "cannot fingerprint AGY"
echo "[agy-recovery] probing live eligibility model=$MODEL"
if ! printf 'Do not use tools or files. Respond exactly with OK.\n' | \
    timeout --signal=TERM --kill-after=3s "${PROBE_WALL_SECONDS}s" "$AGY_BIN" \
        --output-format stream-json --print-timeout "${PROBE_SECONDS}s" \
        --model "$MODEL" --effort low --disable-slash-commands \
        >"$scratch/probe.jsonl" 2>&1; then
    tail -c 2000 "$scratch/probe.jsonl" >&2 || true
    fail "live AGY eligibility probe failed"
fi
if ! "$PYTHON_BIN" - "$scratch/probe.jsonl" <<'PY'
import json
from pathlib import Path
import sys

result = None
for line in Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").splitlines():
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(event, dict) and event.get("event") == "result":
        result = event.get("result")
if not isinstance(result, dict):
    print("[agy-recovery] no final AGY result event", file=sys.stderr)
    raise SystemExit(1)
if result.get("status") != "SUCCESS" or result.get("error"):
    print("[agy-recovery] final AGY result is not a clean SUCCESS", file=sys.stderr)
    raise SystemExit(1)
if str(result.get("response") or "").strip() != "OK":
    print("[agy-recovery] AGY result has no expected probe response", file=sys.stderr)
    raise SystemExit(1)
PY
then
    fail "live AGY result verification failed"
fi

after_probe_sha="$(sha256sum "$AGY_BIN" | cut -d' ' -f1)" || fail "cannot fingerprint AGY after probe"
[[ "$before_probe_sha" == "$after_probe_sha" ]] || fail "AGY binary changed during probe; refusing recovery success"
printf '[agy-recovery] AGY_RECOVERY_VERIFIED sha256=%s\n' "$after_probe_sha"
