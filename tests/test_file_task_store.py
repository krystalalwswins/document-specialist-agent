"""FileTaskStore: persistence across restarts, atomic writes, defensive reads."""

from pathlib import Path

import pytest

from task.file_task_store import FileTaskStore
from task.task_manager import TaskManager
from task.task_model import StepStatus, Task, TaskError, TaskNotFoundError, TaskStatus


def test_task_survives_a_new_store_instance(tmp_path):
    task = Task(user_input="analyze excel")
    FileTaskStore(tmp_path).create(task)

    reopened = FileTaskStore(tmp_path)  # simulated process restart
    loaded = reopened.get(task.id)

    assert loaded.id == task.id
    assert loaded.user_input == "analyze excel"
    assert loaded.status is TaskStatus.CREATED


def test_full_lifecycle_is_persisted(tmp_path):
    manager = TaskManager(FileTaskStore(tmp_path))
    task = manager.create_task("x")
    manager.start_task(task.id)
    step = manager.add_step(task.id, "run_python", tool="run_python")
    manager.start_step(task.id, step.id)
    manager.set_step_attempts(task.id, step.id, 2)
    manager.succeed_step(task.id, step.id, output="42")
    manager.succeed_task(task.id, {"answer": "42"})

    restarted = TaskManager(FileTaskStore(tmp_path))
    loaded = restarted.get_task(task.id)

    assert loaded.status is TaskStatus.SUCCESS
    assert loaded.result == {"answer": "42"}
    assert loaded.steps[0].status is StepStatus.SUCCESS
    assert loaded.steps[0].output == "42"
    assert loaded.steps[0].attempts == 2
    assert loaded.steps[0].duration_ms is not None


def test_construction_is_side_effect_free(tmp_path):
    target = tmp_path / "nested" / "tasks"
    FileTaskStore(target)
    assert not target.exists()
    assert FileTaskStore(target).list() == []


def test_get_missing_task_raises(tmp_path):
    with pytest.raises(TaskNotFoundError):
        FileTaskStore(tmp_path).get("0" * 32)


@pytest.mark.parametrize("task_id", ["../../etc/passwd", "not-hex", "", "a" * 31, "A" * 32])
def test_unsafe_task_id_is_rejected(tmp_path, task_id):
    with pytest.raises(TaskNotFoundError):
        FileTaskStore(tmp_path).get(task_id)


def test_duplicate_create_raises(tmp_path):
    store = FileTaskStore(tmp_path)
    task = Task(user_input="x")
    store.create(task)
    with pytest.raises(TaskError, match="already exists"):
        store.create(task)


def test_update_unknown_task_raises(tmp_path):
    with pytest.raises(TaskNotFoundError):
        FileTaskStore(tmp_path).update(Task(user_input="x"))


def test_writes_leave_no_temp_files(tmp_path):
    store = FileTaskStore(tmp_path)
    store.create(Task(user_input="x"))
    assert list(Path(tmp_path).glob("*.tmp")) == []
    assert len(list(Path(tmp_path).glob("*.json"))) == 1


def test_list_is_ordered_and_skips_corrupt_records(tmp_path):
    store = FileTaskStore(tmp_path)
    first = Task(user_input="first")
    second = Task(user_input="second")
    store.create(first)
    store.create(second)
    corrupt_id = "a" * 32
    (Path(tmp_path) / f"{corrupt_id}.json").write_text("{ not json", encoding="utf-8")

    assert [task.id for task in store.list()] == [first.id, second.id]
    with pytest.raises(TaskNotFoundError, match="unreadable record"):
        store.get(corrupt_id)


def test_concurrent_creates_are_safe(tmp_path):
    import threading

    store = FileTaskStore(tmp_path)
    tasks = [Task(user_input=f"task-{i}") for i in range(20)]
    errors = []

    def worker(task):
        try:
            store.create(task)
        except Exception as exc:  # pragma: no cover - surfaced through the assert below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(task,)) for task in tasks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(store.list()) == 20
