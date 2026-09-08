"""FileTool: read a file's text content from the sandbox workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sandbox.client import SandboxClient
from tools.base_tool import BaseTool, ToolResult


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
        content = self._client.read_text_file(filename)
        return ToolResult(success=True, output=content)
