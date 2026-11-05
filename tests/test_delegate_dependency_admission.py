import pytest

from chatgpt_web_oauth_mcp.executors import ExecutorRegistry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    registry = ExecutorRegistry(
        codex_command="true", allow_unsafe_explore_command=True, automatic_routing=False,
        delegate_state_root=tmp_path / "state",
    )
    # Synchronously inspect transitions without launching external runners.
    monkeypatch.setattr(registry.scheduler, "_start_threads", lambda tasks: None)
    monkeypatch.setattr(registry.scheduler, "runner", lambda task: {"status": "succeeded"})
    return registry


def test_reader_barrier_skips_blocked_reader_and_preserves_writer_fifo(registry, tmp_path):
    scheduler = registry.scheduler
    batch = registry.run_codex_batch(tasks=[{"task": "upstream"}], cwd=tmp_path, wait_seconds=0)
    group = scheduler.get_group(batch["group_id"])

    def submit(name, kind="explore", **kwargs):
        result = registry.run_codex(task=name, cwd=tmp_path, kind=kind, wait_seconds=0, **kwargs)
        return scheduler.get_task(result["delegate_id"])

    reader = submit("review after upstream", work_role="review", depends_on_group_ids=[group.group_id])
    independent = submit("independent reader")
    writer = submit("writer", kind="code")
    late = submit("late reader")
    assert [reader.state, independent.state, writer.state, late.state] == [
        "queued", "running", "queued", "queued",
    ]
    scheduler._execute_task(independent)
    upstream = scheduler.get_task(group.child_ids[0])
    scheduler._execute_task(upstream, runner=lambda task: {"status": "failed"})
    assert group.state == "failed" and reader.state == "running"
    assert writer.state == late.state == "queued"
    scheduler._execute_task(reader)
    assert writer.state == "running" and late.state == "queued"
    scheduler._execute_task(writer)
    assert late.state == "running"


def test_group_join_rejects_cycles_before_reserving_or_registering(registry, tmp_path):
    def batch(name):
        result = registry.run_codex_batch(tasks=[{"task": name}], cwd=tmp_path, wait_seconds=0)
        return registry.scheduler.get_group(result["group_id"])

    a, b = batch("A"), batch("B")

    def join(name, group, dependencies=(), **kwargs):
        return registry.run_codex(
            task=name, cwd=tmp_path, kind="explore", group_id=group.group_id,
            depends_on_group_ids=list(dependencies), wait_seconds=0, **kwargs,
        )

    def reject(name, group, dependencies=()):
        children = list(group.child_ids)
        result = join(name, group, dependencies, before_submit=lambda: pytest.fail("reserved rejected join"))
        assert result["error"]["code"] == "delegate_group_dependency_conflict"
        assert group.child_ids == children

    reject("self", a, [a.group_id])
    waiting = join("B waits for A", b, [a.group_id])
    assert waiting["status"] == "queued"
    reject("A waits for B", a, [b.group_id])
    # An unblocked reader may still join A while B waits.
    assert join("additional A reader", a)["status"] == "running"
    writer = registry.run_codex(
        task="writer waiting for A", kind="code", cwd=tmp_path,
        depends_on_group_ids=[a.group_id], wait_seconds=0,
    )
    assert writer["status"] == "queued"
    reject("A child behind the writer that waits for A", a)
