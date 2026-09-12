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
from api.workers import QueueFull, TaskWorkerPool
from core.config import Settings, get_settings
from task.task_manager import TaskManager
from task.task_model import TaskNotFoundError

logger = logging.getLogger(__name__)


class TaskInputFile(BaseModel):
    oss_key: str = Field(min_length=1)
    filename: str | None = Field(default=None, min_length=1)


class CreateTaskRequest(BaseModel):
    user_input: str = Field(min_length=1)
    # Object-storage keys to load into the sandbox before the task runs.
    # Example: [{"oss_key": "raw/sales.xlsx"}]
    input_files: list[TaskInputFile] = Field(default_factory=list)
    # Set when the task must end with a downloadable deliverable: a run that
    # produces no artifact at all is then treated as a failed deliverable.
    require_artifact: bool = False


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


def _task_handler(orchestrator: AgentOrchestrator):
    """Wrap run_task so a failure is logged and never escapes into the worker pool."""

    def run(task_id: str) -> None:
        try:
            orchestrator.run_task(task_id)
        except Exception:
            # run_task already marks the task FAILED before re-raising.
            logger.exception("background task %s failed", task_id)

    return run


def create_app(
    orchestrator: AgentOrchestrator | None = None,
    settings: Settings | None = None,
    pool: TaskWorkerPool | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    orchestrator = orchestrator or build_orchestrator(settings)
    guard = Depends(_token_guard(settings))
    pool = pool or TaskWorkerPool(
        _task_handler(orchestrator),
        max_workers=settings.task_max_workers,
        max_pending=settings.task_max_pending,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        _recover_stale(orchestrator.task_manager, settings, "on startup")
        stop = _start_reaper(orchestrator.task_manager, settings)
        pool.start()
        try:
            yield
        finally:
            stop.set()
            pool.stop()

    app = FastAPI(title="Document Specialist Agent", lifespan=lifespan)

    @app.post("/tasks", status_code=201, dependencies=[guard])
    def create_task(request: CreateTaskRequest):
        task = orchestrator.task_manager.create_task(
            request.user_input,
            input_files=[item.model_dump() for item in request.input_files],
            require_artifact=request.require_artifact,
        )
        try:
            pool.submit(task.id)
        except QueueFull as exc:
            reason = f"rejected: {exc}"
            orchestrator.task_manager.fail_task(task.id, reason)
            orchestrator.task_manager.add_metric_events(
                task.id, "recovery_events", [{"kind": "rejected", "reason": reason}]
            )
            raise HTTPException(status_code=429, detail=reason, headers={"Retry-After": "5"})
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

    @app.get("/health", dependencies=[guard])
    def health():
        return {"status": "ok", "workers": pool.stats()}

    return app


app = create_app()
