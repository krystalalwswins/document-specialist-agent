"""Dependency-free memory domain objects."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryType(str, Enum):
    PREFERENCE = "PREFERENCE"
    CONSTRAINT = "CONSTRAINT"
    BUSINESS_FACT = "BUSINESS_FACT"
    PROCEDURE = "PROCEDURE"


class MemoryStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INVALIDATED = "INVALIDATED"
    DELETED = "DELETED"


class MemorySourceKind(str, Enum):
    USER_EXPLICIT = "USER_EXPLICIT"
    TASK_VERIFIED = "TASK_VERIFIED"


@dataclass(frozen=True)
class MemoryCandidate:
    memory_type: MemoryType
    content: str
    confidence: float
    source_kind: MemorySourceKind
    evidence: str


@dataclass(frozen=True)
class MemoryRecord:
    user_id: str
    project_id: str
    memory_type: MemoryType
    content: str
    normalized_content: str
    content_hash: str
    source_task_id: str
    source_kind: MemorySourceKind
    source_excerpt: str
    confidence: float
    id: str = ""
    status: MemoryStatus = MemoryStatus.ACTIVE
    created_at: str = ""
    updated_at: str = ""
    invalidated_at: str | None = None

    def with_defaults(self) -> "MemoryRecord":
        now = utc_now_iso()
        return MemoryRecord(
            id=self.id or uuid.uuid4().hex,
            user_id=self.user_id,
            project_id=self.project_id,
            memory_type=self.memory_type,
            content=self.content,
            normalized_content=self.normalized_content,
            content_hash=self.content_hash,
            source_task_id=self.source_task_id,
            source_kind=self.source_kind,
            source_excerpt=self.source_excerpt,
            confidence=self.confidence,
            status=self.status,
            created_at=self.created_at or now,
            updated_at=self.updated_at or now,
            invalidated_at=self.invalidated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "memory_type": self.memory_type.value,
            "content": self.content,
            "source_task_id": self.source_task_id,
            "source_kind": self.source_kind.value,
            "source_excerpt": self.source_excerpt,
            "confidence": self.confidence,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "invalidated_at": self.invalidated_at,
        }
