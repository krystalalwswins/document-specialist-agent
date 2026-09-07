"""Deterministic offline verification of the actual Registry/Executor security path.

Run: python -m demo.security_demo. No LLM, Docker or storage credentials required.
"""

from types import SimpleNamespace

from agent.executor import Executor
from agent.planner import Plan
from retry.retry_policy import RetryPolicy
from security.permission_manager import PermissionManager
from task.task_manager import TaskManager
from tools.base_tool import BaseTool, ToolResult
from tools.tool_registry import ToolRegistry


class ProbeTool(BaseTool):
    name = "probe"
    description = "offline file-read probe"
    file_parameters = ("filename",)
    retry_safe = True

    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure

    def parameters_schema(self):
        return {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"], "additionalProperties": False}

    def execute(self, filename):
        self.calls += 1
        if self.failure:
            raise self.failure
        return ToolResult(True, output="probe completed")


class ScriptedLLM:
    def __init__(self, arguments):
        self.arguments = arguments
        self.requests = 0

    def chat(self, messages, **kwargs):
        self.requests += 1
        call = SimpleNamespace(id="probe-call", function=SimpleNamespace(name="probe", arguments=self.arguments))
        message = SimpleNamespace(content=None, tool_calls=[call]) if self.requests == 1 else SimpleNamespace(content="verified", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def scenario(title, arguments, *, allowed=True, failure=None, retry_safe=True, expected_calls=0, expected_error=None):
    tool = ProbeTool(failure)
    tool.retry_safe = retry_safe
    registry = ToolRegistry(PermissionManager(allowed_tools=frozenset({"probe"}) if allowed else frozenset()))
    registry.register(tool)
    manager = TaskManager()
    task = manager.create_task(title)
    manager.start_task(task.id)
    executor = Executor(ScriptedLLM(arguments), registry, manager, retry_policy=RetryPolicy(base_delay=0, jitter=False))
    executor.run(task.id, title, Plan(title))
    event = task.metrics["retry_events"][-1]
    assert tool.calls == expected_calls, (title, tool.calls)
    assert event["error_type"] == expected_error, (title, event)
    print(f"PASS {title}: actual_calls={tool.calls}, reason={event['retry_reason']}, audit_events={len(task.metrics.get('security_events', []))}")


def main():
    scenario("normal file", '{"filename":"input.csv"}', expected_calls=1)
    scenario("tool denied", '{"filename":"input.csv"}', allowed=False, expected_error="PERMISSION_DENIED")
    scenario("path traversal", '{"filename":"../secret"}', expected_error="PERMISSION_DENIED")
    scenario("malformed JSON", '{broken', expected_error="INVALID_ARGUMENT")
    scenario("actual PermissionError", '{"filename":"input.csv"}', failure=PermissionError("denied"), expected_calls=1, expected_error="PERMISSION_DENIED")
    scenario("unsafe replay denied", '{"filename":"input.csv"}', failure=ConnectionError("unknown outcome"), retry_safe=False, expected_calls=1, expected_error="TRANSIENT")


if __name__ == "__main__":
    main()
