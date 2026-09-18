"""Planner tests with a fake LLM."""

import json
from types import SimpleNamespace

import pytest

from agent.planner import Plan, Planner, PlannerError


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


# --- P0-2: local replanning input and version handling -------------------------

def _plan_v1():
    return Plan.from_dict({
        "user_input": "summarize sales",
        "version": 1,
        "steps": [
            {"step_id": "read", "name": "read", "description": "read the workbook",
             "tool": "read_file", "depends_on": [],
             "completion_criteria": ["Columns are identified"]},
            {"step_id": "report", "name": "report", "description": "save the summary",
             "tool": "save_report", "depends_on": ["read"],
             "completion_criteria": ["Report exists"]},
        ],
    })


def _plan_v2_steps():
    return [
        {"step_id": "read", "name": "read", "description": "read the workbook",
         "tool": "read_file", "depends_on": [],
         "completion_criteria": ["Columns are identified"]},
        {"step_id": "report_v2", "name": "report", "description": "save the summary",
         "tool": "save_report", "depends_on": ["read"],
         "completion_criteria": ["Report exists"]},
    ]


def _completed_read():
    step = _plan_v1().steps[0]
    return {
        "step_id": step.step_id,
        "name": step.name,
        "description": step.description,
        "tool": step.tool,
        "depends_on": list(step.depends_on),
        "completion_criteria": list(step.completion_criteria),
        "evidence": "columns A and B found",
    }


def _replan(llm, plan=None, completed_steps=None, observations=None, available_tools=None):
    return Planner(llm).replan(
        "summarize sales", plan or _plan_v1(),
        completed_steps=completed_steps if completed_steps is not None else [_completed_read()],
        observations=observations if observations is not None else [
            {"step_id": "report", "error": "workbook missing", "error_type": "BUSINESS"}
        ],
        available_tools=available_tools if available_tools is not None else ["read_file", "save_report"],
    )


def test_replan_produces_the_next_version_and_feeds_the_planner_every_input():
    args = json.dumps({"steps": _plan_v2_steps()})
    llm = FakeLLM(_message(tool_calls=[_tool_call("create_plan", args)]))

    plan = _replan(llm)

    assert plan.version == 2
    assert [step.step_id for step in plan.steps] == ["read", "report_v2"]
    prompt = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
    assert "summarize sales" in prompt
    assert "read the workbook" in prompt and "save the summary" in prompt
    assert "columns A and B found" in prompt
    assert "workbook missing" in prompt
    assert "read_file" in prompt and "save_report" in prompt
    assert llm.calls[0]["tool_choice"]["function"]["name"] == "create_plan"


def test_replan_rejects_a_plan_that_rewrites_a_completed_step():
    steps = _plan_v2_steps()
    steps[0]["completion_criteria"] = ["Something else entirely"]
    llm = FakeLLM(_message(tool_calls=[_tool_call("create_plan", json.dumps({"steps": steps}))]))

    with pytest.raises(PlannerError, match="completed"):
        _replan(llm)


def test_replan_rejects_a_plan_that_drops_a_completed_step():
    dropped = _plan_v2_steps()[1]
    dropped["depends_on"] = []  # a valid standalone plan, but the finished step is gone
    llm = FakeLLM(_message(tool_calls=[_tool_call("create_plan", json.dumps({"steps": [dropped]}))]))

    with pytest.raises(PlannerError, match="completed"):
        _replan(llm)


def test_replan_without_a_tool_call_is_a_planner_error():
    llm = FakeLLM(_message(content="I would rather explain the plan in prose"))

    with pytest.raises(PlannerError, match="did not return a plan"):
        _replan(llm)
