"""Local learning API: durable task submission, polling and evaluation."""
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

from agent.wiring import build_orchestrator
from core.config import get_settings
from documents.files import decode_inputs
from evaluation.metrics import summarize
from task.task_model import TaskError, TaskNotFoundError
from task.worker import TaskWorkers


class InputDocument(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(max_length=2800000)


class ArtifactRequirements(BaseModel):
    required: bool = False
    format: Literal['csv', 'xlsx', 'txt', 'md', 'json'] | None = None
    required_columns: list[str] = Field(default_factory=list, max_length=200)
    min_rows: int = Field(default=1, ge=0, le=9999)


class CreateTaskRequest(BaseModel):
    user_input: str = Field(min_length=1, max_length=8000)
    inputs: list[InputDocument] = Field(default_factory=list, max_length=5)
    artifact_requirements: ArtifactRequirements = Field(default_factory=ArtifactRequirements)
    memory_note: str = Field(default='', max_length=1000)

    @model_validator(mode='after')
    def validate_inputs(self):
        if not self.user_input.strip():
            raise ValueError('user_input must not be blank')
        decode_inputs([item.model_dump() for item in self.inputs])
        return self


def task_payload(task):
    data = task.to_dict()
    # Content is persisted, but polling only needs filenames.
    data['inputs'] = [{'filename': item['filename']} for item in task.inputs]
    return data


def create_app(orchestrator=None, *, max_concurrent_tasks=None, queue_capacity=None):
    settings = get_settings()
    orchestrator = orchestrator or build_orchestrator(settings)
    limit = max_concurrent_tasks if max_concurrent_tasks is not None else settings.max_concurrent_tasks
    capacity = queue_capacity if queue_capacity is not None else settings.task_queue_capacity
    if capacity < 1:
        raise ValueError('queue capacity must be positive')
    workers = TaskWorkers(orchestrator, limit)

    @asynccontextmanager
    async def lifespan(app):
        workers.start()
        try:
            yield
        finally:
            # Workers finish their active task. Queued CREATED rows survive shutdown.
            import asyncio
            await asyncio.to_thread(workers.stop)

    app = FastAPI(title='Document Specialist Agent', lifespan=lifespan)
    app.state.workers = workers

    @app.post('/tasks', status_code=201)
    def create_task(request: CreateTaskRequest):
        try:
            task = orchestrator.task_manager.create_task(
                request.user_input, inputs=[item.model_dump() for item in request.inputs],
                artifact_requirements=request.artifact_requirements.model_dump(),
                memory_note=request.memory_note, capacity=capacity)
        except TaskError as exc:
            raise HTTPException(503, str(exc)) from exc
        return task_payload(task)

    @app.get('/tasks/{task_id}')
    def get_task(task_id: str):
        try:
            return task_payload(orchestrator.task_manager.get_task(task_id))
        except TaskNotFoundError:
            raise HTTPException(404, 'task not found')

    @app.get('/tasks')
    def list_tasks():
        return [task_payload(task) for task in orchestrator.task_manager.list_tasks()]

    @app.get('/evaluation')
    def evaluation():
        return summarize(orchestrator.task_manager.list_tasks())

    @app.delete('/memory/{task_id}', status_code=204)
    def forget_memory(task_id: str):
        if orchestrator.memory:
            orchestrator.memory.forget(task_id)
        try:
            orchestrator.task_manager.set_metric(task_id, 'memory_note', '')
        except TaskNotFoundError:
            pass

    return app


app = create_app()
