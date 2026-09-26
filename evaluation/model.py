"""Serializable domain models for Agent evaluation.

The evaluation package deliberately owns its schema instead of putting evaluation
fields on ``Task``. A task remains the execution truth; these models describe what
an evaluation expected and how that task trace was scored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _non_empty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class EvaluationExpectation:
    """Observable facts that must hold for one case."""

    status: str = "SUCCESS"
    tools: tuple[str, ...] = ()
    completed_steps: tuple[str, ...] = ()
    artifact_validation: bool = False
    replan: bool = False
    failure_recovery: bool = False
    required_events: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvaluationExpectation":
        if not isinstance(data, dict):
            raise ValueError("expectation must be an object")
        event_groups: dict[str, tuple[str, ...]] = {}
        raw_events = data.get("required_events", {})
        if not isinstance(raw_events, dict):
            raise ValueError("required_events must be an object")
        for bucket, kinds in raw_events.items():
            event_groups[_non_empty(bucket, "metric bucket")] = _string_tuple(
                kinds, f"required_events.{bucket}"
            )
        status = _non_empty(data.get("status", "SUCCESS"), "expectation.status")
        if status not in {"CREATED", "RUNNING", "SUCCESS", "FAILED"}:
            raise ValueError(f"unsupported expected task status: {status}")
        return cls(
            status=status,
            tools=_string_tuple(data.get("tools", []), "expectation.tools"),
            completed_steps=_string_tuple(
                data.get("completed_steps", []), "expectation.completed_steps"
            ),
            artifact_validation=bool(data.get("artifact_validation", False)),
            replan=bool(data.get("replan", False)),
            failure_recovery=bool(data.get("failure_recovery", False)),
            required_events=event_groups,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "tools": list(self.tools),
            "completed_steps": list(self.completed_steps),
            "artifact_validation": self.artifact_validation,
            "replan": self.replan,
            "failure_recovery": self.failure_recovery,
            "required_events": {
                bucket: list(kinds) for bucket, kinds in self.required_events.items()
            },
        }


@dataclass(frozen=True)
class EvaluationCase:
    """One versioned task and its expected observable behaviour."""

    id: str
    category: str
    description: str
    user_input: str
    expectation: EvaluationExpectation
    fixture: str | None = None
    user_id: str = "evaluation-user"
    project_id: str = "evaluation-project"
    input_files: tuple[dict[str, Any], ...] = ()
    require_artifact: bool = False
    fake: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvaluationCase":
        if not isinstance(data, dict):
            raise ValueError("evaluation case must be an object")
        case_id = _non_empty(data.get("id"), "case.id")
        raw_files = data.get("input_files", [])
        if not isinstance(raw_files, list) or any(
            not isinstance(item, dict) for item in raw_files
        ):
            raise ValueError("input_files must be a list of objects")
        raw_fake = data.get("fake", {})
        if not isinstance(raw_fake, dict):
            raise ValueError("fake must be an object")
        fixture = data.get("fixture")
        if fixture is not None:
            fixture = _non_empty(fixture, "fixture")
        return cls(
            id=case_id,
            category=_non_empty(data.get("category"), "case.category"),
            description=_non_empty(data.get("description"), "case.description"),
            user_input=_non_empty(data.get("user_input"), "case.user_input"),
            expectation=EvaluationExpectation.from_dict(data.get("expectation", {})),
            fixture=fixture,
            user_id=_non_empty(data.get("user_id", "evaluation-user"), "case.user_id"),
            project_id=_non_empty(
                data.get("project_id", f"eval-{case_id}"), "case.project_id"
            ),
            input_files=tuple(dict(item) for item in raw_files),
            require_artifact=bool(data.get("require_artifact", False)),
            fake=dict(raw_fake),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "description": self.description,
            "user_input": self.user_input,
            "fixture": self.fixture,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "input_files": [dict(item) for item in self.input_files],
            "require_artifact": self.require_artifact,
            "expectation": self.expectation.to_dict(),
            "fake": self.fake,
        }


@dataclass(frozen=True)
class EvaluationDataset:
    id: str
    version: str
    description: str
    cases: tuple[EvaluationCase, ...]

    def __post_init__(self) -> None:
        if not self.cases:
            raise ValueError("evaluation dataset must contain at least one case")
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case ids must be unique")


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    category: str
    passed: bool
    task_id: str
    task_status: str
    checks: dict[str, bool]
    tool_selection_accuracy: float
    plan_step_completion_rate: float
    artifact_validation_passed: bool | None
    tool_calls: int
    replans: int
    failure_recovered: bool | None
    total_tokens: int
    latency_ms: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "category": self.category,
            "passed": self.passed,
            "task_id": self.task_id,
            "task_status": self.task_status,
            "checks": dict(self.checks),
            "tool_selection_accuracy": self.tool_selection_accuracy,
            "plan_step_completion_rate": self.plan_step_completion_rate,
            "artifact_validation_passed": self.artifact_validation_passed,
            "tool_calls": self.tool_calls,
            "replans": self.replans,
            "failure_recovered": self.failure_recovered,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass(frozen=True)
class SuiteMetrics:
    case_count: int
    case_pass_rate: float
    task_completion_rate: float
    tool_selection_accuracy: float
    plan_step_completion_rate: float
    artifact_validation_pass_rate: float | None
    average_tool_calls: float
    average_replans: float
    failure_recovery_rate: float | None
    average_tokens: float
    p95_latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class EvaluationReport:
    run_id: str
    mode: str
    model: str
    prompt_version: str
    dataset_id: str
    dataset_version: str
    started_at: str
    finished_at: str
    metrics: SuiteMetrics
    cases: tuple[CaseResult, ...]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed": self.passed,
            "metrics": self.metrics.to_dict(),
            "cases": [case.to_dict() for case in self.cases],
        }
