"""AgentOrchestrator: wire task lifecycle, planning and execution together."""

from __future__ import annotations

import logging
from typing import Any

from agent.executor import Executor
from agent.planner import Planner
from agent.validator import ArtifactValidator
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
    ) -> None:
        self._task_manager = task_manager
        self._planner = planner
        self._executor = executor
        self._input_stager = input_stager
        self._validator = validator

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
            answer = self._executor.run(task_id, task.user_input, plan)
            artifacts = self._task_manager.get_task(task_id).artifacts
            self._validate_artifacts(task_id, artifacts)
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
        """Load declared inputs from object storage before planning."""
        task = self._task_manager.get_task(task_id)
        if not task.input_files or self._input_stager is None:
            return
        staged = self._input_stager.stage_all(task.input_files)
        self._task_manager.set_input_files(task_id, staged)
        logger.info("task %s: staged %d input file(s)", task_id, len(staged))

    def _validate_artifacts(self, task_id: str, artifacts: list[dict]) -> None:
        if self._validator is None or not artifacts:
            return
        result = self._validator.validate(artifacts)
        sink = self._task_manager.metric_sink(task_id, "validation_events")
        for event in result.events():
            sink(event)
        if not result.ok:
            raise ArtifactValidationError(
                f"artifact validation failed: {result.failure_summary()}"
            )
