"""Retry verification: run the real Executor + RetryPolicy against scenario tools.

Uses a deterministic scripted LLM so the retry behavior (which lives at the
tool layer) can be observed precisely. No sandbox / OSS / external LLM needed.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.executor import Executor
from agent.planner import Plan, PlanStep
from retry.retry_policy import RetryPolicy
from task.task_manager import TaskManager
from tools.base_tool import BaseTool, ErrorType, ToolResult
from tools.tool_registry import ToolRegistry


class FlakyTool(BaseTool):
    name = "flaky"
    description = "fails once then succeeds"

    def __init__(self):
        self.calls = 0

    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ToolResult(success=False, error="temporary hiccup", error_type=ErrorType.TRANSIENT)
        return ToolResult(success=True, output="succeeded on attempt 2")


class AlwaysFailTool(BaseTool):
    name = "always_fail"
    description = "always fails transiently"

    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        return ToolResult(success=False, error="backend down", error_type=ErrorType.TRANSIENT)


class InvalidArgTool(BaseTool):
    name = "invalid"
    description = "invalid argument"

    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        return ToolResult(success=False, error="bad argument", error_type=ErrorType.INVALID_ARGUMENT)


class PermissionTool(BaseTool):
    name = "forbidden"
    description = "permission denied"

    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        return ToolResult(success=False, error="access denied", error_type=ErrorType.PERMISSION_DENIED)


def _tool_call(name):
    return SimpleNamespace(
        id="c1", type="function", function=SimpleNamespace(name=name, arguments="{}")
    )


def _message(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


class ScriptedLLM:
    def __init__(self, tool_name):
        self.tool_name = tool_name
        self.calls = 0

    def chat(self, messages, tools=None, tool_choice=None):
        self.calls += 1
        if self.calls == 1:
            message = _message(tool_calls=[_tool_call(self.tool_name)])
        else:
            message = _message(content="final answer")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def run_scenario(title, tool):
    task_manager = TaskManager()
    registry = ToolRegistry()
    registry.register(tool)
    policy = RetryPolicy(
        max_attempts=3, base_delay=0.2, backoff_factor=2.0, max_delay=1.0, jitter=False
    )
    executor = Executor(ScriptedLLM(tool.name), registry, task_manager, retry_policy=policy)
    task = task_manager.create_task(title)
    task_manager.start_task(task.id)
    executor.run(task.id, title, Plan(user_input=title, steps=[PlanStep(name=tool.name)]))
    saved = task_manager.get_task(task.id)

    step = saved.steps[0]
    events = saved.metrics.get("retry_events", [])
    print(f"\n=== {title} ===")
    print(f"  step.status   = {step.status.value}")
    print(f"  step.attempts = {step.attempts}")
    print(f"  retry_events  = {len(events)}")
    for e in events:
        print(
            f"    attempt={e['attempt']} error_type={e['error_type']} "
            f"retry_reason={e['retry_reason']} final={e['final_status']}"
        )


if __name__ == "__main__":
    run_scenario("1. flaky: fail once then succeed", FlakyTool())
    run_scenario("2. always_fail: exhaust max_attempts", AlwaysFailTool())
    run_scenario("3. invalid_arg: non-retryable", InvalidArgTool())
    run_scenario("4. permission_denied: non-retryable", PermissionTool())
