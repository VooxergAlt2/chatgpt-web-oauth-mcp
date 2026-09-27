from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import threading
import time
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

from .process_env import sanitized_child_env


CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"
CLAUDE_USER_AGENT = "claude-code"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _epoch_to_iso(value: object) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
    except (OSError, OverflowError, ValueError):
        return None


def _percent(value: object) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value), 1)


def _remaining_from_used(value: object) -> float | None:
    used = _percent(value)
    return None if used is None else round(max(0.0, 100.0 - used), 1)


class UsageLimitCollector:
    """Background reader for CLI/account quota windows used by Ops MCP health."""

    def __init__(
        self,
        *,
        antigravity_command: str | None,
        codex_reader: Callable[[], dict[str, Any]] | None,
        claude_credentials_path: Path | None = None,
        refresh_interval_seconds: float = 300.0,
        command_timeout_seconds: float = 10.0,
        http_timeout_seconds: float = 5.0,
    ) -> None:
        self.antigravity_command = antigravity_command
        self.codex_reader = codex_reader
        self.claude_credentials_path = (
            claude_credentials_path
            if claude_credentials_path is not None
            else Path.home() / ".claude" / ".credentials.json"
        )
        self.refresh_interval_seconds = max(30.0, float(refresh_interval_seconds))
        self.command_timeout_seconds = max(1.0, float(command_timeout_seconds))
        self.http_timeout_seconds = max(0.5, float(http_timeout_seconds))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, Any] = {
            "status": "warming",
            "updated_at": None,
            "last_attempt_at": None,
            "refresh_interval_seconds": self.refresh_interval_seconds,
            "providers": {},
        }

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="usage-limit-collector",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            join_timeout = (
                max(self.command_timeout_seconds, self.http_timeout_seconds) + 2.0
                if timeout is None
                else max(0.0, timeout)
            )
            thread.join(timeout=join_timeout)

    def snapshot(self) -> dict[str, Any]:
        self.start()
        with self._lock:
            return deepcopy(self._snapshot)

    def refresh_now(self) -> dict[str, Any]:
        attempt_at = _iso_now()
        fresh = {
            "antigravity": self._safe_collect(self._collect_antigravity),
            "claude": self._safe_collect(self._collect_claude),
            "codex": self._safe_collect(self._collect_codex),
        }
        with self._lock:
            previous = self._snapshot.get("providers")
            previous = previous if isinstance(previous, dict) else {}
            merged: dict[str, Any] = {}
            successful = 0
            for name, current in fresh.items():
                if current.get("status") == "ok":
                    current["updated_at"] = attempt_at
                    current["last_attempt_at"] = attempt_at
                    merged[name] = current
                    successful += 1
                    continue
                prior = previous.get(name)
                if isinstance(prior, dict) and prior.get("status") in {"ok", "stale"}:
                    stale = deepcopy(prior)
                    stale["status"] = "stale"
                    stale["error"] = current.get("error")
                    stale["last_attempt_at"] = attempt_at
                    merged[name] = stale
                else:
                    current["last_attempt_at"] = attempt_at
                    merged[name] = current

            provider_states = {str(item.get("status") or "") for item in merged.values()}
            if provider_states == {"ok"}:
                overall = "ok"
            elif "ok" in provider_states or "stale" in provider_states:
                overall = "partial"
            else:
                overall = "unavailable"

            last_success = attempt_at if successful else self._snapshot.get("updated_at")
            self._snapshot = {
                "status": overall,
                "updated_at": last_success,
                "last_attempt_at": attempt_at,
                "refresh_interval_seconds": self.refresh_interval_seconds,
                "providers": merged,
            }
            return deepcopy(self._snapshot)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.refresh_now()
            self._stop.wait(self.refresh_interval_seconds)

    @staticmethod
    def _safe_collect(callback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            result = callback()
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": "timeout"}
        except urlerror.HTTPError as exc:
            return {"status": "error", "error": f"http_{exc.code}"}
        except urlerror.URLError:
            return {"status": "error", "error": "network_error"}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"status": "error", "error": "collector_error"}
        except Exception:
            return {"status": "error", "error": "unexpected_error"}
        return result

    def _collect_antigravity(self) -> dict[str, Any]:
        parts = shlex.split(self.antigravity_command or "")
        if not parts:
            return {"status": "unavailable", "error": "command_not_configured"}
        process = subprocess.run(
            [*parts, "-p", "/usage", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=self.command_timeout_seconds,
            check=False,
            env=sanitized_child_env(),
        )
        if process.returncode != 0:
            return {"status": "error", "error": f"exit_{process.returncode}"}
        payload = json.loads(process.stdout)
        command = payload.get("command")
        data = command.get("data") if isinstance(command, dict) else None
        groups = data.get("groups") if isinstance(data, dict) else None
        if not isinstance(groups, list):
            return {"status": "error", "error": "usage_payload_missing"}

        windows: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_name = str(group.get("name") or "")
            buckets = group.get("buckets")
            if not isinstance(buckets, list):
                continue
            for bucket in buckets:
                if not isinstance(bucket, dict):
                    continue
                remaining_fraction = bucket.get("remaining_fraction")
                remaining_percent = (
                    round(float(remaining_fraction) * 100.0, 1)
                    if isinstance(remaining_fraction, (int, float))
                    else None
                )
                windows.append(
                    {
                        "id": bucket.get("id"),
                        "group": group_name,
                        "window": bucket.get("window"),
                        "remaining_percent": remaining_percent,
                        "used_percent": (
                            None
                            if remaining_percent is None
                            else round(max(0.0, 100.0 - remaining_percent), 1)
                        ),
                        "resets_at": bucket.get("reset_time"),
                    }
                )
        return {"status": "ok", "windows": windows}

    def _collect_claude(self) -> dict[str, Any]:
        try:
            root = json.loads(self.claude_credentials_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"status": "unavailable", "error": "credentials_missing"}
        oauth = root.get("claudeAiOauth") if isinstance(root, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not isinstance(token, str) or not token:
            return {"status": "unavailable", "error": "access_token_missing"}

        request = urlrequest.Request(
            CLAUDE_USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": CLAUDE_OAUTH_BETA,
                "User-Agent": CLAUDE_USER_AGENT,
                "Accept": "application/json",
            },
            method="GET",
        )
        with urlrequest.urlopen(request, timeout=self.http_timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return {"status": "error", "error": "usage_payload_invalid"}

        windows: list[dict[str, Any]] = []
        for key, label, minutes in (
            ("five_hour", "5h", 300),
            ("seven_day", "7d", 10080),
            ("seven_day_sonnet", "7d_sonnet", 10080),
        ):
            raw = payload.get(key)
            if not isinstance(raw, dict):
                continue
            used = _percent(raw.get("utilization"))
            windows.append(
                {
                    "id": key,
                    "window": label,
                    "duration_minutes": minutes,
                    "used_percent": used,
                    "remaining_percent": _remaining_from_used(used),
                    "resets_at": raw.get("resets_at"),
                }
            )

        plan = payload.get("plan")
        if isinstance(plan, dict):
            plan = plan.get("name") or plan.get("type")
        if not isinstance(plan, str):
            plan = oauth.get("subscriptionType") if isinstance(oauth, dict) else None
        return {"status": "ok", "plan": plan, "windows": windows}

    def _collect_codex(self) -> dict[str, Any]:
        if self.codex_reader is None:
            return {"status": "unavailable", "error": "reader_not_configured"}
        payload = self.codex_reader()
        limits = payload.get("rateLimits") if isinstance(payload, dict) else None
        if not isinstance(limits, dict):
            return {"status": "error", "error": "usage_payload_missing"}

        windows: list[dict[str, Any]] = []
        for name in ("primary", "secondary"):
            raw = limits.get(name)
            if not isinstance(raw, dict):
                continue
            used = _percent(raw.get("usedPercent"))
            windows.append(
                {
                    "id": name,
                    "window": name,
                    "duration_minutes": raw.get("windowDurationMins"),
                    "used_percent": used,
                    "remaining_percent": _remaining_from_used(used),
                    "resets_at": _epoch_to_iso(raw.get("resetsAt")),
                }
            )
        return {
            "status": "ok",
            "limit_id": limits.get("limitId"),
            "plan": limits.get("planType"),
            "windows": windows,
        }
