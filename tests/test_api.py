"""Light API tests using TestClient with a fake orchestrator."""

import time
import threading
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from api.app import create_app
from core.config import Settings
from task.file_task_store import FileTaskStore
from task.task_manager import TaskManager


class FakeOrchestrator:
    def __init__(self):
        self.task_manager = TaskManager()
        self.run_task_calls = []

    def run_task(self, task_id):
        self.run_task_calls.append(task_id)
        self.task_manager.start_task(task_id)
        self.task_manager.succeed_task(task_id, {"answer": "ok"})


def _client(orchestrator, pool=None, **settings_overrides):
    """Build a client with deterministic settings (never read the developer's .env)."""
    settings = Settings(_env_file=None, **settings_overrides)
    return TestClient(create_app(orchestrator, settings, pool))


def test_create_task_and_poll_until_success():
    orchestrator = FakeOrchestrator()
    client = _client(orchestrator)

    resp = client.post("/tasks", json={"user_input": "analyze excel"})
    assert resp.status_code == 201
    task_id = resp.json()["id"]
    assert task_id

    status = None
    for _ in range(100):
        payload = client.get(f"/tasks/{task_id}").json()
        status = payload["status"]
        if status == "SUCCESS":
            break
        time.sleep(0.01)

    assert status == "SUCCESS"
    assert payload["result"] == {"answer": "ok"}
    assert orchestrator.run_task_calls == [task_id]


def test_get_missing_task_returns_404():
    client = _client(FakeOrchestrator())
    resp = client.get("/tasks/nope")
    assert resp.status_code == 404


def test_list_tasks():
    client = _client(FakeOrchestrator())
    client.post("/tasks", json={"user_input": "a"})
    client.post("/tasks", json={"user_input": "b"})
    payload = client.get("/tasks").json()
    assert len(payload) == 2


def test_empty_user_input_rejected():
    client = _client(FakeOrchestrator())
    resp = client.post("/tasks", json={"user_input": ""})
    assert resp.status_code == 422


def test_auth_disabled_when_token_not_configured():
    client = _client(FakeOrchestrator())
    assert client.get("/tasks").status_code == 200
    assert client.post("/tasks", json={"user_input": "a"}).status_code == 201


def test_requests_without_token_rejected_when_enabled():
    client = _client(FakeOrchestrator(), api_token="s3cret")
    assert client.get("/tasks").status_code == 401
    assert client.post("/tasks", json={"user_input": "a"}).status_code == 401
    assert client.get("/tasks/whatever").status_code == 401


def test_requests_with_wrong_token_rejected():
    client = _client(FakeOrchestrator(), api_token="s3cret")
    resp = client.get("/tasks", headers={"X-API-Token": "nope"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "missing or invalid API token"


def test_requests_with_token_allowed():
    orchestrator = FakeOrchestrator()
    client = _client(orchestrator, api_token="s3cret")
    headers = {"X-API-Token": "s3cret"}

    resp = client.post("/tasks", json={"user_input": "analyze excel"}, headers=headers)
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    status = None
    for _ in range(100):
        payload = client.get(f"/tasks/{task_id}", headers=headers).json()
        status = payload["status"]
        if status == "SUCCESS":
            break
        time.sleep(0.01)

    assert status == "SUCCESS"
    assert client.get("/tasks", headers=headers).status_code == 200


def test_startup_recovers_stale_tasks(tmp_path):
    manager = TaskManager(FileTaskStore(tmp_path))
    task = manager.create_task("abandoned by a dead worker")
    manager.start_task(task.id)
    record = manager.get_task(task.id)
    record.updated_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    manager.store.update(record)

    orchestrator = FakeOrchestrator()
    orchestrator.task_manager = manager
    settings = Settings(_env_file=None, task_stale_after_seconds=60)

    with TestClient(create_app(orchestrator, settings)) as client:
        body = client.get(f"/tasks/{task.id}").json()

    assert body["status"] == "FAILED"
    assert "stale" in body["error"]


def test_create_task_records_declared_input_files():
    orchestrator = FakeOrchestrator()
    client = _client(orchestrator)

    resp = client.post(
        "/tasks",
        json={
            "user_input": "summarise the workbook",
            "input_files": [{"oss_key": "raw/sales.xlsx"}, {"oss_key": "raw/q3.csv", "filename": "q3.csv"}],
        },
    )

    assert resp.status_code == 201
    assert resp.json()["input_files"] == [
        {"oss_key": "raw/sales.xlsx", "filename": None},
        {"oss_key": "raw/q3.csv", "filename": "q3.csv"},
    ]


def test_input_files_default_to_empty_and_are_validated():
    client = _client(FakeOrchestrator())
    assert client.post("/tasks", json={"user_input": "x"}).json()["input_files"] == []
    assert client.post("/tasks", json={"user_input": "x", "input_files": [{"oss_key": ""}]}).status_code == 422


def test_require_artifact_flag_is_persisted_on_the_task():
    orchestrator = FakeOrchestrator()
    client = _client(orchestrator)

    resp = client.post("/tasks", json={"user_input": "save a report", "require_artifact": True})

    assert resp.status_code == 201
    assert resp.json()["require_artifact"] is True
    assert client.post("/tasks", json={"user_input": "x"}).json()["require_artifact"] is False


def test_queue_full_returns_429_and_fails_the_task():
    from api.workers import TaskWorkerPool

    release = threading.Event()
    pool = TaskWorkerPool(lambda _task_id: release.wait(timeout=5), max_workers=1, max_pending=0)
    orchestrator = FakeOrchestrator()
    client = _client(orchestrator, pool=pool)

    assert client.post("/tasks", json={"user_input": "first"}).status_code == 201
    time.sleep(0.1)  # let the single worker pick the first task up

    resp = client.post("/tasks", json={"user_input": "second"})
    release.set()

    assert resp.status_code == 429
    assert "rejected" in resp.json()["detail"]
    assert resp.headers["Retry-After"] == "5"
    rejected = [t for t in orchestrator.task_manager.list_tasks() if t.error]
    assert len(rejected) == 1
    assert rejected[0].status.value == "FAILED"
    assert rejected[0].metrics["recovery_events"][0]["kind"] == "rejected"


def test_health_reports_worker_stats():
    client = _client(FakeOrchestrator())
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "workers" in body
