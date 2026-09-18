"""Planner tests with a fake LLM."""

import json
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

    def chat(self, messages, tools=None, tool_choice=None, on_event=None):
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])


def test_plan_parses_steps_from_tool_call():
    args = '{"steps": [{"step_id": "read", "name": "read", "description": "read file", "tool": "file_tool", "depends_on": [], "completion_criteria": ["File content available"]}]}'
    llm = FakeLLM(_message(tool_calls=[_tool_call("create_plan", args)]))
    plan = Planner(llm).plan("analyze excel")
    assert len(plan.steps) == 1
    assert plan.steps[0].name == "read"
    assert plan.steps[0].tool == "file_tool"
    assert plan.steps[0].step_id == "read"
    assert plan.steps[0].completion_criteria == ["File content available"]
    assert plan.version == 1
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


@pytest.mark.parametrize("calls", [
    [_tool_call("other_tool", '{"steps": []}')],
    [_tool_call("create_plan", '{}'), _tool_call("create_plan", '{}')],
    [_tool_call("create_plan", 'null')],
    [_tool_call("create_plan", '[]')],
    [_tool_call("create_plan", '{"steps": "read"}')],
])
def test_invalid_tool_envelopes_are_planner_errors(calls):
    with pytest.raises(PlannerError):
        Planner(FakeLLM(_message(tool_calls=calls))).plan("x")


def test_cycle_in_model_output_is_rejected():
    steps = [
        {"step_id": sid, "name": sid, "description": "process",
         "depends_on": [dep], "completion_criteria": ["Evidence available"]}
        for sid, dep in [("a", "b"), ("b", "a")]
    ]
    llm = FakeLLM(_message(tool_calls=[_tool_call("create_plan", json.dumps({"steps": steps}))]))
    with pytest.raises(PlannerError, match="cycle"):
        Planner(llm).plan("x")


def test_empty_response_is_a_planner_error():
    llm = SimpleNamespace(chat=lambda *a, **k: SimpleNamespace(choices=[]))
    with pytest.raises(PlannerError):
        Planner(llm).plan("x")
