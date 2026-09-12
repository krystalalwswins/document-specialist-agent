"""Stale task recovery: a dead worker must not leave a task RUNNING forever."""

from datetime import datetime, timedelta, timezone

from task.file_task_store import FileTaskStore
from task.task_manager import TaskManager
from task.task_model import TaskStatus

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
THRESHOLD = 1800


def _age(manager, task_id, minutes):
    """Backdate a task's last update so it looks abandoned."""
    task = manager.get_task(task_id)
    task.updated_time = (NOW - timedelta(minutes=minutes)).isoformat()
    manager.store.update(task)


def test_stale_running_task_is_recovered():
    manager = TaskManager()
    task = manager.create_task("x")
    manager.start_task(task.id)
    _age(manager, task.id, 45)

    recovered = manager.recover_stale_tasks(THRESHOLD, now=NOW)

    assert [t.id for t in recovered] == [task.id]
    loaded = manager.get_task(task.id)
    assert loaded.status is TaskStatus.FAILED
    assert "stale" in loaded.error
    assert "2700s" in loaded.error
    events = loaded.metrics["recovery_events"]
    assert len(events) == 1
    assert events[0]["kind"] == "stale_recovery"
    assert events[0]["previous_status"] == "RUNNING"
    assert events[0]["stale_seconds"] == 2700


def test_stale_created_task_is_recovered_without_starting_it():
    manager = TaskManager()
    task = manager.create_task("x")
    _age(manager, task.id, 60)

    recovered = manager.recover_stale_tasks(THRESHOLD, now=NOW)

    assert [t.id for t in recovered] == [task.id]
    loaded = manager.get_task(task.id)
    assert loaded.status is TaskStatus.FAILED
    assert loaded.steps == []  # never started, never executed anything
    assert loaded.metrics["recovery_events"][0]["previous_status"] == "CREATED"


def test_fresh_and_finished_tasks_are_left_alone():
    manager = TaskManager()
    fresh = manager.create_task("fresh")
    manager.start_task(fresh.id)
    _age(manager, fresh.id, 5)

    done = manager.create_task("done")
    manager.start_task(done.id)
    manager.succeed_task(done.id, {"answer": "ok"})
    _age(manager, done.id, 600)

    failed = manager.create_task("failed")
    manager.start_task(failed.id)
    manager.fail_task(failed.id, "boom")
    _age(manager, failed.id, 600)

    assert manager.recover_stale_tasks(THRESHOLD, now=NOW) == []
    assert manager.get_task(fresh.id).status is TaskStatus.RUNNING
    assert manager.get_task(done.id).status is TaskStatus.SUCCESS
    assert manager.get_task(failed.id).error == "boom"


def test_recovery_after_restart_with_a_persistent_store(tmp_path):
    first = TaskManager(FileTaskStore(tmp_path))
    task = first.create_task("long job")
    first.start_task(task.id)
    _age(first, task.id, 120)

    # New process: same directory, no in-flight worker thread.
    restarted = TaskManager(FileTaskStore(tmp_path))
    recovered = restarted.recover_stale_tasks(THRESHOLD, now=NOW)

    assert [t.id for t in recovered] == [task.id]
    assert restarted.get_task(task.id).status is TaskStatus.FAILED


def test_unreadable_timestamp_is_not_treated_as_stale():
    manager = TaskManager()
    task = manager.create_task("x")
    manager.start_task(task.id)
    _age(manager, task.id, 45)
    broken = manager.get_task(task.id)
    broken.updated_time = "not-a-timestamp"
    manager.store.update(broken)

    assert manager.recover_stale_tasks(THRESHOLD, now=NOW) == []
    assert manager.get_task(task.id).status is TaskStatus.RUNNING
