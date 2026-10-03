from __future__ import annotations

from pathlib import Path

from chatgpt_web_oauth_mcp import server
from chatgpt_web_oauth_mcp import session_continuation
from chatgpt_web_oauth_mcp.session_checkpoints import SessionCheckpointStore


def _call(tool, *args, **kwargs):
    fn = tool.fn if hasattr(tool, "fn") else tool
    return fn(*args, **kwargs)


def test_delegate_task_maps_mcp_arguments_to_registry(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeRegistry:
        def run_delegate(self, **kwargs):
            captured.update(kwargs)
            return {"success": True, "status": "succeeded"}

    monkeypatch.setattr(server, "registry", FakeRegistry())

    result = _call(
        server.delegate_task,
        task="review this slice",
        cwd=str(tmp_path),
        harness="antigravity",
        kind="explore",
        files_in_scope=["src"],
        acceptance_criteria=["report findings"],
        commit_mode="required",
        resume_from_delegate_id="abc123abc123",
        wait_seconds=1,
    )

    assert result["success"] is True
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["harness"] == "antigravity"
    assert captured["kind"] == "explore"
    assert captured["files_in_scope"] == ["src"]
    assert captured["acceptance_criteria"] == ["report findings"]
    assert captured["commit_mode"] == "required"
    assert captured["resume_from_delegate_id"] == "abc123abc123"


def test_delegate_task_and_status_update_automatic_resume_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )

    class FakeRegistry:
        def run_delegate(self, **kwargs):
            before_submit = kwargs.get("before_submit")
            if callable(before_submit):
                admission_error = before_submit()
                if admission_error is not None:
                    return admission_error
            return {
                "success": True,
                "status": "running",
                "completed": False,
                "delegate_id": "d1",
                "cwd": str(tmp_path),
                "harness": "antigravity",
                "activity_state": "active",
            }

        def delegate_status(self, **kwargs):
            return {
                "success": True,
                "delegate": {
                    "success": True,
                    "status": "succeeded",
                    "completed": True,
                    "delegate_id": "d1",
                    "cwd": str(tmp_path),
                    "harness": "antigravity",
                    "activity_state": "active",
                    "summary": "review complete",
                    "error": None,
                    "logs": {
                        "stdout": str(tmp_path / "stdout.log"),
                        "stderr": str(tmp_path / "stderr.log"),
                    },
                    "exit_code": 0,
                    "model": "gemini-3.8-flash",
                    "reasoning_effort": "high",
                    "timed_out": False,
                    "sandbox_mode": "plan+sandbox",
                    "structured_output": {
                        "status": "succeeded",
                        "findings": ["none"],
                    },
                },
            }

    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(
        session_continuation.session,
        "get_current_session_id",
        lambda: "openai:test-delegate",
    )

    started = _call(
        server.delegate_task,
        task="review slice",
        cwd=str(tmp_path),
        harness="antigravity",
        kind="explore",
        wait_seconds=0,
    )
    assert started["delegate_id"] == "d1"
    checkpoint = store.get("openai:test-delegate")
    assert checkpoint is not None
    assert checkpoint["runtime"]["delegates"]["d1"]["status"] == "running"
    assert checkpoint["runtime"]["last_tool"] == "delegate_task"

    status = _call(server.delegate_status, delegate_id="d1")
    assert status["delegate"]["status"] == "succeeded"
    checkpoint = store.get("openai:test-delegate")
    assert checkpoint is not None
    saved = checkpoint["runtime"]["delegates"]["d1"]
    assert saved["status"] == "succeeded"
    assert saved["terminal"] is True
    assert saved["summary"] == "review complete"
    assert saved["logs"]["stdout"] == str(tmp_path / "stdout.log")
    assert saved["structured_output"] == {
        "status": "succeeded",
        "findings": ["none"],
    }
    assert saved["model"] == "gemini-3.8-flash"
    assert saved["reasoning_effort"] == "high"
    assert checkpoint["runtime"]["last_tool"] == "delegate_status"


def test_delegate_checkpoint_omits_large_structured_output_but_keeps_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )

    class FakeRegistry:
        def delegate_status(self, **kwargs):
            return {
                "success": True,
                "delegate": {
                    "success": False,
                    "status": "failed",
                    "completed": True,
                    "delegate_id": "large",
                    "cwd": str(tmp_path),
                    "harness": "antigravity",
                    "summary": "x" * 5000,
                    "error": {
                        "code": "antigravity_quota_exhausted",
                        "retryable": True,
                    },
                    "logs": {"stdout": "/tmp/agy/stdout.log"},
                    "structured_output": {"blob": "y" * 20000},
                },
            }

    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(
        session_continuation.session,
        "get_current_session_id",
        lambda: "openai:test-large-delegate",
    )
    store.record_runtime(
        session_key="openai:test-large-delegate",
        last_tool="delegate_task",
        delegates={
            "large": {
                "status": "running",
                "completed": False,
                "terminal": False,
                "cwd": str(tmp_path),
            }
        },
    )

    _call(server.delegate_status, delegate_id="large")
    checkpoint = store.get("openai:test-large-delegate")
    assert checkpoint is not None
    saved = checkpoint["runtime"]["delegates"]["large"]
    assert len(saved["summary"]) == 4096
    assert saved["summary_truncated"] is True
    assert saved["error"]["code"] == "antigravity_quota_exhausted"
    assert saved["logs"]["stdout"] == "/tmp/agy/stdout.log"
    assert saved["structured_output_omitted"] is True
    assert "structured_output" not in saved


def test_delegate_batch_maps_to_project_scoped_registry(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeRegistry:
        def run_delegate_batch(self, **kwargs):
            captured.update(kwargs)
            return {"success": True, "status": "running"}

    monkeypatch.setattr(server, "registry", FakeRegistry())

    result = _call(
        server.delegate_batch,
        tasks=[{"task": "inspect A"}, {"task": "inspect B"}],
        cwd=str(tmp_path),
        harness="claude",
        max_concurrency=2,
        wait_seconds=0,
    )

    assert result["success"] is True
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["harness"] == "claude"
    assert captured["max_concurrency"] == 2
    assert len(captured["tasks"]) == 2


def test_delegate_status_and_cancel_forward_exact_filters(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeRegistry:
        def delegate_status(self, **kwargs):
            calls.append(("status", kwargs))
            return {"success": True}

        def delegate_cancel(self, **kwargs):
            calls.append(("cancel", kwargs))
            return {"success": True}

    monkeypatch.setattr(server, "registry", FakeRegistry())

    assert _call(server.delegate_status, delegate_id="d1", watch_seconds=3)["success"] is True
    assert _call(server.delegate_cancel, group_id="g1")["success"] is True

    status_args = calls[0][1]
    cancel_args = calls[1][1]
    assert status_args["delegate_id"] == "d1"
    assert status_args["watch_seconds"] == 3
    assert status_args["max_tokens"] == server._tool_context.tool_output_token_budget
    assert cancel_args == {
        "delegate_id": None,
        "group_id": "g1",
        "force_running": False,
    }

    assert _call(
        server.delegate_cancel,
        delegate_id="d2",
        force_running=True,
    )["success"] is True
    assert calls[-1][1] == {
        "delegate_id": "d2",
        "group_id": None,
        "force_running": True,
    }


def test_delegate_harnesses_reports_registry_capabilities(monkeypatch) -> None:
    class FakeRegistry:
        def harness_info(self):
            return {
                "claude": {"available": True, "read_only_supported": True},
                "antigravity": {"available": True, "read_only_supported": True},
            }

        def routing_guidance(self):
            return {
                "automatic_routing": False,
                "profiles": {
                    "bounded_explore": {"preferred_harness": "claude"},
                },
            }

        def runtime_info(self):
            return {
                "status": "ready",
                "tasks": {"total": 0},
                "groups": {"total": 0},
                "last_recovery": None,
            }

    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(server, "DELEGATE_DEFAULT_HARNESS", "antigravity")

    result = _call(server.delegate_harnesses)

    assert result["success"] is True
    assert result["default_harness"] == "antigravity"
    assert set(result["harnesses"]) == {"claude", "antigravity"}
    assert result["routing"]["automatic_routing"] is False
    assert result["routing"]["profiles"]["bounded_explore"]["preferred_harness"] == "claude"
    assert result["runtime"]["status"] == "ready"
    assert result["runtime"]["tasks"]["total"] == 0


def test_delegate_task_fails_closed_when_session_ownership_cannot_persist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cleanup_calls: list[dict[str, object]] = []

    class FailingCheckpointStore:
        def reserve_claim_capacity(self, *_args, **_kwargs):
            return "claim-delegate-failure"

        def release_claim_reservation(self, *_args, **_kwargs):
            return True

        def record_runtime(self, **_kwargs):
            raise OSError("checkpoint unavailable")

    class FakeRegistry:
        def run_delegate(self, **kwargs):
            before_submit = kwargs.get("before_submit")
            if callable(before_submit):
                admission_error = before_submit()
                if admission_error is not None:
                    return admission_error
            return {
                "success": True,
                "delegate_id": "delegate_owned_fail",
                "status": "running",
                "completed": False,
                "cwd": str(tmp_path),
            }

        def delegate_cancel(self, **kwargs):
            cleanup_calls.append(kwargs)
            return {
                "success": True,
                "delegate": {
                    "delegate_id": kwargs["delegate_id"],
                    "status": "cancelled",
                    "completed": True,
                },
            }

    monkeypatch.setattr(server, "checkpoint_store", FailingCheckpointStore())
    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(
        session_continuation.session,
        "get_current_session_id",
        lambda: "openai:delegate-ownership-failure",
    )

    result = _call(
        server.delegate_task,
        task="review slice",
        cwd=str(tmp_path),
        harness="antigravity",
        kind="explore",
        wait_seconds=0,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "session_ownership_persistence_failed"
    assert result["delegate_id"] == "delegate_owned_fail"
    assert cleanup_calls == [
        {
            "delegate_id": "delegate_owned_fail",
            "group_id": None,
            "reason": "ownership_cleanup",
        }
    ]
    assert result["cleanup"]["delegate"]["status"] == "cancelled"


def test_delegate_batch_fails_closed_when_child_ownership_cannot_persist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cleanup_calls: list[dict[str, object]] = []

    class FailingCheckpointStore:
        def reserve_claim_capacity(self, *_args, **_kwargs):
            return "claim-batch-failure"

        def release_claim_reservation(self, *_args, **_kwargs):
            return True

        def record_runtime(self, **_kwargs):
            raise OSError("checkpoint unavailable")

    class FakeRegistry:
        def run_delegate_batch(self, **_kwargs):
            return {
                "success": True,
                "group_id": "group_owned_fail",
                "status": "running",
                "children": [
                    {
                        "delegate_id": "child_owned_fail",
                        "status": "running",
                        "completed": False,
                    }
                ],
            }

        def delegate_cancel(self, **kwargs):
            cleanup_calls.append(kwargs)
            return {
                "success": True,
                "group": {
                    "group_id": kwargs["group_id"],
                    "status": "failed",
                    "completed": True,
                },
            }

    monkeypatch.setattr(server, "checkpoint_store", FailingCheckpointStore())
    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(
        session_continuation.session,
        "get_current_session_id",
        lambda: "openai:batch-ownership-failure",
    )

    result = _call(
        server.delegate_batch,
        tasks=[{"task": "inspect A"}],
        cwd=str(tmp_path),
        harness="antigravity",
        wait_seconds=0,
    )

    assert result["success"] is False
    assert result["error"]["code"] == "session_ownership_persistence_failed"
    assert result["group_id"] == "group_owned_fail"
    assert cleanup_calls == [
        {
            "delegate_id": None,
            "group_id": "group_owned_fail",
            "reason": "ownership_cleanup",
        }
    ]
    assert result["cleanup"]["group"]["completed"] is True


def test_delegate_launches_reject_before_start_when_ownership_admission_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    requested_slots: list[int] = []

    class FullCheckpointStore:
        def reserve_claim_capacity(self, _session_key, *, slots=1):
            requested_slots.append(slots)
            raise ValueError("session ownership capacity exceeded")

    class FakeRegistry:
        def run_delegate(self, **kwargs):
            before_submit = kwargs.get("before_submit")
            if callable(before_submit):
                admission_error = before_submit()
                if admission_error is not None:
                    return admission_error
            calls.append("task")
            return {"success": True, "delegate_id": "should-not-start"}

        def run_delegate_batch(self, **_kwargs):
            calls.append("batch")
            return {"success": True, "group_id": "should-not-start"}

    monkeypatch.setattr(server, "checkpoint_store", FullCheckpointStore())
    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(
        session_continuation.session,
        "get_current_session_id",
        lambda: "openai:delegate-ownership-full",
    )

    task_result = _call(
        server.delegate_task,
        task="review slice",
        cwd=str(tmp_path),
        harness="antigravity",
        kind="explore",
        wait_seconds=0,
    )
    batch_result = _call(
        server.delegate_batch,
        tasks=[{"task": "inspect A"}, {"task": "inspect B"}],
        cwd=str(tmp_path),
        harness="antigravity",
        wait_seconds=0,
    )

    assert task_result["error"]["code"] == "session_ownership_admission_failed"
    assert batch_result["error"]["code"] == "session_ownership_admission_failed"
    assert requested_slots == [1, 2]
    assert calls == []


def test_delegate_cancel_records_terminal_result_in_session_inbox(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:cancel-owner",
        last_tool="delegate_task",
        delegates={
            "delegate_cancelled": {
                "status": "running",
                "terminal": False,
            }
        },
    )

    class FakeRegistry:
        def delegate_cancel(self, **kwargs):
            return {
                "success": True,
                "delegate": {
                    "delegate_id": kwargs["delegate_id"],
                    "status": "cancelled",
                    "completed": True,
                    "success": False,
                },
            }

    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(
        session_continuation.session,
        "get_current_session_id",
        lambda: "openai:cancel-owner",
    )

    cancelled = _call(server.delegate_cancel, delegate_id="delegate_cancelled")
    assert cancelled["delegate"]["status"] == "cancelled"
    pending = store.pending_results("openai:cancel-owner")
    assert len(pending) == 1
    assert pending[0]["kind"] == "delegate"
    assert pending[0]["id"] == "delegate_cancelled"
    assert pending[0]["status"] == "cancelled"


def test_delegate_group_cancel_records_children_not_group_as_pending_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:group-cancel-owner",
        last_tool="delegate_batch",
        delegates={
            "child_a": {"status": "running", "terminal": False},
            "child_b": {"status": "running", "terminal": False},
        },
    )

    class FakeRegistry:
        def delegate_cancel(self, **kwargs):
            return {
                "success": True,
                "group": {
                    "group_id": kwargs["group_id"],
                    "status": "failed",
                    "completed": True,
                    "children": [
                        {
                            "delegate_id": "child_a",
                            "status": "cancelled",
                            "completed": True,
                            "success": False,
                        },
                        {
                            "delegate_id": "child_b",
                            "status": "cancelled",
                            "completed": True,
                            "success": False,
                        },
                    ],
                },
            }

    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(
        session_continuation.session,
        "get_current_session_id",
        lambda: "openai:group-cancel-owner",
    )

    cancelled = _call(server.delegate_cancel, group_id="group_1")
    assert cancelled["group"]["completed"] is True
    pending = store.pending_results("openai:group-cancel-owner")
    assert [(item["kind"], item["id"]) for item in pending] == [
        ("delegate", "child_a"),
        ("delegate", "child_b"),
    ]


def test_delegate_tools_reject_foreign_owned_delegate_and_group(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    store.record_runtime(
        session_key="openai:owner-a",
        last_tool="delegate_batch",
        delegates={
            "delegate_foreign": {
                "status": "succeeded",
                "completed": True,
                "terminal": True,
                "group_id": "group_foreign",
            }
        },
    )

    calls: list[tuple[str, dict[str, object]]] = []

    class FakeRegistry:
        def delegate_status(self, **kwargs):
            calls.append(("status", kwargs))
            return {"success": True}

        def delegate_cancel(self, **kwargs):
            calls.append(("cancel", kwargs))
            return {"success": True}

    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(
        session_continuation.session,
        "get_current_session_id",
        lambda: "openai:owner-b",
    )

    results = [
        _call(server.delegate_status, delegate_id="delegate_foreign"),
        _call(server.delegate_status, group_id="group_foreign"),
        _call(server.delegate_cancel, delegate_id="delegate_foreign"),
        _call(server.delegate_cancel, group_id="group_foreign"),
    ]

    assert calls == []
    assert all(
        result["error"]["code"] == "result_owned_by_another_session"
        for result in results
    )
    assert store.result_ownership_scope(
        "openai:owner-b",
        kind="delegate",
        result_id="delegate_foreign",
    ) == "owned_elsewhere"
    assert store.delegate_group_ownership_scope(
        "openai:owner-b",
        group_id="group_foreign",
    ) == "owned_elsewhere"


def test_delegate_tools_reject_live_foreign_owner_before_checkpoint_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeRegistry:
        def delegate_ownership_scope(self, **kwargs):
            assert kwargs["logical_session_id"] == "openai:owner-b"
            if kwargs.get("delegate_id") == "live_foreign":
                return "owned_elsewhere"
            if kwargs.get("group_id") == "live_group":
                return "owned_elsewhere"
            return "unowned"

        def foreign_delegate_ids(self, **kwargs):
            assert kwargs["logical_session_id"] == "openai:owner-b"
            return {"live_foreign"}

        def delegate_status(self, **kwargs):
            calls.append(("status", kwargs))
            return {"success": True, "active_delegates": []}

        def delegate_cancel(self, **kwargs):
            calls.append(("cancel", kwargs))
            return {"success": True}

    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(
        session_continuation.session,
        "get_current_session_id",
        lambda: "openai:owner-b",
    )

    denied = [
        _call(server.delegate_status, delegate_id="live_foreign"),
        _call(server.delegate_status, group_id="live_group"),
        _call(server.delegate_cancel, delegate_id="live_foreign"),
        _call(server.delegate_cancel, group_id="live_group"),
    ]
    assert calls == []
    assert all(
        result["error"]["code"] == "result_owned_by_another_session"
        for result in denied
    )

    discovered = _call(server.delegate_status)
    assert discovered["success"] is True
    assert len(calls) == 1
    assert calls[0][0] == "status"
    assert "live_foreign" in calls[0][1]["exclude_delegate_ids"]


def test_delegate_status_does_not_claim_legacy_unowned_delegate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SessionCheckpointStore(
        path=tmp_path / "session-checkpoints.json",
        ttl_seconds=86400,
    )

    class FakeRegistry:
        def delegate_status(self, **kwargs):
            return {
                "success": True,
                "delegate": {
                    "delegate_id": kwargs["delegate_id"],
                    "status": "succeeded",
                    "completed": True,
                    "success": True,
                },
            }

    monkeypatch.setattr(server, "checkpoint_store", store)
    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(
        session_continuation.session,
        "get_current_session_id",
        lambda: "openai:observer",
    )

    result = _call(server.delegate_status, delegate_id="legacy_unowned")
    assert result["delegate"]["status"] == "succeeded"
    assert store.get("openai:observer") is None
    assert store.result_ownership_scope(
        "openai:observer",
        kind="delegate",
        result_id="legacy_unowned",
    ) == "unowned"


def test_delegate_task_tool_description_specifies_worktree_writer_lane() -> None:
    import asyncio

    tool = asyncio.run(server.mcp.get_tool("delegate_task"))
    assert tool is not None
    assert (
        "uses a worktree-scoped exclusive writer lane with repository-level concurrency cap"
        in tool.description
    )
    assert "project-scoped exclusive writer lane" not in tool.description
    verification_description = tool.parameters["properties"]["verification_commands"]["description"]
    assert "Server-owned acceptance checks" in verification_description
    assert "coding agent must not run these exact declared commands" in verification_description
