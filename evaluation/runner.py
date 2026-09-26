"""Run evaluation cases through an AgentOrchestrator and score their traces."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Protocol

from agent.orchestrator import AgentOrchestrator
from task.task_model import Task, TaskStatus

from .model import (
    EvaluationCase,
    EvaluationDataset,
    EvaluationReport,
    utc_now_iso,
)
from .scorer import EvaluationScorer


@dataclass(frozen=True)
class CaseExecution:
    task: Task
    error: str | None = None


class CaseExecutor(Protocol):
    def execute(self, case: EvaluationCase) -> CaseExecution:
        """Return the final persisted task, including a failed task on exceptions."""


class OrchestratorCaseExecutor:
    """Adapter used by real-model evaluation and reusable by custom datasets."""

    def __init__(self, orchestrator: AgentOrchestrator) -> None:
        self._orchestrator = orchestrator

    def execute(self, case: EvaluationCase) -> CaseExecution:
        manager = self._orchestrator.task_manager
        task = manager.create_task(
            case.user_input,
            input_files=[dict(item) for item in case.input_files],
            require_artifact=case.require_artifact,
            user_id=case.user_id,
            project_id=case.project_id,
        )
        error: str | None = None
        try:
            self._orchestrator.run_task(task.id)
        except Exception as exc:  # one bad case must not abort the whole suite
            error = f"{type(exc).__name__}: {exc}"
        return CaseExecution(manager.get_task(task.id), error)


class EvaluationRunner:
    def __init__(
        self,
        executor: CaseExecutor,
        scorer: EvaluationScorer | None = None,
    ) -> None:
        self._executor = executor
        self._scorer = scorer or EvaluationScorer()

    def run(
        self,
        dataset: EvaluationDataset,
        *,
        mode: str,
        model: str,
        prompt_version: str,
    ) -> EvaluationReport:
        started_at = utc_now_iso()
        results = []
        for case in dataset.cases:
            started = time.perf_counter()
            try:
                execution = self._executor.execute(case)
            except Exception as exc:
                # A malformed fixture or adapter failure becomes one failed case;
                # later cases still run and the report keeps the error evidence.
                error = f"{type(exc).__name__}: {exc}"
                execution = CaseExecution(
                    Task(
                        user_input=case.user_input,
                        user_id=case.user_id,
                        project_id=case.project_id,
                        status=TaskStatus.FAILED,
                        error=error,
                    ),
                    error,
                )
            latency_ms = (time.perf_counter() - started) * 1000
            results.append(
                self._scorer.score_case(
                    case,
                    execution.task,
                    latency_ms=latency_ms,
                    error=execution.error,
                )
            )
        finished_at = utc_now_iso()
        case_results = tuple(results)
        return EvaluationReport(
            run_id=uuid.uuid4().hex,
            mode=mode,
            model=model,
            prompt_version=prompt_version,
            dataset_id=dataset.id,
            dataset_version=dataset.version,
            started_at=started_at,
            finished_at=finished_at,
            metrics=self._scorer.aggregate(case_results),
            cases=case_results,
        )
