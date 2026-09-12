"""ParseDocumentTool: sandbox-side conversion with a size budget."""

import json
from types import SimpleNamespace

import pytest

from sandbox.client import SandboxError
from tools.document_tool import DOCUMENT_SCRIPT, ParseDocumentTool, _markdown_name
from tools.file_tool import FileTool


class FakeSandboxClient:
    def __init__(self, stdout="", status="ok", text=None, error=None):
        self.stdout = stdout
        self.status = status
        self.text = text
        self.error = error
        self.calls = []

    def execute_python(self, code, timeout=None, cwd=None):
        self.calls.append({"code": code, "cwd": cwd})
        return SimpleNamespace(
            status=self.status,
            text=self.stdout if self.text is None else self.text,
            error=self.error,
            execution_uncertain=False,
        )

    # FileTool needs these
    def read_text_file(self, filename):
        raise SandboxError(f"read failed for '{filename}': invalid utf-8")


def _payload(**overrides):
    data = {"ok": True, "markdown": "# Title\n\nbody", "markdown_path": "/task/doc.md", "chars": 14}
    data.update(overrides)
    return json.dumps(data)


def test_parses_a_document_and_returns_the_preview():
    client = FakeSandboxClient(stdout=_payload())
    result = ParseDocumentTool(client).execute("report.pdf", cwd="/task")

    assert result.success
    assert result.output == "# Title\n\nbody"
    assert result.metadata == {
        "source": "report.pdf",
        "markdown_path": "/task/doc.md",
        "chars": 14,
        "truncated": False,
    }
    assert client.calls[0]["cwd"] == "/task"


def test_long_documents_are_truncated_and_point_at_the_full_file():
    client = FakeSandboxClient(stdout=_payload(markdown="x" * 5000, chars=5000))
    result = ParseDocumentTool(client, max_chars=1000).execute("big.pdf")

    assert result.success
    assert result.output.startswith("x" * 1000)
    assert "truncated: 5000 chars total" in result.output
    assert "/task/doc.md" in result.output
    assert result.metadata["truncated"] is True


def test_unsupported_type_lists_what_is_supported():
    client = FakeSandboxClient(stdout=json.dumps({"ok": False, "error": "unsupported_type", "suffix": ".zip"}))
    result = ParseDocumentTool(client).execute("archive.zip")

    assert not result.success
    assert "unsupported file type '.zip'" in result.error
    assert "pdf" in result.error


def test_missing_file_is_reported():
    client = FakeSandboxClient(stdout=json.dumps({"ok": False, "error": "not_found", "path": "/task/x.pdf"}))
    result = ParseDocumentTool(client).execute("x.pdf")
    assert not result.success
    assert "not_found" in result.error


def test_parser_exception_is_surfaced():
    client = FakeSandboxClient(stdout=json.dumps({"ok": False, "error": "FileNotFoundError: nope"}))
    result = ParseDocumentTool(client).execute("x.pdf")
    assert not result.success
    assert "FileNotFoundError" in result.error


def test_unparsable_output_is_an_execution_error():
    client = FakeSandboxClient(stdout="some unrelated text")
    result = ParseDocumentTool(client).execute("x.pdf")
    assert not result.success
    assert result.error == "document parser returned no result"


def test_timeout_is_terminal_so_the_code_is_not_replayed():
    client = FakeSandboxClient(stdout="", status="timeout", error="timeout")
    result = ParseDocumentTool(client).execute("x.pdf")
    assert not result.success
    assert result.terminal is True


def test_script_embeds_the_filename_as_json_not_raw_text():
    """A crafted filename must not become code inside the sandbox script."""
    client = FakeSandboxClient(stdout=_payload())
    ParseDocumentTool(client).execute('evil"; import os; os.system("rm -rf /") #.pdf')

    code = client.calls[0]["code"]
    assert 'import os; os.system' in code.split("source = ", 1)[1].splitlines()[0]  # inside the literal
    assert code.split("source = ", 1)[1].startswith('"evil\\"; import os')


@pytest.mark.parametrize(
    "filename,expected",
    [("a/b/report.pdf", "report.md"), ("report.xlsx", "report.md"), ("noext", "noext.md")],
)
def test_markdown_name(filename, expected):
    assert _markdown_name(filename) == expected


def test_script_mentions_every_supported_parser():
    for suffix in (".csv", ".xlsx", ".pdf", ".docx", ".pptx"):
        assert f'"{suffix}"' in DOCUMENT_SCRIPT


def test_read_file_points_binary_documents_at_the_parser():
    result = FileTool(FakeSandboxClient()).execute("sales.xlsx")
    assert not result.success
    assert "parse_document" in result.error


def test_read_file_failure_suggests_the_parser_too():
    result = FileTool(FakeSandboxClient()).execute("mystery.dat")
    assert not result.success
    assert "parse_document" in result.error
