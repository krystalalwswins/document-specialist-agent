"""Runtime view of one plan version, derived from the append-only plan events.

``Plan`` stays declarative (what should happen); this module owns the observation
side (what actually happened) so the Executor can gate tool calls on dependencies,
stop completed steps from being replayed, and feed a replan with the real progress.
Standard library only, no IO, no LLM imports.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Optional

from .plan_model import Plan, PlanStep

# The event kinds that carry plan-step lifecycle. They are appended to
# ``Task.plan_events`` and replayed here on every construction.
BOUND_EVENT = "plan_step_bound"
COMPLETED_EVENT = "plan_step_completed"
FAILED_EVENT = "plan_step_failed"


class PlanStepStatus(str, Enum):
    """Where a planned step currently stands."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class PlanRunState:
    """Derived step status of ``plan`` given every event recorded for the task.

    Events that mention step ids of an older plan version are ignored here but stay
    in the task's audit trail, so a replan never rewrites history.
    """

    def __init__(self, plan: Plan, events: Iterable[dict[str, Any]]) -> None:
        plan.validate()
        self._plan = plan
        self._status: dict[str, PlanStepStatus] = {
            step.step_id: PlanStepStatus.PENDING for step in plan.steps
        }
        self._evidence: dict[str, list[str]] = {step.step_id: [] for step in plan.steps}
        self._failures: dict[str, dict[str, Any]] = {}
        self._bindings: dict[str, list[dict[str, Any]]] = {
            step.step_id: [] for step in plan.steps
        }
        for event in events:
            self._apply(event)

    def _apply(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        step_id = event.get("step_id")
        if step_id not in self._status:
            return
        kind = event.get("kind")
        if kind == BOUND_EVENT:
            self._bindings[step_id].append(event)
            if self._status[step_id] is not PlanStepStatus.SUCCESS:
                self._status[step_id] = PlanStepStatus.RUNNING
        elif kind == COMPLETED_EVENT:
            self._status[step_id] = PlanStepStatus.SUCCESS
            evidence = event.get("evidence")
            if isinstance(evidence, str) and evidence.strip():
                self._evidence[step_id].append(evidence)
        elif kind == FAILED_EVENT:
            self._failures[step_id] = event
            if self._status[step_id] is not PlanStepStatus.SUCCESS:
                self._status[step_id] = PlanStepStatus.FAILED

    @property
    def plan(self) -> Plan:
        return self._plan

    @property
    def version(self) -> int:
        return self._plan.version

    def step_ids(self) -> list[str]:
        return list(self._status)

    def status(self, step_id: str) -> PlanStepStatus:
        """Status of ``step_id``; unknown ids are reported as PENDING, never fatal."""
        return self._status.get(step_id, PlanStepStatus.PENDING)

    def is_complete(self, step_id: str) -> bool:
        return step_id in self._status and self._status[step_id] is PlanStepStatus.SUCCESS

    def completed_ids(self) -> list[str]:
        return [sid for sid, status in self._status.items() if status is PlanStepStatus.SUCCESS]

    def unfinished_ids(self) -> list[str]:
        return [sid for sid, status in self._status.items() if status is not PlanStepStatus.SUCCESS]

    def running_ids(self) -> list[str]:
        """Started steps that may still receive more tool calls."""
        return [
            sid for sid, status in self._status.items()
            if status is PlanStepStatus.RUNNING and self.dependencies_satisfied(sid)
        ]

    def ready_ids(self) -> list[str]:
        """Steps that may start now: dependencies complete and not finished yet."""
        return [
            sid for sid, status in self._status.items()
            if status in (PlanStepStatus.PENDING, PlanStepStatus.FAILED)
            and self.dependencies_satisfied(sid)
        ]

    def candidates_for_implicit_binding(self) -> list[str]:
        """The single step an unbound tool call may be attributed to, if unambiguous."""
        return self.running_ids() or self.ready_ids()

    def all_complete(self) -> bool:
        return bool(self._status) and all(
            status is PlanStepStatus.SUCCESS for status in self._status.values()
        )

    def unsatisfied_dependencies(self, step_id: str) -> list[str]:
        if step_id not in self._status:
            return []
        step: PlanStep = self._plan.step_by_id(step_id)
        return [dep for dep in step.depends_on if not self.is_complete(dep)]

    def dependencies_satisfied(self, step_id: str) -> bool:
        return not self.unsatisfied_dependencies(step_id)

    def evidence(self, step_id: str) -> list[str]:
        return list(self._evidence.get(step_id, []))

    def bindings(self, step_id: str) -> list[dict[str, Any]]:
        return list(self._bindings.get(step_id, []))

    def failure(self, step_id: str) -> Optional[dict[str, Any]]:
        return self._failures.get(step_id)

    def failure_observations(self) -> list[dict[str, Any]]:
        """Latest failure of every step that is currently still failed."""
        return [
            {
                "step_id": sid,
                "error": event.get("error"),
                "error_type": event.get("error_type"),
            }
            for sid, event in self._failures.items()
            if self._status.get(sid) is PlanStepStatus.FAILED
        ]
