"""Harness V1 acceptance scenarios with deterministic fake model responses.

These tests exercise the public orchestration path instead of isolated helpers:
Orchestrator -> Planner -> Executor -> Tool Registry -> Task state.  They are kept
small and scripted so each scenario doubles as an executable learning example.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from agent.executor import Executor, MaxIterationsError, ReplanBudgetExceededError
from agent.orchestrator import AgentOrchestrator
from context.compactor import SUMMARY_MARKER, ContextCompactor
from context.hooks import AfterToolCallHook
from context.manager import ContextManager
from context.token_estimator import TokenEstimate
from context.tool_output_store import ToolOutputStore
from security.permission_manager import PermissionManager
from task.plan_model import Plan
from task.task_manager import TaskManager
from task.task_model import StepStatus, TaskStatus
from tools.base_tool import BaseTool, ErrorType, ToolResult
from tools.tool_output_tool import ReadToolOutputTool
from tools.tool_registry import ToolRegistry


def _tool_call(name: str, arguments: dict, call_id: str):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _message(content: str | None = None, tool_calls: list | None = None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


def _response(message):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=None,
    )


def _step(step_id: str, *, tool: str | None = None, depends_on: tuple[str, ...] = ()):
    return {
        "step_id": step_id,
        "name": step_id,
        "description": f"execute {step_id}",
        "tool": tool,
        "depends_on": list(depends_on),
        "completion_criteria": [f"observable evidence for {step_id}"],
    }


def _plan(user_input: str, *steps: dict, version: int = 1) -> Plan:
    return Plan.from_dict(
        {"user_input": user_input, "version": version, "steps": list(steps)}
    )


class ScriptedPlanner:
    """Return explicit plans so a test controls planning without a real provider."""

    def __init__(self, initial: Plan, replans: list[Plan] | None = None) -> None:
        self.initial = initial
        self.replans = list(replans or [])
        self.replan_calls: list[dict] = []

    def plan(self, user_input: str, on_event=None) -> Plan:
        assert user_input == self.initial.user_input
        return self.initial

    def replan(
        self,
        user_input,
        plan,
        *,
        completed_steps,
        observations,
        available_tools,
        on_event=None,
    ) -> Plan:
        self.replan_calls.append(
            {
                "user_input": user_input,
                "plan": plan,
                "completed_steps": completed_steps,
                "observations": observations,
                "available_tools": available_tools,
            }
        )
        return self.replans.pop(0)


class RoutedFakeLLM:
    """Keep the Executor and compaction response streams separate and inspectable."""

    def __init__(self, main: list, compact: list | None = None) -> None:
        self.main = list(main)
        self.compact = list(compact or [])
        self.calls: list[dict] = []

    def chat(self, messages, tools=None, tool_choice=None, on_event=None):
        is_compaction = bool(
            isinstance(tool_choice, dict)
            and tool_choice.get("function", {}).get("name") == "compact_context"
        )
        self.calls.append(
            {
                "kind": "compact" if is_compaction else "main",
                "messages": copy.deepcopy(messages),
                "tools": copy.deepcopy(tools),
            }
        )
        stream = self.compact if is_compaction else self.main
        outcome = stream.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _response(outcome)


class FixedTool(BaseTool):
    description = "Return one deterministic result"

    def __init__(self, name: str, results: list[ToolResult] | None = None) -> None:
        self.name = name
        self.results = list(results or [ToolResult(True, output=f"{name} ok")])
        self.calls: list[dict] = []

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        }

    def execute(self, value: str) -> ToolResult:
        self.calls.append({"value": value})
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


class SummaryAwareEstimator:
    """Deterministic budget model for testing control flow, not token accuracy."""

    def estimate(self, messages, tools=None) -> TokenEstimate:
        has_summary = any(
            isinstance(message.get("content"), str)
            and message["content"].startswith(SUMMARY_MARKER)
            for message in messages
        )
        tokens = 150 if has_summary else len(messages) * 100
        return TokenEstimate(tokens=tokens, characters=tokens * 4, method="test")

    def observe_actual(self, messages, tools, prompt_tokens) -> bool:
        return False


def _summary_call(call_id: str = "compact_1"):
    return _message(
        tool_calls=[
            _tool_call(
                "compact_context",
                {
                    "goal": "ignored: runtime owns this field",
                    "active_plan": "ignored: runtime owns this field",
                    "constraints": [],
                    "confirmed_facts": ["the earlier tool call succeeded"],
                    "result_references": [],
                    "completed_work": [],
                    "unfinished_work": ["work"],
                    "important_errors": [],
                },
                call_id,
            )
        ]
    )


def _context_manager(llm, *, failure_threshold: int = 2) -> ContextManager:
    return ContextManager(
        SummaryAwareEstimator(),
        ContextCompactor(llm, max_attempts=1),
        target_tokens=180,
        soft_limit=300,
        hard_limit=800,
        recent_groups=0,
        failure_threshold=failure_threshold,
        summary_max_chars=1000,
    )


def _build_harness(
    plan: Plan,
    llm,
    tools: list[BaseTool],
    *,
    planner: ScriptedPlanner | None = None,
    registry: ToolRegistry | None = None,
    after_tool_call: AfterToolCallHook | None = None,
    context_manager: ContextManager | None = None,
    max_iterations: int = 8,
    max_replans: int = 2,
):
    planner = planner or ScriptedPlanner(plan)
    registry = registry or ToolRegistry()
    for tool in tools:
        registry.register(tool)
    manager = TaskManager()
    executor = Executor(
        llm,
        registry,
        manager,
        max_iterations=max_iterations,
        planner=planner,
        max_replans=max_replans,
        after_tool_call=after_tool_call,
        context_manager=context_manager,
    )
    return SimpleNamespace(
        orchestrator=AgentOrchestrator(manager, planner, executor),
        manager=manager,
        planner=planner,
        llm=llm,
    )


def _event_kinds(task, key: str) -> list[str]:
    return [event["kind"] for event in task.metrics.get(key, [])]


def test_static_plan_completes_through_the_full_harness_path():
    goal = "summarize the document"
    plan = _plan(goal, _step("summarize", tool="summarize"))
    llm = RoutedFakeLLM(
        [
            _message(
                tool_calls=[
                    _tool_call(
                        "summarize",
                        {"value": "document", "plan_step_id": "summarize"},
                        "call_work",
                    )
                ]
            ),
            _message(
                tool_calls=[
                    _tool_call(
                        "complete_plan_step",
                        {"step_id": "summarize", "evidence": "summary produced"},
                        "call_complete",
                    )
                ]
            ),
            _message(content="summary ready"),
        ]
    )
    harness = _build_harness(plan, llm, [FixedTool("summarize")])

    task = harness.orchestrator.run(goal)

    assert task.status is TaskStatus.SUCCESS
    assert task.result == {"answer": "summary ready"}
    assert task.steps[0].plan_step_id == "summarize"
    assert task.steps[0].status is StepStatus.SUCCESS
    assert any(event["kind"] == "plan_step_completed" for event in task.plan_events)


def test_tool_failure_is_observed_then_the_model_changes_route():
    goal = "extract text using an available route"
    plan = _plan(goal, _step("extract"))
    primary = FixedTool(
        "primary_extract",
        [ToolResult(False, error="parser unavailable", error_type=ErrorType.BUSINESS)],
    )
    fallback = FixedTool("fallback_extract", [ToolResult(True, output="recovered text")])
    llm = RoutedFakeLLM(
        [
            _message(tool_calls=[_tool_call(
                "primary_extract", {"value": "input", "plan_step_id": "extract"}, "primary"
            )]),
            _message(tool_calls=[_tool_call(
                "fallback_extract", {"value": "input", "plan_step_id": "extract"}, "fallback"
            )]),
            _message(tool_calls=[_tool_call(
                "complete_plan_step", {"step_id": "extract", "evidence": "text recovered"}, "complete"
            )]),
            _message(content="used fallback"),
        ]
    )
    harness = _build_harness(plan, llm, [primary, fallback])

    task = harness.orchestrator.run(goal)

    assert task.status is TaskStatus.SUCCESS
    assert [step.status for step in task.steps] == [StepStatus.FAILED, StepStatus.SUCCESS]
    assert [step.tool for step in task.steps] == ["primary_extract", "fallback_extract"]
    assert task.plan.version == 1
    assert not any(event["kind"] == "plan_replanned" for event in task.plan_events)


def test_missing_data_requests_one_local_replan_then_finishes_the_new_step():
    goal = "analyze whichever source is available"
    initial = _plan(goal, _step("read_primary", tool="read_source"))
    replacement = _plan(goal, _step("read_fallback", tool="read_source"), version=2)
    planner = ScriptedPlanner(initial, [replacement])
    llm = RoutedFakeLLM(
        [
            _message(tool_calls=[_tool_call(
                "request_replan",
                {"reason_code": "missing_data", "reason": "primary source is absent"},
                "replan",
            )]),
            _message(tool_calls=[_tool_call(
                "read_source", {"value": "fallback", "plan_step_id": "read_fallback"}, "read"
            )]),
            _message(tool_calls=[_tool_call(
                "complete_plan_step",
                {"step_id": "read_fallback", "evidence": "fallback source loaded"},
                "complete",
            )]),
            _message(content="analysis complete"),
        ]
    )
    harness = _build_harness(
        initial, llm, [FixedTool("read_source")], planner=planner
    )

    task = harness.orchestrator.run(goal)

    assert task.status is TaskStatus.SUCCESS
    assert task.plan.version == 2
    assert [step.step_id for step in task.plan.steps] == ["read_fallback"]
    assert len(planner.replan_calls) == 1
    replanned = [event for event in task.plan_events if event["kind"] == "plan_replanned"]
    assert replanned[0]["reason_code"] == "missing_data"


class RecallAwareLLM:
    """Read the opaque reference from one observation and call the recall tool."""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, tool_choice=None, on_event=None):
        self.calls.append(copy.deepcopy(messages))
        turn = len(self.calls)
        if turn == 1:
            message = _message(tool_calls=[_tool_call(
                "large_result", {"value": "all", "plan_step_id": "inspect"}, "large"
            )])
        elif turn == 2:
            observation = json.loads(messages[-1]["content"])
            message = _message(tool_calls=[_tool_call(
                "read_tool_output",
                {
                    "result_ref": observation["result_ref"],
                    "offset": 0,
                    "limit": 20,
                    "plan_step_id": "inspect",
                },
                "read_page",
            )])
        elif turn == 3:
            message = _message(tool_calls=[_tool_call(
                "complete_plan_step",
                {"step_id": "inspect", "evidence": "needed page inspected"},
                "complete",
            )])
        else:
            message = _message(content="inspection complete")
        return _response(message)


def test_large_result_is_offloaded_and_recalled_one_bounded_page(tmp_path):
    goal = "inspect a large tool result"
    plan = _plan(goal, _step("inspect", tool="large_result"))
    tail = "TAIL_MUST_NOT_ENTER_THE_PROMPT"
    large_text = "A" * 1000 + tail
    store = ToolOutputStore(tmp_path / "tool-output", max_read_chars=20)
    hook = AfterToolCallHook(store, inline_chars=400, preview_chars=40)
    permissions = PermissionManager(
        allowed_permissions=frozenset({"tool_output.read"})
    )
    registry = ToolRegistry(permissions)
    llm = RecallAwareLLM()
    harness = _build_harness(
        plan,
        llm,
        [
            FixedTool("large_result", [ToolResult(True, output=large_text)]),
            ReadToolOutputTool(store),
        ],
        registry=registry,
        after_tool_call=hook,
    )

    task = harness.orchestrator.run(goal)

    assert task.status is TaskStatus.SUCCESS
    assert task.steps[0].result_truncated is True
    assert task.steps[0].result_ref.startswith("out_")
    assert task.steps[1].tool == "read_tool_output"
    assert "A" * 20 in (task.steps[1].output or "")
    assert tail not in json.dumps(llm.calls[1], ensure_ascii=False)
    assert "context_output_offloaded" in _event_kinds(task, "context_events")


def test_soft_limit_compacts_complete_history_before_the_next_model_call():
    goal = "finish a long-running document step"
    plan = _plan(goal, _step("work", tool="work"))
    llm = RoutedFakeLLM(
        [
            _message(tool_calls=[_tool_call(
                "work", {"value": "first", "plan_step_id": "work"}, "work"
            )]),
            _message(tool_calls=[_tool_call(
                "complete_plan_step", {"step_id": "work", "evidence": "work done"}, "complete"
            )]),
            _message(content="done"),
        ],
        compact=[_summary_call()],
    )
    harness = _build_harness(
        plan,
        llm,
        [FixedTool("work")],
        context_manager=_context_manager(llm),
    )

    task = harness.orchestrator.run(goal)

    assert task.status is TaskStatus.SUCCESS
    assert [call["kind"] for call in llm.calls].count("compact") == 1
    assert "context_compacted" in _event_kinds(task, "context_events")
    final_main = [call for call in llm.calls if call["kind"] == "main"][-1]
    assert any(
        isinstance(message.get("content"), str)
        and message["content"].startswith(SUMMARY_MARKER)
        for message in final_main["messages"]
    )


def test_repeated_compaction_failure_opens_the_circuit_and_uses_safe_trim():
    goal = "continue despite summary-provider failure"
    plan = _plan(goal, _step("work", tool="work"))
    tool = FixedTool(
        "work",
        [ToolResult(True, output="first"), ToolResult(True, output="second")],
    )
    llm = RoutedFakeLLM(
        [
            _message(tool_calls=[_tool_call(
                "work", {"value": "first", "plan_step_id": "work"}, "work_1"
            )]),
            _message(tool_calls=[_tool_call(
                "work", {"value": "second", "plan_step_id": "work"}, "work_2"
            )]),
            _message(tool_calls=[_tool_call(
                "complete_plan_step", {"step_id": "work", "evidence": "both calls done"}, "complete"
            )]),
            _message(content="done after fallback"),
        ],
        compact=[RuntimeError("summary unavailable"), RuntimeError("still unavailable")],
    )
    harness = _build_harness(
        plan,
        llm,
        [tool],
        context_manager=_context_manager(llm, failure_threshold=2),
    )

    task = harness.orchestrator.run(goal)

    assert task.status is TaskStatus.SUCCESS
    assert [call["kind"] for call in llm.calls].count("compact") == 2
    context_kinds = _event_kinds(task, "context_events")
    assert "context_circuit_opened" in context_kinds
    assert "context_deterministic_trimmed" in context_kinds


def test_maximum_execution_rounds_fail_the_task_instead_of_looping_forever():
    goal = "keep calling a tool"
    plan = _plan(goal, _step("work", tool="work"))
    llm = RoutedFakeLLM(
        [
            _message(tool_calls=[_tool_call(
                "work", {"value": "one", "plan_step_id": "work"}, "one"
            )]),
            _message(tool_calls=[_tool_call(
                "work", {"value": "two", "plan_step_id": "work"}, "two"
            )]),
        ]
    )
    harness = _build_harness(
        plan, llm, [FixedTool("work")], max_iterations=2
    )

    with pytest.raises(MaxIterationsError, match="exceeded 2"):
        harness.orchestrator.run(goal)

    task = harness.manager.list_tasks()[0]
    assert task.status is TaskStatus.FAILED
    assert len(task.steps) == 2


def test_maximum_replans_fail_the_task_instead_of_replanning_forever():
    goal = "find a source"
    initial = _plan(goal, _step("source_v1"))
    replacement = _plan(goal, _step("source_v2"), version=2)
    planner = ScriptedPlanner(initial, [replacement])
    llm = RoutedFakeLLM(
        [
            _message(tool_calls=[_tool_call(
                "request_replan",
                {"reason_code": "missing_data", "reason": "first source absent"},
                "replan_1",
            )]),
            _message(tool_calls=[_tool_call(
                "request_replan",
                {"reason_code": "missing_data", "reason": "second source absent"},
                "replan_2",
            )]),
        ]
    )
    harness = _build_harness(
        initial,
        llm,
        [],
        planner=planner,
        max_replans=1,
    )

    with pytest.raises(ReplanBudgetExceededError, match="replan budget exhausted"):
        harness.orchestrator.run(goal)

    task = harness.manager.list_tasks()[0]
    assert task.status is TaskStatus.FAILED
    assert task.plan.version == 2
    assert len(planner.replan_calls) == 1
    assert any(event["kind"] == "plan_replan_exhausted" for event in task.plan_events)
