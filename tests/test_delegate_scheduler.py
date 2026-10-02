from __future__ import annotations

import threading
import time
from pathlib import Path

from chatgpt_web_oauth_mcp.delegate_models import DelegateTask, ProjectIdentity
from chatgpt_web_oauth_mcp.executors import (
    DEFAULT_DELEGATE_HISTORY_LIMIT,
    DEFAULT_DELEGATE_SCHEDULER_TERMINAL_LIMIT,
    ExecutorRegistry,
)


def _install_timed_runner(
    registry: ExecutorRegistry,
    monkeypatch,
    delays: dict[str, float],
    failures: set[str] | None = None,
) -> tuple[dict[str, tuple[float, float]], threading.Lock]:
    intervals: dict[str, tuple[float, float]] = {}
    lock = threading.Lock()
    failures = failures or set()

    def run(*, delegate_task: DelegateTask) -> dict[str, object]:
        name = delegate_task.task or delegate_task.goal or delegate_task.delegate_id
        started = time.monotonic()
        time.sleep(delays.get(name, 0.05))
        finished = time.monotonic()
        with lock:
            intervals[name] = (started, finished)
        status = "failed" if name in failures else "succeeded"
        return {
            "success": status == "succeeded",
            "status": status,
            "completed": True,
            "in_progress": False,
            "delegate_id": delegate_task.delegate_id,
            "kind": delegate_task.kind,
            "group_id": delegate_task.group_id,
        }

    monkeypatch.setattr(registry, "_start_codex_delegate_impl", run)
    return intervals, lock


def _wait_task(registry: ExecutorRegistry, delegate_id: str, timeout: float = 3) -> DelegateTask:
    task = registry.scheduler.get_task(delegate_id)
    assert task is not None
    assert task.completed_event.wait(timeout=timeout)
    return task


def _make_code_task_for_project(
    registry: ExecutorRegistry,
    *,
    name: str,
    project: ProjectIdentity,
    cwd: Path,
) -> DelegateTask:
    return registry._make_task(
        harness="codex",
        project=project,
        cwd=cwd,
        kind="code",
        task=name,
        goal=None,
        task_id=None,
        group_id=None,
        model="default",
        reasoning_effort="default",
        sandbox_mode="danger-full-access",
        commit_mode="forbidden",
        execution_timeout_seconds=30,
        depends_on_group_ids=(),
        files_in_scope=[],
        out_of_scope=[],
        context_files=[],
        acceptance_criteria=[],
        done_means=[],
        verification_commands=[],
        output_schema=None,
        parse_structured_output=True,
        request_fingerprint=name,
    )


def test_same_project_fair_reader_writer_scheduling(tmp_path: Path, monkeypatch) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        max_explore_per_project=4,
        allow_unsafe_explore_command=True,
    )
    intervals, _ = _install_timed_runner(
        registry,
        monkeypatch,
        {"reader-1": 0.15, "reader-2": 0.15, "writer": 0.05, "late-reader": 0.05},
    )

    reader_1 = registry.run_codex(task="reader-1", kind="explore", cwd=tmp_path, wait_seconds=0)
    reader_2 = registry.run_codex(task="reader-2", kind="explore", cwd=tmp_path, wait_seconds=0)
    writer = registry.run_codex(task="writer", kind="code", cwd=tmp_path, wait_seconds=0)
    late_reader = registry.run_codex(task="late-reader", kind="explore", cwd=tmp_path, wait_seconds=0)

    assert writer["status"] == "queued"
    assert late_reader["status"] == "queued"
    for result in (reader_1, reader_2, writer, late_reader):
        _wait_task(registry, str(result["delegate_id"]))

    assert intervals["reader-1"][0] < intervals["reader-2"][1]
    assert intervals["reader-2"][0] < intervals["reader-1"][1]
    assert intervals["writer"][0] >= max(intervals["reader-1"][1], intervals["reader-2"][1])
    assert intervals["late-reader"][0] >= intervals["writer"][1]


def test_code_tasks_in_different_projects_overlap(tmp_path: Path, monkeypatch) -> None:
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()
    registry = ExecutorRegistry(
        codex_command="true",
        max_code_global=2,
        allow_unsafe_explore_command=True,
    )
    intervals, _ = _install_timed_runner(
        registry,
        monkeypatch,
        {"code-a": 0.15, "code-b": 0.15},
    )

    results: list[dict[str, object]] = []
    threads = [
        threading.Thread(
            target=lambda name=name, cwd=cwd: results.append(
                registry.run_codex(task=name, kind="code", cwd=cwd, wait_seconds=1)
            )
        )
        for name, cwd in (("code-a", project_a), ("code-b", project_b))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(results) == 2
    assert intervals["code-a"][0] < intervals["code-b"][1]
    assert intervals["code-b"][0] < intervals["code-a"][1]


def test_code_tasks_in_linked_worktrees_overlap_when_repo_limit_allows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worktree_a = tmp_path / "worktree-a"
    worktree_b = tmp_path / "worktree-b"
    worktree_a.mkdir()
    worktree_b.mkdir()
    common = tmp_path / "shared.git"
    repo_key = str(common)
    project_a = ProjectIdentity(repo_key, worktree_a, common, str(worktree_a))
    project_b = ProjectIdentity(repo_key, worktree_b, common, str(worktree_b))
    registry = ExecutorRegistry(
        codex_command="true",
        max_code_global=2,
        allow_unsafe_explore_command=True,
    )
    assert registry.scheduler.max_code_per_project == 2
    intervals, _ = _install_timed_runner(
        registry,
        monkeypatch,
        {"writer-a": 0.15, "writer-b": 0.15},
    )
    first = _make_code_task_for_project(
        registry,
        name="writer-a",
        project=project_a,
        cwd=worktree_a,
    )
    second = _make_code_task_for_project(
        registry,
        name="writer-b",
        project=project_b,
        cwd=worktree_b,
    )

    registry.scheduler.submit_task(first)
    registry.scheduler.submit_task(second)
    assert first.completed_event.wait(timeout=2)
    assert second.completed_event.wait(timeout=2)

    assert intervals["writer-a"][0] < intervals["writer-b"][1]
    assert intervals["writer-b"][0] < intervals["writer-a"][1]
    assert project_a.scheduler_lane_key != project_b.scheduler_lane_key
    assert project_a.repo_key == project_b.repo_key


def test_code_tasks_in_linked_worktrees_respect_repo_writer_limit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    worktree_a = tmp_path / "worktree-a"
    worktree_b = tmp_path / "worktree-b"
    worktree_a.mkdir()
    worktree_b.mkdir()
    common = tmp_path / "shared.git"
    repo_key = str(common)
    project_a = ProjectIdentity(repo_key, worktree_a, common, str(worktree_a))
    project_b = ProjectIdentity(repo_key, worktree_b, common, str(worktree_b))
    registry = ExecutorRegistry(
        codex_command="true",
        max_code_per_project=1,
        max_code_global=2,
        allow_unsafe_explore_command=True,
    )
    intervals, _ = _install_timed_runner(
        registry,
        monkeypatch,
        {"writer-a": 0.15, "writer-b": 0.05},
    )
    first = _make_code_task_for_project(
        registry,
        name="writer-a",
        project=project_a,
        cwd=worktree_a,
    )
    second = _make_code_task_for_project(
        registry,
        name="writer-b",
        project=project_b,
        cwd=worktree_b,
    )

    registry.scheduler.submit_task(first)
    registry.scheduler.submit_task(second)
    assert second.state == "queued"
    assert first.completed_event.wait(timeout=2)
    assert second.completed_event.wait(timeout=2)

    assert intervals["writer-b"][0] >= intervals["writer-a"][1]


def test_code_dependency_waits_for_complete_explore_group(tmp_path: Path, monkeypatch) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        max_explore_per_project=2,
        allow_unsafe_explore_command=True,
    )
    intervals, _ = _install_timed_runner(
        registry,
        monkeypatch,
        {"scan-a": 0.05, "scan-b": 0.15, "dependent-code": 0.01},
    )

    group = registry.run_codex_batch(
        tasks=[{"task": "scan-a"}, {"task": "scan-b"}],
        cwd=tmp_path,
        wait_seconds=0,
    )
    code = registry.run_codex(
        task="dependent-code",
        kind="code",
        cwd=tmp_path,
        depends_on_group_ids=[str(group["group_id"])],
        wait_seconds=0,
    )

    assert code["status"] == "queued"
    _wait_task(registry, str(code["delegate_id"]))
    assert intervals["dependent-code"][0] >= max(
        intervals["scan-a"][1], intervals["scan-b"][1]
    )


def test_terminal_scheduler_memory_is_bounded_and_recent_status_survives_eviction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        allow_unsafe_explore_command=True,
    )
    _install_timed_runner(registry, monkeypatch, {})
    delegate_ids: list[str] = []

    for index in range(DEFAULT_DELEGATE_HISTORY_LIMIT):
        result = registry.run_codex(
            task=f"memory-{index}",
            kind="code",
            cwd=tmp_path,
            wait_seconds=1,
        )
        assert result["status"] == "succeeded"
        delegate_ids.append(str(result["delegate_id"]))

    runtime = registry.runtime_info()
    memory = runtime["memory"]
    assert memory["scheduler_tasks"] == DEFAULT_DELEGATE_SCHEDULER_TERMINAL_LIMIT
    assert memory["task_history"] == DEFAULT_DELEGATE_HISTORY_LIMIT
    assert memory["scheduler_lanes"] == 0
    assert memory["last_prune"]["tasks_evicted"] >= 1

    evicted_id = delegate_ids[0]
    assert registry.scheduler.get_task(evicted_id) is None
    status = registry.delegate_status(delegate_id=evicted_id)["delegate"]
    assert status["delegate_id"] == evicted_id
    assert status["status"] == "succeeded"
    cancelled = registry.delegate_cancel(delegate_id=evicted_id)["delegate"]
    assert cancelled["delegate_id"] == evicted_id
    assert cancelled["status"] == "succeeded"


def test_terminal_group_memory_is_bounded_and_recent_group_status_survives_eviction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        allow_unsafe_explore_command=True,
    )
    _install_timed_runner(registry, monkeypatch, {})
    group_ids: list[str] = []

    for index in range(DEFAULT_DELEGATE_HISTORY_LIMIT):
        result = registry.run_codex_batch(
            tasks=[{"task": f"group-memory-{index}"}],
            cwd=tmp_path,
            wait_seconds=1,
        )
        assert result["status"] == "succeeded"
        group_ids.append(str(result["group_id"]))

    runtime = registry.runtime_info()
    memory = runtime["memory"]
    assert memory["scheduler_groups"] == DEFAULT_DELEGATE_SCHEDULER_TERMINAL_LIMIT
    assert memory["group_history"] == DEFAULT_DELEGATE_HISTORY_LIMIT
    assert memory["scheduler_lanes"] == 0

    evicted_group_id = group_ids[0]
    assert registry.scheduler.get_group(evicted_group_id) is None
    status = registry.delegate_status(group_id=evicted_group_id)["group"]
    assert status["group_id"] == evicted_group_id
    assert status["status"] == "succeeded"
    assert status["completed"] is True
    cancelled = registry.delegate_cancel(group_id=evicted_group_id)["group"]
    assert cancelled["group_id"] == evicted_group_id
    assert cancelled["status"] == "succeeded"


def test_prune_preserves_completed_dependency_group_until_dependent_finishes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        allow_unsafe_explore_command=True,
    )
    _install_timed_runner(
        registry,
        monkeypatch,
        {"dependency-source": 0.01, "dependent-long": 0.3},
    )
    group = registry.run_codex_batch(
        tasks=[{"task": "dependency-source"}],
        cwd=tmp_path,
        wait_seconds=1,
    )
    group_id = str(group["group_id"])
    dependent = registry.run_codex(
        task="dependent-long",
        kind="code",
        cwd=tmp_path,
        depends_on_group_ids=[group_id],
        wait_seconds=0,
    )
    dependent_id = str(dependent["delegate_id"])

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        task = registry.scheduler.get_task(dependent_id)
        if task is not None and task.state == "running":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("dependent task did not start")

    protected = registry.scheduler.prune_terminal(
        max_terminal_tasks=0,
        max_terminal_groups=0,
    )
    assert protected["groups_evicted"] == 0
    assert registry.scheduler.get_group(group_id) is not None

    _wait_task(registry, dependent_id)
    pruned = registry.scheduler.prune_terminal(
        max_terminal_tasks=0,
        max_terminal_groups=0,
    )
    assert pruned["groups_evicted"] == 1
    assert registry.scheduler.get_group(group_id) is None


def test_adopt_running_code_conflict_is_atomic(tmp_path: Path) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        allow_unsafe_explore_command=True,
    )
    project = registry.project_resolver.resolve(tmp_path)

    def make_task(name: str) -> DelegateTask:
        return registry._make_task(
            harness="codex",
            project=project,
            cwd=tmp_path,
            kind="code",
            task=name,
            goal=None,
            task_id=None,
            group_id=None,
            model="default",
            reasoning_effort="default",
            sandbox_mode="danger-full-access",
            commit_mode="allowed",
            execution_timeout_seconds=30,
            depends_on_group_ids=(),
            files_in_scope=[],
            out_of_scope=[],
            context_files=[],
            acceptance_criteria=[],
            done_means=[],
            verification_commands=[],
            output_schema=None,
            parse_structured_output=True,
            request_fingerprint=name,
        )

    release = threading.Event()

    def recovered_runner(task: DelegateTask) -> dict[str, object]:
        release.wait(timeout=1)
        return {
            "success": True,
            "status": "succeeded",
            "completed": True,
            "in_progress": False,
            "delegate_id": task.delegate_id,
        }

    first = make_task("first-recovered-writer")
    second = make_task("second-recovered-writer")
    registry.scheduler.adopt_running_task(first, runner=recovered_runner)

    try:
        registry.scheduler.adopt_running_task(second, runner=recovered_runner)
    except ValueError:
        pass
    else:
        raise AssertionError("second recovered writer must be rejected")

    lane = registry.scheduler.lanes[project.project_key]
    assert registry.scheduler.get_task(first.delegate_id) is first
    assert registry.scheduler.get_task(second.delegate_id) is None
    assert lane.active_code is first
    assert registry.scheduler._active_code_global == 1

    release.set()
    assert first.completed_event.wait(timeout=1)


def test_delegate_cancel_preserves_normalized_reason(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registry = ExecutorRegistry(
        codex_command="true",
        max_code_global=1,
        allow_unsafe_explore_command=True,
    )
    _install_timed_runner(
        registry,
        monkeypatch,
        {"blocker": 0.2, "queued-cancel": 0.01},
    )
    blocker = registry.run_codex(
        task="blocker",
        kind="code",
        cwd=tmp_path,
        wait_seconds=0,
    )
    queued = registry.run_codex(
        task="queued-cancel",
        kind="code",
        cwd=tmp_path,
        wait_seconds=0,
    )
    assert queued["status"] == "queued"

    cancelled = registry.delegate_cancel(
        delegate_id=str(queued["delegate_id"]),
        reason="ownership_cleanup",
    )

    assert cancelled["delegate"]["status"] == "cancelled"
    assert cancelled["delegate"]["error"]["code"] == "ownership_cleanup"
    task = registry.scheduler.get_task(str(queued["delegate_id"]))
    assert task is not None
    assert task.cancel_reason == "ownership_cleanup"
    _wait_task(registry, str(blocker["delegate_id"]))
