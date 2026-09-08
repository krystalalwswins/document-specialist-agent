"""Light API tests using TestClient with a fake orchestrator."""

import time

from fastapi.testclient import TestClient

from api.app import create_app
from task.task_manager import TaskManager


class FakeOrchestrator:
    def __init__(self):
        self.task_manager = TaskManager()
        self.run_task_calls = []

    def run_task(self, task_id):
        self.run_task_calls.append(task_id)
        self.task_manager.start_task(task_id)
        self.task_manager.succeed_task(task_id, {"answer": "ok"})


def test_create_task_and_poll_until_success():
    orchestrator = FakeOrchestrator()
    client = TestClient(create_app(orchestrator))

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
    client = TestClient(create_app(FakeOrchestrator()))
    resp = client.get("/tasks/nope")
    assert resp.status_code == 404


def test_list_tasks():
    client = TestClient(create_app(FakeOrchestrator()))
    client.post("/tasks", json={"user_input": "a"})
    client.post("/tasks", json={"user_input": "b"})
    payload = client.get("/tasks").json()
    assert len(payload) == 2


def test_empty_user_input_rejected():
    client = TestClient(create_app(FakeOrchestrator()))
    resp = client.post("/tasks", json={"user_input": ""})
    assert resp.status_code == 422


def test_capacity_rejects_without_creating_orphan_and_releases_slot():
    import threading
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    class BlockingOrchestrator(FakeOrchestrator):
        def run_task(self, task_id):
            self.task_manager.start_task(task_id)
            started.set()
            assert release.wait(5)
            self.task_manager.succeed_task(task_id)
            finished.set()
    orchestrator = BlockingOrchestrator()
    client = TestClient(create_app(orchestrator, max_concurrent_tasks=1))
    try:
        assert client.post('/tasks', json={'user_input': 'first'}).status_code == 201
        assert started.wait(2)
        assert client.post('/tasks', json={'user_input': 'second'}).status_code == 503
        assert len(orchestrator.task_manager.list_tasks()) == 1
    finally:
        release.set()
    assert finished.wait(2)
    for _ in range(100):
        response = client.post('/tasks', json={'user_input': 'third'})
        if response.status_code == 201:
            break
        time.sleep(0.01)
    assert response.status_code == 201
