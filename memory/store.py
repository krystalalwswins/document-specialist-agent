"""SQLite persistence and deterministic keyword retrieval for long-term memory."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from threading import RLock

from .model import MemoryRecord, MemorySourceKind, MemoryStatus, MemoryType, utc_now_iso


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class MemoryNotFoundError(LookupError):
    pass


class SQLiteMemoryStore:
    """A small local store: SQL scopes candidates, Python explains the ranking."""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path).expanduser().resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._path

    def save(self, record: MemoryRecord) -> tuple[MemoryRecord, bool]:
        """Insert one active memory, or return its active duplicate."""
        record = record.with_defaults()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND project_id = ? AND memory_type = ?
                  AND content_hash = ? AND status = 'ACTIVE'
                """,
                (
                    record.user_id,
                    record.project_id,
                    record.memory_type.value,
                    record.content_hash,
                ),
            ).fetchone()
            if existing is not None:
                return self._from_row(existing), False
            connection.execute(
                """
                INSERT INTO memories (
                    id, user_id, project_id, memory_type, content,
                    normalized_content, content_hash, source_task_id,
                    source_kind, source_excerpt, confidence, status,
                    created_at, updated_at, invalidated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.user_id,
                    record.project_id,
                    record.memory_type.value,
                    record.content,
                    record.normalized_content,
                    record.content_hash,
                    record.source_task_id,
                    record.source_kind.value,
                    record.source_excerpt,
                    record.confidence,
                    record.status.value,
                    record.created_at,
                    record.updated_at,
                    record.invalidated_at,
                ),
            )
        return record, True

    def search(
        self,
        *,
        user_id: str,
        project_id: str,
        query: str,
        limit: int = 5,
        memory_types: list[MemoryType] | None = None,
    ) -> list[MemoryRecord]:
        if limit < 1:
            raise ValueError("memory search limit must be positive")
        parameters: list[object] = [user_id, project_id]
        type_clause = ""
        if memory_types:
            placeholders = ",".join("?" for _ in memory_types)
            type_clause = f" AND memory_type IN ({placeholders})"
            parameters.extend(item.value for item in memory_types)
        parameters.append(max(50, limit * 20))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memories
                WHERE user_id = ? AND project_id = ? AND status = 'ACTIVE'
                {type_clause}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()

        query_normalized = self.normalize(query)
        query_tokens = self.tokens(query_normalized)
        ranked: list[tuple[float, str, MemoryRecord]] = []
        for row in rows:
            record = self._from_row(row)
            score = self._score(record, query_normalized, query_tokens)
            if score > 0:
                ranked.append((score, record.updated_at, record))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in ranked[:limit]]

    def list(
        self,
        *,
        user_id: str,
        project_id: str,
        status: MemoryStatus = MemoryStatus.ACTIVE,
    ) -> list[MemoryRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ? AND project_id = ? AND status = ?
                ORDER BY updated_at DESC
                """,
                (user_id, project_id, status.value),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, memory_id: str) -> MemoryRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        if row is None:
            raise MemoryNotFoundError(f"memory not found: {memory_id}")
        return self._from_row(row)

    def invalidate(self, memory_id: str) -> MemoryRecord:
        return self._set_status(memory_id, MemoryStatus.INVALIDATED)

    def delete(self, memory_id: str) -> MemoryRecord:
        return self._set_status(memory_id, MemoryStatus.DELETED)

    def _set_status(self, memory_id: str, status: MemoryStatus) -> MemoryRecord:
        now = utc_now_iso()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memories
                SET status = ?, updated_at = ?, invalidated_at = ?
                WHERE id = ? AND status = 'ACTIVE'
                """,
                (status.value, now, now, memory_id),
            )
            if cursor.rowcount != 1:
                raise MemoryNotFoundError(f"active memory not found: {memory_id}")
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return self._from_row(row)

    @staticmethod
    def normalize(value: str) -> str:
        return " ".join(value.strip().lower().split())

    @staticmethod
    def tokens(value: str) -> set[str]:
        return set(_TOKEN_PATTERN.findall(value.lower()))

    @classmethod
    def _score(
        cls, record: MemoryRecord, query_normalized: str, query_tokens: set[str]
    ) -> float:
        content_tokens = cls.tokens(record.normalized_content)
        overlap = len(query_tokens & content_tokens)
        phrase_bonus = 3.0 if query_normalized and query_normalized in record.normalized_content else 0.0
        overlap_score = (4.0 * overlap / max(1, len(query_tokens))) if overlap else 0.0
        # Preferences and project constraints are broadly relevant even without a
        # word overlap. Facts and procedures need an actual lexical match.
        scope_bonus = (
            0.5
            if record.memory_type in (MemoryType.PREFERENCE, MemoryType.CONSTRAINT)
            else 0.0
        )
        if not overlap and phrase_bonus == 0 and scope_bonus == 0:
            return 0.0
        return phrase_bonus + overlap_score + scope_bonus + record.confidence * 0.25

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_task_id TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_excerpt TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    invalidated_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_active_memory
                    ON memories(user_id, project_id, memory_type, content_hash)
                    WHERE status = 'ACTIVE';
                CREATE INDEX IF NOT EXISTS ix_memory_scope
                    ON memories(user_id, project_id, status, memory_type, updated_at);
                """
            )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            user_id=row["user_id"],
            project_id=row["project_id"],
            memory_type=MemoryType(row["memory_type"]),
            content=row["content"],
            normalized_content=row["normalized_content"],
            content_hash=row["content_hash"],
            source_task_id=row["source_task_id"],
            source_kind=MemorySourceKind(row["source_kind"]),
            source_excerpt=row["source_excerpt"],
            confidence=float(row["confidence"]),
            status=MemoryStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            invalidated_at=row["invalidated_at"],
        )
