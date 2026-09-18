"""P0-2: tool calls bind to plan steps, completion is judged, replanning is budgeted."""

import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent.executor import Executor, ReplanBudgetExceededError
from agent.orchestrator import AgentOrchestrator
from agent.planner import Plan, PlannerError
from task.plan_state import PlanRunState, PlanStepStatus
from task.task_manager import TaskManager
from task.task_model import TaskStatus
from tools.base_tool import BaseTool, ErrorType, ToolResult
from tools.tool_registry import ToolRegistry


def _tool_call(name, arguments, call_id="call_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, tools=None, tool_choice=None, on_event=None):
        self.calls.append({"messages": [dict(message) for message in messages], "tools": tools})
        return SimpleNamespace(choices=[SimpleNamespace(message=self.responses.pop(0))])


class RecordingTool(BaseTool):
    """Echo tool that refuses unknown arguments, so a leaked reserved key fails loudly."""

    name = "echo"
    description = "echo text"

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def parameters_schema(self):
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    def execute(self, text):
        self.calls.append({"text": text})
        if self.error:
            return ToolResult(success=False, error=self.error, error_type=ErrorType.BUSINESS)
        return ToolResult(success=True, output=text)


def _step(step_id, depends_on=(), tool=None):
    return {
        "step_id": step_id,
        "name": step_id,
        "description": f"do {step_id}",
        "tool": tool,
        "depends_on": list(depends_on),
        "completion_criteria": [f"{step_id} evidence"],
    }


def plan_v1():
    return Plan.from_dict({
        "user_input": "summarize sales",
        "version": 1,
        "steps": [_step("read", tool="echo"), _step("report", ["read"], tool="echo")],
    })


def plan_after_replan():
    return Plan.from_dict({
        "user_input": "summarize sales",
        "version": 2,
        "steps": [_step("read", tool="echo"), _step("report_v2", ["read"], tool="echo")],
    })


class ScriptedPlanner:
    def __init__(self, plan=None, replan_plan=None, replan_error=None):
        self.plan_result = plan
        self.replan_result = replan_plan
        self.replan_error = replan_error
        self.replan_calls = []

    def plan(self, user_input, on_event=None):
        return self.plan_result

    def replan(self, user_input, plan, *, completed_steps, observations, available_tools, on_event=None):
        self.replan_calls.append({
            "user_input": user_input,
            "plan": plan,
            "completed_steps": completed_steps,
            "observations": observations,
            "available_tools": available_tools,
        })
        if self.replan_error:
            raise self.replan_error
        if self.replan_result is not None:
            return self.replan_result
        steps = [_step(s.step_id) for s in plan.steps if s.step_id in
                 [c["step_id"] for c in completed_steps]]
        steps.append(_step("report_v2", [c["step_id"] for c in completed_steps]))
        return Plan.from_dict({
            "user_input": user_input, "version": plan.version + 1, "steps": steps,
        })


def make_executor(plan, responses, *, tool=None, planner=None, max_replans=2, max_iterations=8):
    registry = ToolRegistry()
    tool = tool or RecordingTool()
    registry.register(tool)
    llm = FakeLLM(responses)
    manager = TaskManager()
    task = manager.create_task(plan.user_input)
    manager.start_task(task.id)
    manager.set_plan(task.id, plan)
    executor = Executor(
        llm, registry, manager,
        max_iterations=max_iterations, planner=planner, max_replans=max_replans,
    )
    return SimpleNamespace(
        task=task, manager=manager, executor=executor, llm=llm, tool=tool, registry=registry
    )


def events(context):
    return context.manager.get_task(context.task.id).plan_events


def kinds(context):
    return [event["kind"] for event in events(context)]


def test_tool_call_is_traceable_to_its_plan_step_and_call_id():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call("echo", '{"text": "hi", "plan_step_id": "read"}')]),
        _message(content="done"),
    ])

    assert context.executor.run(context.task.id, "summarize sales", plan_v1()) == "done"

    step = context.manager.get_task(context.task.id).steps[0]
    assert step.plan_step_id == "read"
    assert step.tool_call_id == "call_1"
    bound = [event for event in events(context) if event["kind"] == "plan_step_bound"]
    assert len(bound) == 1
    assert bound[0]["step_id"] == "read"
    assert bound[0]["tool_call_id"] == "call_1"
    assert bound[0]["task_step_id"] == step.id
    assert bound[0]["implicit"] is False


def test_reserved_step_id_never_reaches_the_tool_arguments():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call("echo", '{"text": "hi", "plan_step_id": "read"}')]),
        _message(content="done"),
    ])

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    # RecordingTool declares additionalProperties: False, so a leaked key would fail.
    assert context.tool.calls == [{"text": "hi"}]
    assert context.manager.get_task(context.task.id).steps[0].status.value == "SUCCESS"


def test_a_successful_tool_call_does_not_complete_the_plan_step():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call("echo", '{"text": "hi", "plan_step_id": "read"}')]),
        _message(content="done"),
    ])

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    task = context.manager.get_task(context.task.id)
    state = PlanRunState(task.plan, task.plan_events)
    assert state.status("read") is PlanStepStatus.RUNNING
    assert "plan_step_completed" not in kinds(context)
    finished = [event for event in events(context) if event["kind"] == "plan_finished"][0]
    assert finished["satisfied"] is False
    assert finished["remaining_step_ids"] == ["read", "report"]


def test_completion_is_recorded_with_evidence_for_the_completion_criteria():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call(
            "complete_plan_step",
            '{"step_id": "read", "evidence": "columns A and B identified"}',
        )]),
        _message(content="done"),
    ])

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    task = context.manager.get_task(context.task.id)
    state = PlanRunState(task.plan, task.plan_events)
    assert state.status("read") is PlanStepStatus.SUCCESS
    assert state.ready_ids() == ["report"]
    completed = [event for event in events(context) if event["kind"] == "plan_step_completed"][0]
    assert completed["evidence"] == "columns A and B identified"
    assert completed["completion_criteria"] == ["read evidence"]
    assert completed["version"] == 1
    # A control call is not a tool invocation, so it leaves no TaskStep behind.
    assert task.steps == []


def test_completion_requires_satisfied_dependencies():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call(
            "complete_plan_step", '{"step_id": "report", "evidence": "report saved"}',
        )]),
        _message(content="done"),
    ])

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    task = context.manager.get_task(context.task.id)
    assert PlanRunState(task.plan, task.plan_events).status("report") is PlanStepStatus.PENDING
    assert "plan_step_completed" not in kinds(context)
    rejection = [event for event in events(context) if event["kind"] == "plan_completion_rejected"][0]
    assert rejection["step_id"] == "report"


def test_completion_requires_evidence():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call("complete_plan_step", '{"step_id": "read", "evidence": " "}')]),
        _message(content="done"),
    ])

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    assert "plan_step_completed" not in kinds(context)
    observation = context.llm.calls[1]["messages"][-1]
    assert observation["role"] == "tool"
    assert "evidence" in observation["content"]


def test_completed_step_cannot_be_replayed():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call(
            "complete_plan_step", '{"step_id": "read", "evidence": "columns found"}',
        )]),
        _message(tool_calls=[_tool_call("echo", '{"text": "again", "plan_step_id": "read"}', "call_2")]),
        _message(content="done"),
    ])

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    assert context.tool.calls == []
    assert context.manager.get_task(context.task.id).steps == []
    rejection = [event for event in events(context) if event["kind"] == "plan_binding_rejected"][0]
    assert rejection["tool_call_id"] == "call_2"
    observation = context.llm.calls[2]["messages"][-1]
    assert observation["tool_call_id"] == "call_2"
    assert "already complete" in observation["content"]


def test_step_with_unsatisfied_dependencies_is_not_scheduled():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call('echo', '{"text": "x", "plan_step_id": "report"}')]),
        _message(content="done"),
    ])

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    assert context.tool.calls == []
    rejection = [event for event in events(context) if event["kind"] == "plan_binding_rejected"][0]
    assert rejection["step_id"] == "report"
    assert "read" in rejection["violation"]


def test_single_ready_step_is_bound_implicitly():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call("echo", '{"text": "hi"}')]),
        _message(content="done"),
    ])

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    assert context.tool.calls == [{"text": "hi"}]
    assert context.manager.get_task(context.task.id).steps[0].plan_step_id == "read"
    bound = [event for event in events(context) if event["kind"] == "plan_step_bound"][0]
    assert bound["implicit"] is True


def test_an_unattributable_tool_call_runs_but_is_recorded_as_unbound():
    plan = Plan.from_dict({
        "user_input": "x", "version": 1,
        "steps": [_step("a"), _step("b")],
    })
    context = make_executor(plan, [
        _message(tool_calls=[_tool_call("echo", '{"text": "hi"}')]),
        _message(content="done"),
    ])

    context.executor.run(context.task.id, "x", plan)

    assert context.tool.calls == [{"text": "hi"}]
    assert context.manager.get_task(context.task.id).steps[0].plan_step_id is None
    unbound = [event for event in events(context) if event["kind"] == "plan_unbound_tool_call"][0]
    assert unbound["candidates"] == ["a", "b"]


def test_a_tool_error_only_feeds_back_an_observation():
    context = make_executor(
        plan_v1(),
        [
            _message(tool_calls=[_tool_call("echo", '{"text": "hi", "plan_step_id": "read"}')]),
            _message(content="done"),
        ],
        tool=RecordingTool(error="workbook missing"),
    )

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    task = context.manager.get_task(context.task.id)
    assert task.steps[0].status.value == "FAILED"
    failure = [event for event in events(context) if event["kind"] == "plan_step_failed"][0]
    assert failure["error"] == "workbook missing"
    assert "plan_replanned" not in kinds(context)
    assert task.plan.version == 1
    observation = context.llm.calls[1]["messages"][-1]
    assert observation["role"] == "tool" and "workbook missing" in observation["content"]


def test_replan_keeps_completed_steps_and_replaces_the_rest():
    planner = ScriptedPlanner(plan=plan_v1(), replan_plan=plan_after_replan())
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call(
            "complete_plan_step", '{"step_id": "read", "evidence": "columns found"}',
        )]),
        _message(tool_calls=[_tool_call(
            "request_replan", '{"reason_code": "missing_data", "reason": "source file is unreadable"}',
            "call_2",
        )]),
        _message(tool_calls=[_tool_call("echo", '{"text": "report", "plan_step_id": "report_v2"}', "call_3")]),
        _message(content="done"),
    ], planner=planner)

    assert context.executor.run(context.task.id, "summarize sales", plan_v1()) == "done"

    task = context.manager.get_task(context.task.id)
    assert task.plan.version == 2
    assert [step.step_id for step in task.plan.steps] == ["read", "report_v2"]
    assert asdict(task.plan.steps[0]) == asdict(plan_v1().steps[0])
    assert context.tool.calls == [{"text": "report"}]
    replanned = [event for event in events(context) if event["kind"] == "plan_replanned"][0]
    assert replanned["from_version"] == 1 and replanned["to_version"] == 2
    assert replanned["reason_code"] == "missing_data"
    assert replanned["reason"] == "source file is unreadable"
    assert replanned["preserved_step_ids"] == ["read"]


def test_replan_request_carries_goal_plan_progress_observations_and_tools():
    planner = ScriptedPlanner(plan=plan_v1(), replan_plan=plan_after_replan())
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call("echo", '{"text": "hi", "plan_step_id": "read"}')]),
        _message(tool_calls=[_tool_call(
            "request_replan", '{"reason_code": "tool_failure", "reason": "read failed"}',
        )]),
        _message(content="done"),
    ], tool=RecordingTool(error="workbook missing"), planner=planner)

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    call = planner.replan_calls[0]
    assert call["user_input"] == "summarize sales"
    assert call["plan"].version == 1
    assert call["completed_steps"] == []
    assert call["observations"] == [
        {"step_id": "read", "error": "workbook missing", "error_type": "BUSINESS"}
    ]
    assert call["available_tools"] == ["echo"]


def test_replan_requires_a_known_reason_code():
    planner = ScriptedPlanner(plan=plan_v1(), replan_plan=plan_after_replan())
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call(
            "request_replan", '{"reason_code": "because", "reason": "I want to"}',
        )]),
        _message(content="done"),
    ], planner=planner)

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    assert planner.replan_calls == []
    assert context.manager.get_task(context.task.id).plan.version == 1
    rejection = [event for event in events(context) if event["kind"] == "plan_replan_rejected"][0]
    assert rejection["reason_code"] == "because"


def test_replan_without_a_planner_is_only_an_observation():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call(
            "request_replan", '{"reason_code": "missing_data", "reason": "no source"}',
        )]),
        _message(content="done"),
    ])

    assert context.executor.run(context.task.id, "summarize sales", plan_v1()) == "done"

    assert context.manager.get_task(context.task.id).plan.version == 1
    rejection = [event for event in events(context) if event["kind"] == "plan_replan_rejected"][0]
    assert "not configured" in rejection["violation"]


def test_replan_budget_terminates_the_execution():
    planner = ScriptedPlanner(plan=plan_v1(), replan_plan=plan_after_replan())
    context = make_executor(
        plan_v1(),
        [
            _message(tool_calls=[_tool_call(
                "request_replan", '{"reason_code": "missing_data", "reason": "no source"}',
            )]),
            _message(tool_calls=[_tool_call(
                "request_replan", '{"reason_code": "missing_data", "reason": "still no source"}', "call_2",
            )]),
        ],
        planner=planner, max_replans=1,
    )

    with pytest.raises(ReplanBudgetExceededError):
        context.executor.run(context.task.id, "summarize sales", plan_v1())

    assert len(planner.replan_calls) == 1
    exhausted = [event for event in events(context) if event["kind"] == "plan_replan_exhausted"][0]
    assert exhausted["used"] == 1 and exhausted["max_replans"] == 1


def test_task_fails_clearly_when_the_replan_budget_is_exhausted():
    planner = ScriptedPlanner(plan=plan_v1(), replan_plan=plan_after_replan())
    registry = ToolRegistry()
    registry.register(RecordingTool())
    llm = FakeLLM([
        _message(tool_calls=[_tool_call(
            "request_replan", '{"reason_code": "missing_data", "reason": "no source"}',
        )]),
        _message(tool_calls=[_tool_call(
            "request_replan", '{"reason_code": "missing_data", "reason": "still no source"}', "call_2",
        )]),
    ])
    manager = TaskManager()
    executor = Executor(llm, registry, manager, planner=planner, max_replans=1)
    orchestrator = AgentOrchestrator(manager, planner, executor)

    with pytest.raises(ReplanBudgetExceededError):
        orchestrator.run("summarize sales")

    task = manager.list_tasks()[0]
    assert task.status is TaskStatus.FAILED
    assert "replan budget exhausted" in task.error


def test_control_calls_and_the_step_requirement_are_exposed_to_the_model():
    context = make_executor(plan_v1(), [_message(content="nothing to do")])

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    tools = context.llm.calls[0]["tools"]
    names = [schema["function"]["name"] for schema in tools]
    assert names == ["echo", "complete_plan_step", "request_replan"]
    echo = tools[0]["function"]["parameters"]
    assert "plan_step_id" in echo["properties"]
    assert "plan_step_id" in echo["required"]
    replan = tools[2]["function"]["parameters"]["properties"]
    assert sorted(replan["reason_code"]["enum"]) == [
        "criteria_unmet", "dependency_invalid", "missing_data", "tool_failure",
    ]


def test_system_prompt_reports_the_live_plan_progress():
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call(
            "complete_plan_step", '{"step_id": "read", "evidence": "columns found"}',
        )]),
        _message(content="done"),
    ])

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    second_prompt = context.llm.calls[1]["messages"][0]["content"]
    assert "Plan v1" in second_prompt
    assert "read" in second_prompt and "report" in second_prompt
    assert "complete_plan_step" in second_prompt


def test_replan_rejected_by_the_planner_keeps_the_current_plan():
    planner = ScriptedPlanner(plan=plan_v1(), replan_error=PlannerError("invalid plan: cycle"))
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call(
            "request_replan", '{"reason_code": "criteria_unmet", "reason": "cannot verify"}',
        )]),
        _message(content="done"),
    ], planner=planner)

    assert context.executor.run(context.task.id, "summarize sales", plan_v1()) == "done"

    assert context.manager.get_task(context.task.id).plan.version == 1
    rejection = [event for event in events(context) if event["kind"] == "plan_replan_rejected"][0]
    assert "cycle" in rejection["violation"]


def test_replanned_call_against_a_removed_step_is_rejected():
    planner = ScriptedPlanner(plan=plan_v1(), replan_plan=plan_after_replan())
    context = make_executor(plan_v1(), [
        _message(tool_calls=[_tool_call(
            "request_replan", '{"reason_code": "dependency_invalid", "reason": "source changed"}',
        )]),
        _message(tool_calls=[_tool_call("echo", '{"text": "stale", "plan_step_id": "report"}', "call_2")]),
        _message(content="done"),
    ], planner=planner)

    context.executor.run(context.task.id, "summarize sales", plan_v1())

    assert context.tool.calls == []
    rejection = [event for event in events(context) if event["kind"] == "plan_binding_rejected"][0]
    assert rejection["step_id"] == "report"
    assert "unknown plan step" in rejection["violation"]


def test_repair_run_uses_the_current_plan_version():
    """A replan inside one attempt must not be overwritten by the next attempt."""
    from agent.validator import ArtifactCheck, ArtifactValidator, ValidationResult

    seen = []

    class RepairingExecutor:
        def run(self, task_id, user_input, plan, repair_hint=None):
            seen.append(plan.version)
            manager.add_artifact(task_id, {"oss_key": "reports/out.csv", "bytes": 1})
            if len(seen) == 1:
                manager.replace_plan(task_id, plan_after_replan(), reason_code="missing_data",
                                     reason="source file is unreadable")
            return "done"

    class FailsOnce(ArtifactValidator):
        def __init__(self):
            self.calls = 0

        def validate(self, artifacts):
            self.calls += 1
            ok = self.calls > 1
            return ValidationResult(
                ok=ok,
                checks=[ArtifactCheck("reports/out.csv", ok, "ok" if ok else "missing")],
            )

    manager = TaskManager()
    orchestrator = AgentOrchestrator(
        manager, ScriptedPlanner(plan=plan_v1()), RepairingExecutor(),
        validator=FailsOnce(), max_recovery_attempts=1,
    )
    task = manager.create_task("summarize sales")

    result = orchestrator.run_task(task.id)

    assert seen == [1, 2]
    assert result.status is TaskStatus.SUCCESS
