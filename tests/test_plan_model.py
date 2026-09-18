"""P0-1: plan contract, graph validation and backward-compatible persistence."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.orchestrator import AgentOrchestrator
from agent.planner import Plan, Planner, PlannerError
from api.app import create_app
from core.config import Settings
from task.file_task_store import FileTaskStore
from task.task_manager import TaskManager
from task.task_model import Task, TaskStateError


def plan_data():
    # Deliberately not topologically ordered: a DAG is not just an ordered list.
    return {
        "user_input": "summarize sales", "version": 1,
        "steps": [
            {"step_id": "report", "name": "save", "description": "save summary",
             "depends_on": ["read"], "completion_criteria": ["Report exists and is nonempty"],
             "tool": "save_report"},
            {"step_id": "read", "name": "read", "description": "read source",
             "depends_on": [], "completion_criteria": ["Sales columns are identified"],
             "tool": None},
        ],
    }


def test_dag_round_trip_and_summary_preserve_execution_contract():
    plan = Plan.from_dict(plan_data())
    assert Plan.from_dict(json.loads(json.dumps(plan.to_dict()))).to_dict() == plan_data()
    summary = plan.summary()
    assert "report" in summary and "read" in summary
    assert "Report exists and is nonempty" in summary
    assert "save summary" in summary


@pytest.mark.parametrize("case", [
    "duplicate", "missing_dependency", "self_cycle", "cycle", "empty",
    "missing_id", "blank_name", "empty_criteria", "blank_criteria",
    "string_dependencies", "duplicate_dependencies", "invalid_version", "extra_field",
])
def test_invalid_plans_are_rejected(case):
    data = plan_data()
    first, second = data["steps"]
    if case == "duplicate": second["step_id"] = first["step_id"]
    elif case == "missing_dependency": first["depends_on"] = ["unknown"]
    elif case == "self_cycle": first["depends_on"] = ["report"]
    elif case == "cycle": second["depends_on"] = ["report"]
    elif case == "empty": data["steps"] = []
    elif case == "missing_id": del first["step_id"]
    elif case == "blank_name": first["name"] = "  "
    elif case == "empty_criteria": first["completion_criteria"] = []
    elif case == "blank_criteria": first["completion_criteria"] = [" "]
    elif case == "string_dependencies": first["depends_on"] = "read"
    elif case == "duplicate_dependencies": first["depends_on"] = ["read", "read"]
    elif case == "invalid_version": data["version"] = True
    elif case == "extra_field": first["unexpected"] = 1
    with pytest.raises(ValueError):
        Plan.from_dict(data)


def test_old_task_json_without_plan_remains_readable(tmp_path):
    old = Task(user_input="legacy")
    step = old.add_step("read_file", "read_file")
    data = old.to_dict()
    data.pop("plan", None)
    (tmp_path / f"{old.id}.json").write_text(json.dumps(data), encoding="utf-8")
    loaded = TaskManager(FileTaskStore(tmp_path)).get_task(old.id)
    assert loaded.plan is None
    assert loaded.steps[0].id == step.id
    assert loaded.to_dict()["plan"] is None


def test_plan_persisted_before_execution_and_visible_after_restart(tmp_path):
    manager = TaskManager(FileTaskStore(tmp_path))
    plan = Plan.from_dict(plan_data())

    def execute(task_id, user_input, received_plan, repair_hint=None):
        restarted = TaskManager(FileTaskStore(tmp_path))
        saved = restarted.get_task(task_id)
        assert saved.plan.to_dict() == plan.to_dict()
        assert saved.steps == []  # plan steps are not tool invocation records
        assert received_plan.to_dict() == saved.plan.to_dict()
        return "done"

    orchestrator = AgentOrchestrator(
        manager, SimpleNamespace(plan=lambda *a, **k: plan), SimpleNamespace(run=execute)
    )
    task = orchestrator.run(plan.user_input)
    restarted = TaskManager(FileTaskStore(tmp_path))
    with TestClient(create_app(
        SimpleNamespace(task_manager=restarted, run_task=lambda _: None),
        Settings(_env_file=None),
    )) as client:
        response = client.get(f"/tasks/{task.id}")
        assert response.status_code == 200
        assert response.json()["plan"] == plan_data()
        assert response.json()["status"] == "SUCCESS"


def test_initial_plan_cannot_be_overwritten_and_is_detached_from_caller():
    manager = TaskManager()
    plan = Plan.from_dict(plan_data())
    task = manager.create_task(plan.user_input)
    manager.start_task(task.id)
    manager.set_plan(task.id, plan)
    plan.steps[0].name = "changed by caller"
    assert manager.get_task(task.id).plan.steps[0].name == "save"
    with pytest.raises(TaskStateError):
        manager.set_plan(task.id, Plan.from_dict(plan_data()))


@pytest.mark.parametrize("arguments", ["{broken", '{"steps": []}', '{"steps": [{"name":"read"}]}'])
def test_bad_model_plan_fails_task_without_executing(arguments):
    call = SimpleNamespace(function=SimpleNamespace(name="create_plan", arguments=arguments))
    llm = SimpleNamespace(chat=lambda *a, **k: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[call]))]))
    calls = []
    manager = TaskManager()
    orchestrator = AgentOrchestrator(
        manager, Planner(llm), SimpleNamespace(run=lambda *a, **k: calls.append(a))
    )
    task = manager.create_task("bad plan")
    with pytest.raises(PlannerError):
        orchestrator.run_task(task.id)
    assert calls == []
    assert manager.get_task(task.id).status.value == "FAILED"
    assert manager.get_task(task.id).plan is None


def test_invalid_plan_from_custom_planner_is_also_rejected():
    calls = []
    manager = TaskManager()
    orchestrator = AgentOrchestrator(manager,
        SimpleNamespace(plan=lambda *a, **k: Plan("x")),
        SimpleNamespace(run=lambda *a, **k: calls.append(a)))
    with pytest.raises(ValueError):
        orchestrator.run("x")
    assert calls == []
    assert manager.list_tasks()[0].status.value == "FAILED"


@pytest.mark.parametrize("case", ["wrong_goal", "wrong_version", "not_running", "finished"])
def test_plan_attachment_rejects_wrong_task_or_lifecycle(case):
    manager = TaskManager()
    task = manager.create_task("summarize sales")
    plan = Plan.from_dict(plan_data())
    if case != "not_running":
        manager.start_task(task.id)
    if case == "wrong_goal":
        plan.user_input = "another task"
    elif case == "wrong_version":
        plan.version = 2
    elif case == "finished":
        manager.succeed_task(task.id)
    with pytest.raises(TaskStateError):
        manager.set_plan(task.id, plan)
    assert manager.get_task(task.id).plan is None


def test_plan_write_failure_prevents_tool_execution(tmp_path):
    class FailPlanStore(FileTaskStore):
        def update(self, task):
            if task.plan is not None:
                raise OSError("disk full")
            super().update(task)

    manager = TaskManager(FailPlanStore(tmp_path))
    calls = []
    orchestrator = AgentOrchestrator(manager,
        SimpleNamespace(plan=lambda *a, **k: Plan.from_dict(plan_data())),
        SimpleNamespace(run=lambda *a, **k: calls.append(a)))
    task = manager.create_task("summarize sales")
    with pytest.raises(OSError, match="disk full"):
        orchestrator.run_task(task.id)
    assert calls == []
    assert manager.get_task(task.id).status.value == "FAILED"
    assert manager.get_task(task.id).plan is None
