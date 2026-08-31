"""Executor tests with a fake LLM and fake tool."""

from types import SimpleNamespace

import pytest

from agent.executor import Executor, MaxIterationsError
from agent.planner import Plan, PlanStep
from task.task_manager import TaskManager
from task.task_model import StepStatus
from tools.base_tool import BaseTool, ToolResult
from tools.tool_registry import ToolRegistry


class EchoTool(BaseTool):
    name = "echo"
    description = "echo text"

    def parameters_schema(self):
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    def execute(self, text):
        return ToolResult(success=True, output=text)


class BoomTool(BaseTool):
    name = "boom"
    description = "always raises"

    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        raise RuntimeError("boom")


def _tool_call(name, arguments):
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, tools=None, tool_choice=None):
        self.calls.append({"messages": messages, "tools": tools})
        return SimpleNamespace(choices=[SimpleNamespace(message=self.responses.pop(0))])


def _make_executor(llm, registry):
    task_manager = TaskManager()
    return task_manager, Executor(llm, registry, task_manager, max_iterations=4)


def test_executor_runs_tool_then_returns_final_answer():
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = FakeLLM(
        [
            _message(tool_calls=[_tool_call("echo", '{"text": "hi"}')]),
            _message(content="final answer"),
        ]
    )
    task_manager, executor = _make_executor(llm, registry)
    task = task_manager.create_task("x")
    task_manager.start_task(task.id)
    plan = Plan(user_input="x", steps=[PlanStep(name="echo")])

    answer = executor.run(task.id, "x", plan)

    assert answer == "final answer"
    steps = task_manager.get_task(task.id).steps
    assert len(steps) == 1
    assert steps[0].status == StepStatus.SUCCESS
    assert steps[0].tool == "echo"
    assert len(llm.calls) == 2


def test_executor_records_failed_step_and_continues():
    registry = ToolRegistry()
    registry.register(BoomTool())
    llm = FakeLLM(
        [
            _message(tool_calls=[_tool_call("boom", "{}")]),
            _message(content="recovered"),
        ]
    )
    task_manager, executor = _make_executor(llm, registry)
    task = task_manager.create_task("x")
    task_manager.start_task(task.id)
    plan = Plan(user_input="x", steps=[PlanStep(name="boom")])

    answer = executor.run(task.id, "x", plan)

    assert answer == "recovered"
    steps = task_manager.get_task(task.id).steps
    assert steps[0].status == StepStatus.FAILED
    assert "boom" in steps[0].error


def test_executor_raises_when_max_iterations_exceeded():
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = FakeLLM([_message(tool_calls=[_tool_call("echo", '{"text": "loop"}')])] * 10)
    task_manager, executor = _make_executor(llm, registry)
    task = task_manager.create_task("x")
    task_manager.start_task(task.id)
    plan = Plan(user_input="x", steps=[PlanStep(name="echo")])

    with pytest.raises(MaxIterationsError):
        executor.run(task.id, "x", plan)

    steps = task_manager.get_task(task.id).steps
    assert len(steps) == 4  # max_iterations
