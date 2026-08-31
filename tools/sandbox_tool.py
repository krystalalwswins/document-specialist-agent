"""SandboxTool: execute Python code in the isolated sandbox."""

from __future__ import annotations

from typing import Any, Optional

from sandbox.client import SandboxClient
from tools.base_tool import BaseTool, ErrorType, ToolResult


class SandboxTool(BaseTool):
    name = "run_python"
    description = "Execute Python code in the isolated sandbox and return stdout/result/error."

    def __init__(self, client: SandboxClient) -> None:
        self._client = client

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "description": "execution timeout in seconds"},
            },
            "required": ["code"],
        }

    def execute(self, code: str, timeout: Optional[int] = None) -> ToolResult:
        result = self._client.execute_python(code, timeout=timeout)
        if result.status == "ok":
            return ToolResult(success=True, output=result.text)
        error_type = ErrorType.TIMEOUT if result.status == "timeout" else ErrorType.EXECUTION
        return ToolResult(
            success=False,
            output=result.text,
            error=result.error or result.status,
            error_type=error_type,
        )
