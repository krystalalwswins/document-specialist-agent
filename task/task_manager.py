"""TaskManager: lifecycle coordination over a swappable TaskStore.

The store is hidden behind an ABC so Phase 2 can replace the in-memory
implementation with Redis / a database without touching callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Optional

from .task_model import Task, TaskError, TaskNotFoundError, TaskStateError, TaskStatus, TaskStep


def _seconds_since(timestamp: Optional[str], now: datetime) -> Optional[float]:
    """Age of an ISO-8601 timestamp in seconds, or None when it is unreadable."""
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds()


class TaskStore(ABC):
    """Persistence contract for tasks (memory / Redis / DB in later phases)."""

    @abstractmethod
    def create(self, task: Task) -> None:
        """Persist a new task."""

    @abstractmethod
    def get(self, task_id: str) -> Task:
        """Return a task by id; raise TaskNotFoundError if missing."""

    @abstractmethod
    def list(self) -> list[Task]:
        """Return all tasks (Phase 1: in-memory snapshot)."""

    @abstractmethod
    def update(self, task: Task) -> None:
        """Persist an updated task; raise TaskNotFoundError if missing."""


class InMemoryTaskStore(TaskStore):
    """Thread-safe dict-backed store for local demo / tests."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = RLock()

    def create(self, task: Task) -> None:
        with self._lock:
            if task.id in self._tasks:
                raise TaskError(f"task already exists: {task.id}")
            self._tasks[task.id] = task

    def get(self, task_id: str) -> Task:
        with self._lock:
            try:
                return self._tasks[task_id]
            except KeyError:
                raise TaskNotFoundError(task_id) from None

    def list(self) -> list[Task]:
        with self._lock:
            return list(self._tasks.values())

    def update(self, task: Task) -> None:
        with self._lock:
            if task.id not in self._tasks:
                raise TaskNotFoundError(task.id)
            self._tasks[task.id] = task

    def clear(self) -> None:
        with self._lock:
            self._tasks.clear()


class TaskManager:
    """Facade for the full task lifecycle: create -> run -> succeed/fail."""

    def __init__(self, store: Optional[TaskStore] = None) -> None:
        self._store = store or InMemoryTaskStore()
        self._lock = RLock()

    @property
    def store(self) -> TaskStore:
        return self._store

    def create_task(self, user_input: str) -> Task:
        task = Task(user_input=user_input)
        with self._lock:
            self._store.create(task)
        return task

    def get_task(self, task_id: str) -> Task:
        return self._store.get(task_id)

    def list_tasks(self) -> list[Task]:
        return self._store.list()

    def start_task(self, task_id: str) -> Task:
        with self._lock:
            task = self._store.get(task_id)
            task.start()
            self._store.update(task)
        return task

    def succeed_task(self, task_id: str, result: Optional[dict[str, Any]] = None) -> Task:
        with self._lock:
            task = self._store.get(task_id)
            task.succeed(result)
            self._store.update(task)
        return task

    def fail_task(self, task_id: str, error: str) -> Task:
        with self._lock:
            task = self._store.get(task_id)
            task.fail(error)
            self._store.update(task)
        return task

    def add_step(self, task_id: str, name: str, tool: Optional[str] = None) -> TaskStep:
        with self._lock:
            task = self._store.get(task_id)
            step = task.add_step(name, tool)
            self._store.update(task)
        return step

    def start_step(self, task_id: str, step_id: str) -> TaskStep:
        with self._lock:
            task = self._store.get(task_id)
            step = task.get_step(step_id)
            step.start()
            self._store.update(task)
        return step

    def succeed_step(
        self, task_id: str, step_id: str, output: Optional[str] = None
    ) -> TaskStep:
        with self._lock:
            task = self._store.get(task_id)
            step = task.get_step(step_id)
            step.succeed(output)
            self._store.update(task)
        return step

    def fail_step(self, task_id: str, step_id: str, error: str) -> TaskStep:
        with self._lock:
            task = self._store.get(task_id)
            step = task.get_step(step_id)
            step.fail(error)
            self._store.update(task)
        return step

    def set_step_attempts(self, task_id: str, step_id: str, attempts: int) -> None:
        with self._lock:
            task = self._store.get(task_id)
            step = task.get_step(step_id)
            step.attempts = attempts
            self._store.update(task)

    def add_metric_events(self, task_id: str, key: str, events: list[dict[str, Any]]) -> None:
        with self._lock:
            task = self._store.get(task_id)
            task.metrics.setdefault(key, []).extend(events)
            self._store.update(task)

    def metric_sink(self, task_id: str, key: str, **context: Any) -> Callable[[dict[str, Any]], None]:
        """Return an event callback that appends to ``task.metrics[key]``.

        Used to hand the LLM client a task-scoped sink without coupling it to
        TaskManager; ``context`` is merged into every event (phase, iteration).
        """

        def sink(event: dict[str, Any]) -> None:
            self.add_metric_events(task_id, key, [{**event, **context}])

        return sink

    def recover_stale_tasks(
        self,
        stale_after_seconds: float,
        *,
        now: Optional[datetime] = None,
    ) -> list[Task]:
        """Fail non-terminal tasks that stopped making progress.

        A process restart kills the worker thread but leaves the persisted task in
        CREATED/RUNNING forever. This marks such records FAILED so the lifecycle
        converges; it deliberately does **not** claim to stop any running code.
        """
        now = now or datetime.now(timezone.utc)
        recovered: list[Task] = []
        for task in self.list_tasks():
            if task.status not in (TaskStatus.CREATED, TaskStatus.RUNNING):
                continue
            age = _seconds_since(task.updated_time, now)
            if age is None or age <= stale_after_seconds:
                continue
            # Snapshot before failing: an in-memory store hands back the live object.
            previous_status = task.status.value
            reason = (
                f"stale: no progress for {int(age)}s "
                f"(last update {task.updated_time}, threshold {int(stale_after_seconds)}s)"
            )
            try:
                failed = self.fail_task(task.id, reason)
            except TaskStateError:  # concurrently finished elsewhere; leave it alone
                continue
            self.add_metric_events(
                task.id,
                "recovery_events",
                [
                    {
                        "occurred_at": now.isoformat(),
                        "task_id": task.id,
                        "kind": "stale_recovery",
                        "previous_status": previous_status,
                        "stale_seconds": int(age),
                        "reason": reason,
                    }
                ],
            )
            recovered.append(failed)
        return recovered
