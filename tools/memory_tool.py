"""Task-scoped, read-only recall tool for local long-term memory."""

from __future__ import annotations

import json

from memory.model import MemoryType, utc_now_iso
from memory.service import MemoryService
from task.task_manager import TaskManager

from .base_tool import BaseTool, ErrorType, ToolResult


class SearchMemoryTool(BaseTool):
    name = "search_memory"
    description = (
        "Search active local long-term memories for this task's user and project. "
        "Use it when the initially recalled memories are insufficient."
    )
    retry_safe = True
    required_permissions = frozenset({"memory.read"})
    requires_task_context = True

    def __init__(
        self,
        memory_service: MemoryService,
        task_manager: TaskManager,
        *,
        max_results: int = 10,
    ) -> None:
        self._memory_service = memory_service
        self._task_manager = task_manager
        self._max_results = max_results

    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "memory_types": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": [item.value for item in MemoryType],
                    },
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": self._max_results,
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    def execute(
        self,
        query: str,
        memory_types: list[str] | None = None,
        limit: int = 5,
        *,
        task_id: str,
    ) -> ToolResult:
        try:
            task = self._task_manager.get_task(task_id)
            records = self._memory_service.recall(
                user_id=task.user_id,
                project_id=task.project_id,
                query=query,
                limit=limit,
                memory_types=(
                    [MemoryType(item) for item in memory_types]
                    if memory_types
                    else None
                ),
            )
        except (ValueError, KeyError) as exc:
            return ToolResult(
                False, error=str(exc), error_type=ErrorType.INVALID_ARGUMENT
            )
        self._task_manager.add_metric_events(task_id, "memory_events", [{
            "occurred_at": utc_now_iso(),
            "kind": "memory_searched",
            "memory_ids": [record.id for record in records],
            "count": len(records),
            "memory_types": list(memory_types or []),
        }])
        return ToolResult(
            True,
            output=json.dumps(
                {
                    "count": len(records),
                    "memories": [record.to_dict() for record in records],
                    "scope": {
                        "user_id": task.user_id,
                        "project_id": task.project_id,
                    },
                },
                ensure_ascii=False,
            ),
            metadata={"kind": "memory_search", "content_type": "application/json"},
        )
