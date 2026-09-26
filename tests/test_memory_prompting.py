"""P1-1: recalled memory is visible to both planning and execution prompts."""

import json
from types import SimpleNamespace

from agent.executor import Executor
from agent.planner import Plan, Planner
from task.task_manager import TaskManager
from tools.tool_registry import ToolRegistry


def _call(name, arguments):
    return SimpleNamespace(
        id="call-1",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


class RecordingLLM:
    def __init__(self, messages):
        self.responses = list(messages)
        self.calls = []

    def chat(self, messages, tools=None, tool_choice=None, on_event=None):
        self.calls.append(messages)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=self.responses.pop(0))], usage=None
        )


def _plan(goal):
    return Plan.from_dict({
        "user_input": goal,
        "version": 1,
        "steps": [{
            "step_id": "answer",
            "name": "answer",
            "description": "answer",
            "tool": None,
            "depends_on": [],
            "completion_criteria": ["answer returned"],
        }],
    })


def test_planner_prompt_contains_memory_without_changing_plan_identity():
    llm = RecordingLLM([_message(tool_calls=[_call("create_plan", {
        "steps": [{
            "step_id": "answer",
            "name": "answer",
            "description": "answer",
            "tool": None,
            "depends_on": [],
            "completion_criteria": ["answer returned"],
        }]
    })])])

    plan = Planner(llm).plan(
        "summarize sales",
        memory_context="[PREFERENCE] Prefer concise reports (source_task_id=old)",
    )

    assert "Prefer concise reports" in llm.calls[0][1]["content"]
    assert plan.user_input == "summarize sales"


def test_executor_prompt_contains_memory_as_pinned_initial_context():
    llm = RecordingLLM([_message(content="done")])
    manager = TaskManager()
    task = manager.create_task("summarize sales")
    manager.start_task(task.id)
    plan = _plan(task.user_input)
    manager.set_plan(task.id, plan)
    executor = Executor(llm, ToolRegistry(), manager)

    executor.run(
        task.id,
        task.user_input,
        plan,
        memory_context="[CONSTRAINT] Reports use CNY (source_task_id=old)",
    )

    assert "Reports use CNY" in llm.calls[0][1]["content"]
