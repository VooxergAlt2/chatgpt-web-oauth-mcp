#!/usr/bin/env bash
set -euo pipefail

PATCHER_DIR="${CHATGPT_MCP_AGY_PATCHER_DIR:-${HOME}/eligibility-antigravity-patcher}"
AGY_BIN="${CHATGPT_MCP_AGY_BIN:-}"
PATCHER_PYTHON="${CHATGPT_MCP_AGY_PATCHER_PYTHON:-}"
MODEL="${CHATGPT_MCP_AGY_RECOVERY_MODEL:-gemini-3.8-flash}"
PATCH_TIMEOUT_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_PATCH_TIMEOUT_SECONDS:-60}"
PROBE_TIMEOUT_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_PROBE_TIMEOUT_SECONDS:-30}"
PROBE_WALL_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_PROBE_WALL_SECONDS:-45}"
LOCK_TIMEOUT_SECONDS="${CHATGPT_MCP_AGY_RECOVERY_LOCK_TIMEOUT_SECONDS:-2}"
RECOVERY_STATE_DIR="${CHATGPT_MCP_AGY_RECOVERY_STATE_DIR:-${CHATGPT_MCP_STATE_DIR:-${HOME}/.chatgpt-web-oauth-mcp}/agy-recovery}"
LAST_KNOWN_GOOD_DIR="${RECOVERY_STATE_DIR}/last-known-good"
LAST_KNOWN_GOOD_BIN="${LAST_KNOWN_GOOD_DIR}/agy"
LAST_KNOWN_GOOD_META="${LAST_KNOWN_GOOD_DIR}/metadata.json"

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
if [[ -z "$PATCHER_PYTHON" || ! -x "$PATCHER_PYTHON" ]]; then
    echo "[agy-recovery] Python interpreter unavailable" >&2
    exit 12
fi
if ! command -v timeout >/dev/null 2>&1; then
    echo "[agy-recovery] timeout command unavailable" >&2
    exit 13
fi
if ! command -v flock >/dev/null 2>&1; then
    echo "[agy-recovery] flock command unavailable" >&2
    exit 15
fi

RECOVERY_STATE_PARENT="$(dirname "$RECOVERY_STATE_DIR")"
if ! mkdir -p "$RECOVERY_STATE_PARENT"; then
    echo "[agy-recovery] could not create recovery state parent: $RECOVERY_STATE_PARENT" >&2
    exit 16
fi
RECOVERY_LOCK_PATH="${RECOVERY_STATE_DIR}.lock"
exec {RECOVERY_LOCK_FD}>>"$RECOVERY_LOCK_PATH"
chmod 600 "$RECOVERY_LOCK_PATH"
if ! flock -w "$LOCK_TIMEOUT_SECONDS" "$RECOVERY_LOCK_FD"; then
    echo "[agy-recovery] another recovery invocation holds the state lock" >&2
    exit 24
fi
if ! mkdir -p "$RECOVERY_STATE_DIR"; then
    echo "[agy-recovery] could not create recovery state directory: $RECOVERY_STATE_DIR" >&2
    exit 16
fi
chmod 700 "$RECOVERY_STATE_DIR"

echo "[agy-recovery] patcher=$PATCHER_DIR"
echo "[agy-recovery] agy=$AGY_BIN"
echo "[agy-recovery] rollback_cache=$LAST_KNOWN_GOOD_BIN"

cd "$PATCHER_DIR"

read_patch_state() {
    timeout --signal=TERM --kill-after=5s "${PATCH_TIMEOUT_SECONDS}s" \
        "$PATCHER_PYTHON" - "$AGY_BIN" <<'PY'
import sys
import manager

path = sys.argv[1]
try:
    ranges, arch = manager.executable_info(path)
    with manager.mapped(path) as data:
        state, _offset, _gate = manager.CLI_GATE.resolve(data, ranges, arch)
except manager.SignatureNotFound as exc:
    print(f"signature_not_found:{exc}", file=sys.stderr)
    raise SystemExit(42)
except manager.SignatureAmbiguous as exc:
    print(f"signature_ambiguous:{exc}", file=sys.stderr)
    raise SystemExit(43)
except (LookupError, OSError, ValueError) as exc:
    print(f"patch_state_error:{type(exc).__name__}:{exc}", file=sys.stderr)
    raise SystemExit(44)
print(state)
PY
}

restore_last_known_good() {
    "$PATCHER_PYTHON" - "$AGY_BIN" "$LAST_KNOWN_GOOD_BIN" "$LAST_KNOWN_GOOD_META" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

target = Path(sys.argv[1])
cached = Path(sys.argv[2])
metadata_path = Path(sys.argv[3])

if not cached.is_file() or not metadata_path.is_file():
    print("last-known-good cache is missing", file=sys.stderr)
    raise SystemExit(1)

try:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    print(f"invalid last-known-good metadata: {exc}", file=sys.stderr)
    raise SystemExit(1)

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

cached_sha = sha256(cached)
expected_sha = str(metadata.get("sha256") or "")
if not expected_sha or cached_sha != expected_sha:
    print("last-known-good checksum mismatch", file=sys.stderr)
    raise SystemExit(1)

current_sha = sha256(target) if target.is_file() else None
if current_sha == cached_sha:
    print("last-known-good is identical to current binary", file=sys.stderr)
    raise SystemExit(1)

def version(path: Path) -> str:
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    text = completed.stdout if completed.stdout.strip() else completed.stderr
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return "unknown"

from_version = version(target) if target.exists() else "missing"
to_version = str(metadata.get("version") or version(cached))

target.parent.mkdir(parents=True, exist_ok=True)
tmp = target.with_name(f".{target.name}.rollback-{os.getpid()}")
try:
    shutil.copy2(cached, tmp)
    os.chmod(tmp, cached.stat().st_mode & 0o777)
    os.replace(tmp, target)
finally:
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass

if sha256(target) != cached_sha:
    print("rollback copy verification failed", file=sys.stderr)
    raise SystemExit(1)

print(f"{from_version}\t{to_version}\t{cached_sha}")
PY
}

cache_last_known_good() {
    local expected_sha="$1"
    "$PATCHER_PYTHON" - "$AGY_BIN" "$LAST_KNOWN_GOOD_BIN" "$LAST_KNOWN_GOOD_META" "$expected_sha" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

source = Path(sys.argv[1])
cached = Path(sys.argv[2])
metadata_path = Path(sys.argv[3])
expected_source_sha = sys.argv[4]

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

source_sha = sha256(source)
if source_sha != expected_source_sha:
    print("AGY binary changed after live probe; refusing to cache unverified bytes", file=sys.stderr)
    raise SystemExit(1)

completed = subprocess.run(
    [str(source), "--version"],
    text=True,
    capture_output=True,
    timeout=10,
    check=False,
)
if completed.returncode != 0:
    print("could not read AGY version for rollback cache", file=sys.stderr)
    raise SystemExit(1)
version = next(
    (
        line.strip()
        for line in (completed.stdout if completed.stdout.strip() else completed.stderr).splitlines()
        if line.strip()
    ),
    "",
)
if not version:
    print("empty AGY version for rollback cache", file=sys.stderr)
    raise SystemExit(1)

cached.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(cached.parent, 0o700)
tmp_binary = cached.with_name(f".{cached.name}.tmp-{os.getpid()}")
tmp_meta = metadata_path.with_name(f".{metadata_path.name}.tmp-{os.getpid()}")
try:
    shutil.copy2(source, tmp_binary)
    os.chmod(tmp_binary, source.stat().st_mode & 0o777)
    digest = sha256(tmp_binary)
    if digest != expected_source_sha:
        print("AGY binary changed while copying last-known-good cache", file=sys.stderr)
        raise SystemExit(1)
    metadata = {
        "version": version,
        "sha256": digest,
        "saved_at_epoch": time.time(),
        "source_path": str(source),
    }
    tmp_meta.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp_meta, 0o600)
    os.replace(tmp_binary, cached)
    os.replace(tmp_meta, metadata_path)
finally:
    for tmp in (tmp_binary, tmp_meta):
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

print(f"{version}\t{digest}")
PY
}

sha256_file() {
    "$PATCHER_PYTHON" - "$1" <<'PY'
import hashlib
from pathlib import Path
import sys

path = Path(sys.argv[1])
digest = hashlib.sha256()
with path.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
}

rollback_used=0

try_rollback() {
    local reason="$1"
    if [[ $rollback_used -ne 0 ]]; then
        echo "[agy-recovery] rollback already used; refusing retry loop" >&2
        return 1
    fi
    echo "[agy-recovery] attempting last-known-good rollback reason=$reason"
    local rollback_info rollback_rc
    set +e
    rollback_info="$(restore_last_known_good)"
    rollback_rc=$?
    set -e
    if [[ $rollback_rc -ne 0 ]]; then
        echo "[agy-recovery] last-known-good rollback unavailable or failed" >&2
        return 1
    fi
    rollback_used=1
    local from_version to_version rollback_sha
    IFS=$'\t' read -r from_version to_version rollback_sha <<<"$rollback_info"
    printf '[agy-recovery] AGY_ROLLBACK_APPLIED from=%s to=%s sha256=%s\n' \
        "$from_version" "$to_version" "$rollback_sha"
    return 0
}

if [[ ! -x "$AGY_BIN" ]]; then
    echo "[agy-recovery] AGY binary missing or not executable; trying last-known-good rollback" >&2
    if ! try_rollback "binary_missing"; then
        echo "[agy-recovery] AGY binary not executable: $AGY_BIN" >&2
        exit 11
    fi
fi

while true; do
    echo "[agy-recovery] reading authoritative patch state"
    set +e
    patch_state_before="$(read_patch_state)"
    state_rc=$?
    set -e
    if [[ $state_rc -eq 42 ]]; then
        if try_rollback "unsupported_signature"; then
            continue
        fi
        echo "[agy-recovery] unsupported AGY version and no usable previous version" >&2
        exit 23
    fi
    if [[ $state_rc -ne 0 ]]; then
        echo "[agy-recovery] could not read patch state (exit code $state_rc)" >&2
        exit 20
    fi
    printf '[agy-recovery] patch_state_before=%s\n' "$patch_state_before"

    case "$patch_state_before" in
        patched)
            echo "[agy-recovery] CLI already patched; skipping binary write"
            ;;
        unpatched)
            echo "[agy-recovery] applying CLI eligibility patch"
            set +e
            timeout --signal=TERM --kill-after=5s "${PATCH_TIMEOUT_SECONDS}s" \
                "$PATCHER_PYTHON" manager.py patch cli --path-cli "$AGY_BIN"
            patch_rc=$?
            set -e
            if [[ $patch_rc -ne 0 ]]; then
                set +e
                read_patch_state >/dev/null
                post_patch_state_rc=$?
                set -e
                if [[ $post_patch_state_rc -eq 42 ]] && try_rollback "signature_changed_during_patch"; then
                    continue
                fi
                echo "[agy-recovery] patch command failed with exit code $patch_rc" >&2
                exit 14
            fi
            ;;
        *)
            echo "[agy-recovery] unsupported patch state: $patch_state_before" >&2
            exit 20
            ;;
    esac

    echo "[agy-recovery] verifying authoritative patch state"
    set +e
    patch_state="$(read_patch_state)"
    state_rc=$?
    set -e
    printf '[agy-recovery] patch_state=%s\n' "$patch_state"
    if [[ $state_rc -eq 42 ]]; then
        if try_rollback "signature_changed_before_verification"; then
            continue
        fi
        echo "[agy-recovery] unsupported AGY version after patch attempt and rollback failed" >&2
        exit 23
    fi
    if [[ $state_rc -ne 0 || "$patch_state" != "patched" ]]; then
        echo "[agy-recovery] patch state verification failed" >&2
        exit 20
    fi
    break
done

probe_output="$(mktemp)"
trap 'rm -f "$probe_output"' EXIT

set +e
probe_binary_sha="$(sha256_file "$AGY_BIN")"
probe_binary_sha_rc=$?
set -e
if [[ $probe_binary_sha_rc -ne 0 || -z "$probe_binary_sha" ]]; then
    echo "[agy-recovery] could not fingerprint AGY before live probe" >&2
    exit 25
fi
printf '[agy-recovery] probe_binary_sha256=%s\n' "$probe_binary_sha"

echo "[agy-recovery] probing live AGY eligibility"
set +e
printf 'Do not use tools or files. Return exactly OK.\n' | timeout --signal=TERM --kill-after=5s "${PROBE_WALL_SECONDS}s" \
    "$AGY_BIN" \
    --output-format stream-json \
    --print-timeout "${PROBE_TIMEOUT_SECONDS}s" \
    --model "$MODEL" \
    --effort low \
    --disable-slash-commands \
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

set +e
cache_info="$(cache_last_known_good "$probe_binary_sha")"
cache_rc=$?
set -e
if [[ $cache_rc -eq 0 ]]; then
    IFS=$'\t' read -r cached_version cached_sha <<<"$cache_info"
    printf '[agy-recovery] AGY_LAST_KNOWN_GOOD_SAVED version=%s sha256=%s\n' \
        "$cached_version" "$cached_sha"
else
    echo "[agy-recovery] could not refresh verified last-known-good cache" >&2
    exit 26
fi

set +e
final_binary_sha="$(sha256_file "$AGY_BIN")"
final_binary_sha_rc=$?
set -e
if [[ $final_binary_sha_rc -ne 0 || "$final_binary_sha" != "$probe_binary_sha" ]]; then
    echo "[agy-recovery] AGY binary changed after verified probe; refusing recovery success" >&2
    exit 27
fi

echo "[agy-recovery] AGY_RECOVERY_VERIFIED"
