"""Light API tests using TestClient with a fake orchestrator."""

import time

from fastapi.testclient import TestClient

from api.app import create_app
from core.config import Settings
from task.task_manager import TaskManager


class FakeOrchestrator:
    def __init__(self):
        self.task_manager = TaskManager()
        self.run_task_calls = []

    def run_task(self, task_id):
        self.run_task_calls.append(task_id)
        self.task_manager.start_task(task_id)
        self.task_manager.succeed_task(task_id, {"answer": "ok"})


def _client(orchestrator, **settings_overrides):
    """Build a client with deterministic settings (never read the developer's .env)."""
    settings = Settings(_env_file=None, **settings_overrides)
    return TestClient(create_app(orchestrator, settings))


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
