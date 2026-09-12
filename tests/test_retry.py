"""Retry: policy, error classification, and Executor integration."""

from types import SimpleNamespace

from agent.executor import Executor
from agent.planner import Plan, PlanStep
from retry.retry_policy import RetryPolicy, classify_exception
from task.task_manager import TaskManager
from task.task_model import StepStatus
from tools.base_tool import BaseTool, ErrorType, ToolResult
from tools.tool_registry import ToolRegistry


class FlakyTool(BaseTool):
    retry_safe = True  # Fake operation has no side effects.
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
        return ToolResult(success=True, output="ok")


class AlwaysFailTool(BaseTool):
    retry_safe = True  # Fake operation has no side effects.
    name = "always_fail"
    description = "always fails transiently"

    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        return ToolResult(success=False, error="backend down", error_type=ErrorType.TRANSIENT)


class InvalidArgTool(BaseTool):
    name = "invalid"
    description = "bad arguments"

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


class RaisingTool(BaseTool):
    retry_safe = True  # Fake operation has no side effects.
    name = "raising"
    description = "raises a connection error"

    def parameters_schema(self):
        return {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        raise ConnectionError("network down")


def _tool_call(name):
    return SimpleNamespace(
        id="c1", type="function", function=SimpleNamespace(name=name, arguments="{}")
    )


def _msg(content=None, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls or [])


class ScriptedLLM:
    def __init__(self, tool_name):
        self.tool_name = tool_name
        self.calls = 0

    def chat(self, messages, tools=None, tool_choice=None, on_event=None):
        self.calls += 1
        if self.calls == 1:
            message = _msg(tool_calls=[_tool_call(self.tool_name)])
        else:
            message = _msg(content="final answer")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _run_tool(tool, max_attempts=3):
    task_manager = TaskManager()
    registry = ToolRegistry()
    registry.register(tool)
    policy = RetryPolicy(max_attempts=max_attempts, base_delay=0, jitter=False)
    executor = Executor(ScriptedLLM(tool.name), registry, task_manager, retry_policy=policy)
    task = task_manager.create_task("x")
    task_manager.start_task(task.id)
    executor.run(task.id, "x", Plan(user_input="x", steps=[PlanStep(name=tool.name)]))
    return task_manager.get_task(task.id)


def test_error_type_retryable():
    assert ErrorType.TRANSIENT.retryable is True
    assert ErrorType.TIMEOUT.retryable is True
    assert ErrorType.INVALID_ARGUMENT.retryable is False
    assert ErrorType.PERMISSION_DENIED.retryable is False
    assert ErrorType.EXECUTION.retryable is False
    assert ErrorType.BUSINESS.retryable is False


def test_classify_exception():
    assert classify_exception(TimeoutError("t")) == ErrorType.TIMEOUT
    assert classify_exception(ConnectionError("c")) == ErrorType.TRANSIENT
    assert classify_exception(ValueError("v")) == ErrorType.INVALID_ARGUMENT


def test_policy_retryable_and_non_retryable():
    policy = RetryPolicy(jitter=False)
    decision = policy.decide(1, ErrorType.TRANSIENT)
    assert decision.should_retry is True
    assert decision.reason == "retryable_TRANSIENT"
    assert decision.final is False

    decision = policy.decide(1, ErrorType.INVALID_ARGUMENT)
    assert decision.should_retry is False
    assert decision.reason == "non_retryable_INVALID_ARGUMENT"
    assert decision.final is True


def test_policy_max_attempts():
    policy = RetryPolicy(max_attempts=3, jitter=False)
    decision = policy.decide(3, ErrorType.TRANSIENT)
    assert decision.should_retry is False
    assert decision.reason == "max_attempts_exhausted"
    assert decision.final is True


def test_policy_exponential_backoff_without_jitter():
    policy = RetryPolicy(
        max_attempts=5, base_delay=1.0, backoff_factor=2.0, max_delay=10.0, jitter=False
    )
    assert policy.decide(1, ErrorType.TRANSIENT).delay_seconds == 1.0
    assert policy.decide(2, ErrorType.TRANSIENT).delay_seconds == 2.0
    assert policy.decide(3, ErrorType.TRANSIENT).delay_seconds == 4.0


def test_policy_jitter_stays_in_range():
    policy = RetryPolicy(base_delay=1.0, max_delay=10.0, jitter=True)
    for _ in range(50):
        delay = policy.decide(1, ErrorType.TRANSIENT).delay_seconds
        assert 0.5 <= delay <= 1.5


def test_flaky_tool_retries_then_succeeds():
    task = _run_tool(FlakyTool())
    step = task.steps[0]
    assert step.status == StepStatus.SUCCESS
    assert step.attempts == 2
    events = task.metrics["retry_events"]
    assert [e["final_status"] for e in events] == ["RETRYING", "SUCCESS"]
    assert events[0]["error_type"] == "TRANSIENT"


def test_always_fail_exhausts_attempts():
    task = _run_tool(AlwaysFailTool())
    step = task.steps[0]
    assert step.status == StepStatus.FAILED
    assert step.attempts == 3
    events = task.metrics["retry_events"]
    assert events[-1]["final_status"] == "FAILED"
    assert events[-1]["retry_reason"] == "max_attempts_exhausted"


def test_invalid_argument_is_not_retried():
    task = _run_tool(InvalidArgTool())
    step = task.steps[0]
    assert step.attempts == 1
    assert step.status == StepStatus.FAILED
    assert task.metrics["retry_events"][0]["retry_reason"] == "non_retryable_INVALID_ARGUMENT"


def test_permission_denied_is_not_retried():
    task = _run_tool(PermissionTool())
    step = task.steps[0]
    assert step.attempts == 1
    assert task.metrics["retry_events"][0]["retry_reason"] == "non_retryable_PERMISSION_DENIED"


def test_raised_connection_error_is_classified_transient():
    task = _run_tool(RaisingTool())
    step = task.steps[0]
    assert step.attempts == 3
    assert task.metrics["retry_events"][0]["error_type"] == "TRANSIENT"


def test_retry_events_have_all_fields():
    task = _run_tool(FlakyTool())
    event = task.metrics["retry_events"][0]
    for field in (
        "task_id",
        "tool_name",
        "attempt",
        "error_type",
        "error_message",
        "retry_reason",
        "duration_ms",
        "final_status",
    ):
        assert field in event
