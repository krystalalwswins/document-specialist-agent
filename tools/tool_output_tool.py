"""Bounded recall tool for results offloaded by the AfterToolCall hook."""

from __future__ import annotations

from context.tool_output_store import (
    ToolOutputAccessDenied,
    ToolOutputStore,
    ToolOutputStoreError,
)
from tools.base_tool import BaseTool, ErrorType, ToolResult


class ReadToolOutputTool(BaseTool):
    name = "read_tool_output"
    description = (
        "Read one bounded page of a large earlier tool result by its opaque result_ref. "
        "Use next_offset to continue and request only the part needed for the current step."
    )
    retry_safe = True
    required_permissions = frozenset({"tool_output.read"})
    requires_task_context = True

    def __init__(self, store: ToolOutputStore) -> None:
        self._store = store

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "result_ref": {
                    "type": "string",
                    "pattern": r"^out_[0-9a-f]{32}$",
                    "description": "Opaque reference returned by a previous tool result.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Character offset at which this page starts.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": self._store.max_read_chars,
                    "default": self._store.max_read_chars,
                    "description": "Maximum characters returned in this page.",
                },
            },
            "required": ["result_ref"],
        }

    def execute(
        self,
        result_ref: str,
        offset: int = 0,
        limit: int | None = None,
        *,
        task_id: str,
    ) -> ToolResult:
        try:
            page = self._store.read_page(
                task_id=task_id,
                result_ref=result_ref,
                offset=offset,
                limit=limit,
            )
        except ToolOutputAccessDenied as exc:
            return ToolResult(
                False, error=str(exc), error_type=ErrorType.PERMISSION_DENIED
            )
        except ValueError as exc:
            return ToolResult(False, error=str(exc), error_type=ErrorType.INVALID_ARGUMENT)
        except ToolOutputStoreError as exc:
            return ToolResult(False, error=str(exc), error_type=ErrorType.EXECUTION)

        next_value = "end" if page.next_offset is None else str(page.next_offset)
        footer = (
            "\n\n[tool output page: "
            f"result_ref={page.result_ref}; offset={page.offset}; "
            f"next_offset={next_value}; total_chars={page.total_chars}; "
            f"size_bytes={page.size_bytes}; content_type={page.content_type}]"
        )
        return ToolResult(
            True,
            output=page.content + footer,
            metadata={"kind": "tool_output_page", "content_type": page.content_type},
        )
