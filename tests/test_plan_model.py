"""P0-1: plan contract, graph validation and backward-compatible persistence."""

import json
from dataclasses import asdict
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


# --- P0-2: versioned replanning, plan events and tool-call binding -------------

def plan_v2_data():
    data = plan_data()
    data["version"] = 2
    data["steps"][0] = {
        "step_id": "report_v2", "name": "save", "description": "save the revised summary",
        "depends_on": ["read"], "completion_criteria": ["Report exists and is nonempty"],
        "tool": "save_report",
    }
    return data


def _running_task_with_plan(manager, task_id):
    manager.start_task(task_id)
    manager.set_plan(task_id, Plan.from_dict(plan_data()))
    manager.add_step(task_id, "read_file", tool="read_file", plan_step_id="read",
                     tool_call_id="call_1")
    manager.add_plan_events(task_id, [{
        "kind": "plan_step_completed", "step_id": "read", "version": 1,
        "evidence": "Sales columns are identified",
    }])
    return manager.get_task(task_id)


def test_plan_creation_and_replanning_are_recorded_as_versioned_events():
    manager = TaskManager()
    task = manager.create_task("summarize sales")
    created = _running_task_with_plan(manager, task.id).plan_events[0]
    assert created["kind"] == "plan_created"
    assert created["version"] == 1
    assert created["step_ids"] == ["report", "read"]

    manager.replace_plan(task.id, Plan.from_dict(plan_v2_data()),
                         reason_code="missing_data", reason="source file is unreadable")

    reloaded = manager.get_task(task.id)
    assert reloaded.plan.version == 2
    assert [step.step_id for step in reloaded.plan.steps] == ["report_v2", "read"]
    assert asdict(reloaded.plan.steps[1]) == asdict(Plan.from_dict(plan_data()).steps[1])
    assert reloaded.steps[0].plan_step_id == "read"
    assert reloaded.steps[0].tool_call_id == "call_1"
    replanned = reloaded.plan_events[-1]
    assert replanned["kind"] == "plan_replanned"
    assert (replanned["from_version"], replanned["to_version"]) == (1, 2)
    assert replanned["reason_code"] == "missing_data"
    assert replanned["reason"] == "source file is unreadable"
    assert replanned["preserved_step_ids"] == ["read"]
    assert replanned["step_ids"] == ["report_v2", "read"]


@pytest.mark.parametrize("case", [
    "wrong_version", "skipped_version", "rewritten_completed_step",
    "dropped_completed_step", "wrong_goal", "not_running", "no_initial_plan",
])
def test_replan_invariants_are_rejected(case):
    manager = TaskManager()
    task = manager.create_task("summarize sales")
    if case == "no_initial_plan":
        manager.start_task(task.id)
        manager.add_step(task.id, "read_file", tool="read_file", plan_step_id="read",
                         tool_call_id="call_1")
        manager.add_plan_events(task.id, [{
            "kind": "plan_step_completed", "step_id": "read", "version": 1, "evidence": "e",
        }])
    elif case == "not_running":
        pass
    else:
        _running_task_with_plan(manager, task.id)

    data = plan_v2_data()
    if case == "wrong_version":
        data["version"] = 1
    elif case == "skipped_version":
        data["version"] = 3
    elif case == "rewritten_completed_step":
        data["steps"][1]["completion_criteria"] = ["something else"]
    elif case == "dropped_completed_step":
        data["steps"] = [data["steps"][0]]
    elif case == "wrong_goal":
        data["user_input"] = "another task"

    with pytest.raises((TaskStateError, ValueError)):
        manager.replace_plan(task.id, Plan.from_dict(data),
                             reason_code="missing_data", reason="why not")

    reloaded = manager.get_task(task.id)
    assert reloaded.plan is None or reloaded.plan.version == 1
    assert all(event["kind"] != "plan_replanned" for event in reloaded.plan_events)


def test_plan_events_and_bindings_survive_a_restart(tmp_path):
    manager = TaskManager(FileTaskStore(tmp_path))
    task = manager.create_task("summarize sales")
    _running_task_with_plan(manager, task.id)
    manager.replace_plan(task.id, Plan.from_dict(plan_v2_data()),
                         reason_code="missing_data", reason="source file is unreadable")

    restarted = TaskManager(FileTaskStore(tmp_path)).get_task(task.id)

    assert [event["kind"] for event in restarted.plan_events] == [
        "plan_created", "plan_step_completed", "plan_replanned",
    ]
    assert restarted.plan.version == 2
    assert restarted.steps[0].plan_step_id == "read"
    assert restarted.steps[0].tool_call_id == "call_1"
    assert restarted.to_dict()["steps"][0]["plan_step_id"] == "read"


def test_legacy_task_json_without_plan_events_or_bindings_is_readable(tmp_path):
    legacy_id = "0123456789abcdef0123456789abcdef"
    legacy_step = {
        "id": "step-1", "name": "read_file", "tool": "read_file", "status": "SUCCESS",
        "output": "text", "error": None, "started_at": None, "finished_at": None,
        "duration_ms": None, "attempts": 1,
    }
    payload = {
        "id": legacy_id, "user_input": "old task", "status": "SUCCESS", "steps": [legacy_step],
        "plan": None, "result": None, "error": None, "input_files": [], "artifacts": [],
        "workspace_dir": None, "require_artifact": False, "metrics": {},
        "created_time": "2026-09-01T00:00:00+00:00", "updated_time": "2026-09-01T00:00:00+00:00",
    }
    (tmp_path / f"{legacy_id}.json").write_text(json.dumps(payload), encoding="utf-8")

    loaded = TaskManager(FileTaskStore(tmp_path)).get_task(legacy_id)

    assert loaded.plan_events == []
    assert loaded.steps[0].plan_step_id is None
    assert loaded.steps[0].tool_call_id is None
    assert loaded.to_dict()["plan_events"] == []


def test_execution_binding_is_persisted_and_visible_through_the_api(tmp_path):
    """End-to-end without a real LLM: plan -> bound call -> judged completion -> events."""
    from agent.executor import Executor
    from tools.base_tool import BaseTool, ToolResult
    from tools.tool_registry import ToolRegistry

    class EchoTool(BaseTool):
        name = "echo"
        description = "echo text"

        def parameters_schema(self):
            return {
                "type": "object", "additionalProperties": False,
                "properties": {"text": {"type": "string"}}, "required": ["text"],
            }

        def execute(self, text):
            return ToolResult(success=True, output=text)

    def _call(name, arguments, call_id):
        return SimpleNamespace(
            id=call_id, type="function",
            function=SimpleNamespace(name=name, arguments=arguments),
        )

    class FullFlowLLM:
        """One planner turn, then a bound tool call, a completion and a final answer."""

        def __init__(self):
            self.turns = 0

        def chat(self, messages, tools=None, tool_choice=None, on_event=None):
            if tool_choice and tool_choice["function"]["name"] == "create_plan":
                message = SimpleNamespace(content=None, tool_calls=[_call("create_plan", json.dumps({
                    "steps": [{
                        "step_id": "read", "name": "read", "description": "read the source",
                        "tool": "echo", "depends_on": [],
                        "completion_criteria": ["The columns are identified"],
                    }],
                }), "plan_1")])
            else:
                # Only the execution loop advances the turn counter.
                turn, self.turns = self.turns, self.turns + 1
                if turn == 0:
                    message = SimpleNamespace(content=None, tool_calls=[
                        _call("echo", '{"text": "A,B", "plan_step_id": "read"}', "call_1")
                    ])
                elif turn == 1:
                    message = SimpleNamespace(content=None, tool_calls=[
                        _call("complete_plan_step",
                              '{"step_id": "read", "evidence": "columns A and B identified"}',
                              "call_2")
                    ])
                else:
                    message = SimpleNamespace(content="done", tool_calls=[])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    registry = ToolRegistry()
    registry.register(EchoTool())
    manager = TaskManager(FileTaskStore(tmp_path))
    llm = FullFlowLLM()
    planner = Planner(llm)
    executor = Executor(llm, registry, manager, max_iterations=4, planner=planner)
    task = AgentOrchestrator(manager, planner, executor).run("summarize the workbook")

    assert task.status.value == "SUCCESS"
    restarted = TaskManager(FileTaskStore(tmp_path))
    with TestClient(create_app(
        SimpleNamespace(task_manager=restarted, run_task=lambda _: None),
        Settings(_env_file=None),
    )) as client:
        payload = client.get(f"/tasks/{task.id}").json()

    assert payload["plan"]["version"] == 1
    assert payload["steps"][0]["plan_step_id"] == "read"
    assert payload["steps"][0]["tool_call_id"] == "call_1"
    assert [event["kind"] for event in payload["plan_events"]] == [
        "plan_created", "plan_step_bound", "plan_step_completed", "plan_finished",
    ]
    assert payload["plan_events"][-1]["satisfied"] is True
