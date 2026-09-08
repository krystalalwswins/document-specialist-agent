"""API tests include lifespan-managed durable workers."""
import base64
import threading
import time

from fastapi.testclient import TestClient
from api.app import create_app
from task.task_manager import TaskManager


class FakeOrchestrator:
    memory = None

    def __init__(self):
        self.task_manager = TaskManager()
        self.run_task_calls = []

    def run_task(self, task_id, *, claimed=False):
        self.run_task_calls.append(task_id)
        if not claimed:
            self.task_manager.start_task(task_id)
        self.task_manager.succeed_task(task_id, {'answer': 'ok'})


def wait_success(client, task_id):
    for _ in range(200):
        payload = client.get('/tasks/' + task_id).json()
        if payload['status'] == 'SUCCESS':
            return payload
        time.sleep(0.01)
    raise AssertionError('worker did not finish')


def test_create_task_and_poll_until_success():
    orchestrator = FakeOrchestrator()
    with TestClient(create_app(orchestrator)) as client:
        response = client.post('/tasks', json={'user_input': 'analyze excel'})
        assert response.status_code == 201
        task_id = response.json()['id']
        assert wait_success(client, task_id)['result'] == {'answer': 'ok'}
        assert orchestrator.run_task_calls == [task_id]


def test_get_missing_task_returns_404():
    with TestClient(create_app(FakeOrchestrator())) as client:
        assert client.get('/tasks/nope').status_code == 404


def test_list_tasks():
    with TestClient(create_app(FakeOrchestrator())) as client:
        client.post('/tasks', json={'user_input': 'a'})
        client.post('/tasks', json={'user_input': 'b'})
        assert len(client.get('/tasks').json()) == 2


def test_empty_user_input_rejected():
    with TestClient(create_app(FakeOrchestrator())) as client:
        for text in ['', '   ']:
            assert client.post('/tasks', json={'user_input': text}).status_code == 422


def test_capacity_rejects_without_creating_orphan_and_releases_slot():
    started, release = threading.Event(), threading.Event()
    class BlockingOrchestrator(FakeOrchestrator):
        def run_task(self, task_id, *, claimed=False):
            started.set()
            assert release.wait(5)
            super().run_task(task_id, claimed=claimed)
    orchestrator = BlockingOrchestrator()
    with TestClient(create_app(orchestrator, max_concurrent_tasks=1, queue_capacity=1)) as client:
        try:
            first = client.post('/tasks', json={'user_input': 'first'})
            assert first.status_code == 201
            assert started.wait(2)
            assert client.post('/tasks', json={'user_input': 'second'}).status_code == 503
            assert len(orchestrator.task_manager.list_tasks()) == 1
        finally:
            release.set()
        wait_success(client, first.json()['id'])
        assert client.post('/tasks', json={'user_input': 'third'}).status_code == 201


def test_input_validation_and_polling_redacts_bytes():
    with TestClient(create_app(FakeOrchestrator())) as client:
        encoded = base64.b64encode(b'a\n1\n').decode()
        payload = {'user_input': 'parse', 'inputs': [{'filename': '../x.csv', 'content_base64': encoded}]}
        assert client.post('/tasks', json=payload).status_code == 422
        payload['inputs'][0]['filename'] = 'x.csv'
        response = client.post('/tasks', json=payload)
        assert response.status_code == 201
        assert response.json()['inputs'] == [{'filename': 'x.csv'}]


def test_evaluation_endpoint():
    with TestClient(create_app(FakeOrchestrator())) as client:
        task = client.post('/tasks', json={'user_input': 'x'}).json()
        wait_success(client, task['id'])
        assert client.get('/evaluation').json()['success_rate'] == 1.0
