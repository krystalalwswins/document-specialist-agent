"""AgentOrchestrator tests with fake planner/executor."""

import pytest

from agent.orchestrator import AgentOrchestrator
from agent.planner import Plan, PlanStep
from task.task_manager import TaskManager
from task.task_model import TaskStatus


class FakePlanner:
    def plan(self, user_input):
        return Plan(user_input=user_input, steps=[PlanStep(name="step", tool="echo")])


class FakeExecutor:
    def __init__(self, result="done", error=None):
        self.result = result
        self.error = error
        self.runs = []

    def run(self, task_id, user_input, plan):
        self.runs.append((task_id, user_input, plan))
        if self.error:
            raise RuntimeError(self.error)
        return self.result


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
        def plan(self, user_input):
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
