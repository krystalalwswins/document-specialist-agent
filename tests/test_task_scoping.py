"""Per-task isolation: tool arguments are bound to the task's own namespace."""

from typing import Any

from security.permission_manager import PermissionManager
from tools.base_tool import BaseTool, ErrorType, ToolResult
from tools.tool_registry import ToolRegistry

WORKSPACE = "/home/gem/workspace"
TASK_ID = "abc123"


class _Tool(BaseTool):
    def __init__(self):
        self.seen: list[dict[str, Any]] = []

    def parameters_schema(self):
        return {"type": "object", "properties": {}, "additionalProperties": True}


class ReadFileTool(_Tool):
    name = "read_file"
    required_permissions = frozenset({"file.read"})
    file_parameters = ("filename",)
    description = "read a text file"

    def execute(self, filename: str):
        self.seen.append({"filename": filename})
        return ToolResult(success=True, output=filename)


class SaveReportTool(_Tool):
    name = "save_report"
    required_permissions = frozenset({"artifact.write"})
    object_key_parameters = ("oss_key",)
    description = "upload a report"

    def execute(self, oss_key: str):
        self.seen.append({"oss_key": oss_key})
        return ToolResult(success=True, output=oss_key)


class RunPythonTool(_Tool):
    name = "run_python"
    required_permissions = frozenset({"sandbox.execute"})
    task_scoped_cwd = True
    description = "run code"

    def execute(self, code: str, cwd: str | None = None):
        self.seen.append({"code": code, "cwd": cwd})
        return ToolResult(success=True, output="ok")


def _registry(*tools):
    registry = ToolRegistry(PermissionManager(workspace=WORKSPACE, report_prefix="reports"))
    for tool in tools:
        registry.register(tool)
    return registry


def test_file_arguments_are_bound_into_the_task_directory():
    tool = ReadFileTool()
    result = _registry(tool).execute("read_file", {"filename": "orders.csv"}, task_id=TASK_ID)

    assert result.success
    assert tool.seen == [{"filename": f"{WORKSPACE}/tasks/{TASK_ID}/orders.csv"}]


def test_absolute_paths_outside_the_task_directory_are_denied():
    tool = ReadFileTool()
    result = _registry(tool).execute(
        "read_file", {"filename": f"{WORKSPACE}/shared.csv"}, task_id=TASK_ID
    )

    assert not result.success
    assert result.error_type is ErrorType.PERMISSION_DENIED
    assert tool.seen == []


def test_another_tasks_directory_is_not_reachable():
    tool = ReadFileTool()
    result = _registry(tool).execute(
        "read_file", {"filename": f"{WORKSPACE}/tasks/other/secret.csv"}, task_id=TASK_ID
    )

    assert not result.success
    assert result.error_type is ErrorType.PERMISSION_DENIED
    assert tool.seen == []


def test_traversal_is_denied():
    tool = ReadFileTool()
    result = _registry(tool).execute(
        "read_file", {"filename": "../../etc/passwd"}, task_id=TASK_ID
    )
    assert not result.success
    assert result.error_type is ErrorType.PERMISSION_DENIED


def test_object_keys_are_re_rooted_under_the_task_prefix():
    tool = SaveReportTool()
    result = _registry(tool).execute(
        "save_report", {"oss_key": "reports/summary.csv"}, task_id=TASK_ID
    )

    assert result.success
    assert tool.seen == [{"oss_key": f"reports/{TASK_ID}/summary.csv"}]


def test_object_keys_outside_the_report_prefix_are_denied():
    tool = SaveReportTool()
    result = _registry(tool).execute(
        "save_report", {"oss_key": "somewhere-else/summary.csv"}, task_id=TASK_ID
    )
    assert not result.success
    assert result.error_type is ErrorType.PERMISSION_DENIED
    assert tool.seen == []


def test_code_tools_receive_the_task_directory_as_cwd():
    tool = RunPythonTool()
    result = _registry(tool).execute("run_python", {"code": "print(1)"}, task_id=TASK_ID)

    assert result.success
    assert tool.seen == [{"code": "print(1)", "cwd": f"{WORKSPACE}/tasks/{TASK_ID}"}]


def test_without_a_task_id_nothing_is_rewritten():
    reader, runner = ReadFileTool(), RunPythonTool()
    registry = _registry(reader, runner)

    registry.execute("read_file", {"filename": "orders.csv"})
    registry.execute("run_python", {"code": "print(1)"})

    assert reader.seen == [{"filename": "orders.csv"}]
    assert runner.seen == [{"code": "print(1)", "cwd": None}]


def test_invalid_task_ids_are_denied():
    tool = ReadFileTool()
    result = _registry(tool).execute(
        "read_file", {"filename": "a.csv"}, task_id="../../escape"
    )
    assert not result.success
    assert result.error_type is ErrorType.PERMISSION_DENIED
