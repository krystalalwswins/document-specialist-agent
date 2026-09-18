"""Plan data contract, independent of the LLM and task storage backends.

Constructors allow assembling a draft. validate()/from_dict()/to_dict() reject
incomplete plans at the planner and persistence boundaries. A plan step describes
intent; TaskStep continues to describe an actual tool invocation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from graphlib import CycleError, TopologicalSorter
from typing import Any, Optional


class PlanValidationError(ValueError):
    """The plan cannot be accepted as an executable contract."""


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError(f"{label} must be a non-empty string")


@dataclass
class PlanStep:
    name: str
    description: str = ""
    tool: Optional[str] = None
    step_id: str = ""
    depends_on: list[str] = field(default_factory=list)
    completion_criteria: list[str] = field(default_factory=list)

    def validate(self) -> None:
        _text(self.step_id, "step_id")
        _text(self.name, "step name")
        _text(self.description, "step description")
        if self.tool is not None:
            _text(self.tool, "tool")
        if not isinstance(self.depends_on, list):
            raise PlanValidationError("depends_on must be a list")
        for dependency in self.depends_on:
            _text(dependency, "dependency")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise PlanValidationError("duplicate dependencies")
        if not isinstance(self.completion_criteria, list) or not self.completion_criteria:
            raise PlanValidationError("completion_criteria must be a non-empty list")
        for criterion in self.completion_criteria:
            _text(criterion, "completion criterion")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanStep":
        required = {"step_id", "name", "description", "depends_on", "completion_criteria"}
        if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - {"tool"}:
            raise PlanValidationError("invalid plan step fields")
        step = cls(**data)
        step.validate()
        # Copy lists so the parsed plan is detached from its input payload.
        step.depends_on = list(step.depends_on)
        step.completion_criteria = list(step.completion_criteria)
        return step


@dataclass
class Plan:
    user_input: str
    steps: list[PlanStep] = field(default_factory=list)
    version: int = 1

    def validate(self) -> None:
        _text(self.user_input, "user_input")
        if type(self.version) is not int or self.version < 1:
            raise PlanValidationError("version must be a positive integer")
        if not isinstance(self.steps, list) or not self.steps:
            raise PlanValidationError("plan is empty or steps is not a list")
        graph: dict[str, list[str]] = {}
        for step in self.steps:
            if not isinstance(step, PlanStep):
                raise PlanValidationError("steps must contain PlanStep instances")
            step.validate()
            if step.step_id in graph:
                raise PlanValidationError(f"duplicate step_id: {step.step_id}")
            graph[step.step_id] = step.depends_on
        for step_id, dependencies in graph.items():
            for dependency in dependencies:
                if dependency not in graph:
                    raise PlanValidationError(f"{step_id}: missing dependency {dependency}")
        try:
            # Checks cycles (including self-dependencies); does not schedule tools.
            tuple(TopologicalSorter(graph).static_order())
        except CycleError as exc:
            raise PlanValidationError("plan dependencies contain a cycle") from exc

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Plan":
        if not isinstance(data, dict) or set(data) != {"user_input", "steps", "version"}:
            raise PlanValidationError("invalid plan fields")
        if not isinstance(data["steps"], list):
            raise PlanValidationError("steps must be a list")
        plan = cls(user_input=data["user_input"], version=data["version"],
                   steps=[PlanStep.from_dict(step) for step in data["steps"]])
        plan.validate()
        return plan

    def summary(self) -> str:
        """Keep all planning constraints visible to the existing Executor."""
        return "\n".join(
            f"{index}. [{step.step_id}] {step.name}"
            + (f" (tool: {step.tool})" if step.tool else "")
            + f"\n   Description: {step.description}"
            + f"\n   Depends on: {', '.join(step.depends_on) or '(none)'}"
            + f"\n   Complete when: {'; '.join(step.completion_criteria)}"
            for index, step in enumerate(self.steps, start=1)
        ) or "(no steps)"

    def step_ids(self) -> list[str]:
        return [step.step_id for step in self.steps]

    def step_by_id(self, step_id: str) -> PlanStep:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise PlanValidationError(f"unknown plan step: {step_id}")


def assert_replan_preserves_completed_steps(
    old_plan: Plan, new_plan: Plan, completed_step_ids: list[str]
) -> None:
    """A new plan version may only replace steps that are not finished yet.

    Replanning must never rewrite or drop finished work, so the runtime re-checks
    (instead of trusting the model) that every completed step survives verbatim.
    """
    previous = {step.step_id: step for step in old_plan.steps}
    current = {step.step_id: step for step in new_plan.steps}
    for step_id in completed_step_ids:
        if step_id not in previous or step_id not in current:
            raise PlanValidationError(
                f"completed step {step_id!r} must stay in the replanned plan"
            )
        if asdict(previous[step_id]) != asdict(current[step_id]):
            raise PlanValidationError(
                f"completed step {step_id!r} was rewritten; only unfinished steps may change"
            )
