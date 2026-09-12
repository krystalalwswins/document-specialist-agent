"""FileTool: read a file's text content from the sandbox workspace."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from sandbox.client import SandboxClient, SandboxError
from tools.base_tool import BaseTool, ErrorType, ToolResult

# Reading these as UTF-8 fails (or produces garbage); point the model at the
# parser instead of burning an iteration on a decode error.
BINARY_SUFFIXES = (".pdf", ".xlsx", ".xlsm", ".docx", ".pptx", ".zip", ".png", ".jpg", ".jpeg", ".gif")


class FileTool(BaseTool):
    name = "read_file"
    retry_safe = True
    required_permissions = frozenset({"file.read"})
    file_parameters = ("filename",)
    description = "Read the text content of a file in the sandbox workspace."

    def __init__(self, client: SandboxClient) -> None:
        self._client = client

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "file path relative to the sandbox workspace",
                },
            },
            "required": ["filename"],
        }

    def execute(self, filename: str) -> ToolResult:
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix in BINARY_SUFFIXES:
            return _binary_hint(filename)
        try:
            content = self._client.read_text_file(filename)
        except SandboxError as exc:
            return ToolResult(
                success=False,
                error=f"{exc} - if this is not a text file, use parse_document instead",
                error_type=ErrorType.INVALID_ARGUMENT,
            )
        return ToolResult(success=True, output=content)


def _binary_hint(filename: str) -> ToolResult:
    return ToolResult(
        success=False,
        error=(
            f"'{filename}' is a binary document and cannot be read as text; "
            "call parse_document to convert it, then work on the extracted text or the file itself"
        ),
        error_type=ErrorType.INVALID_ARGUMENT,
    )
