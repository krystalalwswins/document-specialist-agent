"""Deterministic write policy placed after model candidate extraction."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .model import MemoryCandidate, MemoryRecord, MemorySourceKind, MemoryType
from .store import SQLiteMemoryStore


_SENSITIVE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|password|passwd|secret)\b\s*(?::|=|\bis\b|是)",
        re.IGNORECASE,
    ),
    re.compile(r"(?:密码|密钥|令牌)\s*(?::|=|是)"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


@dataclass(frozen=True)
class MemoryPolicyDecision:
    accepted: bool
    reason: str
    record: MemoryRecord | None = None


class MemoryPolicy:
    def __init__(self, *, min_confidence: float = 0.8, max_content_chars: int = 800) -> None:
        if not 0 <= min_confidence <= 1:
            raise ValueError("memory min confidence must be between 0 and 1")
        if max_content_chars < 50:
            raise ValueError("memory max content chars must be at least 50")
        self._min_confidence = min_confidence
        self._max_content_chars = max_content_chars

    def evaluate(
        self,
        candidate: MemoryCandidate,
        *,
        user_id: str,
        project_id: str,
        source_task_id: str,
        user_input: str,
        verified_evidence: list[str],
    ) -> MemoryPolicyDecision:
        content = " ".join(candidate.content.split())
        evidence = " ".join(candidate.evidence.split())
        if candidate.confidence < self._min_confidence:
            return MemoryPolicyDecision(False, "confidence_below_threshold")
        if len(content) < 3 or len(content) > self._max_content_chars:
            return MemoryPolicyDecision(False, "content_length_out_of_range")
        if len(evidence) < 3 or len(evidence) > 1000:
            return MemoryPolicyDecision(False, "evidence_length_out_of_range")
        if self._contains_sensitive(content) or self._contains_sensitive(evidence):
            return MemoryPolicyDecision(False, "sensitive_content")
        source_violation = self._source_violation(candidate, evidence)
        if source_violation:
            return MemoryPolicyDecision(False, source_violation)

        evidence_source = (
            user_input
            if candidate.source_kind is MemorySourceKind.USER_EXPLICIT
            else "\n".join(verified_evidence)
        )
        if SQLiteMemoryStore.normalize(evidence) not in SQLiteMemoryStore.normalize(evidence_source):
            return MemoryPolicyDecision(False, "evidence_not_found_in_declared_source")

        normalized = SQLiteMemoryStore.normalize(content)
        record = MemoryRecord(
            user_id=user_id,
            project_id=project_id,
            memory_type=candidate.memory_type,
            content=content,
            normalized_content=normalized,
            content_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            source_task_id=source_task_id,
            source_kind=candidate.source_kind,
            source_excerpt=evidence,
            confidence=candidate.confidence,
        ).with_defaults()
        return MemoryPolicyDecision(True, "accepted", record)

    @staticmethod
    def _source_violation(candidate: MemoryCandidate, evidence: str) -> str | None:
        if candidate.memory_type in (MemoryType.PREFERENCE, MemoryType.CONSTRAINT):
            if candidate.source_kind is not MemorySourceKind.USER_EXPLICIT:
                return "preference_or_constraint_requires_user_explicit_source"
        if candidate.memory_type is MemoryType.PROCEDURE:
            if candidate.source_kind is not MemorySourceKind.TASK_VERIFIED:
                return "procedure_requires_verified_source"
        if not evidence:
            return "missing_evidence"
        return None

    @staticmethod
    def _contains_sensitive(value: str) -> bool:
        return any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS)
