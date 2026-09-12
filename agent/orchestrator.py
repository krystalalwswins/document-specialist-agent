"""AgentOrchestrator: wire task lifecycle, planning and execution together."""

from __future__ import annotations

import logging
from typing import Any

from agent.executor import Executor
from agent.planner import Planner
from agent.validator import ArtifactCheck, ArtifactValidator, ValidationResult
from task.task_manager import TaskManager
from task.task_model import Task, TaskStatus

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
    ) -> None:
        self._task_manager = task_manager
        self._planner = planner
        self._executor = executor
        self._input_stager = input_stager
        self._validator = validator
        self._max_recovery_attempts = max_recovery_attempts

    @property
    def task_manager(self) -> TaskManager:
        return self._task_manager

    def run(self, user_input: str) -> Task:
        task = self._task_manager.create_task(user_input)
        return self.run_task(task.id)

    def run_task(self, task_id: str) -> Task:
        task = self._task_manager.get_task(task_id)
        try:
            self._task_manager.start_task(task_id)
            self._stage_inputs(task_id)
            plan = self._planner.plan(
                task.user_input,
                on_event=self._task_manager.metric_sink(task_id, "llm_events", phase="plan"),
            )
            answer, artifacts = self._execute_with_recovery(task_id, task.user_input, plan)
            result: dict[str, Any] = {"answer": answer}
            if artifacts:
                result["artifacts"] = artifacts
            self._task_manager.succeed_task(task_id, result)
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
        self, task_id: str, user_input: str, plan: Any
    ) -> tuple[str, list[dict]]:
        """Execute, and repair a rejected deliverable a bounded number of times.

        Only the artifacts produced by the attempt under test are validated, so a
        stale record from a failed attempt cannot poison the retry.
        """
        repairs = 0
        repair_hint: str | None = None
        while True:
            already = len(self._task_manager.get_task(task_id).artifacts)
            answer = self._executor.run(task_id, user_input, plan, repair_hint=repair_hint)
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
