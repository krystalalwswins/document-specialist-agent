"""AgentOrchestrator: wire task lifecycle, planning and execution together."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from agent.executor import Executor
from agent.planner import Planner
from agent.validator import ArtifactCheck, ArtifactValidator, ValidationResult
from metering.budget import BudgetController
from metering.meter import UsageMeter
from task.task_manager import TaskManager
from task.task_model import Task, TaskStatus

if TYPE_CHECKING:
    from memory.service import MemoryService

logger = logging.getLogger(__name__)


class ArtifactValidationError(Exception):
    """Raised when a produced artifact is missing, empty or inconsistent."""


class AgentOrchestrator:
    def __init__(
        self,
        task_manager: TaskManager,
        planner: Planner,
        executor: Executor,
        input_stager: object | None = None,
        validator: ArtifactValidator | None = None,
        max_recovery_attempts: int = 0,
        memory_service: "MemoryService | None" = None,
        usage_meter: UsageMeter | None = None,
        budget_controller: BudgetController | None = None,
    ) -> None:
        self._task_manager = task_manager
        self._planner = planner
        self._executor = executor
        self._input_stager = input_stager
        self._validator = validator
        self._max_recovery_attempts = max_recovery_attempts
        self._memory_service = memory_service
        self._usage_meter = usage_meter
        self._budget_controller = budget_controller

    @property
    def task_manager(self) -> TaskManager:
        return self._task_manager

    @property
    def memory_service(self) -> "MemoryService | None":
        return self._memory_service

    def run(
        self,
        user_input: str,
        *,
        user_id: str = "local-user",
        project_id: str = "default",
    ) -> Task:
        task = self._task_manager.create_task(
            user_input, user_id=user_id, project_id=project_id
        )
        return self.run_task(task.id)

    def run_task(self, task_id: str) -> Task:
        task = self._task_manager.get_task(task_id)
        try:
            self._task_manager.start_task(task_id)
            self._stage_inputs(task_id)
            memory_context = self._recall_memories(task_id)
            plan_arguments: dict[str, Any] = {
                "on_event": self._llm_sink(task_id, phase="plan")
            }
            if memory_context:
                plan_arguments["memory_context"] = memory_context
            plan = self._planner.plan(task.user_input, **plan_arguments)
            if self._budget_controller is not None:
                self._budget_controller.after_call(task_id, phase="plan")
            # Persist the validated plan before any execution can occur.
            self._task_manager.set_plan(task_id, plan)
            answer, artifacts = self._execute_with_recovery(
                task_id, task.user_input, plan, memory_context
            )
            result: dict[str, Any] = {"answer": answer}
            if artifacts:
                result["artifacts"] = artifacts
            self._task_manager.succeed_task(task_id, result)
            self._capture_memories(task_id, answer)
        except Exception as exc:
            current = self._task_manager.get_task(task_id)
            if current.status in (TaskStatus.CREATED, TaskStatus.RUNNING):
                self._task_manager.fail_task(task_id, str(exc))
            logger.exception("task %s failed", task_id)
            raise
        # Re-read: a persistent store returns copies, so the snapshot taken before
        # the run would still show CREATED.
        return self._task_manager.get_task(task_id)

    def _stage_inputs(self, task_id: str) -> None:
        """Create the task's own sandbox directory, then load its declared inputs."""
        task = self._task_manager.get_task(task_id)
        if self._input_stager is None:
            return
        workspace_dir = self._input_stager.prepare(task_id)
        self._task_manager.set_workspace_dir(task_id, workspace_dir)
        if not task.input_files:
            return
        staged = self._input_stager.stage_all(task_id, task.input_files)
        self._task_manager.set_input_files(task_id, staged)
        logger.info("task %s: staged %d input file(s)", task_id, len(staged))

    def _execute_with_recovery(
        self,
        task_id: str,
        user_input: str,
        plan: Any,
        memory_context: str | None = None,
    ) -> tuple[str, list[dict]]:
        """Execute, and repair a rejected deliverable a bounded number of times.

        Only the artifacts produced by the attempt under test are validated, so a
        stale record from a failed attempt cannot poison the retry.
        """
        repairs = 0
        repair_hint: str | None = None
        while True:
            current = self._task_manager.get_task(task_id)
            # A replan inside the previous attempt must not be overwritten by the
            # plan snapshot this method was called with.
            plan = current.plan or plan
            already = len(current.artifacts)
            execute_arguments: dict[str, Any] = {"repair_hint": repair_hint}
            if memory_context:
                execute_arguments["memory_context"] = memory_context
            answer = self._executor.run(task_id, user_input, plan, **execute_arguments)
            current = self._task_manager.get_task(task_id)
            artifacts = current.artifacts[already:]
            result = self._check_artifacts(task_id, artifacts, current.require_artifact)
            if result is None or result.ok:
                return answer, artifacts
            if repairs >= self._max_recovery_attempts:
                raise ArtifactValidationError(
                    f"artifact validation failed: {result.failure_summary()}"
                )
            repairs += 1
            repair_hint = (
                f"artifact validation failed ({result.failure_summary()}). "
                "The file was reported as saved but could not be confirmed."
            )
            self._record_recovery(task_id, repairs, repair_hint)

    def _recall_memories(self, task_id: str) -> str | None:
        if self._memory_service is None:
            return None
        task = self._task_manager.get_task(task_id)
        try:
            records = self._memory_service.recall(
                user_id=task.user_id,
                project_id=task.project_id,
                query=task.user_input,
            )
            self._task_manager.add_metric_events(task_id, "memory_events", [{
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "kind": "memory_recalled",
                "user_id": task.user_id,
                "project_id": task.project_id,
                "memory_ids": [item.id for item in records],
                "count": len(records),
            }])
            return self._memory_service.context(records)
        except Exception as exc:
            # Memory is an enhancement: a local DB problem must not block the task.
            logger.exception("task %s: memory recall failed", task_id)
            self._task_manager.add_metric_events(task_id, "memory_events", [{
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "kind": "memory_recall_failed",
                "error": type(exc).__name__,
            }])
            return None

    def _capture_memories(self, task_id: str, answer: str) -> None:
        if self._memory_service is None:
            return
        try:
            report = self._memory_service.capture(
                self._task_manager.get_task(task_id),
                answer,
                on_event=self._llm_sink(task_id, phase="memory_capture"),
            )
            self._task_manager.add_metric_events(
                task_id, "memory_events", [report.to_event()]
            )
        except Exception as exc:
            # The user already has a valid task result. Do not turn an optional
            # memory write into a FAILED business task.
            logger.exception("task %s: memory capture failed", task_id)
            self._task_manager.add_metric_events(task_id, "memory_events", [{
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "kind": "memory_capture_failed",
                "error": type(exc).__name__,
            }])

    def _llm_sink(self, task_id: str, *, phase: str):
        if self._usage_meter is not None:
            return self._usage_meter.event_sink(task_id, phase=phase)
        return self._task_manager.metric_sink(task_id, "llm_events", phase=phase)

    def _check_artifacts(
        self, task_id: str, artifacts: list[dict], require_artifact: bool = False
    ) -> Any:
        if self._validator is None:
            return None
        if not artifacts:
            if not require_artifact:
                return None
            result = ValidationResult(
                ok=False,
                checks=[
                    ArtifactCheck("", False, "task requires an artifact but none was produced")
                ],
            )
        else:
            result = self._validator.validate(artifacts)
        sink = self._task_manager.metric_sink(task_id, "validation_events")
        for event in result.events():
            sink(event)
        return result

    def _record_recovery(self, task_id: str, attempt: int, reason: str) -> None:
        sink = self._task_manager.metric_sink(task_id, "recovery_events")
        sink(
            {
                "kind": "artifact_repair",
                "attempt": attempt,
                "reason": reason,
            }
        )
        logger.warning("task %s: repairing artifacts (attempt %d)", task_id, attempt)
