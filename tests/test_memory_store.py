"""P1-1: SQLite memory persistence, scoping, ranking and lifecycle."""

import hashlib

import pytest

from memory.model import MemoryRecord, MemorySourceKind, MemoryStatus, MemoryType
from memory.store import MemoryNotFoundError, SQLiteMemoryStore


def _record(
    content: str,
    *,
    user_id: str = "alice",
    project_id: str = "sales",
    memory_type: MemoryType = MemoryType.BUSINESS_FACT,
) -> MemoryRecord:
    normalized = SQLiteMemoryStore.normalize(content)
    return MemoryRecord(
        user_id=user_id,
        project_id=project_id,
        memory_type=memory_type,
        content=content,
        normalized_content=normalized,
        content_hash=hashlib.sha256(normalized.encode()).hexdigest(),
        source_task_id="task-source",
        source_kind=MemorySourceKind.USER_EXPLICIT,
        source_excerpt=content,
        confidence=0.95,
    )


def test_memory_survives_store_recreation(tmp_path):
    path = tmp_path / "memory.db"
    saved, created = SQLiteMemoryStore(path).save(_record("Revenue uses CNY"))

    reopened = SQLiteMemoryStore(path)
    assert created is True
    assert reopened.get(saved.id).content == "Revenue uses CNY"


def test_active_duplicate_is_not_written_twice(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "memory.db")

    first, created_first = store.save(_record("Use UTF-8 reports"))
    second, created_second = store.save(_record("  use  utf-8 REPORTS "))

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert len(store.list(user_id="alice", project_id="sales")) == 1


def test_keyword_search_never_crosses_user_or_project_scope(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "memory.db")
    wanted, _ = store.save(_record("Quarterly revenue is reported in CNY"))
    store.save(_record("Quarterly revenue is reported in USD", user_id="bob"))
    store.save(_record("Quarterly revenue is reported in EUR", project_id="other"))

    found = store.search(
        user_id="alice", project_id="sales", query="quarterly revenue", limit=5
    )

    assert [item.id for item in found] == [wanted.id]


def test_preferences_and_constraints_are_broadly_recalled_but_unrelated_facts_are_not(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "memory.db")
    preference, _ = store.save(
        _record("Prefer concise reports", memory_type=MemoryType.PREFERENCE)
    )
    store.save(_record("The legal entity code is ACME-01"))

    found = store.search(
        user_id="alice", project_id="sales", query="prepare a summary", limit=5
    )

    assert [item.id for item in found] == [preference.id]


def test_invalidated_and_deleted_memories_are_excluded(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "memory.db")
    invalidated, _ = store.save(_record("Revenue uses CNY"))
    deleted, _ = store.save(_record("Revenue excludes tax"))

    assert store.invalidate(invalidated.id).status is MemoryStatus.INVALIDATED
    assert store.delete(deleted.id).status is MemoryStatus.DELETED
    assert store.search(
        user_id="alice", project_id="sales", query="revenue", limit=5
    ) == []
    with pytest.raises(MemoryNotFoundError):
        store.invalidate(invalidated.id)
