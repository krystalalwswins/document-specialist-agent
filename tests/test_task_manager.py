"""Unit tests for TaskManager + InMemoryTaskStore."""

import threading

import pytest

from task.task_manager import InMemoryTaskStore, TaskManager
from task.task_model import StepStatus, TaskNotFoundError, TaskStatus


def test_create_and_get():
    mgr = TaskManager()
    task = mgr.create_task("analyze excel")
    assert mgr.get_task(task.id).id == task.id
    assert task.status == TaskStatus.CREATED


def test_get_missing_raises():
    mgr = TaskManager()
    with pytest.raises(TaskNotFoundError):
        mgr.get_task("nope")


def test_full_lifecycle_via_manager():
    mgr = TaskManager()
    task = mgr.create_task("filter employees")
    task_id = task.id

    mgr.start_task(task_id)
    assert mgr.get_task(task_id).status == TaskStatus.RUNNING

    step = mgr.add_step(task_id, "read csv", tool="file_tool")
    mgr.start_step(task_id, step.id)
    mgr.succeed_step(task_id, step.id, output="3 rows")
    assert mgr.get_task(task_id).steps[0].status == StepStatus.SUCCESS

    mgr.succeed_task(task_id, {"download_url": "https://x"})
    done = mgr.get_task(task_id)
    assert done.status == TaskStatus.SUCCESS
    assert done.result == {"download_url": "https://x"}


def test_failed_step_then_failed_task():
    mgr = TaskManager()
    task = mgr.create_task("x")
    mgr.start_task(task.id)
    step = mgr.add_step(task.id, "run code", tool="sandbox_tool")
    mgr.start_step(task.id, step.id)
    mgr.fail_step(task.id, step.id, error="exit code 1")
    mgr.fail_task(task.id, error="step failed")

    failed = mgr.get_task(task.id)
    assert failed.status == TaskStatus.FAILED
    assert failed.error == "step failed"
    assert failed.steps[0].error == "exit code 1"


def test_invalid_transition_via_manager_is_atomic():
    mgr = TaskManager()
    task = mgr.create_task("x")
    mgr.start_task(task.id)
    mgr.succeed_task(task.id, {"ok": True})
    with pytest.raises(Exception, match="invalid task transition"):
        mgr.fail_task(task.id, "too late")
    # State must not be corrupted by the rejected transition.
    assert mgr.get_task(task.id).status == TaskStatus.SUCCESS


def test_custom_store_injectable():
    store = InMemoryTaskStore()
    mgr = TaskManager(store=store)
    task = mgr.create_task("x")
    assert store.get(task.id).id == task.id
    assert len(store.list()) == 1


def test_duplicate_create_raises():
    store = InMemoryTaskStore()
    mgr = TaskManager(store=store)
    task = mgr.create_task("x")
    with pytest.raises(Exception, match="already exists"):
        mgr._store.create(task)


def test_concurrent_lifecycle_updates_are_safe():
    mgr = TaskManager()
    errors = []

    def worker():
        try:
            task = mgr.create_task("concurrent task")
            mgr.start_task(task.id)
            step = mgr.add_step(task.id, "step")
            mgr.start_step(task.id, step.id)
            mgr.succeed_step(task.id, step.id, output="ok")
            mgr.succeed_task(task.id, {"status": "ok"})
        except Exception as exc:  # pragma: no cover - failure surfaces via assert
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    tasks = mgr.list_tasks()
    assert len(tasks) == 20
    assert all(t.status == TaskStatus.SUCCESS for t in tasks)
