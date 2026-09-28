from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from chatgpt_web_oauth_mcp.usage_limits import UsageLimitCollector, _percent


def test_usage_percent_normalization_is_bounded_and_rejects_bool() -> None:
    assert _percent(-5) == 0.0
    assert _percent(150) == 100.0
    assert _percent(12.34567) == 12.3457
    assert _percent(True) is None


class _Response:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._raw


def test_usage_limit_collector_normalizes_three_providers(monkeypatch, tmp_path: Path) -> None:
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "secret",
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )

    agy_payload = {
        "command": {
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "id": "gemini-5h",
                                "window": "5h",
                                "remaining_fraction": 0.75,
                                "reset_time": "2026-09-27T18:00:00Z",
                            },
                            {
                                "id": "gemini-weekly",
                                "window": "weekly",
                                "remaining_fraction": 0.6,
                                "reset_time": "2026-10-03T18:00:00Z",
                            }
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [
                            {
                                "id": "3p-5h",
                                "window": "5h",
                                "remaining_fraction": 0.5,
                                "reset_time": "2026-09-27T18:00:00Z",
                            },
                            {
                                "id": "3p-weekly",
                                "window": "weekly",
                                "remaining_fraction": 0.4,
                                "reset_time": "2026-10-04T18:00:00Z",
                            }
                        ],
                    },
                ]
            }
        }
    }

    def fake_run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["agy"],
            returncode=0,
            stdout=json.dumps(agy_payload),
            stderr="",
        )

    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.usage_limits.subprocess.run",
        fake_run,
    )
    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.usage_limits.urlrequest.urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "plan": "max",
                "five_hour": {
                    "utilization": 12.5,
                    "resets_at": "2026-09-27T19:00:00Z",
                },
                "seven_day": {
                    "utilization": 20,
                    "resets_at": "2026-10-04T00:00:00Z",
                },
                "seven_day_sonnet": {
                    "utilization": 30,
                    "resets_at": "2026-10-04T01:00:00Z",
                },
            }
        ),
    )

    collector = UsageLimitCollector(
        antigravity_command="agy",
        codex_reader=lambda: {
            "rateLimits": {
                "limitId": "codex",
                "planType": "plus",
                "primary": {
                    "usedPercent": 7,
                    "windowDurationMins": 300,
                    "resetsAt": 1790532733,
                },
                "secondary": {
                    "usedPercent": 25,
                    "windowDurationMins": 10080,
                    "resetsAt": 1791099943,
                },
            }
        },
        claude_credentials_path=credentials,
    )

    result = collector.refresh_now()

    assert result["status"] == "ok"
    providers = result["providers"]
    antigravity_windows = providers["antigravity"]["windows"]
    assert next(item for item in antigravity_windows if item["id"] == "gemini-5h")[
        "remaining_percent"
    ] == 75.0
    assert next(item for item in antigravity_windows if item["id"] == "gemini-weekly")[
        "remaining_percent"
    ] == 60.0
    assert next(item for item in antigravity_windows if item["id"] == "3p-5h")[
        "remaining_percent"
    ] == 50.0
    assert next(item for item in antigravity_windows if item["id"] == "3p-weekly")[
        "remaining_percent"
    ] == 40.0
    claude_5h = next(
        item for item in providers["claude"]["windows"] if item["id"] == "five_hour"
    )
    assert claude_5h["used_percent"] == 12.5
    assert claude_5h["remaining_percent"] == 87.5
    claude_weekly = next(
        item for item in providers["claude"]["windows"] if item["id"] == "seven_day"
    )
    assert claude_weekly["remaining_percent"] == 80.0
    claude_sonnet_weekly = next(
        item
        for item in providers["claude"]["windows"]
        if item["id"] == "seven_day_sonnet"
    )
    assert claude_sonnet_weekly["remaining_percent"] == 70.0
    codex_5h = next(
        item
        for item in providers["codex"]["windows"]
        if item["duration_minutes"] == 300
    )
    assert codex_5h["used_percent"] == 7.0
    assert codex_5h["remaining_percent"] == 93.0
    assert codex_5h["window"] == "5h"
    assert codex_5h["resets_at"].endswith("Z")
    codex_weekly = next(
        item
        for item in providers["codex"]["windows"]
        if item["duration_minutes"] == 10080
    )
    assert codex_weekly["remaining_percent"] == 75.0
    assert codex_weekly["window"] == "weekly"


def test_usage_limit_collector_separates_antigravity_accounts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    agy2_home = tmp_path / "agy2-home"
    credential = agy2_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    credential.parent.mkdir(parents=True)
    credential.write_text("token", encoding="utf-8")
    seen_homes: list[str | None] = []

    def fake_run(*_args, **kwargs):
        home = kwargs.get("env", {}).get("HOME")
        seen_homes.append(home)
        remaining = 0.25 if home == str(agy2_home) else 0.75
        return subprocess.CompletedProcess(
            args=["agy"],
            returncode=0,
            stdout=json.dumps(
                {
                    "command": {
                        "data": {
                            "groups": [
                                {
                                    "name": "Gemini Models",
                                    "buckets": [
                                        {
                                            "id": "gemini-5h",
                                            "window": "5h",
                                            "remaining_fraction": remaining,
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.usage_limits.subprocess.run",
        fake_run,
    )
    collector = UsageLimitCollector(
        antigravity_command="agy",
        antigravity_accounts={
            "antigravity": {"command": "agy", "env_overrides": None},
            "antigravity2": {
                "command": "agy",
                "env_overrides": {"HOME": str(agy2_home)},
                "credential_path": str(credential),
            },
        },
        codex_reader=None,
        claude_credentials_path=tmp_path / "missing-claude.json",
    )

    result = collector.refresh_now()

    assert result["providers"]["antigravity"]["windows"][0]["remaining_percent"] == 75.0
    assert result["providers"]["antigravity2"]["windows"][0]["remaining_percent"] == 25.0
    assert os.environ.get("HOME") in seen_homes
    assert str(agy2_home) in seen_homes

    seen_homes.clear()
    targeted = collector.refresh_provider("antigravity2")
    assert targeted["providers"]["antigravity2"]["windows"][0][
        "remaining_percent"
    ] == 25.0
    assert seen_homes == [str(agy2_home)]


def test_usage_limit_collector_keeps_last_good_value_as_stale(monkeypatch, tmp_path: Path) -> None:
    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "secret"}}),
        encoding="utf-8",
    )

    calls = {"fail": False}

    def fake_run(*_args, **_kwargs):
        if calls["fail"]:
            return subprocess.CompletedProcess(args=["agy"], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(
            args=["agy"],
            returncode=0,
            stdout=json.dumps(
                {
                    "command": {
                        "data": {
                            "groups": [
                                {
                                    "name": "Gemini Models",
                                    "buckets": [
                                        {
                                            "id": "gemini-5h",
                                            "window": "5h",
                                            "remaining_fraction": 0.9,
                                            "reset_time": "2026-09-27T18:00:00Z",
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.usage_limits.subprocess.run",
        fake_run,
    )
    monkeypatch.setattr(
        "chatgpt_web_oauth_mcp.usage_limits.urlrequest.urlopen",
        lambda *_args, **_kwargs: _Response(
            {"five_hour": {"utilization": 10, "resets_at": "2026-09-27T19:00:00Z"}}
        ),
    )

    collector = UsageLimitCollector(
        antigravity_command="agy",
        codex_reader=lambda: {
            "rateLimits": {
                "primary": {
                    "usedPercent": 10,
                    "windowDurationMins": 300,
                    "resetsAt": 1790532733,
                }
            }
        },
        claude_credentials_path=credentials,
    )
    first = collector.refresh_now()
    assert first["providers"]["antigravity"]["status"] == "ok"

    calls["fail"] = True
    second = collector.refresh_now()
    assert second["status"] == "partial"
    assert second["providers"]["antigravity"]["status"] == "stale"
    assert second["providers"]["antigravity"]["error"] == "exit_1"
    assert second["providers"]["antigravity"]["windows"][0]["remaining_percent"] == 90.0
