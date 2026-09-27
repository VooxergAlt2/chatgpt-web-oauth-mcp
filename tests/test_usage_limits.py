from __future__ import annotations

import json
import subprocess
from pathlib import Path

from chatgpt_web_oauth_mcp.usage_limits import UsageLimitCollector


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
    assert providers["antigravity"]["windows"][0]["remaining_percent"] == 75.0
    assert providers["antigravity"]["windows"][1]["remaining_percent"] == 50.0
    claude_5h = next(
        item for item in providers["claude"]["windows"] if item["id"] == "five_hour"
    )
    assert claude_5h["used_percent"] == 12.5
    assert claude_5h["remaining_percent"] == 87.5
    codex_5h = next(
        item
        for item in providers["codex"]["windows"]
        if item["duration_minutes"] == 300
    )
    assert codex_5h["used_percent"] == 7.0
    assert codex_5h["remaining_percent"] == 93.0
    assert codex_5h["resets_at"].endswith("Z")


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
