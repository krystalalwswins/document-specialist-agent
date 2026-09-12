"""AgentOrchestrator tests with fake planner/executor."""

import pytest

from agent.orchestrator import AgentOrchestrator
from agent.planner import Plan, PlanStep
from agent.validator import ArtifactCheck, ArtifactValidator, ValidationResult
from sandbox.inputs import InputStagingError
from task.file_task_store import FileTaskStore
from task.task_manager import TaskManager
from task.task_model import TaskStatus


class FakePlanner:
    def plan(self, user_input, on_event=None):
        return Plan(user_input=user_input, steps=[PlanStep(name="step", tool="echo")])


class FakeExecutor:
    def __init__(self, result="done", error=None, on_run=None):
        self.result = result
        self.error = error
        self.on_run = on_run
        self.runs = []

    def run(self, task_id, user_input, plan):
        self.runs.append((task_id, user_input, plan))
        if self.error:
            raise RuntimeError(self.error)
        if self.on_run:
            self.on_run(task_id)
        return self.result


class FakeStager:
    def __init__(self, error=None, log=None):
        self.error = error
        self.log = log
        self.calls = []

    def prepare(self, task_id):
        if self.log is not None:
            self.log.append("prepare")
        return f"/home/gem/workspace/tasks/{task_id}"

    def stage_all(self, task_id, specs):
        self.calls.append(list(specs))
        if self.log is not None:
            self.log.append("stage")
        if self.error:
            raise self.error
        return [
            {
                **spec,
                "sandbox_path": f"/home/gem/workspace/tasks/{task_id}/{spec['oss_key'].split('/')[-1]}",
                "bytes": 7,
            }
            for spec in specs
        ]


def _validator(ok=True):
    class StubValidator(ArtifactValidator):
        def __init__(self):
            pass

        def validate(self, artifacts):
            return ValidationResult(
                ok=ok,
                checks=[ArtifactCheck(a["oss_key"], ok, "ok" if ok else "missing in object storage") for a in artifacts],
            )

    return StubValidator()


def test_orchestrator_success_lifecycle():
    task_manager = TaskManager()
    orchestrator = AgentOrchestrator(
        task_manager=task_manager,
        planner=FakePlanner(),
        executor=FakeExecutor(result="the answer"),
    )
    task = orchestrator.run("analyze excel")
    assert task.status == TaskStatus.SUCCESS
    assert task.result == {"answer": "the answer"}
    assert task_manager.get_task(task.id).status == TaskStatus.SUCCESS


def test_orchestrator_failure_marks_task_failed_and_reraises():
    task_manager = TaskManager()
    orchestrator = AgentOrchestrator(
        task_manager=task_manager,
        planner=FakePlanner(),
        executor=FakeExecutor(error="sandbox down"),
    )
    with pytest.raises(RuntimeError, match="sandbox down"):
        orchestrator.run("x")
    task = task_manager.list_tasks()[0]
    assert task.status == TaskStatus.FAILED
    assert task.error == "sandbox down"


def test_orchestrator_planner_failure_marks_task_failed():
    class BrokenPlanner:
        def plan(self, user_input, on_event=None):
            raise RuntimeError("plan failed")

    task_manager = TaskManager()
    orchestrator = AgentOrchestrator(
        task_manager=task_manager,
        planner=BrokenPlanner(),
        executor=FakeExecutor(),
    )
    with pytest.raises(RuntimeError, match="plan failed"):
        orchestrator.run("x")
    assert task_manager.list_tasks()[0].status == TaskStatus.FAILED


def test_run_task_returns_the_persisted_final_state(tmp_path):
    """A persistent store returns copies, so run_task must re-read before returning."""
    task_manager = TaskManager(FileTaskStore(tmp_path))
    orchestrator = AgentOrchestrator(
        task_manager=task_manager,
        planner=FakePlanner(),
        executor=FakeExecutor(result="the answer"),
    )

    task = orchestrator.run("analyze excel")

    assert task.status is TaskStatus.SUCCESS
    assert task.result == {"answer": "the answer"}
    assert task_manager.get_task(task.id).status is TaskStatus.SUCCESS


def test_inputs_are_staged_before_planning_and_recorded_on_the_task():
    order = []
    task_manager = TaskManager()

    class OrderPlanner(FakePlanner):
        def plan(self, user_input, on_event=None):
            order.append("plan")
            return super().plan(user_input, on_event)

    orchestrator = AgentOrchestrator(
        task_manager=task_manager,
        planner=OrderPlanner(),
        executor=FakeExecutor(result="ok"),
        input_stager=FakeStager(log=order),
    )
    task = task_manager.create_task("analyze sales.xlsx", input_files=[{"oss_key": "raw/sales.xlsx"}])

    orchestrator.run_task(task.id)

    assert order == ["prepare", "stage", "plan"]
    staged = task_manager.get_task(task.id).input_files
    assert staged[0]["oss_key"] == "raw/sales.xlsx"
    assert staged[0]["sandbox_path"] == f"/home/gem/workspace/tasks/{task.id}/sales.xlsx"
    assert task_manager.get_task(task.id).workspace_dir == f"/home/gem/workspace/tasks/{task.id}"


def test_input_staging_failure_marks_the_task_failed():
    task_manager = TaskManager()
    orchestrator = AgentOrchestrator(
        task_manager=task_manager,
        planner=FakePlanner(),
        executor=FakeExecutor(),
        input_stager=FakeStager(error=InputStagingError("cannot load input 'raw/missing.csv'")),
    )
    task = task_manager.create_task("x", input_files=[{"oss_key": "raw/missing.csv"}])

    with pytest.raises(InputStagingError):
        orchestrator.run_task(task.id)

    failed = task_manager.get_task(task.id)
    assert failed.status is TaskStatus.FAILED
    assert "cannot load input 'raw/missing.csv'" in failed.error


def test_validation_failure_marks_the_task_failed_and_records_events():
    task_manager = TaskManager()
    executor = FakeExecutor(
        result="saved it!",
        on_run=lambda task_id: task_manager.add_artifact(
            task_id, {"oss_key": "reports/out.csv", "bytes": 12}
        ),
    )
    orchestrator = AgentOrchestrator(
        task_manager=task_manager,
        planner=FakePlanner(),
        executor=executor,
        validator=_validator(ok=False),
    )
    task = task_manager.create_task("x")

    from agent.orchestrator import ArtifactValidationError

    with pytest.raises(ArtifactValidationError, match="artifact validation failed"):
        orchestrator.run_task(task.id)

    failed = task_manager.get_task(task.id)
    assert failed.status is TaskStatus.FAILED
    assert failed.result is None  # the optimistic answer is not published
    events = failed.metrics["validation_events"]
    assert events == [
        {
            "kind": "artifact_check",
            "oss_key": "reports/out.csv",
            "ok": False,
            "reason": "missing in object storage",
            "size": None,
        }
    ]


def test_validated_artifacts_are_published_in_the_task_result():
    task_manager = TaskManager()
    orchestrator = AgentOrchestrator(
        task_manager=task_manager,
        planner=FakePlanner(),
        executor=FakeExecutor(
            result="done",
            on_run=lambda task_id: task_manager.add_artifact(
                task_id, {"oss_key": "reports/out.csv", "bytes": 12}
            ),
        ),
        validator=_validator(ok=True),
    )

    task = orchestrator.run("x")

    assert task.status is TaskStatus.SUCCESS
    assert task.result["answer"] == "done"
    assert task.result["artifacts"] == [{"oss_key": "reports/out.csv", "bytes": 12}]
