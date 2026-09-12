"""Minimal FastAPI layer: submit a task and poll its status/result.

Tasks run in a background thread so POST returns immediately with a task id.

Optional bearer-style auth: set ``API_TOKEN`` and every request must carry a
matching ``X-API-Token`` header. Leaving it empty disables the check, which is
only acceptable for local development.
"""

from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agent.orchestrator import AgentOrchestrator
from agent.wiring import build_orchestrator
from core.config import Settings, get_settings
from task.task_manager import TaskManager
from task.task_model import TaskNotFoundError

logger = logging.getLogger(__name__)


class CreateTaskRequest(BaseModel):
    user_input: str = Field(min_length=1)


def _token_guard(settings: Settings):
    """Build the dependency enforcing ``X-API-Token`` when a token is configured."""

    def guard(x_api_token: Optional[str] = Header(default=None, alias="X-API-Token")) -> None:
        if not settings.api_token:
            return
        provided = (x_api_token or "").encode("utf-8")
        if not secrets.compare_digest(provided, settings.api_token.encode("utf-8")):
            raise HTTPException(
                status_code=401,
                detail="missing or invalid API token",
                headers={"WWW-Authenticate": "X-API-Token"},
            )

    return guard


def _run_task(orchestrator: AgentOrchestrator, task_id: str) -> None:
    try:
        orchestrator.run_task(task_id)
    except Exception:
        # run_task already marks the task FAILED before re-raising.
        logger.exception("background task %s failed", task_id)


def _recover_stale(task_manager: TaskManager, settings: Settings, where: str) -> None:
    try:
        recovered = task_manager.recover_stale_tasks(settings.task_stale_after_seconds)
    except Exception:  # recovery must never stop the API from serving
        logger.exception("stale task recovery failed (%s)", where)
        return
    if recovered:
        logger.warning(
            "recovered %d stale task(s) %s: %s",
            len(recovered),
            where,
            [task.id for task in recovered],
        )


def _start_reaper(task_manager: TaskManager, settings: Settings) -> threading.Event:
    """Fail tasks whose worker thread died without converging the lifecycle."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(settings.task_reaper_interval_seconds):
            _recover_stale(task_manager, settings, "while running")

    threading.Thread(target=loop, name="task-reaper", daemon=True).start()
    return stop


def create_app(
    orchestrator: AgentOrchestrator | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    orchestrator = orchestrator or build_orchestrator(settings)
    guard = Depends(_token_guard(settings))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        _recover_stale(orchestrator.task_manager, settings, "on startup")
        stop = _start_reaper(orchestrator.task_manager, settings)
        try:
            yield
        finally:
            stop.set()

    app = FastAPI(title="Document Specialist Agent", lifespan=lifespan)

    @app.post("/tasks", status_code=201, dependencies=[guard])
    def create_task(request: CreateTaskRequest):
        task = orchestrator.task_manager.create_task(request.user_input)
        threading.Thread(
            target=_run_task,
            args=(orchestrator, task.id),
            daemon=True,
        ).start()
        return task.to_dict()

    @app.get("/tasks/{task_id}", dependencies=[guard])
    def get_task(task_id: str):
        try:
            return orchestrator.task_manager.get_task(task_id).to_dict()
        except TaskNotFoundError:
            raise HTTPException(status_code=404, detail="task not found")

    @app.get("/tasks", dependencies=[guard])
    def list_tasks():
        return [task.to_dict() for task in orchestrator.task_manager.list_tasks()]

    return app


app = create_app()
