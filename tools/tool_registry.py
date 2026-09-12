"""ToolRegistry: dynamic tool registration and dispatch."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from security.permission_manager import (
    PermissionDenied,
    PermissionManager,
    task_object_key,
    task_workspace,
    workspace_path,
)
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

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        task_id: str | None = None,
    ) -> ToolResult:
        """Validate, authorize and run one tool call.

        With ``task_id`` the call is bound to that task's namespace: file paths
        resolve inside ``<workspace>/tasks/<task_id>`` and object keys are
        re-rooted under ``<report_prefix>/<task_id>``, so concurrent tasks cannot
        read or overwrite each other's files. Both rewrites happen before the
        permission check, which then re-validates the final values.
        """
        if name not in self._tools:
            return ToolResult(False, error="unknown tool", error_type=ErrorType.INVALID_ARGUMENT)
        tool = self.get(name)
        if not self._permissions.allows_tool(tool):
            return ToolResult(False, error="tool_not_allowed", error_type=ErrorType.PERMISSION_DENIED)
        if not isinstance(arguments, dict) or not self._validators[name].is_valid(arguments):
            return ToolResult(False, error="arguments do not match tool schema", error_type=ErrorType.INVALID_ARGUMENT)
        try:
            if task_id is not None:
                arguments = self._bind_to_task(tool, arguments, task_id)
            self._permissions.check(tool, arguments)
            if task_id is not None and tool.task_scoped_cwd:
                return tool.execute(**arguments, cwd=self.task_directory(task_id))
            return tool.execute(**arguments)
        except PermissionDenied as exc:
            return ToolResult(False, error=str(exc), error_type=ErrorType.PERMISSION_DENIED)

    def task_directory(self, task_id: str) -> str:
        """Return the sandbox directory that belongs to ``task_id``."""
        return task_workspace(self._permissions.workspace, task_id)

    def task_object_prefix(self, task_id: str) -> str:
        """Return the object-storage prefix that belongs to ``task_id``."""
        return f"{self._permissions.report_prefix}/{task_id}"

    def _bind_to_task(
        self, tool: BaseTool, arguments: dict[str, Any], task_id: str
    ) -> dict[str, Any]:
        bound = dict(arguments)
        for parameter in tool.file_parameters:
            if parameter in bound:
                bound[parameter] = workspace_path(
                    self.task_directory(task_id), bound[parameter]
                )
        for parameter in tool.object_key_parameters:
            if parameter in bound:
                bound[parameter] = task_object_key(
                    self._permissions.report_prefix, task_id, bound[parameter]
                )
        return bound

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
