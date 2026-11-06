from __future__ import annotations

import json
import os
from copy import deepcopy

import pytest

from chatgpt_web_oauth_mcp import acp_progress
from chatgpt_web_oauth_mcp.acp_progress import progress_snapshot


def event(*, component="session", phase="prompt", name="tool_call", observed=100, **extra):
    return {"event": "acp_progress", "progress": {
        "event": name,
        "runtime": {"version": 1, "component": component, "phase": phase,
                    "elapsed_seconds": 12.5, "observed_at_epoch": observed},
        **extra,
    }}


def record(value):
    return json.dumps(value).encode() + b"\n"


def running(path):
    return {"status": "running", "completed": False, "logs": {"stdout": str(path)}}


def diagnostics(component="session", phase="teardown"):
    return {"version": 1, "component": component, "phase": phase, "elapsed_seconds": 20,
            "phase_durations_seconds": {phase: 1}, "failure_phase": None,
            "event_count": 3, "first_event_seconds": 5, "last_event_seconds": 19,
            "last_event": "agent_message_chunk"}


def test_live_view_is_content_free_advisory_and_does_not_mutate_snapshot(tmp_path):
    path = tmp_path / "stdout.log"
    data = event(tools=2, answer_bytes=120, arguments="SECRET", prompt="SECRET", path="SECRET")
    data["progress"]["runtime"]["unknown_label"] = "SECRET"
    path.write_bytes(record(data))
    snapshot = running(path) | {"activity_state": "active"}
    before = deepcopy(snapshot)
    view = progress_snapshot(snapshot, now=105)
    assert view == {
        "version": 1, "available": True, "advisory": True, "source": "worker_stdout",
        "event": "tool_call",
        "runtime": {"version": 1, "component": "session", "phase": "prompt",
                    "elapsed_seconds": 12.5, "observed_at_epoch": 100},
        "tools": 2, "answer_bytes": 120, "seconds_since_progress": 5,
    }
    assert snapshot == before and "SECRET" not in json.dumps(view)


@pytest.mark.parametrize("mutation", [
    {"runtime": {"version": True}},
    {"runtime": {"version": 2}},
    {"runtime": {"component": "SECRET"}},
    {"runtime": {"component": []}},
    {"runtime": {"phase": "SECRET"}},
    {"runtime": {"phase": []}},
    {"runtime": {"phase": "verification"}},
    {"runtime": {"elapsed_seconds": -1}},
    {"runtime": {"elapsed_seconds": True}},
    {"runtime": {"elapsed_seconds": float("nan")}},
    {"runtime": {"elapsed_seconds": float("inf")}},
    {"runtime": {"observed_at_epoch": 0}},
    {"runtime": {"observed_at_epoch": -1}},
    {"runtime": {"observed_at_epoch": float("inf")}},
    {"runtime": {"observed_at_epoch": "SECRET"}},
    {"event": "SECRET"},
    {"event": []},
])
def test_malformed_runtime_cannot_replace_last_valid_progress(tmp_path, mutation):
    path = tmp_path / "stdout.log"
    invalid = event()
    if "runtime" in mutation:
        invalid["progress"]["runtime"].update(mutation["runtime"])
    else:
        invalid["progress"].update(mutation)
    path.write_bytes(record(event(name="phase")) + record(invalid))
    view = progress_snapshot(running(path), now=105)
    assert view["available"] and view["event"] == "phase"
    assert "SECRET" not in json.dumps(view)


def test_partial_utf8_invalid_json_deep_json_and_legacy_lines_are_ignored(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes(
        record(event(name="phase"))
        + b"invalid\n\xff\xfe\n"
        + b"[" * 1500 + b"]" * 1500 + b"\n"
        + record({"event": "acp_progress", "progress": {"event": "verifying", "content": "SECRET"}})
        + record(event(observed=102))[:-1]  # Valid JSON, but not completely emitted.
    )
    view = progress_snapshot(running(path), now=105)
    assert view["available"] and view["event"] == "phase"
    assert view["seconds_since_progress"] == 5 and "SECRET" not in json.dumps(view)


@pytest.mark.parametrize("data", [
    b"",
    b'{"event":"acp_progress","progress":{"event":"verifying"}}\n',
    b'{"event":"acp_result","result":{"answer":"SECRET"}}\n',
    b'{"event":"acp_progress","progress":{"event":"phase","runtime":null}}\n',
])
def test_old_or_unstarted_worker_reports_unavailable(tmp_path, data):
    path = tmp_path / "stdout.log"
    path.write_bytes(data)
    view = progress_snapshot(running(path), now=105)
    assert view == {"version": 1, "available": False, "advisory": True,
                    "source": "unavailable", "reason": "acp_progress_not_started_or_legacy"}


def test_prefix_budget_survives_long_native_stream_and_read_is_bounded(tmp_path, monkeypatch):
    path = tmp_path / "stdout.log"
    budget = {"version": 1, "total_seconds": 360, "worker_safety_seconds": 3,
              "verification_reserve_seconds": 90, "native_cancel_grace_seconds": 5,
              "setup_elapsed_seconds": 1, "native_session_seconds": 266,
              "SECRET": 123}
    path.write_bytes(
        record(event(component="worker", phase="native", name="phase", execution_budget=budget))
        + b"x" * 300_000 + b"\n" + record(event(observed=150, tools=3))
    )
    reads = []
    original = acp_progress.os.read

    def bounded_read(descriptor, count):
        data = original(descriptor, count)
        reads.append(len(data))
        return data

    monkeypatch.setattr(acp_progress.os, "read", bounded_read)
    view = progress_snapshot(running(path), now=160)
    assert sum(reads) <= 72 * 1024 and len(reads) == 2
    assert view["runtime"]["component"] == "session" and view["seconds_since_progress"] == 10
    assert view["execution_budget"] == {key: value for key, value in budget.items() if key != "SECRET"}
    assert "SECRET" not in json.dumps(view)


def test_latest_valid_budget_is_selected_independently(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes(
        record(event(component="worker", phase="native", name="phase",
                     execution_budget={"version": 1, "native_session_seconds": 200}))
        + record(event(component="worker", phase="verification", name="phase",
                       execution_budget={"version": 1, "native_session_seconds": 201,
                                         "total_seconds": float("nan"), "SECRET": 5}))
        + record(event(observed=110, execution_budget={"version": True, "total_seconds": 1}))
    )
    view = progress_snapshot(running(path), now=115)
    assert view["execution_budget"] == {"version": 1, "native_session_seconds": 201}
    assert view["runtime"]["observed_at_epoch"] == 110


def test_invalid_optional_counters_never_leak(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes(record(event(tools=True, answer_bytes=-1)))
    view = progress_snapshot(running(path), now=105)
    assert "tools" not in view and "answer_bytes" not in view
    path.write_bytes(record(event(tools=1 << 64, answer_bytes="SECRET")))
    view = progress_snapshot(running(path), now=105)
    assert "tools" not in view and "answer_bytes" not in view


def test_clock_skew_is_advisory_and_never_creates_negative_age(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes(record(event(observed=120)))
    assert progress_snapshot(running(path), now=100)["seconds_since_progress"] == 0


def attention(count=4):
    return {"version": 1, "reason": "tool_call_threshold", "threshold": 4,
            "observed_calls": count, "requires_review": True, "terminating": False}


def test_attention_survives_phase_updates_with_monotonic_content_free_count(tmp_path):
    path = tmp_path / "stdout.log"
    path.write_bytes(
        record(event(tool_call_attention=attention(7) | {"prompt": "SECRET"}))
        + record(event(tool_call_attention=attention(4)))
        + record(event(name="phase", observed=110))
    )
    view = progress_snapshot(running(path), now=115)
    assert view["event"] == "phase"
    assert view["tool_call_attention"] == attention(7)
    assert "SECRET" not in json.dumps(view)


@pytest.mark.parametrize("invalid", [
    {"version": True}, {"reason": "SECRET"}, {"threshold": 0},
    {"observed_calls": 3}, {"observed_calls": True}, {"requires_review": 1},
    {"terminating": True},
])
def test_invalid_attention_cannot_replace_valid_observation(tmp_path, invalid):
    path = tmp_path / "stdout.log"
    path.write_bytes(
        record(event(tool_call_attention=attention(5)))
        + record(event(tool_call_attention=attention(7) | invalid))
        + record(event(tool_call_attention=attention(12)))[:-1]
    )
    assert progress_snapshot(running(path), now=105)["tool_call_attention"] == attention(5)


def test_terminal_attention_comes_from_sanitized_final_metadata_without_reading_log(monkeypatch):
    def no_stdout(*args):
        raise AssertionError("Terminal attention must not read stdout")
    monkeypatch.setattr(acp_progress, "_stdout_windows", no_stdout)
    snapshot = {"status": "succeeded", "completed": True, "harness_metadata": {
        "tool_call_attention": attention(19) | {"arguments": "SECRET"},
        "acp_session_diagnostics": diagnostics(),
    }}
    view = progress_snapshot(snapshot)
    assert view["tool_call_attention"] == attention(19)
    assert "SECRET" not in json.dumps(view)


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "directory", "fifo", "missing"])
def test_non_regular_or_symlink_stdout_is_never_read(tmp_path, kind, monkeypatch):
    path = tmp_path / "stdout.log"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(record(event()))
        path.symlink_to(target)
    elif kind == "parent_symlink":
        target = tmp_path / "real"
        target.mkdir()
        (target / "stdout.log").write_bytes(record(event()))
        link = tmp_path / "linked"
        link.symlink_to(target, target_is_directory=True)
        path = link / "stdout.log"
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)

    def never_read(*args):
        raise AssertionError("A non-regular or symlink log must not be read")

    monkeypatch.setattr(acp_progress.os, "read", never_read)
    assert not progress_snapshot(running(path), now=105)["available"]


@pytest.mark.parametrize("path", ["relative/stdout.log", "/tmp/other.log", "/tmp/../stdout.log"])
def test_noncanonical_stdout_path_is_not_opened(path, monkeypatch):
    def never_open(*args):
        raise AssertionError("Invalid log path must not be opened")

    monkeypatch.setattr(acp_progress.os, "open", never_open)
    assert not progress_snapshot({"status": "running", "logs": {"stdout": path}}, now=105)["available"]


@pytest.mark.parametrize("status", [
    "succeeded", "failed", "cancelled", "timed_out", "unknown", "interrupted", "blocked",
])
def test_terminal_reads_only_sanitized_final_metadata(status, monkeypatch):
    def no_stdout(*args):
        raise AssertionError("Terminal status must not read stdout")

    monkeypatch.setattr(acp_progress, "_stdout_windows", no_stdout)
    session = diagnostics()
    session["phase_durations_seconds"]["SECRET"] = 99
    session["phase_durations_seconds"]["prompt"] = float("inf")
    session["unknown_field"] = "SECRET"
    worker = diagnostics("worker", "cleanup")
    snapshot = {"status": status, "completed": False, "harness_metadata": {
        "acp_session_diagnostics": session, "acp_worker_diagnostics": worker,
        "execution_budget": {"version": 1, "native_session_seconds": 200, "SECRET": 1},
    }}
    view = progress_snapshot(snapshot)
    assert view["available"] and view["source"] == "final_metadata"
    assert set(view["diagnostics"]) == {"session", "worker"}
    assert view["diagnostics"]["session"]["phase_durations_seconds"] == {"teardown": 1}
    assert view["execution_budget"] == {"version": 1, "native_session_seconds": 200}
    assert "seconds_since_progress" not in view and "SECRET" not in json.dumps(view)


@pytest.mark.parametrize("metadata", [
    None, {}, {"acp_session_diagnostics": {"version": 1, "phase": "SECRET"}},
    {"acp_session_diagnostics": diagnostics() | {"component": "worker"}},
])
def test_completed_legacy_or_malformed_final_metadata_never_falls_back_to_stdout(metadata, monkeypatch):
    def no_stdout(*args):
        raise AssertionError("Missing terminal metadata must not trigger a stdout read")

    monkeypatch.setattr(acp_progress, "_stdout_windows", no_stdout)
    view = progress_snapshot({"status": "running", "completed": True, "harness_metadata": metadata})
    assert view == {"version": 1, "available": False, "advisory": True, "source": "unavailable",
                    "reason": "acp_progress_final_metadata_unavailable"}


def test_final_optional_metadata_is_sanitized_without_unknown_labels():
    data = diagnostics() | {"failure_phase": "SECRET", "last_event": "SECRET",
                            "event_count": True, "first_event_seconds": -1}
    view = progress_snapshot({"status": "failed", "harness_metadata": {"acp_session_diagnostics": data}})
    actual = view["diagnostics"]["session"]
    assert "failure_phase" not in actual and "last_event" not in actual
    assert "event_count" not in actual and "first_event_seconds" not in actual
    assert "SECRET" not in json.dumps(view)
