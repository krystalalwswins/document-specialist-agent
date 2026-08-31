"""AgentOrchestrator: wire task lifecycle, planning and execution together."""

from __future__ import annotations

import logging

from agent.executor import Executor
from agent.planner import Planner
from task.task_manager import TaskManager
from task.task_model import Task, TaskStatus

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    def __init__(
        self,
        task_manager: TaskManager,
        planner: Planner,
        executor: Executor,
    ) -> None:
        self._task_manager = task_manager
        self._planner = planner
        self._executor = executor

    def run(self, user_input: str) -> Task:
        task = self._task_manager.create_task(user_input)
        try:
            self._task_manager.start_task(task.id)
            plan = self._planner.plan(user_input)
            answer = self._executor.run(task.id, user_input, plan)
            self._task_manager.succeed_task(task.id, {"answer": answer})
        except Exception as exc:
            current = self._task_manager.get_task(task.id)
            if current.status in (TaskStatus.CREATED, TaskStatus.RUNNING):
                self._task_manager.fail_task(task.id, str(exc))
            logger.exception("task %s failed", task.id)
            raise
        return task
