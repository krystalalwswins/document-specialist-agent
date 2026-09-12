"""Light tests for the three concrete tools (fake sandbox + fake storage)."""

from types import SimpleNamespace

from sandbox.client import ExecutionResult
from tools.file_tool import FileTool
from tools.report_tool import ReportTool
from tools.sandbox_tool import SandboxTool


class FakeSandboxClient:
    def __init__(self, text="", data=b"", execute_result=None):
        self.text = text
        self.data = data
        self.execute_result = execute_result

    def read_text_file(self, filename):
        return self.text

    def read_bytes_file(self, filename):
        return self.data

    def execute_python(self, code, timeout=None, cwd=None):
        self.cwd = cwd
        return self.execute_result


class FakeStorage:
    def upload_file_content(self, key, content):
        self.key = key
        self.content = content
        return f"https://presigned.example/{key}"


def test_sandbox_tool_success_maps_status_ok():
    client = FakeSandboxClient(
        execute_result=ExecutionResult(status="ok", stdout="hello")
    )
    result = SandboxTool(client).execute("print('hello')")
    assert result.success is True
    assert "hello" in result.output


def test_sandbox_tool_failure_maps_status_error():
    client = FakeSandboxClient(
        execute_result=ExecutionResult(status="error", error="NameError: x")
    )
    result = SandboxTool(client).execute("x")
    assert result.success is False
    assert result.error == "NameError: x"


def test_file_tool_reads_text():
    client = FakeSandboxClient(text="col1,col2\n1,2\n")
    result = FileTool(client).execute("a.csv")
    assert result.success is True
    assert result.output == "col1,col2\n1,2\n"


def test_report_tool_uploads_and_returns_url():
    client = FakeSandboxClient(data=b"a,b\n1,2\n")
    storage = FakeStorage()
    result = ReportTool(client, storage).execute("out.csv", "reports/out.csv")
    assert result.success is True
    assert result.output == "https://presigned.example/reports/out.csv"
    assert storage.content == b"a,b\n1,2\n"
