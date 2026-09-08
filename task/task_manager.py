"""TaskManager: lifecycle coordination over a swappable TaskStore.

The store is hidden behind an ABC so Phase 2 can replace the in-memory
implementation with Redis / a database without touching callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from threading import RLock
from typing import Any, Optional

from .task_model import Task, TaskError, TaskNotFoundError, TaskStep, TaskStatus, StepStatus, _utc_now_iso


class TaskStore(ABC):
    """Persistence contract for tasks (memory / Redis / DB in later phases)."""

    def transaction(self):
        """Override when a backend supports atomic read-modify-write."""
        return nullcontext()

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

    @contextmanager
    def transaction(self):
        with self._lock:
            yield

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

    @staticmethod
    def _event(task: Task, kind: str, step_id: str | None = None) -> None:
        events = task.metrics.setdefault("lifecycle_events", [])
        events.append({"sequence": len(events) + 1, "task_id": task.id,
                       "step_id": step_id, "kind": kind, "occurred_at": _utc_now_iso()})
        task._touch()

    def create_task(self, user_input: str, *, inputs=None, artifact_requirements=None, capacity=None, memory_note="") -> Task:
        task = Task(user_input=user_input, inputs=inputs or [], artifact_requirements=artifact_requirements or {})
        if memory_note:
            from memory.notes import redact
            task.metrics["memory_note"] = redact(memory_note)
        self._event(task, "task.created")
        with self._lock, self._store.transaction():
            if capacity is not None and sum(t.status in (TaskStatus.CREATED, TaskStatus.RUNNING)
                                            for t in self._store.list()) >= capacity:
                raise TaskError("task queue capacity reached")
            self._store.create(task)
        return task

    def get_task(self, task_id: str) -> Task:
        return self._store.get(task_id)

    def list_tasks(self) -> list[Task]:
        return self._store.list()

    def start_task(self, task_id: str) -> Task:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            task.start()
            self._event(task, "task.started")
            self._store.update(task)
        return task

    def succeed_task(self, task_id: str, result: Optional[dict[str, Any]] = None) -> Task:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            task.succeed(result)
            self._event(task, "task.succeeded")
            self._store.update(task)
        return task

    def fail_task(self, task_id: str, error: str) -> Task:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            for step in task.steps:
                if step.status in (StepStatus.PENDING, StepStatus.RUNNING):
                    step.fail(error)
                    self._event(task, "step.failed", step.id)
            task.metrics["termination_reason"] = error
            task.fail(error)
            self._event(task, "task.failed")
            self._store.update(task)
        return task

    def add_step(self, task_id: str, name: str, tool: Optional[str] = None) -> TaskStep:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            step = task.add_step(name, tool)
            self._event(task, "step.created", step.id)
            self._store.update(task)
        return step

    def start_step(self, task_id: str, step_id: str) -> TaskStep:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            step = task.get_step(step_id)
            step.start()
            self._event(task, "step.started", step.id)
            self._store.update(task)
        return step

    def succeed_step(
        self, task_id: str, step_id: str, output: Optional[str] = None
    ) -> TaskStep:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            step = task.get_step(step_id)
            step.succeed(output)
            self._event(task, "step.succeeded", step.id)
            self._store.update(task)
        return step

    def fail_step(self, task_id: str, step_id: str, error: str) -> TaskStep:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            step = task.get_step(step_id)
            step.fail(error)
            self._event(task, "step.failed", step.id)
            self._store.update(task)
        return step

    def set_step_attempts(self, task_id: str, step_id: str, attempts: int) -> None:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            step = task.get_step(step_id)
            step.attempts = attempts
            self._store.update(task)

    def add_metric_events(self, task_id: str, key: str, events: list[dict[str, Any]]) -> None:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            destination = task.metrics.setdefault(key, [])
            for event in events:
                event = deepcopy(event)
                event.setdefault('task_id', task_id)
                event.setdefault('occurred_at', _utc_now_iso())
                event.setdefault('sequence', len(destination) + 1)
                destination.append(event)
            task._touch()
            self._store.update(task)

    def save_plan(self, task_id: str, plan: dict[str, Any]) -> None:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            task.metrics["plan"] = plan
            self._event(task, "plan.saved")
            task._touch()
            self._store.update(task)

    def set_metric(self, task_id: str, key: str, value: Any) -> None:
        with self._lock, self._store.transaction():
            task = self._store.get(task_id)
            task.metrics[key] = value
            task._touch()
            self._store.update(task)

    def claim_next(self) -> Task | None:
        """Single atomic claim across workers sharing the database."""
        with self._lock, self._store.transaction():
            for task in self._store.list():
                if task.status == TaskStatus.CREATED:
                    return self.start_task(task.id)
        return None

    def recover_interrupted(self) -> int:
        """Operator action ONLY while all workers are stopped. Never replay code."""
        with self._lock, self._store.transaction():
            tasks = [t for t in self._store.list() if t.status == TaskStatus.RUNNING]
            for task in tasks:
                self.fail_task(task.id, "worker_interrupted_execution_uncertain")
            return len(tasks)
