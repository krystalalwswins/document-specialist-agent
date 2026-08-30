"""TaskManager: lifecycle coordination over a swappable TaskStore.

The store is hidden behind an ABC so Phase 2 can replace the in-memory
implementation with Redis / a database without touching callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from threading import RLock
from typing import Any, Optional

from .task_model import Task, TaskError, TaskNotFoundError, TaskStep


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
