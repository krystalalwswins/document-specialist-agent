"""Unit tests for the task domain model (state machine + serialization)."""

import pytest

from task.task_model import StepStatus, Task, TaskStateError, TaskStatus


def test_task_defaults():
    task = Task(user_input="analyze excel")
    assert task.id
    assert task.status == TaskStatus.CREATED
    assert task.steps == []
    assert task.result is None
    assert task.error is None
    assert task.metrics == {}
    assert task.created_time
    assert task.updated_time >= task.created_time


def test_task_created_running_success():
    task = Task(user_input="x")
    task.start()
    assert task.status == TaskStatus.RUNNING
    task.succeed({"download_url": "https://x"})
    assert task.status == TaskStatus.SUCCESS
    assert task.result == {"download_url": "https://x"}


def test_task_failed_keeps_error():
    task = Task(user_input="x")
    task.start()
    task.fail("sandbox timeout")
    assert task.status == TaskStatus.FAILED
    assert task.error == "sandbox timeout"


def test_cannot_succeed_before_start():
    task = Task(user_input="x")
    with pytest.raises(TaskStateError):
        task.succeed({"x": 1})


def test_cannot_fail_after_success():
    task = Task(user_input="x")
    task.start()
    task.succeed()
    with pytest.raises(TaskStateError):
        task.fail("too late")


def test_cannot_start_after_failure():
    task = Task(user_input="x")
    task.start()
    task.fail("boom")
    with pytest.raises(TaskStateError):
        task.start()


def test_step_lifecycle():
    task = Task(user_input="x")
    step = task.add_step("read file", tool="file_tool")
    assert step.status == StepStatus.PENDING

    step.start()
    assert step.status == StepStatus.RUNNING
    assert step.started_at

    step.succeed("3 rows")
    assert step.status == StepStatus.SUCCESS
    assert step.output == "3 rows"
    assert step.finished_at
    assert step.duration_ms is not None


def test_step_requires_start_before_finish():
    task = Task(user_input="x")
    step = task.add_step("step")
    with pytest.raises(TaskStateError):
        step.succeed("no")


def test_step_failed_sets_error():
    task = Task(user_input="x")
    step = task.add_step("step")
    step.start()
    step.fail("exit code 1")
    assert step.status == StepStatus.FAILED
    assert step.error == "exit code 1"
    assert step.duration_ms is not None


def test_task_dict_round_trip():
    task = Task(user_input="analyze excel", id="abc123")
    task.start()
    step = task.add_step("read", tool="file_tool")
    step.start()
    step.succeed("ok")
    task.succeed({"download_url": "https://x"})

    restored = Task.from_dict(task.to_dict())
    assert restored.to_dict() == task.to_dict()
    assert restored.id == "abc123"
    assert restored.steps[0].tool == "file_tool"
    assert restored.steps[0].status == StepStatus.SUCCESS
    assert restored.status == TaskStatus.SUCCESS


def test_task_missing_step_raises():
    task = Task(user_input="x")
    with pytest.raises(Exception, match="step not found"):
        task.get_step("nope")


def test_input_files_and_artifacts_round_trip():
    task = Task(user_input="x", input_files=[{"oss_key": "raw/a.csv"}])
    task.add_artifact({"oss_key": "reports/b.csv", "bytes": 3})

    restored = Task.from_dict(task.to_dict())

    assert restored.input_files == [{"oss_key": "raw/a.csv"}]
    assert restored.artifacts == [{"oss_key": "reports/b.csv", "bytes": 3}]


def test_from_dict_tolerates_records_written_before_these_fields_existed():
    data = Task(user_input="x").to_dict()
    data.pop("input_files")
    data.pop("artifacts")

    restored = Task.from_dict(data)

    assert restored.input_files == []
    assert restored.artifacts == []


def test_failing_before_start_is_allowed_but_succeeding_is_not():
    abandoned = Task(user_input="x")
    abandoned.fail("stale")
    assert abandoned.status == TaskStatus.FAILED

    never_started = Task(user_input="x")
    with pytest.raises(TaskStateError):
        never_started.succeed({"answer": "nope"})
