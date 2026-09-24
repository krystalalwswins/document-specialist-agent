"""P1-1: local API can inspect, invalidate and soft-delete scoped memories."""

from fastapi.testclient import TestClient

from api.app import create_app
from core.config import Settings
from memory.model import MemoryCandidate, MemorySourceKind, MemoryType
from memory.policy import MemoryPolicy
from memory.service import MemoryService
from memory.store import SQLiteMemoryStore
from task.task_manager import TaskManager


class EmptyExtractor:
    def extract(self, **kwargs):
        return []


class StubOrchestrator:
    def __init__(self, service):
        self.task_manager = TaskManager()
        self.memory_service = service

    def run_task(self, task_id):
        raise AssertionError("memory management must not start a task")


def _record(service, content):
    decision = MemoryPolicy().evaluate(
        MemoryCandidate(
            MemoryType.CONSTRAINT,
            content,
            0.95,
            MemorySourceKind.USER_EXPLICIT,
            content,
        ),
        user_id="alice",
        project_id="sales",
        source_task_id="source-task",
        user_input=content,
        verified_evidence=[],
    )
    return service.store.save(decision.record)[0]


def test_memory_management_endpoints_respect_scope_and_status(tmp_path):
    service = MemoryService(
        SQLiteMemoryStore(tmp_path / "memory.db"),
        EmptyExtractor(),
        MemoryPolicy(),
    )
    invalidated = _record(service, "Reports must use CNY")
    deleted = _record(service, "Reports must use UTF-8")
    client = TestClient(
        create_app(StubOrchestrator(service), Settings(_env_file=None))
    )

    listed = client.get("/memories?user_id=alice&project_id=sales")
    assert listed.status_code == 200 and len(listed.json()) == 2

    denied = client.post(
        f"/memories/{invalidated.id}/invalidate?user_id=bob&project_id=sales"
    )
    assert denied.status_code == 404

    changed = client.post(
        f"/memories/{invalidated.id}/invalidate?user_id=alice&project_id=sales"
    )
    removed = client.delete(
        f"/memories/{deleted.id}?user_id=alice&project_id=sales"
    )
    active = client.get("/memories?user_id=alice&project_id=sales")

    assert changed.json()["status"] == "INVALIDATED"
    assert removed.json()["status"] == "DELETED"
    assert active.json() == []
