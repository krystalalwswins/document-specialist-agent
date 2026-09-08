"""Tool abstraction, registry and concrete tools."""

from .base_tool import BaseTool, ToolError, ToolResult
from .file_tool import FileTool
from .report_tool import ReportTool
from .sandbox_tool import SandboxTool
# Loading a simple tool should not require the optional dispatch dependencies.
def __getattr__(name):
    if name == "ToolRegistry":
        from .tool_registry import ToolRegistry
        return ToolRegistry
    raise AttributeError(name)


__all__ = [
    "BaseTool",
    "FileTool",
    "ReportTool",
    "SandboxTool",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
]
