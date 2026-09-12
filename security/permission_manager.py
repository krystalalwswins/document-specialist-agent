"""Pure permission decisions shared by Registry and SandboxClient."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tools.base_tool import BaseTool


class PermissionDenied(PermissionError):
    """Stable reason codes contain no submitted paths, code or credentials."""


# A task id only ever becomes one path segment / object-key segment.
_SAFE_ID_PART = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _path_parts(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value.strip():
        raise PermissionDenied("empty_path")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value or ":" in value:
        raise PermissionDenied("invalid_path_characters")
    path = PurePosixPath(value)
    if ".." in path.parts or value.startswith("//"):
        raise PermissionDenied("path_traversal")
    return path


def workspace_path(workspace: str, filename: str, *, allow_root: bool = False) -> str:
    root = _path_parts(workspace)
    if not root.is_absolute() or str(root) == "/":
        raise ValueError("workspace must be an absolute non-root POSIX directory")
    path = _path_parts(filename)
    target = path if path.is_absolute() else root / path
    target = PurePosixPath(posixpath.normpath(str(target)))
    if not target.is_relative_to(root) or (target == root and not allow_root):
        raise PermissionDenied("outside_workspace")
    return str(target)


def object_key(prefix: str, key: str) -> str:
    root = _path_parts(prefix)
    path = _path_parts(key)
    if root.is_absolute() or str(root) == ".":
        raise ValueError("report prefix must be a non-empty relative path")
    # Object keys are not filesystem paths: reject ambiguous spellings rather than rewrite them.
    if path.is_absolute() or str(path) != key or path == root or not path.is_relative_to(root):
        raise PermissionDenied("outside_report_prefix")
    return key


def task_workspace(workspace: str, task_id: str) -> str:
    """Per-task sandbox directory: ``<workspace>/tasks/<task_id>``.

    One directory per task keeps concurrent tasks from overwriting each other's
    inputs and intermediate files.
    """
    root = _path_parts(workspace)
    if not root.is_absolute() or str(root) == "/":
        raise ValueError("workspace must be an absolute non-root POSIX directory")
    if not isinstance(task_id, str) or not task_id or not _SAFE_ID_PART.match(task_id):
        raise PermissionDenied("invalid_task_id")
    return workspace_path(workspace, f"tasks/{task_id}")


def task_object_key(prefix: str, task_id: str, key: str) -> str:
    """Re-root a requested object key under the task's own prefix.

    ``reports/summary.csv`` for task ``abc`` becomes ``reports/abc/summary.csv``;
    the requested key must still live under ``prefix``, so a caller cannot use
    another task's namespace by asking for it directly.
    """
    object_key(prefix, key)
    if not isinstance(task_id, str) or not task_id or not _SAFE_ID_PART.match(task_id):
        raise PermissionDenied("invalid_task_id")
    root = _path_parts(prefix)
    remainder = _path_parts(key).relative_to(root)
    return str(root / task_id / remainder)


@dataclass(frozen=True)
class PermissionManager:
    workspace: str = "/home/gem/workspace"
    report_prefix: str = "reports"
    allowed_tools: frozenset[str] | None = None
    allowed_permissions: frozenset[str] = frozenset({"file.read", "artifact.write", "sandbox.execute"})

    def allows_tool(self, tool: BaseTool) -> bool:
        return (
            (self.allowed_tools is None or tool.name in self.allowed_tools)
            and tool.required_permissions <= self.allowed_permissions
        )

    def check(self, tool: BaseTool, arguments: dict[str, Any]) -> None:
        if not self.allows_tool(tool):
            raise PermissionDenied("tool_not_allowed")
        for parameter in tool.file_parameters:
            if parameter in arguments:
                workspace_path(self.workspace, arguments[parameter])
        for parameter in tool.object_key_parameters:
            if parameter in arguments:
                object_key(self.report_prefix, arguments[parameter])
