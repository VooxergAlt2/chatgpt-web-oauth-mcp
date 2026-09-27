from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import threading
import time
from typing import Any, Callable

from .process_env import sanitized_child_env
from .state_io import atomic_write_bytes, interprocess_file_lock


STATE_SCHEMA_VERSION = 1
PRIME_PROMPT = 'Reply exactly "OK". Do not use tools, inspect files, or perform any other work.'


@dataclass(frozen=True)
class PrimeTarget:
    key: str
    provider: str
    model: str | None
    group_contains: tuple[str, ...] = ()
    window_id: str | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class QuotaWindowManager:
    """Keep selected five-hour CLI quota windows active with tiny verified turns."""

    def __init__(
        self,
        *,
        usage_collector: Any,
        state_path: Path,
        antigravity_command: str | None,
        claude_command: str | None,
        codex_command: str | None,
        enabled: bool = True,
        check_interval_seconds: float = 30.0,
        post_reset_delay_seconds: float = 120.0,
        verification_delay_seconds: float = 5.0,
        verification_probe_delay_seconds: float = 3.0,
        retry_seconds: float = 300.0,
        command_timeout_seconds: float = 90.0,
        max_attempts_per_cycle: int = 3,
        antigravity_gemini_model: str = "gemini-3.8-flash-low",
        antigravity_third_party_model: str = "gpt-oss-120b-medium",
        claude_model: str = "haiku",
        command_runner: Callable[[PrimeTarget], dict[str, Any]] | None = None,
    ) -> None:
        self.usage_collector = usage_collector
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self.antigravity_command = antigravity_command
        self.claude_command = claude_command
        self.codex_command = codex_command
        self.enabled = bool(enabled)
        self.check_interval_seconds = max(5.0, float(check_interval_seconds))
        self.post_reset_delay_seconds = max(0.0, float(post_reset_delay_seconds))
        self.verification_delay_seconds = max(0.0, float(verification_delay_seconds))
        self.verification_probe_delay_seconds = max(
            0.0, float(verification_probe_delay_seconds)
        )
        self.retry_seconds = max(30.0, float(retry_seconds))
        self.command_timeout_seconds = max(5.0, float(command_timeout_seconds))
        self.force_refresh_min_interval_seconds = max(30.0, self.check_interval_seconds)
        self.max_attempts_per_cycle = max(1, int(max_attempts_per_cycle))
        self.workspace_dir = self.state_path.parent / "quota-prime-workspace"
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._targets = {
            target.key: target
            for target in (
                PrimeTarget(
                    key="antigravity_gemini_5h",
                    provider="antigravity",
                    model=antigravity_gemini_model,
                    group_contains=("gemini",),
                ),
                PrimeTarget(
                    key="antigravity_claude_gpt_5h",
                    provider="antigravity",
                    model=antigravity_third_party_model,
                    group_contains=("claude", "gpt"),
                ),
                PrimeTarget(
                    key="claude_5h",
                    provider="claude",
                    model=claude_model,
                    window_id="five_hour",
                ),
                PrimeTarget(
                    key="codex_5h",
                    provider="codex",
                    model=None,
                    window_id="primary",
                ),
            )
        }
        self._command_runner = command_runner or self._run_prime_command
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = self._load_state()

    def start(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="quota-window-manager",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            join_timeout = (
                self.command_timeout_seconds + self.verification_delay_seconds + 2.0
                if timeout is None
                else max(0.0, timeout)
            )
            thread.join(timeout=join_timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = deepcopy(self._state)
        state["enabled"] = self.enabled
        state["check_interval_seconds"] = self.check_interval_seconds
        state["post_reset_delay_seconds"] = self.post_reset_delay_seconds
        state["verification_delay_seconds"] = self.verification_delay_seconds
        state["verification_probe_delay_seconds"] = self.verification_probe_delay_seconds
        state["max_attempts_per_cycle"] = self.max_attempts_per_cycle
        return state

    def run_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        reference = (now or _utc_now()).astimezone(timezone.utc)
        if not self.enabled:
            with self._lock:
                self._state["status"] = "disabled"
                self._state["updated_at"] = _iso(reference)
                self._save_state_locked()
            return self.snapshot()

        usage = self.usage_collector.snapshot()
        if self._should_force_refresh(reference):
            try:
                usage = self.usage_collector.refresh_now()
            except Exception:
                pass
            with self._lock:
                self._state["last_forced_refresh_at"] = _iso(reference)
                self._save_state_locked()
        providers = usage.get("providers") if isinstance(usage, dict) else None
        if not isinstance(providers, dict) or usage.get("status") == "warming":
            with self._lock:
                self._state["status"] = "warming"
                self._state["updated_at"] = _iso(reference)
                self._save_state_locked()
            return self.snapshot()

        for target in self._targets.values():
            should_prime = self._observe_target(target, providers, reference)
            if should_prime:
                self._prime_target(target, reference)

        with self._lock:
            bucket_states = {
                str(item.get("status") or "")
                for item in self._state.get("buckets", {}).values()
                if isinstance(item, dict)
            }
            if "priming" in bucket_states:
                overall = "priming"
            elif (
                "error" in bucket_states
                or "verification_failed" in bucket_states
                or "retry_exhausted" in bucket_states
            ):
                overall = "degraded"
            elif bucket_states and bucket_states <= {"unavailable", "weekly_exhausted"}:
                overall = "blocked"
            else:
                overall = "ok"
            self._state["status"] = overall
            self._state["updated_at"] = _iso(reference)
            self._save_state_locked()
        return self.snapshot()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = self.run_once()
            except Exception:
                with self._lock:
                    self._state["status"] = "degraded"
                    self._state["last_error"] = "manager_cycle_failed"
                    self._state["updated_at"] = _iso(_utc_now())
                    self._save_state_locked()
                snapshot = {"status": "degraded"}
            delay = 2.0 if snapshot.get("status") == "warming" else self.check_interval_seconds
            self._stop.wait(delay)

    def _should_force_refresh(self, now: datetime) -> bool:
        with self._lock:
            last_refresh = _parse_iso(self._state.get("last_forced_refresh_at"))
            if (
                last_refresh is not None
                and (now - last_refresh).total_seconds() < self.force_refresh_min_interval_seconds
            ):
                return False
            buckets = self._state.get("buckets")
            if not isinstance(buckets, dict):
                return False
            for bucket in buckets.values():
                if not isinstance(bucket, dict):
                    continue
                armed_reset = _parse_iso(bucket.get("armed_reset_at"))
                if armed_reset is None:
                    continue
                due_at = armed_reset + timedelta(seconds=self.post_reset_delay_seconds)
                if now >= due_at:
                    return True
        return False

    def _observe_target(
        self,
        target: PrimeTarget,
        providers: dict[str, Any],
        now: datetime,
    ) -> bool:
        provider = providers.get(target.provider)
        provider_status = provider.get("status") if isinstance(provider, dict) else None
        window = self._find_five_hour_window(target, provider)
        weekly = self._find_weekly_window(target, provider)

        with self._lock:
            bucket = self._bucket_locked(target)
            previous_reset_at = _parse_iso(bucket.get("observed_resets_at"))
            previous_verified_at = _parse_iso(bucket.get("last_verified_at"))
            previous_prime_succeeded = bool(bucket.get("last_prime_success"))
            bucket["provider_status"] = provider_status
            bucket["observed_at"] = _iso(now)

            if provider_status != "ok" or window is None:
                bucket["status"] = "unavailable"
                bucket["last_error"] = (
                    provider.get("error")
                    if isinstance(provider, dict) and provider.get("error")
                    else "five_hour_window_unavailable"
                )
                self._save_state_locked()
                return False

            used = _number(window.get("used_percent"))
            remaining = _number(window.get("remaining_percent"))
            reset_at = _parse_iso(window.get("resets_at"))
            weekly_remaining = _number(weekly.get("remaining_percent")) if weekly else None
            bucket["observed_used_percent"] = used
            bucket["observed_remaining_percent"] = remaining
            bucket["observed_resets_at"] = window.get("resets_at")
            bucket["weekly_remaining_percent"] = weekly_remaining
            bucket["weekly_resets_at"] = weekly.get("resets_at") if weekly else None
            bucket["last_error"] = None

            if weekly_remaining is not None and weekly_remaining <= 0:
                bucket["status"] = "weekly_exhausted"
                bucket["next_prime_at"] = None
                self._save_state_locked()
                return False

            codex_reset_recovered = (
                target.provider == "codex"
                and reset_at is not None
                and previous_reset_at == reset_at
                and previous_prime_succeeded
                and previous_verified_at is not None
                and (now - previous_verified_at).total_seconds()
                >= self.verification_probe_delay_seconds
            )
            if (used is not None and used > 0) or codex_reset_recovered:
                if reset_at is not None:
                    bucket["armed_reset_at"] = _iso(reset_at)
                    bucket["next_prime_at"] = _iso(
                        reset_at + timedelta(seconds=self.post_reset_delay_seconds)
                    )
                bucket["status"] = "active"
                bucket["activation_evidence"] = (
                    "usage_nonzero" if used is not None and used > 0 else "stable_reset_at_recovered"
                )
                bucket["last_verified_at"] = _iso(now)
                bucket["next_retry_at"] = None
                bucket["attempts_since_activation"] = 0
                bucket["last_error"] = None
                self._save_state_locked()
                return False

            armed_reset = _parse_iso(bucket.get("armed_reset_at"))
            retry_at = _parse_iso(bucket.get("next_retry_at"))
            if retry_at is not None and now < retry_at:
                bucket["status"] = "retry_wait"
                bucket["next_prime_at"] = _iso(retry_at)
                self._save_state_locked()
                return False

            attempts = int(bucket.get("attempts_since_activation") or 0)
            if attempts >= self.max_attempts_per_cycle:
                bucket["status"] = "retry_exhausted"
                bucket["next_prime_at"] = None
                bucket["last_error"] = "prime_attempt_limit_reached"
                self._save_state_locked()
                return False

            if armed_reset is None:
                bucket["status"] = "due"
                bucket["next_prime_at"] = _iso(now)
                self._save_state_locked()
                return True

            due_at = armed_reset + timedelta(seconds=self.post_reset_delay_seconds)
            bucket["next_prime_at"] = _iso(due_at)
            if now < due_at:
                bucket["status"] = "waiting_reset"
                self._save_state_locked()
                return False

            bucket["status"] = "due"
            self._save_state_locked()
            return True

    def _prime_target(self, target: PrimeTarget, now: datetime) -> None:
        with self._lock:
            bucket = self._bucket_locked(target)
            bucket["status"] = "priming"
            bucket["prime_started_at"] = _iso(now)
            bucket["last_prime_at"] = _iso(now)
            bucket["last_prime_model"] = target.model
            bucket["attempts_since_activation"] = int(
                bucket.get("attempts_since_activation") or 0
            ) + 1
            bucket["last_error"] = None
            bucket["next_retry_at"] = _iso(now + timedelta(seconds=self.retry_seconds))
            self._save_state_locked()

        try:
            result = self._command_runner(target)
        except Exception:
            result = {"success": False, "error": "prime_command_failed"}

        if not result.get("success"):
            with self._lock:
                bucket = self._bucket_locked(target)
                bucket["status"] = "error"
                bucket["last_prime_success"] = False
                bucket["last_error"] = str(result.get("error") or "prime_command_failed")
                bucket["last_prime_exit_code"] = result.get("exit_code")
                self._save_state_locked()
            return

        with self._lock:
            bucket = self._bucket_locked(target)
            bucket["last_prime_success"] = True
            bucket["last_prime_exit_code"] = result.get("exit_code", 0)
            bucket["status"] = "verifying"
            bucket["bootstrap_completed"] = True
            self._save_state_locked()

        if self.verification_delay_seconds:
            self._stop.wait(self.verification_delay_seconds)
        if self._stop.is_set():
            return

        try:
            refreshed = self.usage_collector.refresh_now()
        except Exception:
            refreshed = {}
        providers = refreshed.get("providers") if isinstance(refreshed, dict) else None
        provider = providers.get(target.provider) if isinstance(providers, dict) else None
        window = self._find_five_hour_window(target, provider)
        used = _number(window.get("used_percent")) if window else None
        reset_at = _parse_iso(window.get("resets_at")) if window else None
        activation_evidence = "usage_nonzero" if used is not None and used > 0 else None

        if (
            activation_evidence is None
            and target.provider == "codex"
            and reset_at is not None
        ):
            if self.verification_probe_delay_seconds:
                self._stop.wait(self.verification_probe_delay_seconds)
            if self._stop.is_set():
                return
            try:
                second_refresh = self.usage_collector.refresh_now()
            except Exception:
                second_refresh = {}
            second_providers = (
                second_refresh.get("providers")
                if isinstance(second_refresh, dict)
                else None
            )
            second_provider = (
                second_providers.get(target.provider)
                if isinstance(second_providers, dict)
                else None
            )
            second_window = self._find_five_hour_window(target, second_provider)
            second_used = _number(second_window.get("used_percent")) if second_window else None
            second_reset_at = (
                _parse_iso(second_window.get("resets_at")) if second_window else None
            )
            if second_used is not None and second_used > 0:
                used = second_used
                window = second_window
                reset_at = second_reset_at
                activation_evidence = "usage_nonzero"
            elif second_reset_at is not None and second_reset_at == reset_at:
                window = second_window
                reset_at = second_reset_at
                activation_evidence = "stable_reset_at"

        verified_at = _utc_now()
        with self._lock:
            bucket = self._bucket_locked(target)
            bucket["last_verified_at"] = _iso(verified_at)
            if activation_evidence is not None and window is not None:
                bucket["status"] = "active"
                bucket["activation_evidence"] = activation_evidence
                bucket["observed_used_percent"] = _number(window.get("used_percent"))
                bucket["observed_remaining_percent"] = _number(window.get("remaining_percent"))
                bucket["observed_resets_at"] = window.get("resets_at")
                if reset_at is not None:
                    bucket["armed_reset_at"] = _iso(reset_at)
                    bucket["next_prime_at"] = _iso(
                        reset_at + timedelta(seconds=self.post_reset_delay_seconds)
                    )
                bucket["next_retry_at"] = None
                bucket["attempts_since_activation"] = 0
                bucket["last_error"] = None
            else:
                bucket["status"] = "verification_failed"
                bucket["activation_evidence"] = None
                bucket["last_error"] = "quota_window_not_activated"
            self._save_state_locked()

    def _find_five_hour_window(
        self,
        target: PrimeTarget,
        provider: object,
    ) -> dict[str, Any] | None:
        if not isinstance(provider, dict):
            return None
        windows = provider.get("windows")
        if not isinstance(windows, list):
            return None
        for item in windows:
            if not isinstance(item, dict):
                continue
            if target.provider == "antigravity":
                if item.get("window") != "5h":
                    continue
                group = str(item.get("group") or "").lower()
                if any(token in group for token in target.group_contains):
                    return item
            elif target.provider == "claude" and item.get("id") == target.window_id:
                return item
            elif target.provider == "codex" and (
                item.get("id") == target.window_id or item.get("duration_minutes") == 300
            ):
                return item
        return None

    def _find_weekly_window(
        self,
        target: PrimeTarget,
        provider: object,
    ) -> dict[str, Any] | None:
        if not isinstance(provider, dict):
            return None
        windows = provider.get("windows")
        if not isinstance(windows, list):
            return None
        for item in windows:
            if not isinstance(item, dict):
                continue
            if target.provider == "antigravity":
                if item.get("window") != "weekly":
                    continue
                group = str(item.get("group") or "").lower()
                if any(token in group for token in target.group_contains):
                    return item
            elif target.provider == "claude" and item.get("id") == "seven_day":
                return item
            elif target.provider == "codex" and item.get("duration_minutes") == 10080:
                return item
        return None

    def _bucket_locked(self, target: PrimeTarget) -> dict[str, Any]:
        buckets = self._state.setdefault("buckets", {})
        bucket = buckets.setdefault(
            target.key,
            {
                "provider": target.provider,
                "model": target.model,
                "status": "unknown",
                "bootstrap_completed": False,
                "attempts_since_activation": 0,
            },
        )
        bucket["provider"] = target.provider
        bucket["model"] = target.model
        return bucket

    def _run_prime_command(self, target: PrimeTarget) -> dict[str, Any]:
        argv = self._prime_argv(target)
        if not argv:
            return {"success": False, "error": "command_not_configured"}
        try:
            process = subprocess.Popen(
                argv,
                cwd=self.workspace_dir,
                env=sanitized_child_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=os.name == "posix",
            )
        except (OSError, ValueError):
            return {"success": False, "error": "command_start_failed"}

        deadline = time.monotonic() + self.command_timeout_seconds
        while process.poll() is None:
            if self._stop.wait(0.2):
                self._terminate_prime_process(process)
                return {"success": False, "error": "manager_stopping"}
            if time.monotonic() >= deadline:
                self._terminate_prime_process(process)
                return {"success": False, "error": "timeout"}
        try:
            process.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._terminate_prime_process(process)
        return {
            "success": process.returncode == 0,
            "exit_code": process.returncode,
            "error": None if process.returncode == 0 else f"exit_{process.returncode}",
        }

    @staticmethod
    def _terminate_prime_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover - Windows-specific fallback.
                process.terminate()
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - Windows-specific fallback.
                    process.kill()
            except OSError:
                pass

    def _prime_argv(self, target: PrimeTarget) -> list[str]:
        if target.provider == "antigravity":
            command = shlex.split(self.antigravity_command or "")
            if not command:
                return []
            return [
                *command,
                "--model",
                str(target.model),
                "--mode",
                "plan",
                "--disable-slash-commands",
                "-p",
                PRIME_PROMPT,
                "--output-format",
                "json",
            ]
        if target.provider == "claude":
            command = shlex.split(self.claude_command or "")
            if not command:
                return []
            return [
                *command,
                "--safe-mode",
                "--restricted",
                "--model",
                str(target.model),
                "--effort",
                "low",
                "-p",
                PRIME_PROMPT,
                "--output-format",
                "json",
            ]
        if target.provider == "codex":
            command = shlex.split(self.codex_command or "")
            if not command:
                return []
            return [
                *command,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "-s",
                "read-only",
                "-C",
                str(self.workspace_dir),
                PRIME_PROMPT,
            ]
        return []

    def _load_state(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "schema_version": STATE_SCHEMA_VERSION,
            "status": "disabled" if not self.enabled else "warming",
            "updated_at": None,
            "last_error": None,
            "buckets": {},
        }
        try:
            with interprocess_file_lock(self.lock_path):
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError, ValueError):
            return base
        if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA_VERSION:
            return base
        buckets = payload.get("buckets")
        if not isinstance(buckets, dict):
            payload["buckets"] = {}
        return payload

    def _save_state_locked(self) -> None:
        payload = json.dumps(
            self._state,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        with interprocess_file_lock(self.lock_path):
            atomic_write_bytes(self.state_path, payload, mode=0o600, sync_directory=True)
