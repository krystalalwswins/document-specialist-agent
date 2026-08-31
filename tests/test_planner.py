"""Planner tests with a fake LLM."""

from types import SimpleNamespace

import pytest

from agent.planner import Planner, PlannerError


def _tool_call(name, arguments):
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


class FakeLLM:
    def __init__(self, message):
        self.message = message
        self.calls = []

    def chat(self, messages, tools=None, tool_choice=None):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])


def test_plan_parses_steps_from_tool_call():
    args = '{"steps": [{"name": "read", "description": "read file", "tool": "file_tool"}]}'
    llm = FakeLLM(_message(tool_calls=[_tool_call("create_plan", args)]))
    plan = Planner(llm).plan("analyze excel")
    assert len(plan.steps) == 1
    assert plan.steps[0].name == "read"
    assert plan.steps[0].tool == "file_tool"
    assert llm.calls[0]["tool_choice"]["function"]["name"] == "create_plan"


def test_plan_requires_tool_call():
    llm = FakeLLM(_message(content="no plan here"))
    with pytest.raises(PlannerError, match="did not return a plan"):
        Planner(llm).plan("x")


def test_plan_rejects_empty_steps():
    args = '{"steps": []}'
    llm = FakeLLM(_message(tool_calls=[_tool_call("create_plan", args)]))
    with pytest.raises(PlannerError, match="empty"):
        Planner(llm).plan("x")
