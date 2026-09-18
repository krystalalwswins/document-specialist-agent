"""Persistent, task-scoped storage for tool results removed from LLM context."""

from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SAFE_TASK_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
_SAFE_RESULT_REF = re.compile(r"\Aout_[0-9a-f]{32}\Z")


class ToolOutputStoreError(RuntimeError):
    """Base error raised by the local output store."""


class ToolOutputAccessDenied(ToolOutputStoreError):
    """The logical reference is malformed or does not belong to this task."""


@dataclass(frozen=True)
class ToolOutputRecord:
    result_ref: str
    task_id: str
    tool_name: str
    tool_call_id: str | None
    content: str
    content_type: str
    size_bytes: int
    created_at: str


@dataclass(frozen=True)
class ToolOutputPage:
    result_ref: str
    content: str
    content_type: str
    offset: int
    next_offset: int | None
    total_chars: int
    size_bytes: int

    @property
    def has_more(self) -> bool:
        return self.next_offset is not None


class ToolOutputStore:
    """Map opaque references to local JSON records under a task namespace.

    The model only receives ``out_<random>`` references. It never supplies a
    filesystem path, and every lookup is resolved below ``<root>/<task_id>``.
    Random 128-bit identifiers make valid references impractical to guess; the
    task namespace prevents a known reference from being reused by another task.
    """

    def __init__(self, root_dir: str | Path, *, max_read_chars: int = 4000) -> None:
        if max_read_chars < 1:
            raise ValueError("max_read_chars must be positive")
        self._root = Path(root_dir).expanduser().resolve()
        self._max_read_chars = max_read_chars
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def max_read_chars(self) -> int:
        return self._max_read_chars

    def put(
        self,
        *,
        task_id: str,
        tool_name: str,
        tool_call_id: str | None,
        content: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> ToolOutputRecord:
        self._validate_task_id(task_id)
        if not isinstance(content, str):
            raise TypeError("tool output content must be text")

        result_ref = f"out_{secrets.token_hex(16)}"
        record = ToolOutputRecord(
            result_ref=result_ref,
            task_id=task_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            content=content,
            content_type=content_type,
            size_bytes=len(content.encode("utf-8")),
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        target = self._record_path(task_id, result_ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(asdict(record), file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return record

    def read_page(
        self,
        *,
        task_id: str,
        result_ref: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> ToolOutputPage:
        self._validate_task_id(task_id)
        self._validate_result_ref(result_ref)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        requested = self._max_read_chars if limit is None else limit
        if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
            raise ValueError("limit must be a positive integer")
        if requested > self._max_read_chars:
            raise ValueError(f"limit must not exceed {self._max_read_chars}")

        target = self._record_path(task_id, result_ref)
        if not target.is_file():
            # Do not reveal whether the same opaque reference belongs to another task.
            raise ToolOutputAccessDenied("result_ref_not_available_for_task")
        try:
            payload: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ToolOutputStoreError("tool_output_record_unreadable") from exc
        if payload.get("task_id") != task_id or payload.get("result_ref") != result_ref:
            raise ToolOutputAccessDenied("result_ref_not_available_for_task")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ToolOutputStoreError("tool_output_record_invalid")

        end = min(len(content), offset + requested)
        page_content = content[offset:end] if offset < len(content) else ""
        next_offset = end if end < len(content) else None
        return ToolOutputPage(
            result_ref=result_ref,
            content=page_content,
            content_type=str(payload.get("content_type") or "text/plain; charset=utf-8"),
            offset=offset,
            next_offset=next_offset,
            total_chars=len(content),
            size_bytes=int(payload.get("size_bytes", len(content.encode("utf-8")))),
        )

    def _record_path(self, task_id: str, result_ref: str) -> Path:
        self._validate_task_id(task_id)
        self._validate_result_ref(result_ref)
        target = (self._root / task_id / f"{result_ref}.json").resolve()
        task_root = (self._root / task_id).resolve()
        if target.parent != task_root:
            raise ToolOutputAccessDenied("invalid_result_ref")
        return target

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not isinstance(task_id, str) or not _SAFE_TASK_ID.fullmatch(task_id):
            raise ToolOutputAccessDenied("invalid_task_id")

    @staticmethod
    def _validate_result_ref(result_ref: str) -> None:
        if not isinstance(result_ref, str) or not _SAFE_RESULT_REF.fullmatch(result_ref):
            raise ToolOutputAccessDenied("invalid_result_ref")
