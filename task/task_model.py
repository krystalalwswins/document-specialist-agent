"""Task domain model: lifecycle state machine + step tracking.

Phase 1 keeps this module dependency-free (stdlib only) so it can be
tested in isolation and serialized to any backend (memory / Redis / DB).
"""

from __future__ import annotations

import uuid
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from .plan_model import Plan, assert_replan_preserves_completed_steps


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


_SCOPE_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


@dataclass
class TaskStep:
    """One actual tool invocation. Planned steps live separately in Task.plan."""

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
    # Which planned step this call was working on, and the provider-side call id.
    # Both stay optional so records of an unbound call and pre-P0-2 JSON remain valid.
    plan_step_id: Optional[str] = None
    tool_call_id: Optional[str] = None
    # P0-3 keeps large results in ToolOutputStore. The task record contains only
    # the bounded preview above plus this logical reference and its metadata.
    result_ref: Optional[str] = None
    result_size_bytes: Optional[int] = None
    result_content_type: Optional[str] = None
    result_truncated: bool = False

    _ALLOWED_TRANSITIONS = {
        StepStatus.PENDING: {StepStatus.RUNNING},
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

    def succeed(
        self,
        output: Optional[str] = None,
        *,
        result_ref: Optional[str] = None,
        result_size_bytes: Optional[int] = None,
        result_content_type: Optional[str] = None,
        result_truncated: bool = False,
    ) -> None:
        self._transition(StepStatus.SUCCESS)
        self.output = output
        self._set_result_reference(
            result_ref,
            result_size_bytes,
            result_content_type,
            result_truncated,
        )
        self.finished_at = _utc_now_iso()
        self.duration_ms = _duration_ms(self.started_at, self.finished_at)

    def fail(
        self,
        error: str,
        *,
        result_ref: Optional[str] = None,
        result_size_bytes: Optional[int] = None,
        result_content_type: Optional[str] = None,
        result_truncated: bool = False,
    ) -> None:
        self._transition(StepStatus.FAILED)
        self.error = error
        self._set_result_reference(
            result_ref,
            result_size_bytes,
            result_content_type,
            result_truncated,
        )
        self.finished_at = _utc_now_iso()
        self.duration_ms = _duration_ms(self.started_at, self.finished_at)

    def _set_result_reference(
        self,
        result_ref: Optional[str],
        result_size_bytes: Optional[int],
        result_content_type: Optional[str],
        result_truncated: bool,
    ) -> None:
        self.result_ref = result_ref
        self.result_size_bytes = result_size_bytes
        self.result_content_type = result_content_type
        self.result_truncated = result_truncated

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
            "plan_step_id": self.plan_step_id,
            "tool_call_id": self.tool_call_id,
            "result_ref": self.result_ref,
            "result_size_bytes": self.result_size_bytes,
            "result_content_type": self.result_content_type,
            "result_truncated": self.result_truncated,
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
            plan_step_id=data.get("plan_step_id"),
            tool_call_id=data.get("tool_call_id"),
            result_ref=data.get("result_ref"),
            result_size_bytes=data.get("result_size_bytes"),
            result_content_type=data.get("result_content_type"),
            result_truncated=bool(data.get("result_truncated", False)),
        )


@dataclass
class Task:
    """A user task with its lifecycle, steps and final result."""

    user_input: str
    # Local identity/project scope for long-term memory. Defaults preserve all
    # pre-P1 callers and legacy JSON task records.
    user_id: str = "local-user"
    project_id: str = "default"
    id: str = field(default_factory=_new_id)
    status: TaskStatus = TaskStatus.CREATED
    steps: list[TaskStep] = field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    # Task inputs to load from object storage, and artifacts produced by tools.
    # Both are plain dicts so the domain model keeps zero infrastructure imports.
    input_files: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    # Sandbox directory owned by this task (set before execution starts).
    workspace_dir: Optional[str] = None
    # When true, a run that produces no artifact at all is a failed deliverable.
    require_artifact: bool = False
    # Runtime event buckets (LLM, retry, context, memory, validation, recovery).
    metrics: dict[str, Any] = field(default_factory=dict)
    created_time: str = field(default_factory=_utc_now_iso)
    updated_time: str = field(default_factory=_utc_now_iso)
    # None also represents legacy task records created before plan persistence.
    plan: Optional[Plan] = None
    # Append-only audit of plan lifecycle: creation, step binding, completion,
    # failure, replan requests/rejections and applied replans (with versions).
    plan_events: list[dict[str, Any]] = field(default_factory=list)

    _ALLOWED_TRANSITIONS = {
        # CREATED -> FAILED covers a task that is abandoned/failed before it ever
        # started (e.g. recovered as stale after a process restart).
        TaskStatus.CREATED: {TaskStatus.RUNNING, TaskStatus.FAILED},
        TaskStatus.RUNNING: {TaskStatus.SUCCESS, TaskStatus.FAILED},
    }

    def __post_init__(self) -> None:
        for label, value in (("user_id", self.user_id), ("project_id", self.project_id)):
            if not isinstance(value, str) or not _SCOPE_ID.fullmatch(value):
                raise ValueError(
                    f"{label} must be 1-64 characters: letters, digits, dot, dash or underscore"
                )

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

    def add_step(
        self,
        name: str,
        tool: Optional[str] = None,
        plan_step_id: Optional[str] = None,
        tool_call_id: Optional[str] = None,
    ) -> TaskStep:
        step = TaskStep(
            name=name, tool=tool, plan_step_id=plan_step_id, tool_call_id=tool_call_id
        )
        self.steps.append(step)
        self._touch()
        return step

    def set_plan(self, plan: Plan) -> None:
        """Attach the initial plan once; later versions go through apply_replan."""
        if self.status != TaskStatus.RUNNING or self.plan is not None:
            raise TaskStateError("initial plan requires a running task without a plan")
        plan.validate()
        if plan.user_input != self.user_input or plan.version != 1:
            raise TaskStateError("initial plan must match the task and have version 1")
        self.plan = Plan.from_dict(plan.to_dict())
        self._record_plan_event(
            "plan_created", version=plan.version, step_ids=plan.step_ids()
        )
        self._touch()

    def add_plan_event(self, event: dict[str, Any]) -> None:
        """Append one already-built plan event (the Executor owns its schema)."""
        if not isinstance(event, dict) or not isinstance(event.get("kind"), str):
            raise TaskStateError("a plan event needs a kind")
        self.plan_events.append(dict(event))
        self._touch()

    def completed_plan_steps(self) -> list[str]:
        """Steps of the current plan that were explicitly judged complete."""
        if self.plan is None:
            return []
        known = set(self.plan.step_ids())
        return list(dict.fromkeys(
            event["step_id"]
            for event in self.plan_events
            if event.get("kind") == "plan_step_completed"
            and event.get("step_id") in known
        ))

    def apply_replan(self, plan: Plan, *, reason_code: str, reason: str) -> None:
        """Replace the plan with the next version, keeping finished steps verbatim.

        Only the unfinished part may change: completed steps must reappear with
        identical content, so a replan can never replay or rewrite finished work.
        """
        if self.status != TaskStatus.RUNNING:
            raise TaskStateError("replanning requires a running task")
        if self.plan is None:
            raise TaskStateError("replanning requires an initial plan")
        for label, value in (("replan reason_code", reason_code), ("replan reason", reason)):
            if not isinstance(value, str) or not value.strip():
                raise TaskStateError(f"{label} must be a non-empty string")
        plan.validate()
        if plan.user_input != self.user_input:
            raise TaskStateError("a replan must keep the task goal")
        next_version = self.plan.version + 1
        if plan.version != next_version:
            raise TaskStateError(f"a replan must carry version {next_version}")
        completed = self.completed_plan_steps()
        assert_replan_preserves_completed_steps(self.plan, plan, completed)
        self.plan = Plan.from_dict(plan.to_dict())
        self._record_plan_event(
            "plan_replanned",
            from_version=next_version - 1,
            to_version=plan.version,
            reason_code=reason_code,
            reason=reason,
            preserved_step_ids=completed,
            step_ids=plan.step_ids(),
        )
        self._touch()

    def _record_plan_event(self, kind: str, **fields: Any) -> None:
        self.plan_events.append(
            {"occurred_at": _utc_now_iso(), "kind": kind, **fields}
        )

    def get_step(self, step_id: str) -> TaskStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise StepNotFoundError(step_id)

    def set_input_files(self, input_files: list[dict[str, Any]]) -> None:
        self.input_files = list(input_files)
        self._touch()

    def set_workspace_dir(self, workspace_dir: str) -> None:
        self.workspace_dir = workspace_dir
        self._touch()

    def add_artifact(self, artifact: dict[str, Any]) -> None:
        self.artifacts.append(artifact)
        self._touch()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_input": self.user_input,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "status": self.status.value,
            "steps": [step.to_dict() for step in self.steps],
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "plan_events": [dict(event) for event in self.plan_events],
            "result": self.result,
            "error": self.error,
            "input_files": self.input_files,
            "artifacts": self.artifacts,
            "workspace_dir": self.workspace_dir,
            "require_artifact": self.require_artifact,
            "metrics": self.metrics,
            "created_time": self.created_time,
            "updated_time": self.updated_time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=data["id"],
            user_input=data["user_input"],
            user_id=data.get("user_id", "local-user"),
            project_id=data.get("project_id", "default"),
            status=TaskStatus(data["status"]),
            steps=[TaskStep.from_dict(step) for step in data.get("steps", [])],
            plan=Plan.from_dict(data["plan"]) if data.get("plan") is not None else None,
            plan_events=[dict(event) for event in data.get("plan_events", [])],
            result=data.get("result"),
            error=data.get("error"),
            input_files=data.get("input_files", []),
            artifacts=data.get("artifacts", []),
            workspace_dir=data.get("workspace_dir"),
            require_artifact=bool(data.get("require_artifact", False)),
            metrics=data.get("metrics", {}),
            created_time=data["created_time"],
            updated_time=data["updated_time"],
        )
