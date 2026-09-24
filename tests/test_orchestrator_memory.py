"""P1-1: initial recall reaches Planner/Executor and capture is best-effort."""

from agent.orchestrator import AgentOrchestrator
from memory.model import MemoryRecord, MemorySourceKind, MemoryStatus, MemoryType
from memory.service import MemoryCaptureReport
from task.plan_model import Plan
from task.task_manager import TaskManager
from task.task_model import TaskStatus


def _plan(goal):
    return Plan.from_dict({
        "user_input": goal,
        "version": 1,
        "steps": [{
            "step_id": "answer",
            "name": "answer",
            "description": "answer the request",
            "tool": None,
            "depends_on": [],
            "completion_criteria": ["answer returned"],
        }],
    })


class RecordingPlanner:
    def __init__(self):
        self.memory_context = None

    def plan(self, user_input, on_event=None, *, memory_context=None):
        self.memory_context = memory_context
        return _plan(user_input)


class RecordingExecutor:
    def __init__(self):
        self.memory_context = None

    def run(self, task_id, user_input, plan, repair_hint=None, memory_context=None):
        self.memory_context = memory_context
        return "done"


class FakeMemoryService:
    def __init__(self, fail_capture=False):
        self.fail_capture = fail_capture
        self.captured_status = None

    def recall(self, **kwargs):
        return [MemoryRecord(
            id="memory-1",
            user_id=kwargs["user_id"],
            project_id=kwargs["project_id"],
            memory_type=MemoryType.PREFERENCE,
            content="Prefer concise reports",
            normalized_content="prefer concise reports",
            content_hash="hash",
            source_task_id="old-task",
            source_kind=MemorySourceKind.USER_EXPLICIT,
            source_excerpt="Prefer concise reports",
            confidence=0.9,
            status=MemoryStatus.ACTIVE,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )]

    def context(self, records):
        return "MEMORY: Prefer concise reports; source_task_id=old-task"

    def capture(self, task, answer, on_event=None):
        self.captured_status = task.status
        if self.fail_capture:
            raise RuntimeError("memory disk unavailable")
        return MemoryCaptureReport()


def test_recall_is_injected_into_both_agent_stages_and_capture_runs_after_success():
    manager = TaskManager()
    planner = RecordingPlanner()
    executor = RecordingExecutor()
    memory = FakeMemoryService()
    orchestrator = AgentOrchestrator(
        manager, planner, executor, memory_service=memory
    )

    task = orchestrator.run("summarize", user_id="alice", project_id="sales")

    assert task.status is TaskStatus.SUCCESS
    assert planner.memory_context.startswith("MEMORY:")
    assert executor.memory_context == planner.memory_context
    assert memory.captured_status is TaskStatus.SUCCESS
    assert [event["kind"] for event in task.metrics["memory_events"]] == [
        "memory_recalled", "memory_capture_finished"
    ]


def test_memory_capture_failure_does_not_turn_a_successful_task_into_failure():
    manager = TaskManager()
    orchestrator = AgentOrchestrator(
        manager,
        RecordingPlanner(),
        RecordingExecutor(),
        memory_service=FakeMemoryService(fail_capture=True),
    )

    task = orchestrator.run("summarize", user_id="alice", project_id="sales")

    assert task.status is TaskStatus.SUCCESS
    assert task.metrics["memory_events"][-1]["kind"] == "memory_capture_failed"
