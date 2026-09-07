"""ToolRegistry: dynamic tool registration and dispatch."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from security.permission_manager import PermissionDenied, PermissionManager
from tools.base_tool import BaseTool, ErrorType, ToolError, ToolResult


class ToolRegistry:
    def __init__(self, permissions: PermissionManager | None = None) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._validators: dict[str, Draft202012Validator] = {}
        self._permissions = permissions or PermissionManager()

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ToolError(f"duplicate tool: {tool.name}")
        schema = tool.parameters_schema()
        self._check_local_refs(schema)
        Draft202012Validator.check_schema(schema)
        self._validators[tool.name] = Draft202012Validator(schema)
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(f"unknown tool: {name}") from None

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [tool.to_openai_schema() for tool in self._tools.values() if self._permissions.allows_tool(tool)]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name not in self._tools:
            return ToolResult(False, error="unknown tool", error_type=ErrorType.INVALID_ARGUMENT)
        tool = self.get(name)
        if not self._permissions.allows_tool(tool):
            return ToolResult(False, error="tool_not_allowed", error_type=ErrorType.PERMISSION_DENIED)
        if not isinstance(arguments, dict) or not self._validators[name].is_valid(arguments):
            return ToolResult(False, error="arguments do not match tool schema", error_type=ErrorType.INVALID_ARGUMENT)
        try:
            self._permissions.check(tool, arguments)
            return tool.execute(**arguments)
        except PermissionDenied as exc:
            return ToolResult(False, error=str(exc), error_type=ErrorType.PERMISSION_DENIED)

    def retry_safe(self, name: str) -> bool:
        return name in self._tools and self._tools[name].retry_safe

    @staticmethod
    def _check_local_refs(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(item, str) or not item.startswith("#")):
                    raise ToolError("tool schemas must not resolve external references")
                if key == "$id":
                    raise ToolError("tool schemas must not change the reference base")
                ToolRegistry._check_local_refs(item)
        elif isinstance(value, list):
            for item in value:
                ToolRegistry._check_local_refs(item)
