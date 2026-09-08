"""Task domain model: lifecycle state machine + step tracking.

Phase 1 keeps this module dependency-free (stdlib only) so it can be
tested in isolation and serialized to any backend (memory / Redis / DB).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def _new_id() -> str:
    return uuid.uuid4().hex


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _duration_ms(started_at: Optional[str], finished_at: Optional[str]) -> Optional[int]:
    if not started_at or not finished_at:
        return None
    try:
        delta = datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
    except ValueError:
        return None
    return max(0, int(delta.total_seconds() * 1000))


class TaskStatus(str, Enum):
    """Task-level lifecycle states (matches the product spec)."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class StepStatus(str, Enum):
    """Step-level states inside a task."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class TaskError(Exception):
    """Base error for the task module."""


class TaskNotFoundError(TaskError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"task not found: {task_id}")
        self.task_id = task_id


class StepNotFoundError(TaskError):
    def __init__(self, step_id: str) -> None:
        super().__init__(f"step not found: {step_id}")
        self.step_id = step_id


class TaskStateError(TaskError):
    """Raised when a task/step transition violates the state machine."""


@dataclass
class TaskStep:
    """One executable step produced by the planner / executed by the executor."""

    name: str
    tool: Optional[str] = None
    id: str = field(default_factory=_new_id)
    status: StepStatus = StepStatus.PENDING
    output: Optional[str] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    attempts: int = 1

    _ALLOWED_TRANSITIONS = {
        StepStatus.PENDING: {StepStatus.RUNNING, StepStatus.FAILED},
        StepStatus.RUNNING: {StepStatus.SUCCESS, StepStatus.FAILED},
    }

    def _transition(self, target: StepStatus) -> None:
        allowed = self._ALLOWED_TRANSITIONS.get(self.status, set())
        if target not in allowed:
            raise TaskStateError(
                f"invalid step transition: {self.status.value} -> {target.value}"
            )
        self.status = target

    def start(self) -> None:
        self._transition(StepStatus.RUNNING)
        self.started_at = _utc_now_iso()

    def succeed(self, output: Optional[str] = None) -> None:
        self._transition(StepStatus.SUCCESS)
        self.output = output
        self.finished_at = _utc_now_iso()
        self.duration_ms = _duration_ms(self.started_at, self.finished_at)

    def fail(self, error: str) -> None:
        self._transition(StepStatus.FAILED)
        self.error = error
        self.finished_at = _utc_now_iso()
        self.duration_ms = _duration_ms(self.started_at, self.finished_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "tool": self.tool,
            "status": self.status.value,
            "output": self.output,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskStep":
        return cls(
            id=data["id"],
            name=data["name"],
            tool=data.get("tool"),
            status=StepStatus(data["status"]),
            output=data.get("output"),
            error=data.get("error"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            duration_ms=data.get("duration_ms"),
            attempts=data.get("attempts", 1),
        )


@dataclass
class Task:
    """A user task with its lifecycle, steps and final result."""

    user_input: str
    id: str = field(default_factory=_new_id)
    status: TaskStatus = TaskStatus.CREATED
    steps: list[TaskStep] = field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    inputs: list[dict[str, Any]] = field(default_factory=list)
    artifact_requirements: dict[str, Any] = field(default_factory=dict)
    # Forward-looking hook for Phase 2 evaluation (llm_calls, total_tokens, ...).
    metrics: dict[str, Any] = field(default_factory=dict)
    created_time: str = field(default_factory=_utc_now_iso)
    updated_time: str = field(default_factory=_utc_now_iso)

    _ALLOWED_TRANSITIONS = {
        TaskStatus.CREATED: {TaskStatus.RUNNING, TaskStatus.FAILED},
        TaskStatus.RUNNING: {TaskStatus.SUCCESS, TaskStatus.FAILED},
    }

    def _touch(self) -> None:
        self.updated_time = _utc_now_iso()

    def _transition(self, target: TaskStatus) -> None:
        allowed = self._ALLOWED_TRANSITIONS.get(self.status, set())
        if target not in allowed:
            raise TaskStateError(
                f"invalid task transition: {self.status.value} -> {target.value}"
            )
        self.status = target
        self._touch()

    def start(self) -> None:
        self._transition(TaskStatus.RUNNING)

    def succeed(self, result: Optional[dict[str, Any]] = None) -> None:
        self._transition(TaskStatus.SUCCESS)
        self.result = result

    def fail(self, error: str) -> None:
        self._transition(TaskStatus.FAILED)
        self.error = error

    def add_step(self, name: str, tool: Optional[str] = None) -> TaskStep:
        step = TaskStep(name=name, tool=tool)
        self.steps.append(step)
        self._touch()
        return step

    def get_step(self, step_id: str) -> TaskStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise StepNotFoundError(step_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_input": self.user_input,
            "status": self.status.value,
            "steps": [step.to_dict() for step in self.steps],
            "result": self.result,
            "error": self.error,
            "metrics": self.metrics,
            "inputs": self.inputs,
            "artifact_requirements": self.artifact_requirements,
            "created_time": self.created_time,
            "updated_time": self.updated_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=data["id"],
            user_input=data["user_input"],
            status=TaskStatus(data["status"]),
            steps=[TaskStep.from_dict(step) for step in data.get("steps", [])],
            result=data.get("result"),
            error=data.get("error"),
            metrics=data.get("metrics", {}),
            inputs=data.get("inputs", []),
            artifact_requirements=data.get("artifact_requirements", {}),
            created_time=data["created_time"],
            updated_time=data["updated_time"],
        )
