"""P1-1: capture pipeline and task-scoped search_memory tool."""

from memory.model import MemoryCandidate, MemorySourceKind, MemoryType
from memory.policy import MemoryPolicy
from memory.service import MemoryService
from memory.store import SQLiteMemoryStore
from security.permission_manager import PermissionManager
from task.task_manager import TaskManager
from tools.memory_tool import SearchMemoryTool
from tools.tool_registry import ToolRegistry


class FixedExtractor:
    def __init__(self, candidates):
        self.candidates = candidates

    def extract(self, **kwargs):
        return list(self.candidates)


def _service(tmp_path, candidates=()):
    return MemoryService(
        SQLiteMemoryStore(tmp_path / "memory.db"),
        FixedExtractor(candidates),
        MemoryPolicy(),
    )


def test_successful_task_capture_filters_then_deduplicates(tmp_path):
    candidates = [
        MemoryCandidate(
            MemoryType.PREFERENCE,
            "Prefer concise reports",
            0.95,
            MemorySourceKind.USER_EXPLICIT,
            "I prefer concise reports",
        ),
        MemoryCandidate(
            MemoryType.BUSINESS_FACT,
            "Unverified model guess",
            0.99,
            MemorySourceKind.USER_EXPLICIT,
            "not present",
        ),
    ]
    service = _service(tmp_path, candidates)
    manager = TaskManager()
    task = manager.create_task(
        "I prefer concise reports", user_id="alice", project_id="sales"
    )
    manager.start_task(task.id)
    manager.succeed_task(task.id, {"answer": "done"})
    task = manager.get_task(task.id)

    first = service.capture(task, "done")
    second = service.capture(task, "done")

    assert len(first.created) == 1
    assert first.rejected == [
        {"memory_type": "BUSINESS_FACT", "reason": "evidence_not_found_in_declared_source"}
    ]
    assert len(second.duplicates) == 1


def test_search_memory_uses_task_scope_instead_of_model_supplied_identity(tmp_path):
    service = _service(tmp_path)
    alice_task_manager = TaskManager()
    alice = alice_task_manager.create_task(
        "find revenue rules", user_id="alice", project_id="sales"
    )
    bob = alice_task_manager.create_task(
        "find revenue rules", user_id="bob", project_id="sales"
    )
    alice_record = MemoryPolicy().evaluate(
        MemoryCandidate(
            MemoryType.BUSINESS_FACT,
            "Revenue uses CNY",
            0.95,
            MemorySourceKind.USER_EXPLICIT,
            "Revenue uses CNY",
        ),
        user_id="alice",
        project_id="sales",
        source_task_id="source-a",
        user_input="Revenue uses CNY",
        verified_evidence=[],
    ).record
    service.store.save(alice_record)

    registry = ToolRegistry(
        PermissionManager(allowed_permissions=frozenset({"memory.read"}))
    )
    registry.register(SearchMemoryTool(service, alice_task_manager))
    alice_result = registry.execute(
        "search_memory", {"query": "revenue"}, task_id=alice.id
    )
    bob_result = registry.execute(
        "search_memory", {"query": "revenue"}, task_id=bob.id
    )

    assert alice_result.success and "Revenue uses CNY" in alice_result.output
    assert bob_result.success and '"count": 0' in bob_result.output


def test_memory_context_exposes_source_and_confidence(tmp_path):
    service = _service(tmp_path)
    decision = MemoryPolicy().evaluate(
        MemoryCandidate(
            MemoryType.CONSTRAINT,
            "Reports must use UTF-8",
            0.9,
            MemorySourceKind.USER_EXPLICIT,
            "Reports must use UTF-8",
        ),
        user_id="alice",
        project_id="sales",
        source_task_id="source-task",
        user_input="Reports must use UTF-8",
        verified_evidence=[],
    )
    record, _ = service.store.save(decision.record)

    context = service.context([record])

    assert "source_task_id=source-task" in context
    assert "confidence=0.90" in context
    assert "Current user input wins on conflict" in context
