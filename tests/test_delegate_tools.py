from __future__ import annotations

from pathlib import Path

from chatgpt_web_oauth_mcp import server


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
        wait_seconds=1,
    )

    assert result["success"] is True
    assert captured["cwd"] == tmp_path.resolve()
    assert captured["harness"] == "antigravity"
    assert captured["kind"] == "explore"
    assert captured["files_in_scope"] == ["src"]
    assert captured["acceptance_criteria"] == ["report findings"]
    assert captured["commit_mode"] == "required"


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
    assert cancel_args == {"delegate_id": None, "group_id": "g1"}


def test_delegate_harnesses_reports_registry_capabilities(monkeypatch) -> None:
    class FakeRegistry:
        def harness_info(self):
            return {
                "claude": {"available": True, "read_only_supported": True},
                "antigravity": {"available": True, "read_only_supported": True},
            }

    monkeypatch.setattr(server, "registry", FakeRegistry())
    monkeypatch.setattr(server, "DELEGATE_DEFAULT_HARNESS", "antigravity")

    result = _call(server.delegate_harnesses)

    assert result["success"] is True
    assert result["default_harness"] == "antigravity"
    assert set(result["harnesses"]) == {"claude", "antigravity"}
