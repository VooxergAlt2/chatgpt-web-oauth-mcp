from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterable

from .delegate_models import (
    TERMINAL_TASK_STATES,
    DelegateGroup,
    DelegateTask,
    ProjectIdentity,
    ProjectLane,
    TaskState,
)


TaskRunner = Callable[[DelegateTask], dict[str, object]]
TaskTerminator = Callable[[DelegateTask], None]
TerminalCallback = Callable[[DelegateTask], None]
CancelledResultFactory = Callable[[DelegateTask], dict[str, object]]
RunningPreserver = Callable[[DelegateTask], bool]


class DelegateQueueFullError(RuntimeError):
    def __init__(self, *, scope: str, limit: int) -> None:
        self.scope = scope
        self.limit = limit
        super().__init__(f"Delegate queue limit reached for {scope}: {limit}")


class DelegateSchedulerShuttingDownError(RuntimeError):
    """Raised when new delegate work arrives after shutdown has started."""


class DelegateScheduler:
    """Project-scoped fair reader/writer scheduler with global safety valves."""

    def __init__(
        self,
        *,
        runner: TaskRunner,
        terminator: TaskTerminator,
        on_terminal: TerminalCallback,
        cancelled_result_factory: CancelledResultFactory,
        max_explore_per_project: int = 4,
        max_analysis_per_project: int = 4,
        max_review_per_project: int = 2,
        max_explore_global: int = 8,
        max_explore_per_harness: dict[str, int] | None = None,
        max_active_per_harness: dict[str, int] | None = None,
        max_code_per_project: int = 2,
        max_code_global: int = 4,
        queue_limit_per_project: int = 32,
        queue_limit_global: int = 128,
    ) -> None:
        self.runner = runner
        self.terminator = terminator
        self.on_terminal = on_terminal
        self.cancelled_result_factory = cancelled_result_factory
        self.max_explore_per_project = max(1, int(max_explore_per_project))
        self.max_readers_per_role = {
            "analysis": max(1, int(max_analysis_per_project)),
            "review": max(1, int(max_review_per_project)),
        }
        self.max_explore_global = max(1, int(max_explore_global))
        self.max_explore_per_harness = {
            str(name).strip().lower(): max(1, int(limit))
            for name, limit in (max_explore_per_harness or {}).items()
            if str(name).strip()
        }
        self.max_active_per_harness = {
            str(name).strip().lower(): max(1, int(limit))
            for name, limit in (max_active_per_harness or {}).items()
            if str(name).strip()
        }
        self.max_code_per_project = max(1, int(max_code_per_project))
        self.max_code_global = max(1, int(max_code_global))
        self.queue_limit_per_project = max(1, int(queue_limit_per_project))
        self.queue_limit_global = max(1, int(queue_limit_global))

        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.tasks: dict[str, DelegateTask] = {}
        self.groups: dict[str, DelegateGroup] = {}
        self.lanes: dict[str, ProjectLane] = {}
        self._project_order: deque[str] = deque()
        self._active_explore_global = 0
        self._active_code_global = 0
        self._explore_harness_admissions: dict[str, str] = {}
        self._shutting_down = False

    @property
    def is_shutting_down(self) -> bool:
        with self.lock:
            return self._shutting_down

    def submit_task(self, task: DelegateTask) -> None:
        with self.lock:
            if self._shutting_down:
                raise DelegateSchedulerShuttingDownError(
                    "delegate scheduler is shutting down"
                )
            self._ensure_capacity_locked(task.project.project_key, 1)
            self._register_task_locked(task)
            starts = self._dispatch_locked()
            self.condition.notify_all()
        self._start_threads(starts)

    def submit_group(self, group: DelegateGroup, tasks: list[DelegateTask]) -> None:
        if not tasks:
            raise ValueError("delegate group requires at least one child task")
        if any(task.project.project_key != group.project.project_key for task in tasks):
            raise ValueError("all delegate group children must belong to the group project")
        with self.lock:
            if self._shutting_down:
                raise DelegateSchedulerShuttingDownError(
                    "delegate scheduler is shutting down"
                )
            self._ensure_capacity_locked(group.project.project_key, len(tasks))
            self.groups[group.group_id] = group
            for task in tasks:
                self._register_task_locked(task)
            self._refresh_group_locked(group)
            starts = self._dispatch_locked()
            self.condition.notify_all()
        self._start_threads(starts)

    def adopt_running_task(
        self,
        task: DelegateTask,
        *,
        runner: TaskRunner,
    ) -> bool:
        """Adopt already-running durable work without dispatching it again."""

        with self.lock:
            if self._shutting_down:
                raise DelegateSchedulerShuttingDownError(
                    "delegate scheduler is shutting down"
                )
            if task.delegate_id in self.tasks:
                return False
            lane_key = task.project.scheduler_lane_key
            lane = self.lanes.get(lane_key)
            if lane is None:
                lane = ProjectLane(project=task.project)
                self.lanes[lane_key] = lane
                self._project_order.append(lane_key)
            if task.kind != "explore" and lane.active_code is not None:
                raise ValueError(
                    "cannot adopt multiple running code delegates for one worktree"
                )
            # Already-running durable work is adopted even when a restart
            # lowered repository/global concurrency caps. Rejecting it would
            # orphan a live process. The active counters intentionally reflect
            # the oversubscription and normal dispatch stays blocked until the
            # running set falls back under configured limits.
            task.state = "running"
            self.tasks[task.delegate_id] = task
            if task.kind == "explore":
                lane.active_explores[task.delegate_id] = task
                self._active_explore_global += 1
                self._explore_harness_admissions[task.delegate_id] = (
                    task.harness.strip().lower()
                )
            else:
                lane.active_code = task
                self._active_code_global += 1
            self._refresh_task_group_locked(task)
            self.condition.notify_all()

        threading.Thread(
            target=self._execute_task,
            args=(task, runner),
            name=f"delegate-recovered-{task.delegate_id}",
            daemon=True,
        ).start()
        return True

    def restore_terminal_task(
        self,
        task: DelegateTask,
        *,
        result: dict[str, object],
    ) -> bool:
        """Restore one completed task without dispatching process work."""

        status = str(result.get("status") or "")
        if status not in TERMINAL_TASK_STATES:
            raise ValueError("restored terminal task requires a terminal result")
        with self.lock:
            if task.delegate_id in self.tasks:
                return False
            lane_key = task.project.scheduler_lane_key
            lane = self.lanes.get(lane_key)
            if lane is None:
                lane = ProjectLane(project=task.project)
                self.lanes[lane_key] = lane
                self._project_order.append(lane_key)
            task.state = status  # type: ignore[assignment]
            task.result = result
            # Rehydrate operator-visible state, but mark this as historical:
            # on_terminal must not recreate runtime cooldown/quota side effects.
            task.terminal_replayed_from_disk = True
            completed_at = result.get("completed_at_epoch")
            task.completed_at = (
                float(completed_at)
                if isinstance(completed_at, (int, float))
                and not isinstance(completed_at, bool)
                else time.time()
            )
            task.completed_event.set()
            self.tasks[task.delegate_id] = task
            self._refresh_task_group_locked(task)
            self.condition.notify_all()
        self.on_terminal(task)
        return True

    def restore_group(self, group: DelegateGroup) -> bool:
        """Restore a persisted group shell before or after its children."""

        with self.lock:
            if self._shutting_down:
                raise DelegateSchedulerShuttingDownError(
                    "delegate scheduler is shutting down"
                )
            if group.group_id in self.groups:
                return False
            if any(
                self.tasks[child_id].group_id != group.group_id
                for child_id in group.child_ids
                if child_id in self.tasks
            ):
                raise ValueError("restored group child has mismatched group_id")
            self.groups[group.group_id] = group
            self._refresh_group_locked(group)
            starts = self._dispatch_locked()
            self.condition.notify_all()
        self._start_threads(starts)
        return True

    def get_task(self, delegate_id: str) -> DelegateTask | None:
        with self.lock:
            return self.tasks.get(delegate_id)

    def get_group(self, group_id: str) -> DelegateGroup | None:
        with self.lock:
            return self.groups.get(group_id)

    def group_join_conflicts_with_barrier(
        self, group_id: str, project: ProjectIdentity, dependencies: tuple[str, ...],
    ) -> bool:
        """Under the admission lock, keep an awaited group able to finish.

        Independent readers may still join. Once another queued task waits for
        this group, do not add children behind an unfinished group or a queued
        writer: that barrier could itself be waiting for this group's completion.
        """
        if group_id in dependencies:
            return True
        if not any(
            task.state == "queued" and group_id in task.depends_on_group_ids
            for task in self.tasks.values()
        ):
            return False
        if not self._dependencies_complete_locked(dependencies):
            return True
        lane = self.lanes.get(project.scheduler_lane_key)
        return lane is not None and any(self.tasks[item].kind == "code" for item in lane.pending)

    def nonterminal_tasks(self) -> list[DelegateTask]:
        with self.lock:
            return [task for task in self.tasks.values() if not task.is_terminal]

    def tasks_for_project(self, project_key: str) -> list[DelegateTask]:
        with self.lock:
            return [task for task in self.tasks.values() if task.project.project_key == project_key]

    def task_counts(self, tasks: Iterable[DelegateTask]) -> dict[str, int]:
        counts = {
            "total": 0,
            "queued": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
            "timed_out": 0,
        }
        for task in tasks:
            counts["total"] += 1
            counts[task.state] += 1
        return counts

    def prune_terminal(
        self,
        *,
        max_terminal_tasks: int,
        max_terminal_groups: int,
    ) -> dict[str, int]:
        """Bound terminal scheduler memory without touching active dependency state."""

        task_limit = max(0, int(max_terminal_tasks))
        group_limit = max(0, int(max_terminal_groups))
        with self.lock:
            protected_group_ids = {
                dependency
                for task in self.tasks.values()
                if not task.is_terminal
                for dependency in task.depends_on_group_ids
            }
            terminal_groups: list[tuple[float, str]] = []
            for group in self.groups.values():
                if not group.completed_event.is_set():
                    continue
                child_tasks = [
                    self.tasks.get(child_id)
                    for child_id in group.child_ids
                ]
                completed_at = max(
                    (
                        child.completed_at or child.submitted_at
                        for child in child_tasks
                        if child is not None
                    ),
                    default=group.submitted_at,
                )
                terminal_groups.append((completed_at, group.group_id))
            terminal_groups.sort(reverse=True)

            retained_unprotected = 0
            evicted_groups = 0
            evicted_group_tasks = 0
            for _completed_at, group_id in terminal_groups:
                if group_id in protected_group_ids:
                    continue
                if retained_unprotected < group_limit:
                    retained_unprotected += 1
                    continue
                group = self.groups.pop(group_id, None)
                if group is None:
                    continue
                evicted_groups += 1
                for child_id in group.child_ids:
                    child = self.tasks.get(child_id)
                    if child is not None and child.is_terminal:
                        self.tasks.pop(child_id, None)
                        evicted_group_tasks += 1

            standalone_terminal = sorted(
                (
                    (task.completed_at or task.submitted_at, task.delegate_id)
                    for task in self.tasks.values()
                    if task.is_terminal and task.group_id is None
                ),
                reverse=True,
            )
            evicted_tasks = 0
            for _completed_at, delegate_id in standalone_terminal[task_limit:]:
                if self.tasks.pop(delegate_id, None) is not None:
                    evicted_tasks += 1

            referenced_lanes = {
                task.project.scheduler_lane_key
                for task in self.tasks.values()
                if not task.is_terminal
            }
            removed_lanes = 0
            for lane_key, lane in list(self.lanes.items()):
                if (
                    lane_key not in referenced_lanes
                    and not lane.pending
                    and not lane.active_explores
                    and lane.active_code is None
                ):
                    self.lanes.pop(lane_key, None)
                    removed_lanes += 1
            if removed_lanes:
                live_projects = set(self.lanes)
                self._project_order = deque(
                    lane_key
                    for lane_key in self._project_order
                    if lane_key in live_projects
                )
            self.condition.notify_all()
            return {
                "tasks_evicted": evicted_tasks + evicted_group_tasks,
                "standalone_tasks_evicted": evicted_tasks,
                "group_tasks_evicted": evicted_group_tasks,
                "groups_evicted": evicted_groups,
                "lanes_evicted": removed_lanes,
                "tasks_retained": len(self.tasks),
                "groups_retained": len(self.groups),
                "lanes_retained": len(self.lanes),
            }

    def cancel_task(
        self,
        delegate_id: str,
        *,
        reason: str = "cancelled",
        source: str | None = None,
        rationale: str | None = None,
    ) -> DelegateTask | None:
        running: DelegateTask | None = None
        completed: DelegateTask | None = None
        with self.lock:
            task = self.tasks.get(delegate_id)
            if task is None or task.is_terminal:
                return task
            with task.output_lock:
                task.cancel_requested = True
                task.cancel_reason = reason
                task.cancel_source = source
                task.cancel_rationale = rationale
            if task.state == "queued":
                lane = self.lanes[task.project.scheduler_lane_key]
                try:
                    lane.pending.remove(task.delegate_id)
                except ValueError:
                    pass
                task.state = "cancelled"
                task.completed_at = time.time()
                task.result = self.cancelled_result_factory(task)
                task.completed_event.set()
                self._refresh_task_group_locked(task)
                completed = task
                starts = self._dispatch_locked()
            else:
                running = task
                starts = []
            self.condition.notify_all()
        if completed is not None:
            self.on_terminal(completed)
        if running is not None:
            self.terminator(running)
        self._start_threads(starts)
        return task

    def cancel_group(
        self,
        group_id: str,
        *,
        reason: str = "cancelled",
        source: str | None = None,
        rationale: str | None = None,
    ) -> DelegateGroup | None:
        with self.lock:
            group = self.groups.get(group_id)
            child_ids = list(group.child_ids) if group else []
        if group is None:
            return None
        for delegate_id in child_ids:
            self.cancel_task(
                delegate_id,
                reason=reason,
                source=source,
                rationale=rationale,
            )
        return group

    def shutdown(
        self,
        *,
        reason: str = "server_shutdown",
        wait_seconds: float = 10.0,
        preserve_running: RunningPreserver | None = None,
    ) -> dict[str, object]:
        """Stop accepting work, preserving explicitly durable running delegates."""

        queued: list[DelegateTask] = []
        running: list[DelegateTask] = []
        preserved: list[DelegateTask] = []
        with self.lock:
            self._shutting_down = True
            for task in self.tasks.values():
                if task.is_terminal:
                    continue
                if (
                    task.state == "running"
                    and preserve_running is not None
                    and preserve_running(task)
                ):
                    preserved.append(task)
                    continue
                with task.output_lock:
                    task.cancel_requested = True
                    task.cancel_reason = reason
                    task.cancel_source = "server_shutdown"
                    task.cancel_rationale = None
                if task.state == "queued":
                    lane = self.lanes[task.project.scheduler_lane_key]
                    try:
                        lane.pending.remove(task.delegate_id)
                    except ValueError:
                        pass
                    task.state = "cancelled"
                    task.completed_at = time.time()
                    task.result = self.cancelled_result_factory(task)
                    task.completed_event.set()
                    self._refresh_task_group_locked(task)
                    queued.append(task)
                elif task.state == "running":
                    running.append(task)
            self.condition.notify_all()

        for task in queued:
            self.on_terminal(task)

        terminators = [
            threading.Thread(
                target=self.terminator,
                args=(task,),
                name=f"delegate-shutdown-{task.delegate_id}",
                daemon=True,
            )
            for task in running
        ]
        for thread in terminators:
            thread.start()

        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        for task in running:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            task.completed_event.wait(timeout=remaining)
        for thread in terminators:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

        remaining_tasks = [
            task
            for task in self.nonterminal_tasks()
            if task not in preserved
        ]
        return {
            "success": not remaining_tasks,
            "reason": reason,
            "queued_cancelled": len(queued),
            "running_cancel_requested": len(running),
            "running_preserved": len(preserved),
            "preserved": [task.delegate_id for task in preserved],
            "remaining": [task.delegate_id for task in remaining_tasks],
        }

    def _register_task_locked(self, task: DelegateTask) -> None:
        self.tasks[task.delegate_id] = task
        lane_key = task.project.scheduler_lane_key
        lane = self.lanes.get(lane_key)
        if lane is None:
            lane = ProjectLane(project=task.project)
            self.lanes[lane_key] = lane
            self._project_order.append(lane_key)
        lane.pending.append(task.delegate_id)

    def _ensure_capacity_locked(self, project_key: str, new_count: int) -> None:
        global_queued = sum(len(lane.pending) for lane in self.lanes.values())
        if global_queued + new_count > self.queue_limit_global:
            raise DelegateQueueFullError(scope="global", limit=self.queue_limit_global)
        project_queued = sum(
            1
            for task in self.tasks.values()
            if task.state == "queued" and task.project.project_key == project_key
        )
        if project_queued + new_count > self.queue_limit_per_project:
            raise DelegateQueueFullError(scope="project", limit=self.queue_limit_per_project)

    def _dispatch_locked(self) -> list[DelegateTask]:
        starts: list[DelegateTask] = []
        if self._shutting_down or not self._project_order:
            return starts

        # One task per worktree lane per pass provides round-robin fairness.
        # Readers may bypass readers blocked on role, harness or group
        # capacity, but never a pending writer in the same worktree.
        made_progress = True
        while made_progress:
            made_progress = False
            project_count = len(self._project_order)
            for _ in range(project_count):
                project_key = self._project_order[0]
                self._project_order.rotate(-1)
                lane = self.lanes.get(project_key)
                if lane is None:
                    continue
                task = self._next_runnable_locked(lane)
                if task is None:
                    continue
                lane.pending.remove(task.delegate_id)
                task.state = "running"
                task.started_at = time.time()
                task.started_monotonic = time.monotonic()
                if task.kind == "explore":
                    lane.active_explores[task.delegate_id] = task
                    self._active_explore_global += 1
                    self._explore_harness_admissions[task.delegate_id] = (
                        task.harness.strip().lower()
                    )
                else:
                    lane.active_code = task
                    self._active_code_global += 1
                self._refresh_task_group_locked(task)
                starts.append(task)
                made_progress = True
        return starts

    def _next_runnable_locked(self, lane: ProjectLane) -> DelegateTask | None:
        if lane.active_code is not None or not lane.pending:
            return None
        for task_id in lane.pending:
            task = self.tasks[task_id]
            total_limit = self.max_active_per_harness.get(task.harness.strip().lower())
            harness_full = (
                total_limit is not None and self._active_for_harness_locked(task.harness) >= total_limit
            )
            if harness_full or not self._dependencies_complete_locked(task.depends_on_group_ids):
                if task.kind == "code":
                    return None
                continue
            if task.kind == "code":
                if lane.active_explores:
                    return None
                if self._active_code_global >= self.max_code_global:
                    return None
                if (
                    self._active_code_for_repo_locked(task.project.project_key)
                    >= self.max_code_per_project
                ):
                    return None
                return task

            if len(lane.active_explores) >= self.max_explore_per_project:
                return None
            if self._active_explore_global >= self.max_explore_global:
                return None
            if self._active_readers_for_role_locked(task) >= self.max_readers_per_role[task.capacity_role]:
                continue
            harness_limit = self.max_explore_per_harness.get(task.harness.strip().lower())
            if (
                harness_limit is not None
                and self._active_explore_for_harness_locked(task.harness) >= harness_limit
            ):
                continue
            if task.group_id:
                group = self.groups.get(task.group_id)
                if group and group.max_concurrency is not None:
                    running_in_group = sum(
                        1
                        for child_id in group.child_ids
                        if self.tasks[child_id].state == "running"
                    )
                    if running_in_group >= group.max_concurrency:
                        continue
            return task
        return None

    def _active_explore_for_harness_locked(self, harness: str) -> int:
        normalized = harness.strip().lower()
        return sum(
            1
            for admitted_harness in self._explore_harness_admissions.values()
            if admitted_harness == normalized
        )

    def _active_for_harness_locked(self, harness: str) -> int:
        name = harness.strip().lower()
        return self._active_explore_for_harness_locked(name) + sum(
            1
            for lane in self.lanes.values()
            if lane.active_code is not None
            and lane.active_code.harness.strip().lower() == name
        )

    def harness_capacity(self, harness: str) -> dict[str, int | None]:
        """Account-level slots count both running exploration and code."""
        name = harness.strip().lower()
        with self.lock:
            return {
                "active": self._active_for_harness_locked(name),
                "limit": self.max_active_per_harness.get(name),
            }

    def transition_running_explore_harness(
        self,
        task: DelegateTask,
        target_harness: str,
    ) -> bool:
        """Atomically re-admit a running explore task onto a fallback harness.

        Runtime fallback happens inside an already-running scheduler task. Without
        a second admission step, changing ``task.harness`` bypasses
        ``max_explore_per_harness`` and can oversubscribe a smaller fallback
        pool. Admission is deliberately non-blocking: a saturated fallback tier
        returns ``False`` so routing can continue to the next admissible tier.
        """

        normalized_target = target_harness.strip().lower()
        if not normalized_target:
            return False
        with self.condition:
            current = self.tasks.get(task.delegate_id)
            if current is not task or task.kind != "explore" or task.state != "running":
                return False
            if self._shutting_down or task.cancel_requested:
                return False
            harness_limit = self.max_explore_per_harness.get(normalized_target)
            total_limit = self.max_active_per_harness.get(normalized_target)
            if (
                total_limit is not None
                and self._active_for_harness_locked(normalized_target) >= total_limit
                and self._explore_harness_admissions.get(task.delegate_id) != normalized_target
            ):
                return False
            if (
                harness_limit is not None
                and self._active_explore_for_harness_locked(normalized_target) >= harness_limit
            ):
                return False
            self._explore_harness_admissions[task.delegate_id] = normalized_target
            self.condition.notify_all()
            return True

    def transition_running_code_harness(
        self,
        task: DelegateTask,
        target_harness: str,
    ) -> bool:
        """Keep code fallback within the shared AGY account slot budget."""
        normalized_target = target_harness.strip().lower()
        if not normalized_target:
            return False
        with self.condition:
            if (
                self.tasks.get(task.delegate_id) is not task
                or task.kind != "code" or task.state != "running"
                or self._shutting_down or task.cancel_requested
            ):
                return False
            limit = self.max_active_per_harness.get(normalized_target)
            if (
                normalized_target != task.harness.strip().lower()
                and limit is not None
                and self._active_for_harness_locked(normalized_target) >= limit
            ):
                return False
            task.harness = normalized_target
            self.condition.notify_all()
            return True

    def _active_readers_for_role_locked(self, task: DelegateTask) -> int:
        return sum(
            1
            for lane in self.lanes.values()
            if lane.project.project_key == task.project.project_key
            for reader in lane.active_explores.values()
            if reader.capacity_role == task.capacity_role
        )

    def _active_code_for_repo_locked(self, project_key: str) -> int:
        return sum(
            1
            for lane in self.lanes.values()
            if lane.active_code is not None
            and lane.active_code.project.project_key == project_key
        )

    def _dependencies_complete_locked(self, dependencies: tuple[str, ...]) -> bool:
        for group_id in dependencies:
            group = self.groups.get(group_id)
            if group is None or not group.completed_event.is_set():
                return False
        return True

    def _start_threads(self, tasks: list[DelegateTask]) -> None:
        for task in tasks:
            threading.Thread(
                target=self._execute_task,
                args=(task,),
                name=f"delegate-{task.delegate_id}",
                daemon=True,
            ).start()

    def _execute_task(
        self,
        task: DelegateTask,
        runner: TaskRunner | None = None,
    ) -> None:
        try:
            result = (runner or self.runner)(task)
        except Exception as exc:  # pragma: no cover - defensive scheduler boundary
            result = {
                "success": False,
                "status": "failed",
                "completed": True,
                "in_progress": False,
                "error": {"code": "delegate_runner_failed", "message": str(exc)},
            }

        result["work_role"] = task.work_role
        status = str(result.get("status", "failed"))
        state: TaskState = status if status in TERMINAL_TASK_STATES else "failed"  # type: ignore[assignment]
        if state == "failed" and status != "failed":
            result["status"] = "failed"
        with self.lock:
            task.result = result
            task.state = state
            task.completed_at = time.time()
            lane = self.lanes[task.project.scheduler_lane_key]
            if task.kind == "explore":
                lane.active_explores.pop(task.delegate_id, None)
                self._explore_harness_admissions.pop(task.delegate_id, None)
                self._active_explore_global = max(0, self._active_explore_global - 1)
            elif lane.active_code is task:
                lane.active_code = None
                self._active_code_global = max(0, self._active_code_global - 1)
            task.completed_event.set()
            self._refresh_task_group_locked(task)
            starts = self._dispatch_locked()
            self.condition.notify_all()
        self.on_terminal(task)
        self._start_threads(starts)

    def _refresh_task_group_locked(self, task: DelegateTask) -> None:
        if task.group_id:
            group = self.groups.get(task.group_id)
            if group:
                self._refresh_group_locked(group)

    def _refresh_group_locked(self, group: DelegateGroup) -> None:
        children = [self.tasks.get(child_id) for child_id in group.child_ids]
        if any(child is None for child in children):
            group.state = "running"
            return
        concrete_children = [child for child in children if child is not None]
        states = {task.state for task in concrete_children}
        if states and states <= {"succeeded"}:
            group.state = "succeeded"
            group.completed_event.set()
        elif concrete_children and all(
            task.state in TERMINAL_TASK_STATES for task in concrete_children
        ):
            group.state = "failed"
            group.completed_event.set()
        else:
            # A submitted group is an in-progress fan-in barrier even when all
            # children are still waiting for project/global capacity.
            group.state = "running"
