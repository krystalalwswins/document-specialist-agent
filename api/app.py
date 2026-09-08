"""Minimal FastAPI layer: submit a task and poll its status/result.

Tasks run in a background thread so POST returns immediately with a task id.
"""

from __future__ import annotations

import logging
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.orchestrator import AgentOrchestrator
from agent.wiring import build_orchestrator
from core.config import get_settings
from task.task_model import TaskNotFoundError

logger = logging.getLogger(__name__)


class CreateTaskRequest(BaseModel):
    user_input: str = Field(min_length=1)


def _run_task(orchestrator: AgentOrchestrator, task_id: str) -> None:
    try:
        orchestrator.run_task(task_id)
    except Exception:
        # run_task already marks the task FAILED before re-raising.
        logger.exception("background task %s failed", task_id)


def create_app(orchestrator: AgentOrchestrator | None = None, *, max_concurrent_tasks: int | None = None) -> FastAPI:
    orchestrator = orchestrator or build_orchestrator()
    limit = max_concurrent_tasks if max_concurrent_tasks is not None else get_settings().max_concurrent_tasks
    if limit < 1:
        raise ValueError("max_concurrent_tasks must be positive")
    slots = threading.BoundedSemaphore(limit)

    def run_with_slot(task_id):
        try:
            _run_task(orchestrator, task_id)
        finally:
            slots.release()

    app = FastAPI(title="Document Specialist Agent")

    @app.post("/tasks", status_code=201)
    def create_task(request: CreateTaskRequest):
        # Reserve before creating a task: a rejected request leaves no orphan record.
        if not slots.acquire(blocking=False):
            raise HTTPException(status_code=503, detail="task capacity reached")
        task = None
        try:
            task = orchestrator.task_manager.create_task(request.user_input)
            threading.Thread(target=run_with_slot, args=(task.id,), daemon=True).start()
        except Exception:
            slots.release()
            if task is not None:
                orchestrator.task_manager.start_task(task.id)
                orchestrator.task_manager.fail_task(task.id, "background worker could not start")
            raise
        return task.to_dict()

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str):
        try:
            return orchestrator.task_manager.get_task(task_id).to_dict()
        except TaskNotFoundError:
            raise HTTPException(status_code=404, detail="task not found")

    @app.get("/tasks")
    def list_tasks():
        return [task.to_dict() for task in orchestrator.task_manager.list_tasks()]

    return app


app = create_app()
