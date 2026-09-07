"""Boundary tests: rejected calls cause zero underlying IO, and loops remain well formed."""

import errno
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from agent.executor import Executor, UnsafeExecutionStateError
from agent.orchestrator import AgentOrchestrator
from agent.planner import Plan
from core.config import Settings
from retry.retry_policy import RetryPolicy, classify_exception
from sandbox.client import PATH_GUARD, SandboxExecutionUncertain
from security.permission_manager import PermissionDenied, PermissionManager, object_key, workspace_path
from storage.storage_manager import StorageError
from task.task_manager import TaskManager
from tools.base_tool import BaseTool, ErrorType, ToolError, ToolResult
from tools.file_tool import FileTool
from tools.report_tool import ReportTool
from tools.sandbox_tool import SandboxTool
from tools.tool_registry import ToolRegistry
from test_sandbox_client import _make


@pytest.mark.parametrize("filename", ["../secret", "/etc/passwd", "/home/gem/workspace-other/a", "a/../../b", "", ".", "a\\b", "C:/a", "a\x00b", "//home/gem/workspace/a"])
def test_invalid_paths_rejected_before_any_sdk_io(filename):
    client, sdk = _make()
    with pytest.raises(PermissionDenied):
        client.read_text_file(filename)
    assert not sdk.shell.commands


def test_custom_workspace_and_absolute_child():
    assert workspace_path("/tmp/work", "nested/a.csv") == "/tmp/work/nested/a.csv"
    assert workspace_path("/tmp/work", "/tmp/work/a.csv") == "/tmp/work/a.csv"


@pytest.mark.parametrize("key", ["raw/a.csv", "reports-other/a", "/reports/a", "reports/../a", "reports//a", "reports/./a", "reports"])
def test_report_namespace(key):
    with pytest.raises(PermissionDenied):
        object_key("reports", key)


def test_rejected_report_does_not_read_or_upload(monkeypatch):
    client, sdk = _make()
    storage = SimpleNamespace(upload_file_content=lambda *a: pytest.fail("upload must not run"))
    registry = ToolRegistry()
    registry.register(ReportTool(client, storage))
    result = registry.execute("save_report", {"sandbox_filename": "output.csv", "oss_key": "raw/overwrite.csv"})
    assert result.error_type == ErrorType.PERMISSION_DENIED
    assert not sdk.shell.commands


def test_production_wiring_uses_configured_allowlist():
    from agent.wiring import build_orchestrator
    orchestrator = build_orchestrator(Settings(_env_file=None, allowed_tools=["read_file"]))
    schemas = orchestrator._executor._registry.to_openai_tools()
    assert [schema["function"]["name"] for schema in schemas] == ["read_file"]


def test_remote_path_guard_checks_real_symlinks(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "private"
    outside.write_text("secret")
    (root / "link").symlink_to(outside)
    def run(path):
        return subprocess.run([sys.executable, "-c", PATH_GUARD, str(root), str(path)], timeout=5).returncode
    assert run(root / "new.csv") == 0
    assert run(root / "link") == 73
    assert run(outside) == 73
    (root / "parent").symlink_to(tmp_path, target_is_directory=True)
    assert run(root / "parent" / "private") == 73


def test_remote_denial_prevents_file_download(monkeypatch):
    client, sdk = _make()
    monkeypatch.setattr(sdk.shell, "exec_command", lambda **kw: SimpleNamespace(data=SimpleNamespace(exit_code=73)))
    monkeypatch.setattr(sdk.file, "download_file", lambda **kw: pytest.fail("download must not run"))
    with pytest.raises(PermissionDenied):
        client.read_bytes_file("link.csv")


def test_failed_guard_is_fail_closed(monkeypatch):
    client, sdk = _make()
    monkeypatch.setattr(sdk.shell, "exec_command", lambda **kw: SimpleNamespace(data=None))
    with pytest.raises(Exception, match="did not complete"):
        client.write_text_file("a", "data")
    assert not sdk.file.writes


class CountingTool(BaseTool):
    name = "count"
    description = "observable fake"
    def __init__(self, failure=None):
        self.calls = 0
        self.failure = failure
    def parameters_schema(self):
        return {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
    def execute(self, **kwargs):
        self.calls += 1
        if self.failure:
            raise self.failure
        return ToolResult(True, output="ok")


class ScriptedLLM:
    def __init__(self, calls):
        self.calls = calls
        self.requests = []
    def chat(self, messages, tools=None, **kwargs):
        self.requests.append(json.loads(json.dumps(messages)))
        message = SimpleNamespace(content=None, tool_calls=self.calls) if len(self.requests) == 1 else SimpleNamespace(content="done", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def call(name, arguments, call_id="c1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def run_loop(tool, arguments='{"text":"hi"}', permissions=None, extra_calls=None):
    registry = ToolRegistry(permissions)
    registry.register(tool)
    llm = ScriptedLLM([call(tool.name, arguments), *(extra_calls or [])])
    manager = TaskManager()
    task = manager.create_task("security verification")
    manager.start_task(task.id)
    executor = Executor(llm, registry, manager, retry_policy=RetryPolicy(base_delay=0, jitter=False))
    return task, llm, executor


@pytest.mark.parametrize("arguments", ["{broken", "[]", "null", '{"text":1}', '{"text":"x","extra":1}'])
def test_bad_arguments_return_feedback_without_execution(arguments):
    tool = CountingTool()
    task, llm, executor = run_loop(tool, arguments)
    assert executor.run(task.id, task.user_input, Plan(task.user_input)) == "done"
    assert tool.calls == 0
    assert task.metrics["retry_events"][0]["error_type"] == "INVALID_ARGUMENT"
    feedback = llm.requests[1][-1]
    assert feedback["role"] == "tool" and feedback["tool_call_id"] == "c1"


def test_denied_tool_hidden_and_not_executed_but_audited():
    tool = CountingTool()
    permissions = PermissionManager(allowed_tools=frozenset())
    task, llm, executor = run_loop(tool, permissions=permissions)
    executor.run(task.id, task.user_input, Plan(task.user_input))
    assert tool.calls == 0
    assert executor._registry.to_openai_tools() == []
    event = task.metrics["security_events"][0]
    assert event["tool_call_id"] == "c1" and event["step_id"] == task.steps[0].id
    assert "arguments" not in event
    assert task.steps[0].attempts == 1


def test_registered_tool_needs_no_executor_changes():
    task, llm, executor = run_loop(CountingTool())
    assert executor.run(task.id, task.user_input, Plan(task.user_input)) == "done"
    assert executor._registry.to_openai_tools()[0]["function"]["name"] == "count"


def test_permission_capability_and_duplicate_registration():
    client, _ = _make()
    registry = ToolRegistry(PermissionManager(allowed_permissions=frozenset({"file.read"})))
    registry.register(SandboxTool(client))
    assert registry.execute("run_python", {"code": "print(1)"}).error_type == ErrorType.PERMISSION_DENIED
    with pytest.raises(ToolError, match="duplicate"):
        registry.register(SandboxTool(client))


def test_external_schema_reference_cannot_trigger_network():
    tool = CountingTool()
    tool.parameters_schema = lambda: {"$ref": "https://example.org/schema"}
    with pytest.raises(ToolError, match="external references"):
        ToolRegistry().register(tool)


@pytest.mark.parametrize("exc, expected", [(PermissionError("denied"), ErrorType.PERMISSION_DENIED), (FileNotFoundError("missing"), ErrorType.INVALID_ARGUMENT), (OSError(errno.ENOSPC, "full"), ErrorType.BUSINESS)])
def test_actual_exceptions_are_not_retried(exc, expected):
    tool = CountingTool(exc)
    tool.retry_safe = True
    task, _, executor = run_loop(tool)
    executor.run(task.id, task.user_input, Plan(task.user_input))
    assert tool.calls == 1
    assert task.metrics["retry_events"][0]["error_type"] == expected.value


def test_wrapped_storage_error_keeps_classification():
    cause = ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "GetObject")
    wrapper = StorageError("download failed")
    wrapper.__cause__ = cause
    assert classify_exception(wrapper) == ErrorType.PERMISSION_DENIED


@pytest.mark.parametrize("retry_safe, attempts", [(False, 1), (True, 3)])
def test_replay_requires_explicit_safety(retry_safe, attempts):
    tool = CountingTool(ConnectionError("down"))
    tool.retry_safe = retry_safe
    task, _, executor = run_loop(tool)
    executor.run(task.id, task.user_input, Plan(task.user_input))
    assert tool.calls == attempts
    assert len(task.metrics["retry_events"]) == attempts


def test_event_written_before_next_attempt():
    tool = CountingTool(ConnectionError("down"))
    tool.retry_safe = True
    task, _, executor = run_loop(tool)
    original = tool.execute
    def execute(**kwargs):
        assert len(task.metrics.get("retry_events", [])) == tool.calls
        return original(**kwargs)
    tool.execute = execute
    executor.run(task.id, task.user_input, Plan(task.user_input))


def test_default_timeout_and_fresh_session_cleanup():
    client, sdk = _make()
    sdk.jupyter.response = SimpleNamespace(data=SimpleNamespace(status="ok", outputs=[]))
    client.execute_python("print(1)")
    client.execute_python("print(2)")
    assert len(set(sdk.jupyter.created)) == 2
    assert sdk.jupyter.deleted == sdk.jupyter.created
    request = sdk.jupyter.calls[0]
    assert request["timeout"] == 30 and request["cwd"] == client.workspace
    assert request["request_options"] == {"timeout_in_seconds": 40, "max_retries": 0}


@pytest.mark.parametrize("timeout", [0, -1, 121, True, 1.5])
def test_timeout_limit_rejected_before_sdk(timeout):
    client, sdk = _make()
    with pytest.raises(ValueError):
        client.execute_python("print(1)", timeout=timeout)
    assert not sdk.shell.commands and not sdk.jupyter.created


def test_timeout_stops_loop_and_cleans_session():
    client, sdk = _make()
    sdk.jupyter.response = SimpleNamespace(data=SimpleNamespace(status="timeout", outputs=[]))
    task, llm, executor = run_loop(SandboxTool(client), '{"code":"while True: pass"}', extra_calls=[call("run_python", '{"code":"print(2)"}', "c2")])
    with pytest.raises(UnsafeExecutionStateError):
        executor.run(task.id, task.user_input, Plan(task.user_input))
    assert len(sdk.jupyter.calls) == 1 and len(llm.requests) == 1
    assert sdk.jupyter.deleted == sdk.jupyter.created
    assert task.steps[0].status.value == "FAILED"
    assert task.metrics["retry_events"][0]["retry_reason"] == "execution_state_unknown"


def test_uncertain_execution_marks_whole_task_failed():
    client, sdk = _make()
    sdk.jupyter.response = SimpleNamespace(data=SimpleNamespace(status="timeout", outputs=[]))
    registry = ToolRegistry()
    registry.register(SandboxTool(client))
    manager = TaskManager()
    llm = ScriptedLLM([call("run_python", '{"code":"while True: pass"}')])
    executor = Executor(llm, registry, manager)
    orchestrator = AgentOrchestrator(manager, SimpleNamespace(plan=lambda text: Plan(text)), executor)
    task = manager.create_task("timeout probe")
    with pytest.raises(UnsafeExecutionStateError):
        orchestrator.run_task(task.id)
    assert manager.get_task(task.id).status.value == "FAILED"
    assert manager.get_task(task.id).steps[0].status.value == "FAILED"


def test_transport_failure_cleans_session_and_is_terminal(monkeypatch):
    client, sdk = _make()
    def fail(**kwargs):
        raise TimeoutError("transport timed out")
    monkeypatch.setattr(sdk.jupyter, "execute_code", fail)
    with pytest.raises(SandboxExecutionUncertain):
        client.execute_python("print(1)")
    assert sdk.jupyter.deleted == sdk.jupyter.created


def test_cleanup_failure_cannot_report_success(monkeypatch):
    client, sdk = _make()
    sdk.jupyter.response = SimpleNamespace(data=SimpleNamespace(status="ok", outputs=[]))
    monkeypatch.setattr(sdk.jupyter, "delete_session", lambda *a, **kw: SimpleNamespace(success=False))
    with pytest.raises(SandboxExecutionUncertain, match="cleanup"):
        client.execute_python("print(1)")


def test_timeout_config_invariants():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sandbox_default_timeout=121, sandbox_max_timeout=120)


def test_sdk_response_models_match_adapter():
    from agent_sandbox.types import JupyterExecuteResponse, ResponseJupyterExecuteResponse
    from sandbox.client import SandboxClient
    data = JupyterExecuteResponse(kernel_name="python3", status="ok", code="1", outputs=[])
    assert SandboxClient._normalize(ResponseJupyterExecuteResponse(success=True, data=data)).status == "ok"
    assert SandboxClient._normalize(ResponseJupyterExecuteResponse(success=False, data=None)).execution_uncertain
