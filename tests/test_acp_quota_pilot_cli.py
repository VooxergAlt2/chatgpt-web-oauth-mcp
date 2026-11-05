"""Focused tests for the offline paired quota pilot CLI boundary."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import acp_quota_pilot as cli


def snapshot(at, remaining=80, *, status="ok", windows=None, health=False):
    provider = {
        "status": status,
        "updated_at": at,
        "windows": windows if windows is not None else [{
            "group": "premium", "window": "weekly", "resets_at": "2030-01-08T00:00:00Z",
            "remaining_percent": remaining,
        }],
    }
    usage = {"status": "ok", "providers": {"acct-1": provider}}
    return {"usage_limits": usage} if health else usage


def write_snapshot(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def args_for(before, after, *extra):
    return [before, after, "--account", "acct-1", "--group", "premium",
            "--window", "weekly", "--started-at", "2030-01-01T00:00:00Z",
            "--finished-at", "2030-01-01T00:00:10Z", *extra]


def test_exact_native_selection_and_core_call(monkeypatch, tmp_path, capsys):
    first = write_snapshot(tmp_path / "before.json", snapshot("2029-12-31T23:59:59Z", 80))
    second = write_snapshot(tmp_path / "after.json", snapshot("2030-01-01T00:00:11Z", 70, health=True))
    seen = {}

    def fake_delta(before, after, *, concurrent=False):
        seen.update(before=before, after=after, concurrent=concurrent)
        return 10.0, "observed"

    monkeypatch.setattr(cli, "quota_delta", fake_delta, raising=False)
    assert cli.main(args_for(first, second)) == 0
    assert json.loads(capsys.readouterr().out) == {"delta_pp": 10.0, "reason": "observed"}
    assert seen["before"]["remaining_percent"] == 80
    assert seen["after"]["remaining_percent"] == 70
    assert seen["concurrent"] is False


@pytest.mark.parametrize("status,windows,reason", [
    ("stale", None, "unavailable"),
    ("ok", [], "missing"),
    ("ok", [
        {"group": "premium", "window": "weekly"},
        {"group": "premium", "window": "weekly"},
    ], "ambiguous"),
])
def test_missing_or_nonunique_provider_evidence_is_unknown(tmp_path, status, windows, reason):
    before = write_snapshot(tmp_path / "before.json", snapshot("2029-12-31T23:59:59Z", status=status, windows=windows))
    after = write_snapshot(tmp_path / "after.json", snapshot("2030-01-01T00:00:11Z"))
    result = cli._evaluate(cli.build_parser().parse_args(args_for(before, after)))
    assert result == {"delta_pp": None, "reason": reason}


@pytest.mark.parametrize(("before_at", "after_at", "valid"), [
    ("2030-01-01T00:00:01Z", "2030-01-01T00:00:11Z", False),
    ("2029-12-31T23:59:59Z", "2030-01-01T00:00:09Z", False),
    ("2030-01-01T00:00:00Z", "2030-01-01T00:00:10Z", True),
])
def test_samples_must_bracket_episode(tmp_path, before_at, after_at, valid):
    before = write_snapshot(tmp_path / "before.json", snapshot(before_at, 80))
    after = write_snapshot(tmp_path / "after.json", snapshot(after_at, 70))
    result = cli._evaluate(cli.build_parser().parse_args(args_for(before, after)))
    assert result == ({"delta_pp": 10, "reason": "observed"} if valid
                      else {"delta_pp": None, "reason": "stale"})


@pytest.mark.parametrize("which", ["before", "after"])
def test_each_endpoint_gap_is_checked_independently(tmp_path, which):
    before_at, after_at = "2029-12-31T23:59:59Z", "2030-01-01T00:00:11Z"
    if which == "before":
        before_at = "2029-12-31T23:59:57Z"
    else:
        after_at = "2030-01-01T00:00:13Z"
    before = write_snapshot(tmp_path / "before.json", snapshot(before_at))
    after = write_snapshot(tmp_path / "after.json", snapshot(after_at, 70))
    args = args_for(before, after, "--max-gap-seconds", "2")
    result = cli._evaluate(cli.build_parser().parse_args(args))
    assert result == {"delta_pp": None, "reason": "stale"}


def test_concurrent_observations_are_excluded(monkeypatch, tmp_path, capsys):
    first = write_snapshot(tmp_path / "before.json", snapshot("2029-12-31T23:59:59Z"))
    second = write_snapshot(tmp_path / "after.json", snapshot("2030-01-01T00:00:11Z", 70))
    calls = []
    monkeypatch.setattr(cli, "quota_delta", lambda *a, **kw: calls.append(kw) or (None, "concurrent"), raising=False)
    assert cli.main(args_for(first, second, "--concurrent")) == 0
    assert calls == []
    assert json.loads(capsys.readouterr().out) == {"delta_pp": None, "reason": "concurrent"}


@pytest.mark.parametrize("value", ["2030-01-01T00:00:00", "not-a-date"])
def test_started_at_requires_aware_iso_cli_error(tmp_path, value):
    first = write_snapshot(tmp_path / "before.json", snapshot("2029-12-31T23:59:59Z"))
    second = write_snapshot(tmp_path / "after.json", snapshot("2030-01-01T00:00:11Z"))
    argv = args_for(first, second)
    argv[argv.index("--started-at") + 1] = value
    with pytest.raises(SystemExit):
        cli.main(argv)


def test_cli_subprocess_uses_real_quota_delta(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "acp_quota_pilot.py"
    before = write_snapshot(tmp_path / "before.json", snapshot("2029-12-31T23:59:59Z", 80))
    after = write_snapshot(tmp_path / "after.json", snapshot("2030-01-01T00:00:11Z", 70))
    proc = subprocess.run([sys.executable, str(script), *args_for(before, after)],
                          text=True, capture_output=True, check=True, timeout=10)
    assert json.loads(proc.stdout) == {"delta_pp": 10.0, "reason": "observed"}


@pytest.mark.parametrize("window", ["5h", "weekly"])
def test_field_receipts_replay(window, tmp_path, capsys):
    fixture = Path(__file__).parent / "fixtures" / "acp_quota_pair_20261105.json"
    for episode in json.loads(fixture.read_text(encoding="utf-8")):
        before = write_snapshot(tmp_path / "before.json", episode["before"])
        after = write_snapshot(tmp_path / "after.json", episode["after"])
        assert cli.main([
            before, after, "--account", episode["account"], "--group", episode["group"],
            "--window", window, "--started-at", episode["started_at"],
            "--finished-at", episode["finished_at"],
        ]) == 0
        delta = episode["expected_pp"][window]
        assert json.loads(capsys.readouterr().out) == {
            "delta_pp": delta, "reason": "reset" if delta is None else "observed",
        }, episode["mode"]
