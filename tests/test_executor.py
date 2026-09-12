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

    def chat(self, messages, tools=None, tool_choice=None, on_event=None):
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


class ArtifactTool(BaseTool):
    name = "save_report"
    description = "pretends to persist a report"

    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        return ToolResult(
            success=True,
            output="http://localhost:9000/doc-agent-storage/reports/out.csv",
            metadata={"kind": "artifact", "oss_key": "reports/out.csv", "bytes": 12},
        )


def test_tool_metadata_is_recorded_as_a_task_artifact():
    registry = ToolRegistry()
    registry.register(ArtifactTool())
    llm = FakeLLM(
        [
            _message(tool_calls=[_tool_call("save_report", "{}")]),
            _message(content="saved"),
        ]
    )
    task_manager, executor = _make_executor(llm, registry)
    task = task_manager.create_task("x")
    task_manager.start_task(task.id)

    executor.run(task.id, "x", Plan(user_input="x"))

    assert task_manager.get_task(task.id).artifacts == [
        {"oss_key": "reports/out.csv", "bytes": 12}
    ]


class NonArtifactMetadataTool(BaseTool):
    name = "read_file"
    description = "returns bookkeeping metadata that is not a deliverable"

    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        return ToolResult(success=True, output="text", metadata={"chars": 4, "source": "a.pdf"})


def test_non_artifact_metadata_is_not_recorded_as_a_deliverable():
    registry = ToolRegistry()
    registry.register(NonArtifactMetadataTool())
    llm = FakeLLM(
        [
            _message(tool_calls=[_tool_call("read_file", "{}")]),
            _message(content="done"),
        ]
    )
    task_manager, executor = _make_executor(llm, registry)
    task = task_manager.create_task("x")
    task_manager.start_task(task.id)

    executor.run(task.id, "x", Plan(user_input="x"))

    assert task_manager.get_task(task.id).artifacts == []


def test_staged_input_paths_are_given_to_the_model():
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = FakeLLM([_message(content="ok")])
    task_manager, executor = _make_executor(llm, registry)
    task = task_manager.create_task("summarise the workbook")
    task_manager.set_workspace_dir(task.id, f"/home/gem/workspace/tasks/{task.id}")
    task_manager.set_input_files(
        task.id,
        [
            {
                "oss_key": "raw/sales.xlsx",
                "sandbox_path": f"/home/gem/workspace/tasks/{task.id}/sales.xlsx",
                "bytes": 2048,
            }
        ],
    )
    task = task_manager.get_task(task.id)
    task_manager.start_task(task.id)

    executor.run(task.id, "summarise the workbook", Plan(user_input="x"))

    prompt = llm.calls[0]["messages"][1]["content"]
    assert f"This task owns the sandbox directory: /home/gem/workspace/tasks/{task.id}" in prompt
    assert f"/home/gem/workspace/tasks/{task.id}/sales.xlsx" in prompt
    assert "2048 bytes" in prompt


def test_no_input_section_when_nothing_was_staged():
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = FakeLLM([_message(content="ok")])
    task_manager, executor = _make_executor(llm, registry)
    task = task_manager.create_task("plain task")
    task_manager.start_task(task.id)

    executor.run(task.id, "plain task", Plan(user_input="x"))

    assert "owns the sandbox directory" not in llm.calls[0]["messages"][1]["content"]


def test_executor_binds_tool_calls_to_the_task_directory():
    """Tool arguments are rewritten into the task's own sandbox namespace."""
    registry = ToolRegistry()
    registry.register(EchoTool())
    llm = FakeLLM(
        [
            _message(tool_calls=[_tool_call("echo", '{"text": "hi"}')]),
            _message(content="done"),
        ]
    )
    task_manager, executor = _make_executor(llm, registry)
    task = task_manager.create_task("x")
    task_manager.set_workspace_dir(task.id, f"/home/gem/workspace/tasks/{task.id}")
    task_manager.start_task(task.id)

    calls = {}

    def spy(name, arguments, task_id=None):
        calls["name"], calls["task_id"] = name, task_id
        return ToolResult(success=True, output="ok")

    executor._registry = SimpleNamespace(
        to_openai_tools=registry.to_openai_tools, execute=spy
    )

    executor.run(task.id, "x", Plan(user_input="x"))

    assert calls == {"name": "echo", "task_id": task.id}
