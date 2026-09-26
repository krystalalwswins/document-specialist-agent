"""Deterministic scoring from persisted task traces.

No model judges its own answer here. Every P1-2 metric is derived from Task,
TaskStep, plan_events or metrics, so a score can be explained down to raw evidence.
"""

from __future__ import annotations

import math
from statistics import fmean
from typing import Iterable

from task.task_model import StepStatus, Task, TaskStatus

from .model import CaseResult, EvaluationCase, SuiteMetrics


class EvaluationScorer:
    def score_case(
        self,
        case: EvaluationCase,
        task: Task,
        *,
        latency_ms: float,
        error: str | None = None,
    ) -> CaseResult:
        expected = case.expectation
        actual_tools = tuple(step.tool for step in task.steps if step.tool)
        completed = set(task.completed_plan_steps())
        replans = sum(
            event.get("kind") == "plan_replanned" for event in task.plan_events
        )
        artifact_passed = self._artifact_validation(task, expected.artifact_validation)
        recovered = self._failure_recovery(task, expected.failure_recovery)

        checks: dict[str, bool] = {
            "task_status": task.status.value == expected.status,
            "tool_sequence": actual_tools == expected.tools,
            "completed_steps": set(expected.completed_steps) <= completed,
            "replan": (replans > 0) == expected.replan,
        }
        if expected.artifact_validation:
            checks["artifact_validation"] = artifact_passed is True
        if expected.failure_recovery:
            checks["failure_recovery"] = recovered is True
        for bucket, kinds in expected.required_events.items():
            actual_kinds = {
                event.get("kind")
                for event in task.metrics.get(bucket, [])
                if isinstance(event, dict)
            }
            for kind in kinds:
                checks[f"event:{bucket}:{kind}"] = kind in actual_kinds

        if error:
            checks["execution_error"] = False

        return CaseResult(
            case_id=case.id,
            category=case.category,
            passed=all(checks.values()),
            task_id=task.id,
            task_status=task.status.value,
            checks=checks,
            tool_selection_accuracy=self._sequence_accuracy(
                expected.tools, actual_tools
            ),
            plan_step_completion_rate=self._completion_rate(
                expected.completed_steps, completed
            ),
            artifact_validation_passed=artifact_passed,
            tool_calls=len(task.steps),
            replans=replans,
            failure_recovered=recovered,
            total_tokens=self._total_tokens(task),
            latency_ms=max(0.0, float(latency_ms)),
            error=error,
        )

    def aggregate(self, results: Iterable[CaseResult]) -> SuiteMetrics:
        cases = tuple(results)
        if not cases:
            raise ValueError("cannot aggregate an empty evaluation result")
        artifact_values = [
            float(case.artifact_validation_passed)
            for case in cases
            if case.artifact_validation_passed is not None
        ]
        recovery_values = [
            float(case.failure_recovered)
            for case in cases
            if case.failure_recovered is not None
        ]
        return SuiteMetrics(
            case_count=len(cases),
            case_pass_rate=fmean(float(case.passed) for case in cases),
            task_completion_rate=fmean(
                float(case.task_status == TaskStatus.SUCCESS.value) for case in cases
            ),
            tool_selection_accuracy=fmean(
                case.tool_selection_accuracy for case in cases
            ),
            plan_step_completion_rate=fmean(
                case.plan_step_completion_rate for case in cases
            ),
            artifact_validation_pass_rate=(
                fmean(artifact_values) if artifact_values else None
            ),
            average_tool_calls=fmean(case.tool_calls for case in cases),
            average_replans=fmean(case.replans for case in cases),
            failure_recovery_rate=(fmean(recovery_values) if recovery_values else None),
            average_tokens=fmean(case.total_tokens for case in cases),
            p95_latency_ms=self._percentile95(case.latency_ms for case in cases),
        )

    @staticmethod
    def _sequence_accuracy(expected: tuple[str, ...], actual: tuple[str, ...]) -> float:
        size = max(len(expected), len(actual))
        if size == 0:
            return 1.0
        matches = sum(
            index < len(actual) and expected[index] == actual[index]
            for index in range(len(expected))
        )
        return matches / size

    @staticmethod
    def _completion_rate(expected: tuple[str, ...], actual: set[str]) -> float:
        if not expected:
            return 1.0
        return len(set(expected) & actual) / len(set(expected))

    @staticmethod
    def _artifact_validation(task: Task, required: bool) -> bool | None:
        if not required:
            return None
        result_artifacts = (
            task.result.get("artifacts", []) if isinstance(task.result, dict) else []
        )
        keys = [
            artifact.get("oss_key")
            for artifact in result_artifacts
            if isinstance(artifact, dict) and isinstance(artifact.get("oss_key"), str)
        ]
        if not keys:
            return False
        events = [
            event
            for event in task.metrics.get("validation_events", [])
            if isinstance(event, dict) and event.get("kind") == "artifact_check"
        ]
        latest_by_key = {
            event.get("oss_key"): event
            for event in events
            if isinstance(event.get("oss_key"), str)
        }
        return all(
            latest_by_key.get(key, {}).get("ok") is True
            for key in keys
        )

    @staticmethod
    def _failure_recovery(task: Task, required: bool) -> bool | None:
        if not required:
            return None
        failed_step = any(step.status is StepStatus.FAILED for step in task.steps)
        retry_failure = any(
            isinstance(event, dict)
            and event.get("final_status") in {"RETRYING", "FAILED"}
            for event in task.metrics.get("retry_events", [])
        )
        return (failed_step or retry_failure) and task.status is TaskStatus.SUCCESS

    @staticmethod
    def _total_tokens(task: Task) -> int:
        total = 0
        for event in task.metrics.get("llm_events", []):
            if not isinstance(event, dict):
                continue
            value = event.get("total_tokens")
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                total += value
        return total

    @staticmethod
    def _percentile95(values: Iterable[float]) -> float:
        ordered = sorted(max(0.0, float(value)) for value in values)
        if not ordered:
            return 0.0
        # Nearest-rank P95: the smallest observed value whose cumulative share
        # reaches 95%. It is deterministic and easy to reproduce by hand.
        return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]
