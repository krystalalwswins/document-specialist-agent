"""Post-tool context hook that keeps large results out of the Agent Loop."""

from __future__ import annotations

import json
from dataclasses import dataclass

from context.tool_output_store import ToolOutputStore
from tools.base_tool import ToolResult


@dataclass(frozen=True)
class PreparedToolOutput:
    """The bounded view persisted on a TaskStep and returned to the model."""

    model_text: str
    task_text: str
    result_ref: str | None = None
    size_bytes: int | None = None
    content_type: str | None = None
    truncated: bool = False


class AfterToolCallHook:
    """Offload large tool output and return a bounded, recallable observation."""

    def __init__(
        self,
        store: ToolOutputStore,
        *,
        inline_chars: int = 16000,
        preview_chars: int = 2000,
    ) -> None:
        if inline_chars < 1:
            raise ValueError("inline_chars must be positive")
        if preview_chars < 1 or preview_chars > inline_chars:
            raise ValueError("preview_chars must be between 1 and inline_chars")
        self._store = store
        self._inline_chars = inline_chars
        self._preview_chars = preview_chars

    def process(
        self,
        *,
        task_id: str,
        tool_name: str,
        tool_call_id: str | None,
        result: ToolResult,
    ) -> PreparedToolOutput:
        raw = result.output if result.success else (result.error or "unknown error")
        content_type = self._content_type(result)
        size_bytes = len(raw.encode("utf-8"))
        if len(raw) <= self._inline_chars:
            return PreparedToolOutput(
                model_text=result.to_text(),
                task_text=raw,
                size_bytes=size_bytes,
                content_type=content_type,
            )

        record = self._store.put(
            task_id=task_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=raw,
            content_type=content_type,
        )
        observation = json.dumps(
            {
                "status": "success" if result.success else "error",
                "truncated": True,
                "preview": raw[: self._preview_chars],
                "result_ref": record.result_ref,
                "size_bytes": record.size_bytes,
                "total_chars": len(raw),
                "content_type": record.content_type,
                "recall_hint": (
                    "Call read_tool_output with result_ref, offset and limit to read "
                    "only the next needed page."
                ),
            },
            ensure_ascii=False,
        )
        return PreparedToolOutput(
            model_text=observation,
            task_text=observation,
            result_ref=record.result_ref,
            size_bytes=record.size_bytes,
            content_type=record.content_type,
            truncated=True,
        )

    @staticmethod
    def _content_type(result: ToolResult) -> str:
        value = result.metadata.get("content_type")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return "text/plain; charset=utf-8"
