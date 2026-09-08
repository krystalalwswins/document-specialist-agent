"""SandboxTool: execute Python code in the isolated sandbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from sandbox.client import SandboxClient
from tools.base_tool import BaseTool, ErrorType, ToolResult


class SandboxTool(BaseTool):
    name = "run_python"
    required_permissions = frozenset({"sandbox.execute"})
    description = "Execute Python code in the isolated sandbox and return stdout/result/error."

    def __init__(self, client: SandboxClient, max_timeout: int = 120) -> None:
        self._client = client
        self._max_timeout = max_timeout

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "code": {"type": "string", "minLength": 1, "description": "Python code in a fresh session; exchange state through workspace files."},
                "timeout": {"type": "integer", "minimum": 1, "maximum": self._max_timeout, "description": "execution timeout in seconds"},
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
            terminal=result.execution_uncertain or result.status == "timeout",
        )
